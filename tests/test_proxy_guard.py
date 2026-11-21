"""采集代理看门狗: 备份还原、活进程保护、父进程退出后的清场。

背景(2026-11-18): 采集期间系统代理指向 127.0.0.1:<端口>; 主程序被强杀后
没人还原, 浏览器就连在一个死端口上。本模块只依赖标准库, 测试用 HKCU 下的
临时注册表键, 绝不碰用户真实的系统代理。
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import proxy_guard

REG = r"Software\youziauth-proxy-guard-test"


def dead_pid() -> int:
    """拿一个已经退出的进程号, 模拟崩溃的采集进程。"""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=30)
    return process.pid


def live_pid(testcase) -> int:
    """拿一个还活着的无关进程号, 模拟另一个仍在采集的实例。"""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    testcase.addCleanup(process.wait)
    testcase.addCleanup(process.kill)
    return process.pid


class ProxyGuardTests(unittest.TestCase):
    def setUp(self):
        if os.name != "nt":
            self.skipTest("系统代理是 Windows 注册表")
        import winreg
        self.winreg = winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG) as key:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, "127.0.0.1:7890")
            winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, "<local>")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.backup = self.root / proxy_guard.BACKUP_NAME
        self.addCleanup(self._drop_key)

    def _drop_key(self):
        try:
            self.winreg.DeleteKey(self.winreg.HKEY_CURRENT_USER, REG)
        except OSError:
            pass

    def _write_backup(self, **extra):
        data = {"enable": 1, "server": "127.0.0.1:7890", "override": "<local>"}
        data.update(extra)
        self.backup.write_text(json.dumps(data), encoding="utf-8")

    def _point_at_dead_port(self):
        proxy_guard.write_proxy(1, "127.0.0.1:18888", "<local>", REG)

    # ------------------------------------------------------------- 备份还原
    def test_restore_backup_writes_the_saved_values_and_removes_the_file(self):
        self._write_backup(pid=os.getpid(), port=18888)
        self._point_at_dead_port()
        self.assertTrue(proxy_guard.restore_backup(self.backup, reg_path=REG))
        self.assertEqual(proxy_guard.read_proxy(REG)[1], "127.0.0.1:7890")
        self.assertEqual(proxy_guard.read_proxy(REG)[2], "<local>")
        self.assertFalse(self.backup.exists())

    def test_a_backup_owned_by_a_live_process_is_left_alone(self):
        self._write_backup(pid=live_pid(self), port=18888)
        self._point_at_dead_port()
        self.assertFalse(proxy_guard.recover_stale_backup(self.backup, REG),
                         "别的进程还在采集时不能抢它的备份")
        self.assertTrue(self.backup.exists())
        self.assertEqual(proxy_guard.read_proxy(REG)[1], "127.0.0.1:18888")

    def test_a_backup_whose_owner_died_is_restored(self):
        self._write_backup(pid=dead_pid(), port=18888)
        self._point_at_dead_port()
        self.assertTrue(proxy_guard.recover_stale_backup(self.backup, REG))
        self.assertEqual(proxy_guard.read_proxy(REG)[1], "127.0.0.1:7890")
        self.assertFalse(self.backup.exists())

    def test_stale_backup_preserves_a_newer_proxy_setting(self):
        self._write_backup(pid=dead_pid(), port=18888)
        proxy_guard.write_proxy(1, "new-proxy.example:8080", "<local>", REG)
        self.assertFalse(proxy_guard.recover_stale_backup(self.backup, REG))
        self.assertEqual(proxy_guard.read_proxy(REG)[1], "new-proxy.example:8080")
        self.assertFalse(self.backup.exists())

    def test_an_old_backup_without_a_pid_is_still_restored(self):
        self.backup.write_text(json.dumps(
            {"enable": 1, "server": "127.0.0.1:7890", "override": "<local>"}),
            encoding="utf-8")
        self._point_at_dead_port()
        self.assertTrue(proxy_guard.recover_stale_backup(self.backup, REG))
        self.assertEqual(proxy_guard.read_proxy(REG)[1], "127.0.0.1:7890")

    def test_a_corrupt_backup_never_touches_the_registry(self):
        self.backup.write_text("{not json", encoding="utf-8")
        self._point_at_dead_port()
        self.assertFalse(proxy_guard.recover_stale_backup(self.backup, REG))
        self.assertEqual(proxy_guard.read_proxy(REG)[1], "127.0.0.1:18888")

    def test_write_backup_is_atomic_enough_to_re_read(self):
        proxy_guard.write_backup(self.backup, {"enable": 0, "server": "", "override": ""})
        self.assertEqual(proxy_guard.read_backup(self.backup)["server"], "")
        self.assertFalse(self.backup.with_name(self.backup.name + ".tmp").exists())

    # ------------------------------------------------------------- 看门狗进程
    def test_guard_main_restores_once_its_parent_is_gone(self):
        parent = dead_pid()
        self._write_backup(pid=parent, port=18888)
        self._point_at_dead_port()
        self.assertEqual(proxy_guard.main([str(self.backup), str(parent), REG]), 0)
        self.assertEqual(proxy_guard.read_proxy(REG)[1], "127.0.0.1:7890")
        self.assertFalse(self.backup.exists())

    def test_guard_subprocess_acknowledges_ownership_before_parent_exits(self):
        with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]) as parent:
            try:
                capture_id = "current-capture"
                self._write_backup(pid=parent.pid, port=18888, capture_id=capture_id)
                guard = proxy_guard.spawn_guard(self.backup, parent.pid, REG, capture_id=capture_id)
                self.assertIsNotNone(guard)
                try:
                    self.assertTrue(proxy_guard.guard_ready(self.backup, capture_id, guard))
                    self._point_at_dead_port()
                    parent.terminate()
                    parent.wait(timeout=5)
                    self.assertEqual(guard.wait(timeout=10), 0)
                    self.assertEqual(proxy_guard.read_proxy(REG)[1], "127.0.0.1:7890")
                    self.assertFalse(self.backup.exists())
                finally:
                    if guard.poll() is None:
                        guard.kill()
                        guard.wait(timeout=5)
            finally:
                if parent.poll() is None:
                    parent.terminate()

    def test_guard_main_exits_quietly_when_the_backup_is_already_gone(self):
        self.assertEqual(proxy_guard.main([str(self.backup), str(dead_pid()), REG]), 0)
        self.assertEqual(proxy_guard.read_proxy(REG)[1], "127.0.0.1:7890")

    def test_guard_retries_a_release_request_while_parent_is_alive(self):
        owner = os.getpid()
        capture_id = "sample-capture"
        self._write_backup(pid=owner, port=18888, capture_id=capture_id, release_requested=True)
        self._point_at_dead_port()
        write = proxy_guard.write_proxy
        failures = [PermissionError("temporary registry lock")]

        def restore(*args):
            if failures:
                raise failures.pop()
            write(*args)

        with (
            patch.object(proxy_guard, "pid_alive", side_effect=AssertionError("owner exit not required")),
            patch.object(proxy_guard, "write_proxy", side_effect=restore) as attempt,
            patch.object(proxy_guard.time, "sleep"),
        ):
            self.assertEqual(proxy_guard.main([str(self.backup), str(owner), REG, capture_id]), 0)
        self.assertEqual(attempt.call_count, 2)
        self.assertEqual(proxy_guard.read_proxy(REG)[1], "127.0.0.1:7890")
        self.assertFalse(self.backup.exists())

    def test_guard_recovers_a_dead_listener_even_if_the_application_is_still_alive(self):
        owner = os.getpid()
        capture_id = "manual-capture"
        self._write_backup(pid=owner, port=18888, capture_id=capture_id, monitor_listener=True,
                           expires_at=proxy_guard.time.time() + 60)
        self._point_at_dead_port()
        with (
            patch.object(proxy_guard, "_loopback_reachable", return_value=False) as probe,
            patch.object(proxy_guard.time, "sleep", side_effect=[None, None, AssertionError("guard did not recover")]),
        ):
            self.assertEqual(proxy_guard.main([str(self.backup), str(owner), REG, capture_id]), 0)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(proxy_guard.read_proxy(REG)[1], "127.0.0.1:7890")

    def test_guard_recovers_when_the_session_deadline_passes_with_a_live_owner(self):
        owner = os.getpid()
        capture_id = "manual-capture"
        self._write_backup(pid=owner, port=18888, capture_id=capture_id, monitor_listener=True,
                           expires_at=proxy_guard.time.time() - 1)
        self._point_at_dead_port()
        with patch.object(proxy_guard.time, "sleep", side_effect=AssertionError("guard ignored deadline")):
            self.assertEqual(proxy_guard.main([str(self.backup), str(owner), REG, capture_id]), 0)
        self.assertEqual(proxy_guard.read_proxy(REG)[1], "127.0.0.1:7890")

    def test_proxy_server_write_failure_does_not_enable_a_disabled_proxy(self):
        calls = []

        def record_write(_key, name, *_values):
            calls.append(name)
            if name == "ProxyServer":
                raise PermissionError("could not set new proxy server")

        with patch.object(self.winreg, "SetValueEx", side_effect=record_write):
            with self.assertRaises(PermissionError):
                proxy_guard.write_proxy(1, "127.0.0.1:18888", "<local>", REG)
        self.assertEqual(calls, ["ProxyServer"])

    def test_guard_command_identifies_the_backup_and_parent(self):
        command = proxy_guard.guard_command(self.backup, 4321, REG)
        index = command.index("--proxy-guard")
        self.assertEqual(command[index + 1], str(self.backup))
        self.assertEqual(command[index + 2], "4321")
        self.assertEqual(command[index + 3], REG)

    def test_spawn_guard_detaches_the_watchdog(self):
        with patch.object(proxy_guard.subprocess, "Popen") as popen:
            guard = proxy_guard.spawn_guard(self.backup, 4321, REG)
        self.assertIs(guard, popen.return_value)
        arguments, keywords = popen.call_args
        self.assertIn("--proxy-guard", arguments[0])
        self.assertTrue(keywords["creationflags"] & 0x00000008,
                        "看门狗必须脱离父进程, 否则被一起结束就失去意义")

    def test_frozen_guard_uses_its_own_pyinstaller_runtime(self):
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.dict(os.environ, {
                "PYINSTALLER_RESET_ENVIRONMENT": "0",
                "YOUZIAUTH_TEST_ENV": "preserved",
            }),
            patch.object(proxy_guard.subprocess, "Popen") as popen,
        ):
            proxy_guard.spawn_guard(self.backup, 4321, REG)
            child_env = popen.call_args.kwargs.get("env", {})
            self.assertEqual(child_env.get("PYINSTALLER_RESET_ENVIRONMENT"), "1")
            self.assertEqual(child_env.get("YOUZIAUTH_TEST_ENV"), "preserved")
            self.assertEqual(os.environ["PYINSTALLER_RESET_ENVIRONMENT"], "0")


class ManualRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.backup = Path(temporary) / proxy_guard.BACKUP_NAME
        self.local = self.enterContext(socket.socket())
        self.local.bind(('127.0.0.1', 0))
        self.port = self.local.getsockname()[1]
        self.current = (1, f'127.0.0.1:{self.port}', '<local>;my.example')
        self.read = self.enterContext(patch.object(proxy_guard, 'read_proxy', return_value=self.current))
        self.write = self.enterContext(patch.object(proxy_guard, 'write_proxy'))
        self.owner_alive = self.enterContext(patch.object(proxy_guard, 'pid_alive', return_value=False))
        self.connect_real = socket.create_connection
        self.connect = self.enterContext(patch.object(socket, 'create_connection',
                                                       side_effect=ConnectionRefusedError))

    def save_backup(self, **changes):
        data = {'enable': 1, 'server': 'original.example:3128', 'override': '<local>',
                'pid': 12345, 'port': self.port, 'capture_id': 'owned', 'guard_ready': True}
        data.update(changes)
        self.backup.write_text(json.dumps(data), encoding='utf-8')

    def recover(self):
        return proxy_guard.restore_owned_capture_proxy(self.backup, REG)

    def test_matching_dead_capture_restores_only_the_owned_proxy_settings(self):
        self.save_backup()
        self.assertEqual(self.recover(), 'restored')
        self.write.assert_called_once_with(1, 'original.example:3128', reg_path=REG)
        self.assertFalse(self.backup.exists())

    def test_plain_lab_backup_does_not_identify_a_capture(self):
        self.backup.write_text('{"enable": 0, "server": "127.0.0.1:7897"}', encoding='utf-8')
        self.assertEqual(self.recover(), 'unverified')
        self.write.assert_not_called()
        self.assertTrue(self.backup.exists())

    def test_a_lab_mirror_does_not_prove_which_route_was_owned(self):
        self.save_backup(created=1700000000)
        self.assertEqual(self.recover(), 'unverified')
        self.write.assert_not_called()

    def test_unverified_backup_cannot_restore_a_dead_loopback_alias(self):
        self.save_backup(server=f'https=localhost:{self.port}')
        self.assertEqual(self.recover(), 'unverified')
        self.write.assert_not_called()

    def test_invalid_original_values_cannot_be_recovered(self):
        for changes in ({'enable': None}, {'server': ''}, {'server': []}):
            with self.subTest(changes=changes):
                self.save_backup(**changes)
                self.assertEqual(self.recover(), 'unverified')
        self.write.assert_not_called()

    def test_a_live_owner_is_left_alone(self):
        self.save_backup()
        self.owner_alive.return_value = True
        self.assertEqual(self.recover(), 'unchanged')
        self.write.assert_not_called()

    def test_a_reachable_loopback_listener_is_left_alone(self):
        self.save_backup()
        self.local.listen(1)
        self.connect.side_effect = self.connect_real
        self.assertEqual(self.recover(), 'unchanged')
        self.write.assert_not_called()

    def test_a_loopback_timeout_is_unverified(self):
        self.save_backup()
        self.connect.side_effect = TimeoutError
        self.assertEqual(self.recover(), 'unverified')
        self.write.assert_not_called()

    def test_a_saved_enabled_loopback_that_is_also_down_is_unverified(self):
        with socket.socket() as previous:
            previous.bind(('127.0.0.1', 0))
            self.save_backup(server=f'127.0.0.1:{previous.getsockname()[1]}')
            self.assertEqual(self.recover(), 'unverified')
        self.write.assert_not_called()
        self.assertTrue(self.backup.exists())

    def test_inactive_or_remote_proxies_are_left_alone(self):
        self.save_backup()
        for proxy in ((0, self.current[1], ''), (1, 'work.example:8080', '')):
            with self.subTest(proxy=proxy):
                self.read.return_value = proxy
                self.assertEqual(self.recover(), 'unchanged')
        self.write.assert_not_called()

    def test_a_proxy_that_changed_during_recovery_is_left_alone(self):
        self.save_backup()
        self.read.side_effect = [self.current, (1, 'work.example:8080', '')]
        self.assertEqual(self.recover(), 'unchanged')
        self.write.assert_not_called()
        self.assertTrue(self.backup.exists())

    def test_failed_write_keeps_the_backup(self):
        self.save_backup()
        self.write.side_effect = PermissionError('mock write error')
        with self.assertRaises(PermissionError):
            self.recover()
        self.assertTrue(self.backup.exists())


if __name__ == "__main__":
    unittest.main()
