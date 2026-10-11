# Copyright (C) 2026 yoouzic
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import configparser
import dataclasses
import datetime as dt
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import agent_health
import agent_ipc
import campus_auth
import startup_tasks
import windows_notifications
import windows_tray
import windows_credentials

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ModuleNotFoundError:  # pragma: no cover - exercised only on Python builds without Tk.
    tk = None
    messagebox = None
    ttk = None


APP_NAME = "youziauth"
APP_AUTHOR = "yoouzic"
APP_WINDOW_TITLE = f"{APP_NAME} - 校园网登录设置"
APP_DIR_NAME = APP_NAME
LEGACY_APP_DIR_NAMES = ("CampusNetworkAuth",)
DEFAULT_LOG_LINES = 300
FULL_GEOMETRY = "1080x680"
MIN_WINDOW_SIZE = (980, 640)
BACKGROUND_IMAGE_NAME = "yuzu_background.png"
APP_ICON_NAME = "yuzu_app.ico"
PANEL_ICON_NAME = "yuzu_app.png"
PANEL_ICON_SUBSAMPLE = 6
TRAY_STARTUP_ARGUMENT = "--tray-startup"
LEGACY_STARTUP_ARGUMENT = "--startup"
STARTUP_ARGUMENT = TRAY_STARTUP_ARGUMENT
STARTUP_RETRY_SECONDS = 15
STARTUP_RETRY_ATTEMPTS = 6
STARTUP_SHORTCUT_NAME = f"{APP_NAME}.lnk"
LEGACY_STARTUP_SHORTCUT_NAMES = ("CampusNetworkAuth.lnk",)
STARTUP_TOGGLE_DISABLED_TEXT = "□ 开机自启动"
STARTUP_TOGGLE_ENABLED_TEXT = "✓ 开机自启动"
APP_INSTANCE_MUTEX_NAME = f"Local\\{APP_NAME}-single-instance"
UI_PIPE_NAME = f"{APP_NAME}-ui"
DUPLICATE_INSTANCE_TITLE = f"{APP_NAME} 已在运行"
DUPLICATE_INSTANCE_MESSAGE = f"当前电脑已经运行了 {APP_NAME}，请不要重复启动。"
ERROR_ALREADY_EXISTS = 183
STATUS_COLORS = {
    windows_tray.TrayStatus.STOPPED: "#94a3b8",
    windows_tray.TrayStatus.CHECKING: "#f59e0b",
    windows_tray.TrayStatus.ONLINE: "#16a34a",
    windows_tray.TrayStatus.OFFLINE: "#dc2626",
    windows_tray.TrayStatus.ERROR: "#7f1d1d",
}


def resource_path(*parts: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base.joinpath(*parts)


def background_image_path(asset_dir: Path) -> Path:
    return asset_dir / BACKGROUND_IMAGE_NAME


def app_icon_path(asset_dir: Path) -> Path:
    return asset_dir / APP_ICON_NAME


def panel_icon_path(asset_dir: Path) -> Path:
    return asset_dir / PANEL_ICON_NAME


def format_startup_toggle_text(enabled: bool) -> str:
    return STARTUP_TOGGLE_ENABLED_TEXT if enabled else STARTUP_TOGGLE_DISABLED_TEXT


class SingleInstanceLock:
    def __init__(self, name: str = APP_INSTANCE_MUTEX_NAME):
        self.name = name
        self.handle = None
        self.kernel32 = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True

        import ctypes
        from ctypes import wintypes

        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = self.kernel32.CreateMutexW
        create_mutex.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        create_mutex.restype = wintypes.HANDLE
        close_handle = self.kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        ctypes.set_last_error(0)
        handle = create_mutex(None, False, self.name)
        if not handle:
            return True

        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            close_handle(handle)
            return False

        self.handle = handle
        return True

    def release(self) -> None:
        if self.handle is None or self.kernel32 is None:
            return
        self.kernel32.CloseHandle(self.handle)
        self.handle = None


def notify_duplicate_instance(title: str, message: str) -> None:
    if tk is None or messagebox is None:
        return
    notice_root = tk.Tk()
    notice_root.withdraw()
    try:
        messagebox.showinfo(title, message, parent=notice_root)
    finally:
        notice_root.destroy()


def handle_single_instance_startup(
    startup_mode: bool,
    lock: SingleInstanceLock,
    notify_duplicate=notify_duplicate_instance,
    secondary_action: Optional[str] = None,
    action_sender=None,
) -> Optional[int]:
    if lock.acquire():
        return None
    if secondary_action:
        sender = action_sender or send_ui_action
        try:
            sender(secondary_action)
            return 0
        except (OSError, TimeoutError, ValueError):
            if startup_mode:
                return 0
    if not startup_mode:
        notify_duplicate(DUPLICATE_INSTANCE_TITLE, DUPLICATE_INSTANCE_MESSAGE)
    return 0


def prepare_root_for_mode(root, tray_startup: bool) -> None:
    if tray_startup:
        root.withdraw()


def should_start_hidden(startup_mode: bool, secondary_action: Optional[str]) -> bool:
    return startup_mode or secondary_action in ("retry", "suppress")


def resolve_secondary_action(argv: list[str]) -> Optional[str]:
    if not argv:
        return "show"
    if TRAY_STARTUP_ARGUMENT in argv or LEGACY_STARTUP_ARGUMENT in argv:
        return None
    if "--notification-action" not in argv:
        return None
    index = argv.index("--notification-action")
    if index + 1 >= len(argv):
        return None
    return {
        "youziauth://retry": "retry",
        "youziauth://settings": "settings",
        "youziauth://suppress": "suppress",
    }.get(argv[index + 1].rstrip("/"))


def send_ui_action(action: str) -> dict[str, object]:
    return agent_ipc.send_command(UI_PIPE_NAME, agent_ipc.UiCommand(action), timeout_ms=2000)


def default_config_path(program_data_root: Optional[Path] = None) -> Path:
    return windows_credentials.machine_config_path(program_data_root)


def legacy_config_paths(
    config_path: Path,
    appdata_root: Optional[Path] = None,
) -> list[Path]:
    if appdata_root is None:
        root = os.environ.get("APPDATA")
        appdata_root = Path(root) if root else Path.home() / "AppData" / "Roaming"
    candidates = [appdata_root / APP_DIR_NAME / config_path.name]
    candidates.extend(appdata_root / name / config_path.name for name in LEGACY_APP_DIR_NAMES)
    return [path for path in candidates if path.resolve() != config_path.resolve()]


def ensure_user_config(
    config_path: Optional[Path] = None,
    template_path: Optional[Path] = None,
    legacy_paths: Optional[list[Path]] = None,
    credential_store: Optional[windows_credentials.CredentialStore] = None,
) -> Path:
    config_path = config_path or DEFAULT_CONFIG_PATH
    credential_store = credential_store or windows_credentials.CredentialStore(config_path.parent)
    legacy_candidates = (
        legacy_paths if legacy_paths is not None else legacy_config_paths(config_path)
    )
    if config_path.exists():
        windows_credentials.migrate_plaintext_password(config_path, credential_store)
        for legacy_path in legacy_candidates:
            if legacy_path.exists():
                windows_credentials.clear_matching_plaintext_password(
                    legacy_path, credential_store
                )
        return config_path

    config_path.parent.mkdir(parents=True, exist_ok=True)
    for legacy_path in legacy_candidates:
        if legacy_path.exists():
            config_path.write_text(
                legacy_path.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            windows_credentials.migrate_plaintext_password(config_path, credential_store)
            for candidate in legacy_candidates:
                if candidate.exists():
                    windows_credentials.clear_matching_plaintext_password(
                        candidate, credential_store
                    )
            return config_path

    template_path = template_path or resource_path("config.example.ini")
    if template_path.exists():
        config_path.write_text(template_path.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        config_path.write_text(
            (
                "[auth]\n"
                "portal_url = http://222.198.127.170/\n"
                "login_url =\n"
                "username =\n"
                "password =\n"
                "password_env =\n"
                "service = %E9%BB%98%E8%AE%A4\n"
                "check_interval_seconds = 60\n"
                "request_timeout_seconds = 8\n"
                "password_encrypt = auto\n"
                "log_file = campus_auth.log\n"
            ),
            encoding="utf-8",
        )
    return config_path


DEFAULT_CONFIG_PATH = default_config_path()


@dataclasses.dataclass(frozen=True)
class GuiSettings:
    username: str = ""
    password: str = ""
    check_interval_seconds: int = 60
    log_file: str = "campus_auth.log"


@dataclasses.dataclass(frozen=True)
class StartupLaunchSpec:
    target_path: Path
    arguments: str
    working_directory: Path


def _read_parser(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")
    return parser


def _auth_section(parser: configparser.ConfigParser) -> configparser.SectionProxy:
    if not parser.has_section("auth"):
        parser.add_section("auth")
    return parser["auth"]


def load_gui_settings(path: Path = DEFAULT_CONFIG_PATH) -> GuiSettings:
    parser = _read_parser(path)
    if not parser.has_section("auth"):
        return GuiSettings()

    section = parser["auth"]
    return GuiSettings(
        username=section.get("username", "").strip(),
        password=section.get("password", ""),
        check_interval_seconds=campus_auth.parse_positive_int(
            section.get("check_interval_seconds", "60"), "check_interval_seconds"
        ),
        log_file=section.get("log_file", "campus_auth.log").strip() or "campus_auth.log",
    )


def save_gui_settings(
    path: Path = DEFAULT_CONFIG_PATH,
    settings: Optional[GuiSettings] = None,
    credential_store: Optional[windows_credentials.CredentialStore] = None,
) -> None:
    if settings is None:
        settings = GuiSettings()

    username = settings.username.strip()
    if not username:
        raise ValueError("账号不能为空")
    interval = campus_auth.parse_positive_int(
        str(settings.check_interval_seconds), "check_interval_seconds"
    )

    parser = _read_parser(path)
    section = _auth_section(parser)
    credential_store = credential_store or windows_credentials.CredentialStore(path.parent)
    section.setdefault("portal_url", campus_auth.DEFAULT_PORTAL_URL + "/")
    section.setdefault("login_url", "")
    section.setdefault("service", "")
    section.setdefault("request_timeout_seconds", "8")
    section.setdefault("password_encrypt", "auto")
    section.setdefault("log_file", settings.log_file or "campus_auth.log")

    section["username"] = username
    section["check_interval_seconds"] = str(interval)
    if settings.password:
        credential_store.save_password(settings.password)
        if credential_store.load_password() != settings.password:
            raise windows_credentials.CredentialError("protected credential verification failed")
    elif section.get("password", ""):
        credential_store.save_password(section.get("password", ""))
    section["password"] = ""
    section["password_env"] = ""

    windows_credentials.atomic_write_config(path, parser)


def tail_log(path: Path, max_lines: int = DEFAULT_LOG_LINES) -> str:
    if max_lines <= 0 or not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-max_lines:])


def resolve_log_path(config_path: Path, log_file: str) -> Path:
    if Path(config_path).resolve() == Path(DEFAULT_CONFIG_PATH).resolve():
        from system_storage import default_system_storage_path
        protected_log = default_system_storage_path() / "logs" / "agent.log"
        if protected_log.is_file():
            return protected_log
    path = Path(log_file or "campus_auth.log")
    if path.is_absolute():
        return path
    return config_path.parent / path


def agent_runtime_path(config_path: Path) -> Path:
    if Path(config_path).resolve() == Path(DEFAULT_CONFIG_PATH).resolve():
        from system_storage import default_system_storage_path
        protected = default_system_storage_path() / "runtime.json"
        if protected.is_file():
            return protected
    return Path(config_path).parent / "runtime.json"


def default_startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return (
            Path(appdata)
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
        )
    return (
        Path.home()
        / "AppData"
        / "Roaming"
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
    )


def startup_shortcut_path(startup_dir: Optional[Path] = None) -> Path:
    return (startup_dir or default_startup_dir()) / STARTUP_SHORTCUT_NAME


def legacy_startup_shortcut_paths(shortcut_path: Path) -> list[Path]:
    return [
        shortcut_path.parent / name
        for name in LEGACY_STARTUP_SHORTCUT_NAMES
        if name != shortcut_path.name
    ]


def startup_launch_spec(
    executable: Optional[Path] = None,
    script_path: Optional[Path] = None,
    frozen: Optional[bool] = None,
) -> StartupLaunchSpec:
    executable = Path(executable or sys.executable)
    script_path = Path(script_path or __file__).resolve()
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen

    if frozen:
        return StartupLaunchSpec(
            target_path=executable,
            arguments=TRAY_STARTUP_ARGUMENT,
            working_directory=executable.parent,
        )

    pythonw = executable.with_name("pythonw.exe")
    target_path = pythonw if pythonw.exists() else executable
    return StartupLaunchSpec(
        target_path=target_path,
        arguments=f'"{script_path}" {TRAY_STARTUP_ARGUMENT}',
        working_directory=script_path.parent,
    )


def is_startup_enabled(shortcut_path: Optional[Path] = None) -> bool:
    if shortcut_path is None:
        return startup_tasks.is_system_startup_enabled()
    return shortcut_path.exists() or any(
        legacy_path.exists() for legacy_path in legacy_startup_shortcut_paths(shortcut_path)
    )


def _ps_quote(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def set_startup_enabled(enabled: bool, shortcut_path: Optional[Path] = None) -> None:
    if shortcut_path is None:
        # Wait for the elevated helper so its exit code can be reported. A declined UAC
        # prompt or a failed schtasks run must surface as an error, not as an assumed success.
        code = startup_tasks.launch_elevated(
            ["--configure-system-startup=" + ("enable" if enabled else "disable")]
        )
        if code != 0:
            raise RuntimeError("开机自启动配置失败（提权助手退出码 %d），请重试。" % code)
        if startup_tasks.is_system_startup_enabled() != enabled:
            raise RuntimeError(
                "开机自启动状态未生效：管理员授权可能被取消，请在 UAC 提示中选择“是”后重试。"
            )
        return
    for legacy_path in legacy_startup_shortcut_paths(shortcut_path):
        if legacy_path.exists():
            legacy_path.unlink()

    if not enabled:
        if shortcut_path.exists():
            shortcut_path.unlink()
        return

    shortcut_path.parent.mkdir(parents=True, exist_ok=True)
    spec = startup_launch_spec()
    icon_path = app_icon_path(resource_path("assets"))
    icon_location = icon_path if icon_path.exists() else spec.target_path
    script = "; ".join(
        [
            "$shell = New-Object -ComObject WScript.Shell",
            f"$shortcut = $shell.CreateShortcut({_ps_quote(shortcut_path)})",
            f"$shortcut.TargetPath = {_ps_quote(spec.target_path)}",
            f"$shortcut.Arguments = {_ps_quote(spec.arguments)}",
            f"$shortcut.WorkingDirectory = {_ps_quote(spec.working_directory)}",
            f"$shortcut.Description = 'Start {APP_NAME} when Windows signs in.'",
            f"$shortcut.IconLocation = {_ps_quote(icon_location)}",
            "$shortcut.Save()",
        ]
    )
    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        check=True,
        creationflags=creationflags,
        startupinfo=startupinfo,
    )


def build_auth_config(
    config_path: Path = DEFAULT_CONFIG_PATH,
    settings: Optional[GuiSettings] = None,
    credential_store: Optional[windows_credentials.CredentialStore] = None,
) -> campus_auth.AuthConfig:
    parser = _read_parser(config_path)
    section = _auth_section(parser)
    if settings is not None:
        section["username"] = settings.username.strip()
        section["check_interval_seconds"] = str(settings.check_interval_seconds)
    password = settings.password if settings is not None and settings.password else ""
    if not password:
        credential_store = credential_store or windows_credentials.CredentialStore(config_path.parent)
        password = credential_store.load_password()
    section["password"] = password
    section["password_env"] = ""
    config = campus_auth.load_config_from_parser(parser)
    log_path = resolve_log_path(config_path, config.log_file)
    return dataclasses.replace(config, log_file=str(log_path))


def next_monitor_delay(
    success: bool,
    regular_interval_seconds: int,
    attempt_index: int,
    startup_mode: bool,
) -> int:
    if startup_mode and not success and attempt_index < STARTUP_RETRY_ATTEMPTS:
        return min(STARTUP_RETRY_SECONDS, regular_interval_seconds)
    return regular_interval_seconds


class CampusAuthGui:
    def __init__(
        self,
        root,
        config_path: Path = DEFAULT_CONFIG_PATH,
        tray_startup: bool = False,
        dorm_store=None,
    ):
        if tk is None or ttk is None or messagebox is None:
            raise RuntimeError("Tkinter is not available in this Python installation")

        self.root = root
        self.config_path = ensure_user_config(config_path)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.tray_icon: Optional[windows_tray.WindowsTrayIcon] = None
        self.tray_status = windows_tray.TrayStatus.STOPPED
        self.tray_detail = "就绪"
        self.startup_mode = tray_startup
        self.tray_startup = tray_startup
        self.agent_mode = is_startup_enabled()
        self.startup_enabled = self.agent_mode
        self.agent_ipc_ok: bool | None = None
        self.agent_probe_in_flight = False
        self.agent_last_probe_monotonic = 0.0
        self.agent_startup_deadline = (
            dt.datetime.now().astimezone() + dt.timedelta(seconds=90)
        )
        self.agent_health_state = (
            agent_health.AgentHealthState.STARTING
            if self.agent_mode
            else agent_health.AgentHealthState.DISABLED
        )
        self.notification_tracker = windows_notifications.NotificationTracker()
        self.last_agent_snapshot: agent_ipc.RuntimeSnapshot | None = None
        self.status_dot = None
        self.status_dot_id = None
        self.background_image = None
        self.background_label = None
        self.panel_icon_source_image = None
        self.panel_icon_image = None
        # 多账号：账号层持有「当前活动账号」的控制器。注入 dorm_store 的旧调用点
        # （预览与自检）保持单控制器行为。账号档案读不出来时不在这里抛异常——
        # 窗口必须能打开，把原因显示出来，并且所有打卡操作 fail-closed。
        from dorm_accounts import DormAccounts
        from dorm_panel import DormController
        self.dorm_accounts = None if dorm_store is not None else DormAccounts()
        self.dorm_controller = (DormController(dorm_store) if dorm_store is not None
                                else self.dorm_accounts.controller_or_none())
        self.dorm_panel = None
        self._dorm_notice = None

        self.username_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.interval_var = tk.StringVar(value="60")
        self.startup_var = tk.BooleanVar(value=False)
        self.startup_toggle_text_var = tk.StringVar(
            value=format_startup_toggle_text(False)
        )
        self.status_var = tk.StringVar(value="就绪")

        self._configure_window()
        self._build_layout()
        self._load_settings_into_form()
        self.refresh_log()
        self.root.after(250, self._process_events)
        self.root.after(5000, self._auto_refresh_log)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self._ensure_tray_icon()
        self._start_ui_command_server()
        self.root.after(500, self._poll_agent_status)
        self.root.after(1000, self._poll_dorm)
        if self.agent_mode:
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="disabled")
            self._set_status("系统级后台认证已启用", windows_tray.TrayStatus.CHECKING)

    def _start_ui_command_server(self) -> None:
        server = agent_ipc.NamedPipeServer(
            UI_PIPE_NAME,
            self._receive_ui_command,
            command_parser=agent_ipc.UiCommand.parse,
        )

        def serve() -> None:
            while not self.stop_event.is_set():
                try:
                    server.serve_once()
                except Exception as exc:  # noqa: BLE001 - tray must survive one IPC error.
                    if not self.stop_event.is_set():
                        self.events.put(("status", (f"托盘命令通道异常：{exc}", windows_tray.TrayStatus.ERROR)))

        threading.Thread(target=serve, name="youziauth-ui-ipc", daemon=True).start()

    def _receive_ui_command(self, command: agent_ipc.UiCommand) -> dict[str, object]:
        self.events.put(("ui_action", command.command))
        return {"ok": True}

    def _start_agent_health_probe(self) -> None:
        now = time.monotonic()
        if self.agent_probe_in_flight or now - self.agent_last_probe_monotonic < 5:
            return
        self.agent_probe_in_flight = True
        self.agent_last_probe_monotonic = now

        def worker() -> None:
            ok = False
            try:
                response = agent_ipc.send_command(
                    "youziauth-agent",
                    agent_ipc.AgentCommand("status"),
                    timeout_ms=1000,
                )
                ok = response.get("ok") is True
            except (OSError, TimeoutError, ValueError):
                pass
            self.events.put(("agent_probe", ok))

        threading.Thread(
            target=worker,
            name="youziauth-agent-health",
            daemon=True,
        ).start()

    def _poll_agent_status(self) -> None:
        if self.agent_mode:
            snapshot = None
            try:
                snapshot = agent_ipc.read_snapshot(
                    agent_runtime_path(self.config_path)
                )
            except (OSError, ValueError):
                pass
            self._start_agent_health_probe()
            try:
                interval = campus_auth.parse_positive_int(
                    self.interval_var.get(), "check_interval_seconds"
                )
            except ValueError:
                interval = 60
            health = agent_health.evaluate_agent_health(
                startup_enabled=self.startup_enabled,
                snapshot=snapshot,
                ipc_ok=self.agent_ipc_ok,
                check_interval_seconds=interval,
                now=dt.datetime.now().astimezone(),
                startup_deadline=self.agent_startup_deadline,
            )
            self.agent_health_state = health.state
            status = {
                agent_health.AgentHealthState.STARTING: windows_tray.TrayStatus.CHECKING,
                agent_health.AgentHealthState.HEALTHY: windows_tray.TrayStatus.ONLINE,
                agent_health.AgentHealthState.DEGRADED: windows_tray.TrayStatus.ERROR,
            }[health.state]
            self._set_status(health.detail, status)
            self._update_agent_controls(health.state)
            if health.state is agent_health.AgentHealthState.HEALTHY and health.snapshot:
                self.last_agent_snapshot = health.snapshot
                toast_xml = self.notification_tracker.evaluate(health.snapshot)
                if toast_xml:
                    self._show_notification(toast_xml)
        self.root.after(1000, self._poll_agent_status)

    def _update_agent_controls(self, state: agent_health.AgentHealthState) -> None:
        if state is agent_health.AgentHealthState.DEGRADED:
            self.start_button.configure(state="normal", text="修复系统代理")
        elif state is agent_health.AgentHealthState.HEALTHY:
            self.start_button.configure(state="disabled", text="系统代理运行中")
        elif state is agent_health.AgentHealthState.STARTING:
            self.start_button.configure(state="disabled", text="等待系统代理")
        else:
            self.start_button.configure(state="normal", text="开始后台检测")

    def repair_system_agent(self) -> None:
        try:
            code = startup_tasks.launch_elevated(["--configure-system-startup=enable"])
            if code != 0:
                raise RuntimeError("提权助手退出码 %d" % code)
        except Exception as exc:  # noqa: BLE001 - report UAC/setup failures in the UI.
            self._set_status(
                f"修复系统代理失败：{exc}", windows_tray.TrayStatus.ERROR
            )
            return
        self.agent_ipc_ok = None
        self.agent_probe_in_flight = False
        self.startup_enabled = True
        self.agent_mode = True
        self.agent_startup_deadline = (
            dt.datetime.now().astimezone() + dt.timedelta(seconds=90)
        )
        self._set_status(
            "已请求管理员修复，正在等待系统代理",
            windows_tray.TrayStatus.CHECKING,
        )

    def _show_notification(self, toast_xml: str) -> None:
        threading.Thread(
            target=windows_notifications.show_toast,
            args=(toast_xml,),
            name="youziauth-toast",
            daemon=True,
        ).start()

    def _send_agent_command(self, command: str) -> None:
        def worker() -> None:
            try:
                agent_ipc.send_command(
                    "youziauth-agent",
                    agent_ipc.AgentCommand(command),
                    timeout_ms=3000,
                )
            except Exception as exc:  # noqa: BLE001 - surface local agent errors in the tray.
                self.events.put(("status", (f"系统认证代理不可用：{exc}", windows_tray.TrayStatus.ERROR)))

        threading.Thread(target=worker, name=f"youziauth-agent-{command}", daemon=True).start()

    def _configure_window(self) -> None:
        self.root.title(APP_WINDOW_TITLE)
        self.root.geometry(FULL_GEOMETRY)
        self.root.minsize(*MIN_WINDOW_SIZE)
        icon_path = app_icon_path(resource_path("assets"))
        if icon_path.exists():
            try:
                self.root.iconbitmap(default=str(icon_path))
            except tk.TclError:
                pass
        self._install_background()
        self.panel_icon_source_image = self._load_photo_image(
            panel_icon_path(resource_path("assets"))
        )
        if self.panel_icon_source_image is not None:
            self.panel_icon_image = self.panel_icon_source_image.subsample(
                PANEL_ICON_SUBSAMPLE,
                PANEL_ICON_SUBSAMPLE,
            )
        self.root.columnconfigure(0, weight=0)
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#f6f7fb")
        style.configure("Panel.TFrame", background="#ffffff")
        style.configure("TLabel", background="#f6f7fb", foreground="#1f2937")
        style.configure("Panel.TLabel", background="#ffffff", foreground="#1f2937")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Hint.TLabel", foreground="#6b7280")
        style.configure("Panel.TCheckbutton", background="#ffffff", foreground="#1f2937")
        style.configure("TButton", padding=(12, 6))
        style.configure("Primary.TButton", padding=(12, 6))

    def _load_photo_image(self, path: Path):
        if not path.exists():
            return None
        try:
            return tk.PhotoImage(file=str(path))
        except tk.TclError:
            return None

    def _install_background(self) -> None:
        self.background_image = self._load_photo_image(
            background_image_path(resource_path("assets"))
        )
        if self.background_image is None:
            return
        self.background_label = tk.Label(
            self.root,
            image=self.background_image,
            borderwidth=0,
            highlightthickness=0,
            background="#fff8ed",
        )
        self.background_label.place(x=0, y=0, relwidth=1, relheight=1)
        self.background_label.lower()

    def _build_layout(self) -> None:
        self.root.configure(background="#fff8ed")

        self.settings_panel = ttk.Frame(self.root, style="Panel.TFrame", padding=18)
        settings = self.settings_panel
        settings.grid(row=0, column=0, sticky="nsew", padx=(18, 12), pady=18)
        settings.columnconfigure(0, weight=1)

        title_row = ttk.Frame(settings, style="Panel.TFrame")
        title_row.grid(row=0, column=0, sticky="ew")
        if self.panel_icon_image is not None:
            ttk.Label(title_row, image=self.panel_icon_image, style="Panel.TLabel").grid(
                row=0, column=0, sticky="w", padx=(0, 8)
            )
        ttk.Label(title_row, text=APP_NAME, style="Title.TLabel").grid(
            row=0, column=1, sticky="w"
        )
        ttk.Label(
            settings,
            text=f"校园网登录设置 · 作者：{APP_AUTHOR}\n保存后可立即检测，也可以开启后台检测。",
            style="Panel.TLabel",
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(4, 14))

        ttk.Label(settings, text="账号", style="Panel.TLabel").grid(
            row=2, column=0, sticky="w"
        )
        self.username_entry = ttk.Entry(settings, textvariable=self.username_var, width=28)
        self.username_entry.grid(row=3, column=0, sticky="ew", pady=(4, 10))

        ttk.Label(settings, text="密码", style="Panel.TLabel").grid(
            row=4, column=0, sticky="w"
        )
        self.password_entry = ttk.Entry(
            settings, textvariable=self.password_var, width=28, show="*"
        )
        self.password_entry.grid(row=5, column=0, sticky="ew", pady=(4, 10))

        ttk.Label(settings, text="检测间隔（秒）", style="Panel.TLabel").grid(
            row=6, column=0, sticky="w"
        )
        self.interval_spinbox = ttk.Spinbox(
            settings,
            from_=5,
            to=3600,
            increment=5,
            textvariable=self.interval_var,
            width=12,
        )
        self.interval_spinbox.grid(row=7, column=0, sticky="w", pady=(4, 12))

        self.startup_toggle = tk.Checkbutton(
            settings,
            textvariable=self.startup_toggle_text_var,
            variable=self.startup_var,
            command=self._sync_startup_toggle_text,
            indicatoron=False,
            onvalue=True,
            offvalue=False,
            anchor="w",
            background="#ffffff",
            activebackground="#eef2ff",
            selectcolor="#dcfce7",
            foreground="#1f2937",
            activeforeground="#111827",
            relief="ridge",
            borderwidth=1,
            highlightthickness=0,
            padx=10,
            pady=6,
            cursor="hand2",
            font=("Microsoft YaHei UI", 10),
        )
        self.startup_toggle.grid(row=8, column=0, sticky="ew", pady=(0, 12))

        buttons = ttk.Frame(settings, style="Panel.TFrame")
        buttons.grid(row=9, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)

        ttk.Button(buttons, text="保存", command=self.save_settings).grid(
            row=0, column=0, sticky="ew", padx=(0, 6), pady=(0, 10)
        )
        ttk.Button(buttons, text="检测一次", command=self.run_once).grid(
            row=0, column=1, sticky="ew", padx=(6, 0), pady=(0, 10)
        )
        self.start_button = ttk.Button(buttons, text="开始后台检测", command=self.start_monitor)
        self.start_button.grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=(0, 10))
        self.stop_button = ttk.Button(
            buttons, text="停止后台检测", command=self.stop_monitor, state="disabled"
        )
        self.stop_button.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=(0, 10))
        ttk.Button(buttons, text="隐藏到托盘", command=self.hide_to_tray).grid(
            row=2, column=0, columnspan=2, sticky="ew"
        )

        status_row = ttk.Frame(settings, style="Panel.TFrame")
        status_row.grid(row=10, column=0, sticky="ew", pady=(16, 0))
        status_row.columnconfigure(1, weight=1)
        self.status_dot = tk.Canvas(
            status_row,
            width=14,
            height=14,
            highlightthickness=0,
            background="#ffffff",
        )
        self.status_dot.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.status_dot_id = self.status_dot.create_oval(
            2,
            2,
            12,
            12,
            fill=STATUS_COLORS[windows_tray.TrayStatus.STOPPED],
            outline="",
        )
        ttk.Label(status_row, textvariable=self.status_var, style="Panel.TLabel").grid(
            row=0, column=1, sticky="ew"
        )
        ttk.Label(
            settings,
            text=f"配置文件：{self.config_path.name}",
            style="Panel.TLabel",
        ).grid(row=11, column=0, sticky="w", pady=(8, 0))
        ttk.Button(settings, text="寝室打卡…", command=self.open_dorm).grid(
            row=12, column=0, sticky="ew", pady=(10, 0)
        )

        self.logs_panel = ttk.Frame(self.root, padding=(10, 18, 18, 18))
        logs = self.logs_panel
        logs.grid(row=0, column=1, sticky="nsew")
        logs.columnconfigure(0, weight=1)
        logs.rowconfigure(1, weight=1)

        header = ttk.Frame(logs)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="日志", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(header, text="刷新", command=self.refresh_log).grid(
            row=0, column=1, sticky="e"
        )

        log_frame = ttk.Frame(logs)
        log_frame.grid(row=1, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(
            log_frame,
            wrap="none",
            state="disabled",
            background="#0f172a",
            foreground="#e5e7eb",
            insertbackground="#e5e7eb",
            relief="flat",
            padx=12,
            pady=12,
            font=("Consolas", 10),
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)
        if self.background_label is not None:
            self.background_label.lower()

    def _load_settings_into_form(self) -> None:
        try:
            settings = load_gui_settings(self.config_path)
        except Exception as exc:  # noqa: BLE001 - show config problems in the GUI.
            self._set_status(f"读取配置失败：{exc}", windows_tray.TrayStatus.ERROR)
            return

        self.username_var.set(settings.username)
        self.password_var.set(settings.password)
        self.interval_var.set(str(settings.check_interval_seconds))
        self.startup_var.set(is_startup_enabled())
        self._sync_startup_toggle_text()

    def _sync_startup_toggle_text(self) -> None:
        self.startup_toggle_text_var.set(
            format_startup_toggle_text(bool(self.startup_var.get()))
        )

    def _current_settings(self) -> GuiSettings:
        interval = campus_auth.parse_positive_int(
            self.interval_var.get(), "check_interval_seconds"
        )
        current = load_gui_settings(self.config_path)
        return GuiSettings(
            username=self.username_var.get().strip(),
            password=self.password_var.get(),
            check_interval_seconds=interval,
            log_file=current.log_file,
        )

    def save_settings(self) -> bool:
        try:
            settings = self._current_settings()
            save_gui_settings(self.config_path, settings)
            requested_startup = bool(self.startup_var.get())
            current_startup = is_startup_enabled()
            if requested_startup != current_startup:
                set_startup_enabled(requested_startup)
            self.startup_enabled = requested_startup
            self.agent_mode = requested_startup
            if self.agent_mode:
                self._send_agent_command("reload-config")
        except Exception as exc:  # noqa: BLE001 - GUI should report validation errors.
            messagebox.showerror("保存失败", str(exc))
            self._set_status(f"保存失败：{exc}", windows_tray.TrayStatus.ERROR)
            return False

        self._set_status("设置已保存")
        self.refresh_log()
        return True

    def run_once(self) -> None:
        if self.agent_mode:
            self._set_status("正在请求系统代理重新认证...", windows_tray.TrayStatus.CHECKING)
            self._send_agent_command("retry")
            return
        try:
            settings = self._current_settings()
            config = build_auth_config(self.config_path, settings)
        except Exception as exc:  # noqa: BLE001 - GUI should report validation errors.
            messagebox.showerror("无法开始检测", str(exc))
            self._set_status(f"无法开始检测：{exc}", windows_tray.TrayStatus.ERROR)
            return

        self._set_status("正在检测...", windows_tray.TrayStatus.CHECKING)
        thread = threading.Thread(
            target=self._run_once_worker,
            args=(config,),
            daemon=True,
        )
        thread.start()

    def start_monitor(self) -> bool:
        if self.agent_mode:
            if self.agent_health_state is agent_health.AgentHealthState.DEGRADED:
                self.repair_system_agent()
            else:
                self._set_status(
                    "正在请求系统认证代理重新加载配置",
                    windows_tray.TrayStatus.CHECKING,
                )
                self._send_agent_command("reload-config")
            return True
        if self.worker and self.worker.is_alive():
            return True
        if not self.save_settings():
            return False
        try:
            config = build_auth_config(self.config_path)
        except Exception as exc:  # noqa: BLE001 - GUI should report validation errors.
            messagebox.showerror("无法开始后台检测", str(exc))
            self._set_status(f"无法开始后台检测：{exc}", windows_tray.TrayStatus.ERROR)
            return False

        self.stop_event.clear()
        self.worker = threading.Thread(
            target=self._monitor_worker,
            args=(config,),
            daemon=True,
        )
        self.worker.start()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._set_status("后台检测已启动", windows_tray.TrayStatus.CHECKING)
        return True

    def stop_monitor(self) -> None:
        if self.agent_mode:
            self._set_status("系统级后台认证由计划任务持续运行")
            return
        self.stop_event.set()
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self._set_status("正在停止后台检测...", windows_tray.TrayStatus.STOPPED)

    def _run_once_worker(self, config: campus_auth.AuthConfig) -> None:
        ok = self._run_auth_once(config)
        self.events.put(
            (
                "status",
                (
                    "检测完成：已认证或登录成功" if ok else "检测完成：登录失败",
                    windows_tray.TrayStatus.ONLINE if ok else windows_tray.TrayStatus.OFFLINE,
                ),
            )
        )
        self.events.put(("log", "refresh"))

    def _monitor_worker(self, config: campus_auth.AuthConfig) -> None:
        self.events.put(("status", ("后台检测运行中", windows_tray.TrayStatus.CHECKING)))
        attempt_index = 0
        while not self.stop_event.is_set():
            ok = self._run_auth_once(config)
            self.events.put(
                (
                    "status",
                    (
                        "后台检测：正常" if ok else "后台检测：登录失败",
                        windows_tray.TrayStatus.ONLINE if ok else windows_tray.TrayStatus.OFFLINE,
                    ),
                )
            )
            self.events.put(("log", "refresh"))
            delay = next_monitor_delay(
                success=ok,
                regular_interval_seconds=config.check_interval_seconds,
                attempt_index=attempt_index,
                startup_mode=self.startup_mode,
            )
            attempt_index = 0 if ok else attempt_index + 1
            if self.stop_event.wait(delay):
                break
        self.events.put(("status", ("后台检测已停止", windows_tray.TrayStatus.STOPPED)))
        self.events.put(("monitor_stopped", ""))

    def _run_auth_once(self, config: campus_auth.AuthConfig) -> bool:
        logger = campus_auth.configure_logging(config.log_file, verbose=True)
        client = campus_auth.CampusAuthClient(config, logger)
        return campus_auth.run_once(client, logger)

    def _process_events(self) -> None:
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                if isinstance(value, tuple):
                    message, tray_status = value
                    self._set_status(message, tray_status)
                else:
                    self._set_status(str(value))
            elif kind == "log":
                self.refresh_log()
            elif kind == "monitor_stopped":
                self.start_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
            elif kind == "tray_command":
                self._handle_tray_command(str(value))
            elif kind == "ui_action":
                self._handle_ui_action(str(value))
            elif kind == "agent_probe":
                self.agent_ipc_ok = bool(value)
                self.agent_probe_in_flight = False
        self.root.after(250, self._process_events)

    def _auto_refresh_log(self) -> None:
        self.refresh_log()
        self.root.after(5000, self._auto_refresh_log)

    def refresh_log(self) -> None:
        try:
            settings = load_gui_settings(self.config_path)
            log_path = resolve_log_path(self.config_path, settings.log_file)
            text = tail_log(log_path, DEFAULT_LOG_LINES)
        except Exception as exc:  # noqa: BLE001 - show log read problems inline.
            text = f"读取日志失败：{exc}"

        if not text:
            text = "暂无日志"

        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", text)
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _set_status(
        self,
        message: str,
        tray_status: Optional[windows_tray.TrayStatus] = None,
    ) -> None:
        self.status_var.set(message)
        self.tray_detail = message
        if tray_status is not None:
            self.tray_status = tray_status
        self._update_tray_icon()

    def _ensure_tray_icon(self) -> bool:
        if self.tray_icon is not None:
            return True
        self.tray_icon = windows_tray.WindowsTrayIcon(
            on_command=self._queue_tray_command,
            status=self.tray_status,
            detail=self.tray_detail,
            icon_paths=windows_tray.default_icon_paths(resource_path("assets")),
        )
        if not self.tray_icon.start():
            self.tray_icon = None
            return False
        self._update_tray_icon()
        return True

    def _update_tray_icon(self) -> None:
        if self.tray_icon is not None:
            self.tray_icon.update(self.tray_status, self.tray_detail)
        if self.status_dot is not None and self.status_dot_id is not None:
            self.status_dot.itemconfigure(
                self.status_dot_id,
                fill=STATUS_COLORS.get(self.tray_status, "#94a3b8"),
            )

    def _queue_tray_command(self, command: str) -> None:
        self.events.put(("tray_command", command))

    def _handle_tray_command(self, command: str) -> None:
        if command == "show":
            self.show_main_window()
        elif command == "settings":
            self.open_settings()
        elif command == "check":
            self.run_once()
        elif command == "dorm":
            self.open_dorm()
        elif command == "quit":
            self.quit_application()

    def _handle_ui_action(self, action: str) -> None:
        if action in ("show", "settings"):
            self.open_settings()
        elif action == "retry":
            if self.agent_mode:
                self.notification_tracker.mark_retry(self.last_agent_snapshot)
            self.run_once()
        elif action == "suppress" and self.agent_mode:
            self._send_agent_command("suppress-notifications-for-boot")

    def hide_to_tray(self) -> None:
        if not self.agent_mode and not (self.worker and self.worker.is_alive()):
            if not self.start_monitor():
                return
        if self._ensure_tray_icon():
            self.root.withdraw()
            self._set_status("已隐藏到托盘，后台检测继续运行")
        else:
            self.root.iconify()
            self._set_status("托盘不可用，已最小化到任务栏")

    def show_main_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def open_settings(self) -> None:
        self.show_main_window()
        self.username_entry.focus_set()

    def quit_application(self) -> None:
        if hasattr(self, "dorm_controller") and self.dorm_controller is not None:
            self.dorm_controller.close()
        if getattr(self, "dorm_accounts", None) is not None:
            self.dorm_accounts.close()
        self.stop_event.set()
        if self.tray_icon is not None:
            self.tray_icon.stop()
            self.tray_icon = None
        self.root.destroy()

    def close(self) -> None:
        self.quit_application()

    def open_dorm(self) -> None:
        from dorm_panel import DormPanel
        if self.dorm_controller is None:
            self._set_status("账号档案无法读取，寝室打卡暂不可用", windows_tray.TrayStatus.ERROR)
            return
        if self.dorm_panel is None or not self.dorm_panel.window.winfo_exists():
            self.dorm_panel = DormPanel(self.root, self.dorm_controller)
        self.dorm_panel.show()

    def _poll_dorm(self) -> None:
        if self.dorm_accounts is not None:
            # 多账号：调度必须覆盖所有账号，不能只跑面板正在显示的那一个。
            self.dorm_controller = self.dorm_accounts.controller_or_none()
            events = self.dorm_accounts.poll()
        else:
            if self.dorm_controller is None:
                # 账号档案读不出来：不静默换一个默认账号，也不刷屏，只把调度继续挂着。
                self.root.after(1000, self._poll_dorm)
                return
            self.dorm_controller.poll()
            events = [('', result) for result in self.dorm_controller.drain()]
        for label, result in events:
            if self.dorm_panel is not None and self.dorm_panel.window.winfo_exists():
                self.dorm_panel.update(result)
            # 账号名进提醒去重键：多账号下两个账号的失败文案可能一模一样。
            notice = (label, result.at[:10] or dt.date.today().isoformat(),
                      result.task.key if result.task else '', result.state)
            if result.state in ('signed', 'login_required', 'location_required', 'uncertain', 'error', 'network_error'):
                if notice != self._dorm_notice:
                    self._dorm_notice = notice
                    self._show_notification(windows_notifications.build_dorm_toast(
                        f'{label}：{result.message}' if label else result.message))
        if self.dorm_panel is not None and self.dorm_panel.window.winfo_exists():
            self.dorm_panel.refresh()
        self.root.after(1000, self._poll_dorm)


def _run_elevated_startup_configuration(value: str) -> int:
    enabled = value == "enable"
    if value not in ("enable", "disable"):
        print("invalid system startup mode", file=sys.stderr)
        return 2
    try:
        config_path = ensure_user_config()
        user_sid = startup_tasks.current_user_sid()
        windows_credentials.secure_program_data_acl(config_path.parent, user_sid)
        install_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
        startup_tasks.configure_system_startup(enabled, install_dir, user_sid)
    except Exception as exc:  # noqa: BLE001 - elevated helper must return a clear exit code.
        print(f"system startup configuration failed: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if "--legacy-ui" not in argv:
        from campus_auth_desktop import main as desktop_main
        return desktop_main(argv)
    if len(argv) == 2 and argv[0] in ("--dorm-self-test", "--dorm-login-probe"):
        from dorm_selftest import run
        return run(argv[1], online_login=argv[0] == "--dorm-login-probe")
    for argument in argv:
        if argument.startswith("--configure-system-startup="):
            return _run_elevated_startup_configuration(argument.split("=", 1)[1])
    if tk is None:
        raise RuntimeError("Tkinter is not available in this Python installation")
    startup_mode = TRAY_STARTUP_ARGUMENT in argv or LEGACY_STARTUP_ARGUMENT in argv
    secondary_action = resolve_secondary_action(argv)
    hidden_mode = should_start_hidden(startup_mode, secondary_action)
    instance_lock = SingleInstanceLock()
    exit_code = handle_single_instance_startup(
        startup_mode,
        instance_lock,
        secondary_action=secondary_action,
    )
    if exit_code is not None:
        return exit_code

    root = tk.Tk()
    prepare_root_for_mode(root, hidden_mode)
    app = CampusAuthGui(root, tray_startup=hidden_mode)
    if secondary_action and secondary_action not in ("show",):
        root.after(0, lambda: app._handle_ui_action(secondary_action))
    try:
        root.mainloop()
        return 0
    finally:
        instance_lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
