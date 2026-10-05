"""自动续期登录熔断的回归测试。

背景（真实事故）：2026-09-24 21:02–23:27，自动打卡在时段内每 5 分钟遇到一次
login_required，每次都走自动续期登录（拉起一个浏览器），连续 2.5 小时、成功率 0；
同期本机出现大量 Windows 登录失败事件并导致账户被锁定。

结论：自动行为必须有**硬上限**，不能只依赖"应该会成功"。
"""

import datetime as dt
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dorm_panel  # noqa: E402
from dorm_checkin import CheckinError, Result, Settings, Store  # noqa: E402

SHANGHAI = dt.timezone(dt.timedelta(hours=8))


class LoginRenewalBreaker(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name),
                           protector=Mock(protect=lambda b: b[::-1], unprotect=lambda b: b[::-1]))
        self.store.save_settings(Settings(enabled=True))
        # 破的是「令牌失效、会话 cookie 还在」这条真实路径：没有可恢复会话时，
        # 续期注定失败，控制器会先拦下来且不记账（见 DormController._renewal_blocker）。
        self.store.save_browser_session('stale-token', 'student', [dict(
            name='SSO', value='cookie', domain='idm.swu.edu.cn', path='/', expires=-1)])
        self.controller = dorm_panel.DormController(self.store)
        self.addCleanup(self.controller.close)
        self.controller.engine.clock = lambda: dt.datetime(2026, 9, 25, 21, 30, tzinfo=SHANGHAI)
        # 每次都返回 login_required，模拟会话始终失效
        self.controller.engine.tick = Mock(
            return_value=Result('login_required', '登录已失效，请重新登录'))
        self.controller.engine.run = Mock(
            return_value=Result('login_required', '登录已失效，请重新登录'))
        self.renewals = []

    def renew(self, action='automatic', failure='network_error'):
        """跑一次并绕开 300 秒节流；返回本次是否真的发起了自动续期登录。

        替身模拟**瞬时失败**（2026-09-24 那次连续 2.5 小时的 TLS 失败）：登录没成功，
        所以会话 cookie 还在，下一拍仍可再试 —— 这正是「每账号每天最多 3 次」要挡住的循环。
        `failure=None` 表示这次登录成功（会换上新的会话）。
        """
        self.controller._renew_at = 0.0

        def fake_login(store, api, cancel, *, interactive, idm_store=None):
            self.renewals.append(1)
            if failure:
                raise CheckinError(failure, '暂时失败')
            store.save_browser_session('renewed-token', 'student', [dict(
                name='SSO', value='cookie', domain='idm.swu.edu.cn', path='/', expires=-1)])

        with patch.object(dorm_panel, 'login', side_effect=fake_login):
            return self.controller._run(action)

    def test_renewal_is_capped_per_day(self):
        for _ in range(dorm_panel.MAX_DAILY_LOGIN_RENEWALS):
            self.renew()
        self.assertEqual(len(self.renewals), dorm_panel.MAX_DAILY_LOGIN_RENEWALS,
                         '上限之内应当尝试续期')
        result = self.renew()          # 第 4 次：必须熔断，不再拉起浏览器
        self.assertEqual(len(self.renewals), dorm_panel.MAX_DAILY_LOGIN_RENEWALS,
                         '超过上限后不得再自动续期登录')
        self.assertEqual(result.state, 'login_required')
        self.assertIn('已停止自动重试', result.message)

    def test_manual_login_is_never_capped(self):
        """人工点「登录 / 重新登录」不受熔断限制。"""
        for _ in range(dorm_panel.MAX_DAILY_LOGIN_RENEWALS + 2):
            self.renew('automatic')
        self.renew('login')
        self.assertEqual(len(self.renewals), dorm_panel.MAX_DAILY_LOGIN_RENEWALS + 1)

    def test_counter_resets_next_day(self):
        for _ in range(dorm_panel.MAX_DAILY_LOGIN_RENEWALS):
            self.renew()
        self.assertEqual(self.renew().state, 'login_required')
        self.controller.engine.clock = lambda: dt.datetime(2026, 9, 26, 21, 30, tzinfo=SHANGHAI)
        self.renew()
        self.assertEqual(len(self.renewals), dorm_panel.MAX_DAILY_LOGIN_RENEWALS + 1,
                         '第二天应重新获得完整的自动续期额度')

    def test_counter_is_persisted_not_just_in_memory(self):
        """计数必须落盘：重启程序不能把额度重置回满。"""
        for _ in range(dorm_panel.MAX_DAILY_LOGIN_RENEWALS):
            self.renew()
        self.assertEqual(self.store.daily_attempts('2026-09-25', 'login_renewal'),
                         dorm_panel.MAX_DAILY_LOGIN_RENEWALS)
        fresh = dorm_panel.DormController(self.store)
        self.addCleanup(fresh.close)
        fresh.engine.clock = lambda: dt.datetime(2026, 9, 25, 21, 40, tzinfo=SHANGHAI)
        fresh.engine.tick = Mock(return_value=Result('login_required', '登录已失效，请重新登录'))
        fresh._renew_at = 0.0
        calls = []
        with patch.object(dorm_panel, 'login', side_effect=lambda *a, **k: calls.append(1)):
            fresh._run('automatic')
        self.assertEqual(calls, [], '重启后仍应处于熔断状态')

    def test_successful_renewal_path_still_runs(self):
        """没到上限时行为不变：照常续期，并用换来的新会话重新判定。"""
        self.controller.engine.run = Mock(return_value=Result('signed', '服务器已确认今日任务完成'))
        result = self.renew(failure=None)
        self.assertEqual(len(self.renewals), 1)
        self.assertEqual(result.state, 'signed')
        self.assertNotEqual(self.store.token(), '')

    def test_unreadable_counter_file_does_not_break_login(self):
        (self.store.root / 'daily.json').write_text('{', encoding='utf-8')
        self.renew()
        self.assertEqual(len(self.renewals), 1, '计数文件坏了也要能正常尝试')


class AutoLoginOnlyInsideWindow(unittest.TestCase):
    """自动登录只在检查时段（默认 21:00–23:15）内进行。

    用户要求：每天晚上 21 点才开始尝试自动登录。
    时段外即使出现 login_required（例如白天人工点了一次「查询今日任务」），
    也不得偷偷拉起浏览器。
    """

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name),
                           protector=Mock(protect=lambda b: b[::-1], unprotect=lambda b: b[::-1]))
        self.store.save_settings(Settings(enabled=True, start='21:00', end='23:15'))
        self.store.save_browser_session('stale-token', 'student', [dict(
            name='SSO', value='cookie', domain='idm.swu.edu.cn', path='/', expires=-1)])
        self.controller = dorm_panel.DormController(self.store)
        self.addCleanup(self.controller.close)
        self.controller.engine.tick = Mock(
            return_value=Result('login_required', '登录已失效，请重新登录'))
        self.controller.engine.run = Mock(
            return_value=Result('login_required', '登录已失效，请重新登录'))
        self.renewals = []

    def renew_at(self, hour, minute=0, action='automatic'):
        self.controller.engine.clock = lambda: dt.datetime(
            2026, 9, 25, hour, minute, tzinfo=SHANGHAI)
        self.controller._renew_at = 0.0
        with patch.object(dorm_panel, 'login',
                          side_effect=lambda *a, **k: self.renewals.append(1)):
            return self.controller._run(action)

    def test_no_login_attempt_before_the_window(self):
        for hour, minute in ((0, 0), (9, 12), (20, 59)):
            result = self.renew_at(hour, minute)
            self.assertEqual(result.state, 'login_required')
            self.assertIn('不在自动打卡时段', result.message)
        self.assertEqual(self.renewals, [], '时段外不得自动登录')

    def test_no_login_attempt_after_the_window(self):
        result = self.renew_at(23, 15)
        self.assertIn('不在自动打卡时段', result.message)
        self.renew_at(23, 59)
        self.assertEqual(self.renewals, [], '时段结束后不得自动登录')

    def test_login_attempts_resume_at_2100(self):
        self.renew_at(20, 59)
        self.assertEqual(self.renewals, [])
        self.renew_at(21, 0)
        self.assertEqual(len(self.renewals), 1, '21:00 整点应当开始尝试')

    def test_manual_query_outside_the_window_does_not_open_a_browser(self):
        """白天人工点「查询今日任务」发现会话过期 —— 只提示，不开浏览器。"""
        result = self.renew_at(14, 0, action='query')
        self.assertEqual(self.renewals, [])
        self.assertIn('手动完成', result.message)

    def test_unreadable_settings_fail_closed(self):
        (self.store.root / 'settings.json').write_text('{', encoding='utf-8')
        self.renew_at(21, 30)
        self.assertEqual(self.renewals, [], '设置读不动时宁可不登录')

    def test_window_message_names_the_configured_span(self):
        result = self.renew_at(9, 0)
        self.assertIn('21:00–23:15', result.message)


if __name__ == '__main__':
    unittest.main()
