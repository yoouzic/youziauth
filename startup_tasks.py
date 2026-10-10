# Copyright (C) 2026 yoouzic
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import csv
import ctypes
import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable


TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
SYSTEM_TASK_NAME = r"\youziauth\SystemAgent"
TRAY_TASK_NAME = r"\youziauth\Tray"
SYSTEM_SID = "S-1-5-18"

ET.register_namespace("", TASK_NS)


def _tag(name: str) -> str:
    return f"{{{TASK_NS}}}{name}"


def _add_text(parent: ET.Element, name: str, value: str) -> ET.Element:
    element = ET.SubElement(parent, _tag(name))
    element.text = value
    return element


def _build_task_xml(
    install_dir: Path,
    *,
    trigger: str,
    user_sid: str,
    logon_type: str | None,
    run_level: str,
    executable_name: str,
    arguments: str = "",
) -> str:
    root = ET.Element(_tag("Task"), {"version": "1.4"})
    registration = ET.SubElement(root, _tag("RegistrationInfo"))
    _add_text(registration, "Author", "youziauth")
    _add_text(registration, "Description", "Start youziauth without the Startup-folder delay.")

    triggers = ET.SubElement(root, _tag("Triggers"))
    trigger_element = ET.SubElement(triggers, _tag(trigger))
    _add_text(trigger_element, "Enabled", "true")
    if trigger == "LogonTrigger":
        _add_text(trigger_element, "UserId", user_sid)

    principals = ET.SubElement(root, _tag("Principals"))
    principal = ET.SubElement(principals, _tag("Principal"), {"id": "Author"})
    _add_text(principal, "UserId", user_sid)
    if logon_type:
        _add_text(principal, "LogonType", logon_type)
    _add_text(principal, "RunLevel", run_level)

    settings = ET.SubElement(root, _tag("Settings"))
    _add_text(settings, "MultipleInstancesPolicy", "IgnoreNew")
    _add_text(settings, "DisallowStartIfOnBatteries", "false")
    _add_text(settings, "StopIfGoingOnBatteries", "false")
    _add_text(settings, "AllowHardTerminate", "true")
    _add_text(settings, "StartWhenAvailable", "true")
    _add_text(settings, "RunOnlyIfIdle", "false")
    _add_text(settings, "WakeToRun", "false")
    _add_text(settings, "ExecutionTimeLimit", "PT0S")
    _add_text(settings, "Priority", "4")
    restart = ET.SubElement(settings, _tag("RestartOnFailure"))
    _add_text(restart, "Interval", "PT1M")
    _add_text(restart, "Count", "3")

    actions = ET.SubElement(root, _tag("Actions"), {"Context": "Author"})
    execute = ET.SubElement(actions, _tag("Exec"))
    _add_text(execute, "Command", str(Path(install_dir) / executable_name))
    if arguments:
        _add_text(execute, "Arguments", arguments)
    _add_text(execute, "WorkingDirectory", str(install_dir))
    return ET.tostring(root, encoding="unicode", xml_declaration=False)


def build_system_task_xml(install_dir: Path, user_sid: str) -> str:
    return _build_task_xml(
        install_dir,
        trigger="BootTrigger",
        user_sid=SYSTEM_SID,
        logon_type=None,
        run_level="HighestAvailable",
        executable_name="youziauth-agent.exe",
        arguments=f"--allowed-user-sid {user_sid}",
    )


def build_tray_task_xml(install_dir: Path, user_sid: str) -> str:
    return _build_task_xml(
        install_dir,
        trigger="LogonTrigger",
        user_sid=user_sid,
        logon_type="InteractiveToken",
        run_level="LeastPrivilege",
        executable_name="youziauth.exe",
        arguments="--tray-startup",
    )


def _write_xml(path: Path, xml_text: str) -> None:
    path.write_text('<?xml version="1.0" encoding="UTF-16"?>\n' + xml_text, encoding="utf-16")


def default_legacy_shortcut() -> Path:
    appdata = os.environ.get("APPDATA")
    root = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return root / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "youziauth.lnk"


def _run_task_command(
    runner: Callable[..., subprocess.CompletedProcess],
    arguments: list[str],
    *,
    check: bool,
) -> subprocess.CompletedProcess:
    return runner(arguments, check=check, capture_output=True, text=True)


def configure_system_startup(
    enabled: bool,
    install_dir: Path,
    user_sid: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    legacy_shortcut: Path | None = None,
) -> None:
    legacy_shortcut = legacy_shortcut or default_legacy_shortcut()
    if not enabled:
        for task_name in (SYSTEM_TASK_NAME, TRAY_TASK_NAME):
            _run_task_command(
                runner,
                ["schtasks.exe", "/Delete", "/TN", task_name, "/F"],
                check=False,
            )
        legacy_shortcut.unlink(missing_ok=True)
        return

    with tempfile.TemporaryDirectory(prefix="youziauth-tasks-") as temporary:
        root = Path(temporary)
        system_xml = root / "system-agent.xml"
        tray_xml = root / "tray.xml"
        _write_xml(system_xml, build_system_task_xml(install_dir, user_sid))
        _write_xml(tray_xml, build_tray_task_xml(install_dir, user_sid))
        _run_task_command(
            runner,
            ["schtasks.exe", "/Create", "/TN", SYSTEM_TASK_NAME, "/XML", str(system_xml), "/F"],
            check=True,
        )
        try:
            _run_task_command(
                runner,
                ["schtasks.exe", "/Create", "/TN", TRAY_TASK_NAME, "/XML", str(tray_xml), "/F"],
                check=True,
            )
            _run_task_command(
                runner,
                ["schtasks.exe", "/Run", "/TN", SYSTEM_TASK_NAME],
                check=True,
            )
        except Exception:
            for task_name in (TRAY_TASK_NAME, SYSTEM_TASK_NAME):
                _run_task_command(
                    runner,
                    ["schtasks.exe", "/Delete", "/TN", task_name, "/F"],
                    check=False,
                )
            raise
    legacy_shortcut.unlink(missing_ok=True)


def install_dir_for_current_process(executable: Path | None = None) -> Path | None:
    """The directory this process was installed into, or ``None`` when it is not installed.

    A frozen build lives next to its own executable; a source checkout has no installed
    MSI, and ``None`` is how callers find that out (the background updater is skipped).
    """
    if executable is None:
        if not getattr(sys, "frozen", False):
            return None
        executable = Path(sys.executable)
    candidate = Path(executable).resolve().parent
    return candidate if candidate.is_dir() else None


def run_tray_task(
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> bool:
    """Ask Task Scheduler to start the tray in the signed-in user's session.

    The privileged side may be unable to draw on the interactive desktop itself, and the
    tray task was created for exactly this: it runs as the user with an interactive
    token, so triggering it puts the window back where the user can see it.
    """
    try:
        result = runner(
            ["schtasks.exe", "/Run", "/TN", TRAY_TASK_NAME],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return result.returncode == 0


def current_user_sid() -> str:
    result = subprocess.run(
        ["whoami.exe", "/user", "/fo", "csv", "/nh"],
        check=True,
        capture_output=True,
        text=True,
    )
    row = next(csv.reader([result.stdout.strip()]))
    for value in row:
        match = re.search(r"S-1-5-(?:\d+-)+\d+", value, re.IGNORECASE)
        if match:
            return match.group(0)
    raise RuntimeError("could not determine the current Windows user SID")


def is_system_startup_enabled(
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> bool:
    # A SYSTEM task created with its default ACL may be intentionally invisible to
    # the interactive user. The tray task is created only after the SYSTEM task
    # succeeds and is removed together with it, so it is the user-visible marker.
    result = runner(
        ["schtasks.exe", "/Query", "/TN", TRAY_TASK_NAME],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def relaunch_elevated_configuration(enabled: bool, executable: Path | None = None) -> int:
    """Kept for callers that only need to request elevation, not observe it."""
    if os.name != "nt":
        raise OSError("system startup configuration is Windows-only")
    executable = Path(executable or sys.executable)
    argument = "--configure-system-startup=" + ("enable" if enabled else "disable")
    result = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        str(executable),
        argument,
        str(executable.parent),
        0,
    )
    if result <= 32:
        if result == 5:
            raise PermissionError("administrator approval was cancelled")
        raise OSError(f"could not start elevated setup helper: ShellExecuteW={result}")
    return int(result)


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("fMask", ctypes.c_ulong),
        ("hwnd", ctypes.c_void_p),
        ("lpVerb", ctypes.c_wchar_p),
        ("lpFile", ctypes.c_wchar_p),
        ("lpParameters", ctypes.c_wchar_p),
        ("lpDirectory", ctypes.c_wchar_p),
        ("nShow", ctypes.c_int),
        ("hInstApp", ctypes.c_void_p),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", ctypes.c_wchar_p),
        ("hkeyClass", ctypes.c_void_p),
        ("dwHotKey", ctypes.c_ulong),
        ("hIcon", ctypes.c_void_p),
        ("hProcess", ctypes.c_void_p),
    ]


def launch_elevated(
    arguments: list[str],
    executable: Path | None = None,
    *,
    timeout_seconds: float = 30.0,
    wait_implementation: Callable[..., int] | None = None,
) -> int:
    """Run the executable elevated and return its exit code.

    ShellExecuteW only reports that the UAC dialog was accepted; it never reports what the
    helper did. ShellExecuteExW with a process wait makes the helper's result observable, so
    a cancelled or failed elevation is reported instead of being mistaken for success.
    """
    if os.name != "nt":
        raise OSError("system startup configuration is Windows-only")
    executable = Path(executable or sys.executable)
    wait_process = wait_implementation or ctypes.windll.kernel32.WaitForSingleObject
    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(_SHELLEXECUTEINFOW)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = str(executable)
    info.lpParameters = subprocess.list2cmdline(arguments)
    info.lpDirectory = str(executable.parent)
    info.nShow = 0
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error() or ctypes.windll.kernel32.GetLastError()
        if error == 1223:  # ERROR_CANCELLED: the user declined the elevation prompt
            raise PermissionError("administrator approval was cancelled")
        raise OSError(f"could not start elevated setup helper: WinError={error}")
    if not info.hProcess:
        raise OSError("elevated setup helper started without a process handle")
    try:
        waited = wait_process(info.hProcess, int(timeout_seconds * 1000))
        if waited == 0x00000102:  # WAIT_TIMEOUT
            raise TimeoutError("elevated setup helper did not finish in time")
        if waited != 0:
            raise OSError(f"could not wait for the elevated setup helper: wait={waited}")
        code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code)):
            raise OSError("could not read the elevated setup helper exit code")
        return int(code.value)
    finally:
        ctypes.windll.kernel32.CloseHandle(info.hProcess)
