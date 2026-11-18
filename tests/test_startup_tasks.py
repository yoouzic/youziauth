import base64
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import startup_tasks
from startup_tasks import (
    SYSTEM_TASK_NAME,
    TRAY_TASK_NAME,
    build_system_task_xml,
    build_tray_task_xml,
    configure_system_startup,
    is_system_startup_enabled,
)


NS = {"task": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


class StartupTaskXmlTests(unittest.TestCase):
    def test_system_task_is_zero_delay_system_priority_four(self):
        install_dir = Path(r"C:\Program Files\youziauth")
        user_sid = "S-1-5-21-123-456-789-1001"
        root = ET.fromstring(build_system_task_xml(install_dir, user_sid))

        self.assertIsNotNone(root.find(".//task:BootTrigger", NS))
        self.assertIsNone(root.find(".//task:BootTrigger/task:Delay", NS))
        self.assertEqual(root.findtext(".//task:Principal/task:UserId", namespaces=NS), "S-1-5-18")
        self.assertIsNone(root.find(".//task:Principal/task:LogonType", NS))
        self.assertEqual(root.findtext(".//task:Settings/task:Priority", namespaces=NS), "4")
        self.assertEqual(
            root.findtext(".//task:Exec/task:Command", namespaces=NS),
            str(install_dir / "youziauth-agent.exe"),
        )
        self.assertEqual(root.findtext(".//task:Settings/task:ExecutionTimeLimit", namespaces=NS), "PT0S")
        self.assertIsNone(root.find(".//task:Settings/task:RunOnlyIfNetworkAvailable", NS))
        self.assertEqual(
            root.findtext(".//task:Exec/task:Arguments", namespaces=NS),
            f"--allowed-user-sid {user_sid}",
        )

    def test_tray_task_uses_interactive_token_and_hidden_mode(self):
        install_dir = Path(r"C:\Program Files\youziauth [O'Neil]")
        user_sid = "S-1-5-21-123-456-789-1001"
        root = ET.fromstring(build_tray_task_xml(install_dir, user_sid))

        self.assertIsNotNone(root.find(".//task:LogonTrigger", NS))
        self.assertIsNone(root.find(".//task:LogonTrigger/task:Delay", NS))
        self.assertEqual(root.findtext(".//task:Principal/task:UserId", namespaces=NS), user_sid)
        self.assertEqual(
            root.findtext(".//task:Principal/task:LogonType", namespaces=NS),
            "InteractiveToken",
        )
        command = root.findtext(".//task:Exec/task:Command", namespaces=NS)
        arguments = root.findtext(".//task:Exec/task:Arguments", namespaces=NS).split()
        self.assertEqual(Path(command).name, "powershell.exe")
        self.assertEqual(arguments[:-1], [
            "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand",
        ])
        script = base64.b64decode(arguments[-1]).decode("utf-16le")
        encoded_install_dir = base64.b64encode(str(install_dir).encode("utf-16le")).decode("ascii")
        self.assertIn(f"FromBase64String('{encoded_install_dir}')", script)
        self.assertIn("base_library.zip", script)
        self.assertIn("encodings/__init__.pyc", script)
        self.assertLess(script.index("encodings/__init__.pyc"), script.index("--tray-startup"))
        self.assertEqual(root.findtext(".//task:Exec/task:WorkingDirectory", namespaces=NS), str(install_dir))


class ConfigureStartupTests(unittest.TestCase):
    def test_enable_creates_both_tasks_before_removing_legacy_shortcut(self):
        with tempfile.TemporaryDirectory() as temporary:
            shortcut = Path(temporary) / "youziauth.lnk"
            shortcut.write_text("legacy", encoding="utf-8")
            calls = []

            def runner(arguments, **kwargs):
                calls.append(arguments)
                return subprocess.CompletedProcess(arguments, 0, "", "")

            configure_system_startup(
                True,
                Path(r"C:\Program Files\youziauth"),
                "S-1-5-21-1-2-3-1001",
                runner=runner,
                legacy_shortcut=shortcut,
            )

            self.assertEqual(
                [call[1] for call in calls], ["/Create", "/Create", "/Run"]
            )
            self.assertIn(SYSTEM_TASK_NAME, calls[0])
            self.assertIn(TRAY_TASK_NAME, calls[1])
            self.assertIn(SYSTEM_TASK_NAME, calls[2])
            self.assertFalse(shortcut.exists())

    def test_agent_start_failure_rolls_back_tasks_and_keeps_legacy_shortcut(self):
        with tempfile.TemporaryDirectory() as temporary:
            shortcut = Path(temporary) / "youziauth.lnk"
            shortcut.write_text("legacy", encoding="utf-8")
            calls = []

            def runner(arguments, **kwargs):
                calls.append(arguments)
                if arguments[1] == "/Run":
                    raise subprocess.CalledProcessError(1, arguments)
                return subprocess.CompletedProcess(arguments, 0, "", "")

            with self.assertRaises(subprocess.CalledProcessError):
                configure_system_startup(
                    True,
                    Path(r"C:\Program Files\youziauth"),
                    "S-1-5-21-1-2-3-1001",
                    runner=runner,
                    legacy_shortcut=shortcut,
                )

            self.assertTrue(shortcut.exists())
            self.assertTrue(
                any(
                    call[1] == "/Delete" and SYSTEM_TASK_NAME in call
                    for call in calls
                )
            )
            self.assertTrue(
                any(
                    call[1] == "/Delete" and TRAY_TASK_NAME in call
                    for call in calls
                )
            )

    def test_partial_enable_failure_rolls_back_and_keeps_legacy_shortcut(self):
        with tempfile.TemporaryDirectory() as temporary:
            shortcut = Path(temporary) / "youziauth.lnk"
            shortcut.write_text("legacy", encoding="utf-8")
            calls = []

            def runner(arguments, **kwargs):
                calls.append(arguments)
                if arguments[1] == "/Create" and TRAY_TASK_NAME in arguments:
                    raise subprocess.CalledProcessError(1, arguments)
                return subprocess.CompletedProcess(arguments, 0, "", "")

            with self.assertRaises(subprocess.CalledProcessError):
                configure_system_startup(
                    True,
                    Path(r"C:\Program Files\youziauth"),
                    "S-1-5-21-1-2-3-1001",
                    runner=runner,
                    legacy_shortcut=shortcut,
                )

            self.assertTrue(shortcut.exists())
            self.assertTrue(any(call[1] == "/Delete" and SYSTEM_TASK_NAME in call for call in calls))


class EnabledStateTests(unittest.TestCase):
    def test_visible_tray_task_is_the_non_admin_enabled_marker(self):
        calls = []

        def runner(arguments, **kwargs):
            calls.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, "", "")

        self.assertTrue(is_system_startup_enabled(runner=runner))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][3], TRAY_TASK_NAME)

    def test_missing_tray_task_means_disabled(self):
        def runner(arguments, **kwargs):
            return subprocess.CompletedProcess(arguments, 1, "", "not found")

        self.assertFalse(is_system_startup_enabled(runner=runner))


class ElevatedLaunchTests(unittest.TestCase):
    """ShellExecuteW hid the UAC outcome; launch_elevated must report it (1.4.2 regression)."""

    def setUp(self):
        self.calls = []
        self.kernel = mock.Mock()
        self.process = 4242
        self.shell_result = 1
        self.exit_code = 0

        def shell_execute_ex_w(pointer):
            self.calls.append(pointer._obj)
            if self.shell_result:
                pointer._obj.hProcess = self.process
            return self.shell_result

        def get_exit_code_process(handle, pointer):
            pointer._obj.value = self.exit_code
            return 1

        self.kernel.WaitForSingleObject.return_value = 0
        self.kernel.GetExitCodeProcess.side_effect = get_exit_code_process
        self.kernel.GetLastError.return_value = 0
        fakes = mock.Mock()
        fakes.shell32.ShellExecuteExW.side_effect = shell_execute_ex_w
        fakes.kernel32 = self.kernel
        patch = mock.patch.object(startup_tasks.ctypes, "windll", fakes)
        patch.start()
        self.addCleanup(patch.stop)
        # ctypes.get_last_error() 是线程私有的「最近一次错误」, 会被同线程里更早的
        # 调用(例如看门狗探测进程存活)留成别的值。测试里固定为 0, 让错误码只来自
        # 下面的假 kernel32.GetLastError() —— 否则这个用例会随执行顺序偶发失败。
        last_error = mock.patch.object(startup_tasks.ctypes, "get_last_error",
                                       return_value=0)
        last_error.start()
        self.addCleanup(last_error.stop)

    def test_runs_the_helper_with_runas_and_returns_its_exit_code(self):
        code = startup_tasks.launch_elevated(["--configure-system-startup=enable"])
        self.assertEqual(code, 0)
        info = self.calls[0]
        self.assertEqual(info.lpVerb, "runas")
        self.assertIn("--configure-system-startup=enable", info.lpParameters)
        self.assertEqual(info.fMask, 0x40)
        self.kernel.CloseHandle.assert_called_once_with(self.process)

    def test_helper_failure_exit_code_reaches_the_caller(self):
        self.exit_code = 1
        self.assertEqual(startup_tasks.launch_elevated(["--configure-system-startup=enable"]), 1)

    def test_a_declined_uac_prompt_is_reported_as_permission_error(self):
        self.shell_result = 0
        self.kernel.GetLastError.return_value = 1223
        with self.assertRaises(PermissionError):
            startup_tasks.launch_elevated(["--configure-system-startup=enable"])

    def test_an_unexpected_shell_error_is_reported(self):
        self.shell_result = 0
        self.kernel.GetLastError.return_value = 5
        with self.assertRaises(OSError):
            startup_tasks.launch_elevated(["--configure-system-startup=enable"])

    def test_a_helper_that_never_finishes_times_out(self):
        self.kernel.WaitForSingleObject.return_value = 0x102
        with self.assertRaises(TimeoutError):
            startup_tasks.launch_elevated(["--configure-system-startup=enable"], timeout_seconds=1)


if __name__ == "__main__":
    unittest.main()
