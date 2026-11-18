"""采集代理看门狗: 备份还原、活进程保护、父进程退出后的清场。

背景(2026-11-18): 采集期间系统代理指向 127.0.0.1:<端口>; 主程序被强杀后
没人还原, 浏览器就连在一个死端口上。本模块只依赖标准库, 测试用 HKCU 下的
临时注册表键, 绝不碰用户真实的系统代理。
"""
import json
import os
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

    def test_guard_main_exits_quietly_when_the_backup_is_already_gone(self):
        self.assertEqual(proxy_guard.main([str(self.backup), str(dead_pid()), REG]), 0)
        self.assertEqual(proxy_guard.read_proxy(REG)[1], "127.0.0.1:7890")

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


if __name__ == "__main__":
    unittest.main()
