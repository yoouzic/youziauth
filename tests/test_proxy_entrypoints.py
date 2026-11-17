"""采集代理相关命令行入口: 看门狗与一次性还原。

这两条路径必须在任何界面/单实例/加密组件初始化之前分派 —— 主程序崩溃后
看门狗是唯一还会执行的东西, 而 `--recover-proxy` 是用户/支持人员手动兜底。
"""
import os
import unittest
from unittest.mock import patch

import campus_auth_desktop
import campus_auth_gui


class ProxyGuardEntryTests(unittest.TestCase):
    def test_desktop_dispatches_the_guard_before_any_gui_setup(self):
        with patch("proxy_guard.main", return_value=0) as guard, \
                patch.object(campus_auth_desktop, "set_desktop_app_id") as setup:
            code = campus_auth_desktop.main(
                ["--proxy-guard", "backup.json", "4321", "Software\\Test"])
        self.assertEqual(code, 0)
        guard.assert_called_once_with(["backup.json", "4321", "Software\\Test"])
        setup.assert_not_called()

    def test_desktop_recover_proxy_restores_without_opening_the_ui(self):
        with patch("proxy_guard.recover_stale_backup", return_value=True) as recover, \
                patch.dict(os.environ, {"LOCALAPPDATA": "C:/tmp-local"}), \
                patch.object(campus_auth_desktop, "set_desktop_app_id") as setup:
            self.assertEqual(campus_auth_desktop.main(["--recover-proxy"]), 0)
        recover.assert_called_once()
        expected = ("C:/tmp-local" + "/youziauth/lptiyu/capture/proxy_backup.json")
        self.assertEqual(str(recover.call_args.args[0]).replace("\\", "/"), expected)
        setup.assert_not_called()

    def test_desktop_reports_when_there_was_nothing_to_recover(self):
        with patch("proxy_guard.recover_stale_backup", return_value=False):
            self.assertEqual(campus_auth_desktop.main(["--recover-proxy"]), 1)

    def test_frozen_gui_entry_dispatches_the_guard_before_the_instance_lock(self):
        with patch("proxy_guard.main", return_value=0) as guard, \
                patch.object(campus_auth_gui, "handle_single_instance_startup") as single, \
                patch.object(campus_auth_gui.sys, "argv",
                             ["youziauth.exe", "--proxy-guard", "backup.json", "7"]):
            self.assertEqual(campus_auth_gui.main(), 0)
        guard.assert_called_once_with(["backup.json", "7"])
        single.assert_not_called()


if __name__ == "__main__":
    unittest.main()