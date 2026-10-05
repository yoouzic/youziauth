import datetime as dt
import importlib.util
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dorm_checkin import CheckinError, Settings, Store, Result, Task, SHANGHAI
import windows_tray

PRESENT = importlib.util.find_spec('dorm_panel') is not None
if PRESENT:
    from dorm_login import LoginGate
    from dorm_panel import DormController


class FeatureExists(unittest.TestCase):
    def test_panel_exists(self):
        self.assertTrue(PRESENT, 'Dormitory UI controller missing')

    def test_tray_has_dorm_entry(self):
        self.assertIn('dorm', [x.command for x in windows_tray.build_tray_menu_items(windows_tray.TrayStatus.ONLINE)])

    def test_packaged_smoke_entry_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('dorm_selftest'), 'Packaged smoke check missing')


@unittest.skipUnless(PRESENT, 'Controller missing')
class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.engine = Mock()
        self.engine.cancel = __import__('threading').Event()
        self.engine.run.return_value = Result('ready', '待完成')
        self.controller = DormController(self.store, engine=self.engine)
        self.addCleanup(self.controller.close)

    def wait(self):
        for _ in range(100):
            if not self.controller.busy:
                return
            time.sleep(.01)
        self.fail('Worker did not stop')

    def test_query_dispatch_is_read_only(self):
        self.controller.start('query')
        self.wait()
        self.engine.run.assert_called_once_with(submit=False)
        self.assertEqual(self.controller.drain()[-1].state, 'ready')

    def test_submit_is_explicit_action(self):
        self.controller.start('submit')
        self.wait()
        self.engine.run.assert_called_once_with(submit=True)

    def test_disable_does_not_schedule_requests(self):
        self.controller.poll()
        self.wait()
        self.engine.tick.assert_not_called()

    def test_exit_prevents_further_jobs(self):
        self.controller.close()
        self.assertFalse(self.controller.start('query'))
        self.engine.run.assert_not_called()

    def test_only_one_worker_and_cancel_does_not_touch_network_agent(self):
        gate = __import__('threading').Event()
        self.engine.run.side_effect = lambda **kw: (gate.wait(1), Result('ready', '待完成'))[1]
        self.controller.start('query')
        self.assertFalse(self.controller.start('submit'))
        self.controller.cancel()
        self.assertTrue(self.engine.cancel.is_set())
        gate.set()
        self.wait()

    def test_logout_preserves_global_location_source(self):
        self.assertIn('location_source', Settings.__dataclass_fields__)
        self.store.save_settings(Settings(enabled=True, location_source='simulation'))
        self.controller.start('logout')
        self.wait()
        settings = self.store.settings()
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.location_source, 'simulation')

    def test_legacy_settings_save_preserves_global_location_source(self):
        from dorm_panel import DormPanel
        self.assertIn('location_source', Settings.__dataclass_fields__)
        self.store.save_settings(Settings(location_source='simulation'))
        panel = DormPanel.__new__(DormPanel)
        panel.controller = self.controller
        panel.enabled = Mock(get=lambda: False)
        panel.start = Mock(get=lambda: '20:00')
        panel.end = Mock(get=lambda: '23:00')
        panel.interval = Mock(get=lambda: '600')
        panel.state, panel.refresh = Mock(), Mock()
        panel.save()
        self.assertEqual(self.store.settings().location_source, 'simulation')
        self.assertEqual(self.store.settings().interval, 600)

    def test_legacy_refresh_displays_the_current_location_source(self):
        from dorm_panel import DormPanel
        panel = DormPanel.__new__(DormPanel)
        panel.controller = self.controller
        panel.schedule, panel.location_text = Mock(), Mock()
        panel.buttons = []
        panel._history_text = '暂无打卡记录'
        self.store.save_settings(Settings(location_source='simulation'))
        panel.refresh()
        value = panel.location_text.set.call_args.args[0] if panel.location_text.set.called else ''
        self.assertIn('模拟', value)
        self.assertIn('非实时', value)
        self.store.save_settings(Settings(location_source='windows'))
        panel.refresh()
        self.assertIn('Windows', panel.location_text.set.call_args.args[0])
        self.assertNotIn('模拟', panel.location_text.set.call_args.args[0])

    def test_legacy_valid_save_recovers_corrupt_settings(self):
        from dorm_panel import DormPanel
        path = self.store.root / 'settings.json'
        path.write_text('{', encoding='utf-8')
        panel = DormPanel.__new__(DormPanel)
        panel.controller, panel.window = self.controller, None
        panel.enabled = Mock(get=lambda: False)
        panel.start, panel.end = Mock(get=lambda: '21:00'), Mock(get=lambda: '23:30')
        panel.interval = Mock(get=lambda: '300')
        panel.state, panel.refresh = Mock(), Mock()
        with patch('tkinter.messagebox.showerror'):
            panel.save()
        self.assertNotEqual(path.read_text(encoding='utf-8'), '{')
        self.assertEqual(self.store.settings().location_source, 'windows')


class GlobalLocationTests(unittest.TestCase):
    def setUp(self):
        self.assertIn('location_source', Settings.__dataclass_fields__)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name), protector=Mock(protect=lambda b: b[::-1], unprotect=lambda b: b[::-1]))
        self.store.save_token('offline-fixture-token')
        self.sample = dict(latitude=39.908823, longitude=116.397470, accuracy=141,
                           timestamp=1700000000, source='WI_FI')
        (self.store.root / 'location-sample.json').write_text(json.dumps(self.sample), encoding='utf-8')
        self.controller = DormController(self.store)
        self.addCleanup(self.controller.close)
        self.api = Mock()
        self.api.user.return_value = 'fixture-student'
        self.api.today.return_value = Task('fixture-task', 'fixture-form', 'fixture-publish', 'fixture-student',
                                          '2026-09-21', '测试查寝', '21:00', '23:30', False, '测试寝室', '800米')
        self.api.is_signed.return_value = True
        self.controller.engine.api = self.api
        self.controller.engine.clock = lambda: dt.datetime(2026, 9, 21, 22, tzinfo=SHANGHAI)
        guard = patch('socket.socket.connect', side_effect=AssertionError('Unexpected network access'))
        guard.start()
        self.addCleanup(guard.stop)

    def test_saved_source_applies_to_manual_and_automatic_operations(self):
        raw = dict(latitude=51.5, longitude=-0.1, accuracy=50, timestamp=time.time(), source='WI_FI')
        with patch('dorm_location.read_position', return_value=raw):
            self.assertEqual(self.controller.engine.run(submit=True).state, 'signed')
        self.assertEqual(self.api.submit.call_args.args[2]['provider'], 'windows')
        # 新规则：当天一旦确认完成，自动检查当天就不再判定（见 Engine._already_signed_today）。
        # 本用例要验证的是「定位来源同样作用于自动检查」，所以先清掉当天已完成记录，
        # 否则 tick() 会正确地短路掉，测不到自动分支。
        (self.store.root / 'signed.json').unlink(missing_ok=True)
        self.controller.save(Settings(enabled=True, location_source='simulation'))
        with patch('dorm_location.read_position', side_effect=AssertionError('Simulation must not locate')):
            self.assertEqual(self.controller.engine.tick().state, 'signed')
        position = self.api.submit.call_args.args[2]
        self.assertEqual(position['provider'], 'windows')  # same wire label as a live fix
        self.assertNotIn('simulation', position.values())
        self.assertAlmostEqual(position['latitude'], 39.910226, delta=0.0004)   # ≤25 m drift
        self.assertAlmostEqual(position['longitude'], 116.403714, delta=0.0004)
        self.assertAlmostEqual(position['accuracy'], 141, delta=141 * 0.15)
        self.assertEqual(self.api.verify.call_args.args[2], position)

    def test_simulation_still_requires_school_range_approval(self):
        self.controller.save(Settings(location_source='simulation'))
        self.api.verify.side_effect = CheckinError('location_required', '范围未通过')
        with patch('dorm_location.read_position', side_effect=AssertionError('Simulation must not locate')):
            result = self.controller.engine.run(submit=True)
        self.assertEqual(result.state, 'location_required')
        self.api.submit.assert_not_called()

    def test_missing_sample_stops_without_falling_back_to_windows(self):
        self.controller.save(Settings(location_source='simulation'))
        (self.store.root / 'location-sample.json').unlink()
        with patch('dorm_location.read_position', side_effect=AssertionError('No silent fallback')):
            result = self.controller.engine.run(submit=True)
        self.assertEqual(result.state, 'location_required')
        self.api.verify.assert_not_called()
        self.api.submit.assert_not_called()

    def test_saving_while_busy_does_not_change_source(self):
        self.controller.busy = True
        with self.assertRaises(RuntimeError):
            self.controller.save(Settings(location_source='simulation'))
        self.assertEqual(self.store.settings().location_source, 'windows')

    def test_bad_simulation_quality_points_to_the_sample_not_windows(self):
        self.controller.save(Settings(location_source='simulation'))
        for change in ({'accuracy': 600}, {'latitude': float('nan')}, {'source': 'DEFAULT'}):
            with self.subTest(change=change):
                (self.store.root / 'location-sample.json').write_text(json.dumps(dict(self.sample, **change)), encoding='utf-8')
                result = self.controller.engine.run(submit=True)
                self.assertEqual(result.state, 'location_required')
                self.assertIn('模拟', result.message)
                self.assertNotIn('打开 Wi-Fi', result.message)
                self.api.verify.assert_not_called()
                self.api.submit.assert_not_called()


@unittest.skipUnless(PRESENT, 'Controller missing')
class SchedulingConstraintTests(unittest.TestCase):
    """多账号才需要、但每条都直接影响自动行为安全性的三道约束：错峰、登录闸、整机预算。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name),
                           protector=Mock(protect=lambda b: b[::-1], unprotect=lambda b: b[::-1]))
        # 有可恢复的会话，才轮得到预算与登录闸这些后面的约束（见 _renewal_blocker）。
        self.store.save_browser_session('stale-token', 'student', [dict(
            name='SSO', value='cookie', domain='idm.swu.edu.cn', path='/', expires=-1)])
        self.engine = Mock()
        self.engine.cancel = threading.Event()
        self.engine.clock = lambda: dt.datetime(2026, 10, 4, 22, 0, tzinfo=SHANGHAI)
        self.engine.tick.return_value = Result('ready', '待完成')

    def build(self, **kwargs):
        kwargs.setdefault('login_gate', LoginGate())
        controller = DormController(self.store, engine=self.engine, **kwargs)
        self.addCleanup(controller.close)
        return controller

    def wait(self, controller):
        for _ in range(200):
            if not controller.busy:
                return
            time.sleep(.01)
        self.fail('Worker did not stop')

    def at(self, hour, minute, second=0):
        self.engine.clock = lambda: dt.datetime(2026, 10, 4, hour, minute, second, tzinfo=SHANGHAI)

    # ---- 错峰 -----------------------------------------------------------------
    def test_the_first_automatic_check_waits_for_this_accounts_offset(self):
        self.store.save_settings(Settings(enabled=True, start='21:00', end='23:15'))
        controller = self.build(stagger_seconds=90)

        self.at(21, 0, 30)                     # 窗口已开，但还没轮到这个账号
        controller.poll()
        self.wait(controller)
        self.engine.tick.assert_not_called()

        self.at(21, 1, 30)                     # 窗口开始 + 90 秒
        controller._poll_at = 0.0
        controller.poll()
        self.wait(controller)
        self.engine.tick.assert_called_once_with()

    def test_no_offset_and_a_broken_window_never_block_a_check(self):
        self.assertTrue(self.build()._stagger_ready(Settings()))
        controller = self.build(stagger_seconds=90)
        # 时段读不出来时该由 Engine fail-closed，这里不替它做决定。
        self.assertTrue(controller._stagger_ready(Mock(start='25:00')))

    # ---- 机器级登录闸 ---------------------------------------------------------
    def test_automatic_renewal_skips_while_another_account_is_logging_in(self):
        self.store.save_settings(Settings(enabled=True, start='21:00', end='23:15'))
        gate = LoginGate()
        gate.acquire()                          # 另一个账号正拿着闸
        try:
            controller = self.build(login_gate=gate)
            self.engine.tick.return_value = Result('login_required', '请先登录统一身份认证')
            result = controller._run('automatic')
        finally:
            gate.release()
        self.assertEqual(result.state, 'waiting')
        self.assertIn('等待其他账号', result.message)
        # 关键：被挡下的这一拍不记账，否则额度会被排队白白烧光。
        self.assertEqual(self.store.daily_attempts('2026-10-04', 'login_renewal'), 0)

    def test_a_manual_login_is_refused_with_a_clear_reason(self):
        gate = LoginGate()
        gate.acquire()
        try:
            controller = self.build(login_gate=gate)
            controller.start('login')
            self.wait(controller)
        finally:
            gate.release()
        result = controller.drain()[-1]
        self.assertEqual(result.state, 'busy')
        self.assertIn('另一个账号正在登录', result.message)

    def test_the_gate_is_released_even_when_the_login_fails(self):
        gate = LoginGate()
        controller = self.build(login_gate=gate)
        with patch('dorm_panel.login', side_effect=CheckinError('error', '登录失败')):
            controller.start('login')
            self.wait(controller)
        self.assertFalse(gate.held(), '登录失败也必须把闸放回去，否则后面所有账号都登不了')
        self.assertEqual(controller.drain()[-1].state, 'error')

    # ---- 注定失败的那一趟不许烧额度 -------------------------------------------
    def test_an_account_with_no_session_and_no_credentials_is_told_so_without_burning_an_attempt(self):
        """2026-10-05 实测的坑：没登录过、也没存凭据的账号空转两次，吃掉自己 3 次额度里的
        2 次和整机预算 6 次里的 2 次，界面只有一句含糊的「没有可恢复的学校会话」。"""
        self.store.save_settings(Settings(enabled=True, start='21:00', end='23:15'))
        self.store.clear_browser_session()
        self.store.clear_token()
        budget = Mock()
        budget.check.return_value = ''
        controller = self.build(login_budget=budget,
                                idm_store=Mock(exists=Mock(return_value=False)))
        self.engine.tick.return_value = Result('login_required', '请先登录统一身份认证')
        with patch('dorm_panel.login') as login:
            automatic = controller._run('automatic')
        self.assertEqual(automatic.state, 'login_required')
        self.assertIn('还没有登录过', automatic.message)
        self.assertIn('统一认证凭据', automatic.message)
        self.assertIn('学校登录 / 重新登录', automatic.message)
        login.assert_not_called()
        budget.spend.assert_not_called()
        self.assertEqual(self.store.daily_attempts('2026-10-04', 'login_renewal'), 0)

    def test_an_expired_session_without_credentials_says_both(self):
        self.store.save_settings(Settings(enabled=True, start='21:00', end='23:15'))
        self.store.save_browser_session('stale-token', 'student', [dict(
            name='SSO', value='dead', domain='idm.swu.edu.cn', path='/', expires=1)])
        controller = self.build(idm_store=Mock(exists=Mock(return_value=False)))
        self.engine.tick.return_value = Result('login_required', '请先登录统一身份认证')
        result = controller._run('automatic')
        self.assertIn('学校会话已过期', result.message)
        self.assertIn('统一认证凭据', result.message)
        self.assertEqual(self.store.daily_attempts('2026-10-04', 'login_renewal'), 0)

    def test_saved_credentials_allow_an_unattended_first_login(self):
        """用户选择：没有会话、但存了统一认证凭据时，允许 headless 自己登一次
        （仍然只在时段内、受登录闸与每天上限约束）。"""
        self.store.save_settings(Settings(enabled=True, start='21:00', end='23:15'))
        self.store.clear_browser_session()
        self.store.clear_token()
        budget = Mock()
        budget.check.return_value = ''
        controller = self.build(login_budget=budget,
                                idm_store=Mock(exists=Mock(return_value=True),
                                               load=Mock(return_value=Mock())))
        self.assertEqual(controller._unattended_login_blocker(), '')
        self.engine.tick.return_value = Result('login_required', '请先登录统一身份认证')
        with patch('dorm_panel.login') as login:
            controller._run('automatic')
        login.assert_called_once()
        self.assertEqual(login.call_args.kwargs.get('interactive'), False)
        budget.spend.assert_called_once_with('2026-10-04')
        self.assertEqual(self.store.daily_attempts('2026-10-04', 'login_renewal'), 1)

    def test_unreadable_credentials_fail_closed(self):
        from idm_credentials import CredentialError
        self.store.save_settings(Settings(enabled=True, start='21:00', end='23:15'))
        self.store.clear_browser_session()
        self.store.clear_token()
        controller = self.build(idm_store=Mock(exists=Mock(return_value=True),
                                               load=Mock(side_effect=CredentialError('坏了'))))
        self.assertTrue(controller._unattended_login_blocker())
        self.engine.tick.return_value = Result('login_required', '请先登录统一身份认证')
        with patch('dorm_panel.login') as login:
            controller._run('automatic')
        login.assert_not_called()

    def test_a_skipped_login_is_recorded_so_the_reason_survives_on_disk(self):
        """2026-10-05 实测：界面说「已停止自动重试」，磁盘上却只有引擎那句
        「请先登录统一身份认证」，事后看不出到底为什么停。"""
        self.store.save_settings(Settings(enabled=True, start='21:00', end='23:15'))
        self.store.clear_browser_session()
        self.store.clear_token()
        controller = self.build(idm_store=Mock(exists=Mock(return_value=False)))
        self.engine.tick.return_value = Result('login_required', '请先登录统一身份认证')
        controller._run('automatic')
        history = (self.store.root / 'history.log').read_text(encoding='utf-8')
        self.assertIn('还没有登录过', history)
        status = (self.store.root / 'status.json').read_text(encoding='utf-8')
        self.assertIn('还没有登录过', status)

    def test_a_healthy_session_is_not_blocked(self):
        self.store.save_settings(Settings(enabled=True, start='21:00', end='23:15'))
        controller = self.build()
        self.assertEqual(controller._unattended_login_blocker(), '')
        self.engine.tick.return_value = Result('login_required', '请先登录统一身份认证')
        with patch('dorm_panel.login'):
            controller._run('automatic')
        self.assertEqual(self.store.daily_attempts('2026-10-04', 'login_renewal'), 1)

    # ---- 整机预算 -------------------------------------------------------------
    def test_the_machine_budget_blocks_automatic_logins_but_not_manual_ones(self):
        self.store.save_settings(Settings(enabled=True, start='21:00', end='23:15'))
        budget = Mock()
        budget.check.return_value = '本机今天已自动尝试登录 6 次（上限 6 次），已停止自动重试'
        controller = self.build(login_budget=budget)
        self.engine.tick.return_value = Result('login_required', '请先登录统一身份认证')

        automatic = controller._run('automatic')
        self.assertEqual(automatic.state, 'login_required')
        self.assertIn('上限 6 次', automatic.message)
        budget.spend.assert_not_called()

        self.engine.run.return_value = Result('login_required', '请先登录统一身份认证')
        controller._renew_at = 0.0              # 自动那一拍留下的续期冷却不影响人工操作
        with patch('dorm_panel.login') as manual:
            controller._run('submit')
        manual.assert_called_once()
        budget.spend.assert_not_called()        # 人工登录不受整机预算限制

    def test_each_automatic_attempt_is_counted_against_the_machine_budget(self):
        self.store.save_settings(Settings(enabled=True, start='21:00', end='23:15'))
        budget = Mock()
        budget.check.return_value = ''
        controller = self.build(login_budget=budget)
        self.engine.tick.return_value = Result('login_required', '请先登录统一身份认证')
        with patch('dorm_panel.login'):
            controller._run('automatic')
        budget.spend.assert_called_once_with('2026-10-04')


class RenewalTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name), protector=Mock(protect=lambda b: b[::-1], unprotect=lambda b: b[::-1]))
        # 真实前提：令牌还在、但已被学校判为失效，**会话 cookie 仍然可用**——
        # 无人值守续期正是靠这批 cookie 接着走 SSO。只有令牌没有会话的状态下，
        # login(interactive=False) 在真实实现里会立刻拒绝（见 _renewal_blocker）。
        self.store.save_browser_session('old-token', 'student', [dict(
            name='SSO', value='old-cookie', domain='idm.swu.edu.cn', path='/', expires=-1)])
        self.controller = DormController(self.store)
        self.addCleanup(self.controller.close)
        self.api = Mock()
        self.api.user.side_effect = self.identify
        self.task = Task('task', 'form', 'publish', 'student', '2026-09-21', '测试查寝',
                         '21:00', '23:30', False, '宿舍', '800米')
        self.api.today.return_value = self.task
        self.api.submit.return_value = True
        self.controller.api = self.controller.engine.api = self.api
        self.controller.engine.clock = lambda: dt.datetime(2026, 9, 21, 22, tzinfo=SHANGHAI)
        self.controller.engine.location = lambda: dict(latitude=29.8, longitude=106.4, accuracy=50)
        self.renewals = 0
        renewal = patch('dorm_panel.login', side_effect=self.renew)
        self.login = renewal.start()
        self.addCleanup(renewal.stop)
        guard = patch('socket.socket.connect', side_effect=AssertionError('Unexpected network access'))
        guard.start()
        self.addCleanup(guard.stop)

    def identify(self, token):
        if token == 'old-token':
            raise CheckinError('login_required', '登录已失效')
        return 'student'

    def renew(self, store, api, cancel, *, interactive, idm_store=None):
        # 多账号：控制器会把「本账号自己的」统一认证凭据存储一并传下去（见 dorm_panel）。
        self.assertIs(idm_store, self.controller.idm_store)
        self.assertFalse(interactive)
        self.renewals += 1
        store.save_browser_session('renewed-token', 'student', [dict(
            name='SSO', value='renewed-cookie', domain='idm.swu.edu.cn', path='/', expires=-1)])
        return 'student'

    def run_action(self, action):
        self.assertTrue(self.controller.start(action))
        deadline = time.monotonic() + 5
        while self.controller.busy and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertFalse(self.controller.busy)
        events = self.controller.drain()
        return events[-1] if events else None

    def test_query_renews_expired_token_without_submitting(self):
        self.assertEqual(self.run_action('query').state, 'ready')
        self.assertEqual(self.store.token(), 'renewed-token')
        self.assertEqual(self.renewals, 1)
        self.api.submit.assert_not_called()

    def test_submission_renews_before_fetching_and_submits_once(self):
        self.assertEqual(self.run_action('submit').state, 'signed')
        self.assertEqual(self.renewals, 1)
        self.assertEqual(self.api.submit.call_count, 1)
        self.assertEqual(self.api.submit.call_args.args[0], 'renewed-token')

    def test_automatic_renewal_does_not_get_skipped_by_tick_throttle(self):
        self.store.save_settings(Settings(enabled=True))
        self.assertEqual(self.run_action('automatic').state, 'signed')
        self.assertEqual(self.renewals, 1)
        self.assertEqual(self.api.submit.call_count, 1)

    def test_valid_token_never_launches_renewal(self):
        self.store.save_token('valid-token')
        self.assertEqual(self.run_action('submit').state, 'signed')
        self.assertEqual(self.renewals, 0)

    def test_renewal_failure_is_cooled_down(self):
        def fail(*args, **kwargs):
            self.renewals += 1
            raise CheckinError('network_error', '暂时离线')
        self.login.side_effect = fail
        self.assertEqual(self.run_action('query').state, 'network_error')
        self.assertEqual(self.run_action('query').state, 'login_required')
        self.assertEqual(self.renewals, 1)
        self.assertEqual(self.store.token(), 'old-token')

    def test_rejected_new_token_does_not_create_a_renewal_loop(self):
        self.api.user.side_effect = CheckinError('login_required', '仍需验证')
        self.assertEqual(self.run_action('submit').state, 'login_required')
        self.assertEqual(self.renewals, 1)
        self.assertIsNone(self.store.browser_session('renewed-token'))
        self.api.submit.assert_not_called()

    def test_cancellation_during_renewal_prevents_submission(self):
        def cancel(store, api, event, **kwargs):
            self.renewals += 1
            store.save_token('renewed-token')
            event.set()
            return 'student'
        self.login.side_effect = cancel
        self.assertEqual(self.run_action('submit').state, 'cancelled')
        self.api.submit.assert_not_called()

    def test_disabled_schedule_never_renews(self):
        self.run_action('automatic')
        self.assertEqual(self.renewals, 0)
        self.api.user.assert_not_called()

    def test_renewal_after_unknown_submission_only_reads_back(self):
        self.store.save_browser_session('valid-token', 'student', [dict(
            name='SSO', value='cookie', domain='idm.swu.edu.cn', path='/', expires=-1)])
        self.api.submit.side_effect = TimeoutError()
        self.api.is_signed.side_effect = [CheckinError('login_required', '登录已失效'), False]
        self.assertEqual(self.run_action('submit').state, 'ready')
        self.assertEqual(self.renewals, 1)
        self.assertEqual(self.api.submit.call_count, 1)
        self.assertFalse(self.store.pending(self.task.key))

    def test_renewal_does_not_resubmit_a_rejected_write_in_same_action(self):
        self.store.save_browser_session('valid-token', 'student', [dict(
            name='SSO', value='cookie', domain='idm.swu.edu.cn', path='/', expires=-1)])
        self.api.submit.side_effect = CheckinError('login_required', '登录已失效')
        self.api.is_signed.return_value = False
        self.assertEqual(self.run_action('submit').state, 'ready')
        self.assertEqual(self.renewals, 1)
        self.assertEqual(self.api.submit.call_count, 1)

    def test_logout_clears_all_session_material_and_disables_automatic(self):
        self.store.save_settings(Settings(enabled=True))
        self.store.save_browser_session('old-token', 'student', [dict(name='SSO', value='cookie')])
        self.assertEqual(self.run_action('logout').state, 'login_required')
        self.assertEqual(self.store.token(), '')
        self.assertIsNone(self.store.browser_session('old-token'))
        self.assertFalse(self.store.settings().enabled)
        self.assertEqual(self.renewals, 0)


if __name__ == '__main__':
    unittest.main()
