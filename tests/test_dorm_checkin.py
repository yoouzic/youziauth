import datetime as dt
import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

PRESENT = importlib.util.find_spec('dorm_checkin') is not None
if PRESENT:
    from dorm_checkin import (CheckinError, Engine, Result, Settings, Store, SUBMIT_COOLDOWN_SECONDS,
                              SUBMIT_MAX_ATTEMPTS, Task, SHANGHAI)


class FeatureExists(unittest.TestCase):
    def test_feature_exists(self):
        self.assertTrue(PRESENT, 'Independent dorm check-in engine is not implemented')


@unittest.skipUnless(PRESENT, 'Engine is not implemented')
class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name), protector=Mock(
            protect=lambda b: b[::-1], unprotect=lambda b: b[::-1]))
        self.now = dt.datetime(2026, 9, 21, 22, 0, tzinfo=SHANGHAI)
        self.task = Task('task-1', 'form-1', 'publish-1', 'student',
                         '2026-09-21', '今日查寝', '21:00', '23:30', False,
                         '宿舍', '800米', 'dorm-form')
        self.api = Mock()
        self.api.user.return_value = 'student'
        self.api.today.return_value = self.task
        self.api.is_signed.return_value = True
        self.position = Mock(return_value={'latitude': 29.8, 'longitude': 106.4})
        self.engine = Engine(self.store, self.api, self.position, clock=lambda: self.now)
        self.store.save_token('private-token')

    def test_default_disabled_and_no_school_requests(self):
        self.assertFalse(self.store.settings().enabled)
        self.assertEqual(self.engine.run(automatic=True).state, 'disabled')
        self.api.user.assert_not_called()

    def test_location_source_defaults_to_windows_for_existing_settings(self):
        self.store._write('settings.json', {'enabled': False, 'start': '21:00', 'end': '23:30', 'interval': 300})
        self.assertEqual(getattr(Store(self.store.root).settings(), 'location_source', None), 'windows')

    def test_location_source_is_persisted_and_validated(self):
        self.assertIn('location_source', Settings.__dataclass_fields__)
        self.store.save_settings(Settings(location_source='simulation'))
        self.assertEqual(Store(self.store.root).settings().location_source, 'simulation')
        for source in ('unknown', '', None):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.store.save_settings(Settings(location_source=source))

    def test_query_never_submits_or_reads_position(self):
        result = self.engine.run()
        self.assertEqual(result.state, 'ready')
        self.api.submit.assert_not_called()
        self.position.assert_not_called()

    def test_query_with_an_old_pending_task_never_submits_a_new_task(self):
        import dataclasses
        previous = dataclasses.replace(self.task, id='previous-task', date='2026-09-20')
        self.store.set_pending(previous.key)
        self.assertEqual(self.engine.run(submit=False).state, 'ready')
        self.assertTrue(self.store.pending(previous.key))
        self.api.submit.assert_not_called()
        self.position.assert_not_called()

    def test_signed_task_is_skipped(self):
        import dataclasses
        self.api.today.return_value = dataclasses.replace(self.task, signed=True)
        self.assertEqual(self.engine.run(submit=True).state, 'signed')
        self.api.submit.assert_not_called()

    def test_expired_task_never_submitted(self):
        self.now = self.now.replace(hour=23, minute=45)
        self.assertEqual(self.engine.run(submit=True).state, 'expired')
        self.api.submit.assert_not_called()

    def test_previous_day_never_submitted(self):
        import dataclasses
        self.api.today.return_value = dataclasses.replace(self.task, date='2026-09-20')
        self.assertEqual(self.engine.run(submit=True).state, 'error')
        self.api.submit.assert_not_called()

    def test_save_response_alone_confirms_the_check_in(self):
        self.api.submit.return_value = True
        self.assertEqual(self.engine.run(submit=True).state, 'signed')
        self.api.is_signed.assert_not_called()
        self.assertEqual(self.store.pending(), '')

    def test_unconfirmed_save_response_is_settled_by_readback(self):
        self.api.submit.return_value = False
        self.assertEqual(self.engine.run(submit=True).state, 'signed')
        self.api.is_signed.assert_called_once()
        self.assertEqual(self.store.pending(), '')

    def test_confirmed_non_submission_reports_the_reason_instead_of_hiding_it(self):
        self.api.submit.side_effect = CheckinError('network_error', '学校接口暂不可用，请稍后重试')
        self.api.is_signed.return_value = False
        result = self.engine.run(submit=True)
        self.assertEqual(result.state, 'ready')
        self.assertIn('学校接口暂不可用', result.message)
        self.assertIn('重试', result.message)
        self.assertEqual(self.store.pending(), '')
        self.assertEqual(self.store.attempts(self.task.key)[0], 1)

    def test_lost_login_is_reported_and_stays_retryable(self):
        self.api.submit.side_effect = CheckinError('login_required', '登录已失效，请重新登录')
        self.api.is_signed.return_value = False
        result = self.engine.run(submit=True)
        self.assertEqual(result.state, 'login_required')
        self.assertIn('本次提交未记录', result.message)
        self.assertEqual(self.store.pending(), '')

    def test_unknown_outcome_stays_readback_only_even_after_restart(self):
        self.api.submit.side_effect = TimeoutError()
        self.api.is_signed.side_effect = OSError('offline')
        self.assertEqual(self.engine.run(submit=True).state, 'uncertain')
        other = Engine(self.store, self.api, self.position, clock=lambda: self.now)
        self.assertEqual(other.run(submit=True).state, 'uncertain')
        self.assertEqual(self.api.submit.call_count, 1)
        self.assertEqual(self.store.pending(), self.task.key)

    def test_unrecorded_submission_is_retried_on_the_next_automatic_run(self):
        self.store.save_settings(Settings(enabled=True))
        self.api.submit.side_effect = [CheckinError('network_error', '学校接口暂不可用，请稍后重试'), True]
        self.api.is_signed.return_value = False
        self.assertEqual(self.engine.run(automatic=True, submit=True).state, 'ready')
        self.now += dt.timedelta(minutes=5)
        self.assertEqual(self.engine.run(automatic=True, submit=True).state, 'signed')
        self.assertEqual(self.api.submit.call_count, 2)
        self.assertEqual(self.store.attempts(self.task.key)[0], 0)

    def test_automatic_retry_waits_for_the_cooldown(self):
        self.store.save_settings(Settings(enabled=True))
        self.api.submit.side_effect = CheckinError('network_error', '学校接口暂不可用，请稍后重试')
        self.api.is_signed.return_value = False
        self.engine.run(automatic=True, submit=True)
        result = self.engine.run(automatic=True, submit=True)
        self.assertEqual(result.state, 'ready')
        self.assertIn('秒后可重试', result.message)
        self.assertEqual(self.api.submit.call_count, 1)

    def test_automatic_retries_are_capped_and_then_reported(self):
        self.store.save_settings(Settings(enabled=True))
        self.api.submit.side_effect = CheckinError('network_error', '学校接口暂不可用，请稍后重试')
        self.api.is_signed.return_value = False
        for _ in range(SUBMIT_MAX_ATTEMPTS):
            self.engine.run(automatic=True, submit=True)
            self.now += dt.timedelta(minutes=5)
        self.assertEqual(self.api.submit.call_count, SUBMIT_MAX_ATTEMPTS)
        result = self.engine.run(automatic=True, submit=True)
        self.assertEqual(result.state, 'uncertain')
        self.assertIn('上限', result.message)
        self.assertEqual(self.api.submit.call_count, SUBMIT_MAX_ATTEMPTS)

    def test_a_manual_submission_is_never_blocked_by_the_retry_limits(self):
        self.store.save_settings(Settings(enabled=True))
        self.api.submit.side_effect = CheckinError('network_error', '学校接口暂不可用，请稍后重试')
        self.api.is_signed.return_value = False
        for _ in range(SUBMIT_MAX_ATTEMPTS + 1):
            self.engine.run(automatic=True, submit=True)
            self.now += dt.timedelta(minutes=5)
        self.assertEqual(self.api.submit.call_count, SUBMIT_MAX_ATTEMPTS)
        self.engine.run(submit=True)
        self.assertEqual(self.api.submit.call_count, SUBMIT_MAX_ATTEMPTS + 1)

    def test_readback_can_resolve_a_timeout(self):
        self.api.submit.side_effect = TimeoutError()
        self.assertEqual(self.engine.run(submit=True).state, 'signed')
        self.assertEqual(self.store.pending(), '')

    def test_expired_login_during_readback_preserves_pending_until_renewal(self):
        self.api.submit.side_effect = TimeoutError()
        self.api.is_signed.side_effect = CheckinError('login_required', '登录已失效')
        self.assertEqual(self.engine.run(submit=True).state, 'login_required')
        self.assertTrue(self.store.pending(self.task.key))
        self.store.save_token('renewed-token')
        self.api.is_signed.side_effect = None
        self.api.is_signed.return_value = True
        self.assertEqual(self.engine.run(submit=False).state, 'signed')
        self.assertEqual(self.api.submit.call_count, 1)
        self.assertFalse(self.store.pending(self.task.key))

    def test_attempts_are_per_task_and_older_keys_are_dropped(self):
        self.store.record_attempt(self.task.key, 100)
        self.store.record_attempt(self.task.key, 200)
        self.assertEqual(Store(self.store.root).attempts(self.task.key), (2, 200))
        self.store.record_attempt('another-task', 300)
        self.assertEqual(self.store.attempts('another-task'), (1, 300))
        self.assertEqual(self.store.attempts(self.task.key), (0, 0.0))
        self.store.clear_attempts('another-task')
        self.assertEqual(self.store.attempts('another-task'), (0, 0.0))

    def test_corrupt_attempt_record_fails_closed(self):
        (self.store.root / 'submit-attempts.json').write_text('{"broken": 1}')
        self.assertEqual(self.engine.run(submit=True).state, 'error')
        self.api.submit.assert_not_called()

    def test_location_error_cannot_submit(self):
        from dorm_checkin import CheckinError
        self.position.side_effect = CheckinError('location_required', '无法定位')
        self.assertEqual(self.engine.run(submit=True).state, 'location_required')
        self.api.submit.assert_not_called()
        self.assertEqual(self.store.pending(), '')

    def test_missing_token_requires_login_without_request(self):
        self.store.clear_token()
        self.assertEqual(self.engine.run().state, 'login_required')
        self.api.user.assert_not_called()

    def test_automatic_execution_is_throttled(self):
        self.store.save_settings(Settings(enabled=True))
        self.engine.tick()
        self.engine.tick()
        self.assertEqual(self.api.today.call_count, 1)

    def test_login_failure_does_not_leak_exception_or_token(self):
        self.api.user.side_effect = ValueError('private-token')
        result = self.engine.run()
        self.assertNotIn('private-token', result.message)
        self.assertNotIn('private-token', self.store.history())

    def test_corrupt_settings_fail_closed(self):
        (self.store.root / 'settings.json').write_text('{')
        self.assertEqual(self.engine.run(automatic=True).state, 'error')
        self.api.user.assert_not_called()

    def test_token_is_not_saved_as_plaintext(self):
        self.assertEqual(self.store.token(), 'private-token')
        for f in self.store.root.rglob('*'):
            if f.is_file():
                self.assertNotIn(b'private-token', f.read_bytes())

    def test_cancel_before_submit(self):
        self.engine.cancel.set()
        self.assertEqual(self.engine.run(submit=True).state, 'cancelled')
        self.api.submit.assert_not_called()

    def test_invalid_schedule_rejected(self):
        with self.assertRaises(ValueError):
            self.store.save_settings(Settings(start='23:00', end='21:00'))

    def test_account_switch_preserves_each_uncertain_submission(self):
        import dataclasses
        self.api.submit.side_effect = TimeoutError()
        self.api.is_signed.side_effect = OSError('offline')
        self.assertEqual(self.engine.run(submit=True).state, 'uncertain')
        original = self.task
        self.api.user.return_value = 'other'
        self.api.today.return_value = dataclasses.replace(original, student='other', id='task-2')
        self.assertEqual(self.engine.run(submit=True).state, 'uncertain')
        self.api.user.return_value = 'student'
        self.api.today.return_value = original
        self.assertEqual(self.engine.run(submit=True).state, 'uncertain')
        self.assertEqual(self.api.submit.call_count, 2)
        self.assertTrue(self.store.pending(original.key))
        self.assertTrue(self.store.pending(self.api.today.return_value.key))

    def test_attempts_do_not_leak_between_tasks(self):
        import dataclasses
        self.api.submit.return_value = False
        self.api.is_signed.return_value = False
        self.assertEqual(self.engine.run(submit=True).state, 'ready')
        original = self.task
        self.api.user.return_value = 'other'
        self.api.today.return_value = dataclasses.replace(original, student='other', id='task-2')
        self.assertEqual(self.engine.run(submit=True).state, 'ready')
        self.assertEqual(self.api.submit.call_count, 2)
        self.assertEqual(self.store.attempts(original.key), (0, 0.0))

    def test_cancel_during_marker_write_does_not_submit_or_leave_pending(self):
        write = self.store.set_pending
        def cancel_after_write(key):
            write(key)
            self.engine.cancel.set()
        self.store.set_pending = cancel_after_write
        self.assertEqual(self.engine.run(submit=True).state, 'cancelled')
        self.api.submit.assert_not_called()
        self.assertEqual(self.store.pending(), '')

    def test_window_change_during_marker_write_does_not_submit(self):
        self.store.save_settings(Settings(enabled=True))
        write = self.store.set_pending
        def change_after_write(key):
            write(key)
            self.store.save_settings(Settings(enabled=True, start='22:30'))
        self.store.set_pending = change_after_write
        self.assertEqual(self.engine.run(submit=True, automatic=True).state, 'waiting')
        self.api.submit.assert_not_called()
        self.assertEqual(self.store.pending(), '')

    def test_expiry_during_marker_write_does_not_submit(self):
        write = self.store.set_pending
        def expire_after_write(key):
            write(key)
            self.now = self.now.replace(hour=23, minute=31)
        self.store.set_pending = expire_after_write
        self.assertEqual(self.engine.run(submit=True).state, 'expired')
        self.api.submit.assert_not_called()
        self.assertEqual(self.store.pending(), '')


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(Store, 'save_browser_session'), 'Encrypted browser session storage missing')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.protector = Mock(protect=lambda b: b[::-1], unprotect=lambda b: b[::-1])
        self.store = Store(Path(self.temp.name), protector=self.protector)
        self.cookies = [dict(name='SSO', value='private-cookie', domain='idm.swu.edu.cn',
                             path='/', expires=-1, httpOnly=True, secure=True, sameSite='Lax')]

    def test_session_survives_store_restart_without_plaintext_secrets(self):
        self.store.save_token('private-token')
        self.store.save_browser_session('private-token', 'student', self.cookies)
        restarted = Store(self.store.root, protector=self.protector)
        session = restarted.browser_session(restarted.token())
        self.assertEqual(session['student'], 'student')
        self.assertEqual(session['cookies'], self.cookies)
        for path in self.store.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(b'private-cookie', path.read_bytes())
                self.assertNotIn(b'private-token', path.read_bytes())

    def test_session_replacement_commits_matching_token_and_cookies(self):
        self.store.save_token('old-token')
        self.store.save_browser_session('old-token', 'student', self.cookies)
        renewed_cookies = [dict(self.cookies[0], value='renewed-cookie')]
        self.store.save_browser_session('new-token', 'student', renewed_cookies)
        restarted = Store(self.store.root, protector=self.protector)
        self.assertEqual(restarted.token(), 'new-token')
        self.assertEqual(restarted.browser_session('new-token')['cookies'], renewed_cookies)

    def test_token_only_installation_has_no_browser_session(self):
        self.store.save_token('existing-token')
        self.assertIsNone(self.store.browser_session('existing-token'))
        self.assertEqual(self.store.token(), 'existing-token')

    def test_session_cannot_be_reused_for_a_different_token(self):
        self.store.save_browser_session('old-token', 'student', self.cookies)
        self.store.save_token('other-token')
        self.assertIsNone(self.store.browser_session(self.store.token()))
        self.assertIsNone(self.store.browser_session(''))

    def test_has_session_only_looks_at_the_ciphertext_file(self):
        """has_session 是给只读快照用的：必须不解密、不抛异常、只看文件在不在。"""
        protector = Mock(protect=lambda b: b[::-1], unprotect=Mock(side_effect=AssertionError('不许解密')))
        store = Store(Path(self.store.root), protector=protector)
        self.assertFalse(store.has_session())
        self.store.save_token('private-token')
        self.assertTrue(store.has_session())
        # 密文被写坏时 token() 会抛，has_session() 仍要能回答「有」——
        # 否则「退出登录」这类补救按钮会因为状态说不清而被禁掉。
        (store.root / 'session' / 'credential.dat').write_text('corrupt', encoding='utf-8')
        self.assertTrue(store.has_session())

    def test_logout_removes_session_without_removing_settings_or_pending(self):
        self.store.save_token('private-token')
        self.store.save_browser_session('private-token', 'student', self.cookies)
        self.store.save_settings(Settings(start='20:00'))
        self.store.set_pending('unconfirmed-task')
        self.store.clear_token()
        self.assertEqual(self.store.token(), '')
        self.assertIsNone(self.store.browser_session('private-token'))
        self.assertEqual(self.store.settings().start, '20:00')
        self.assertTrue(self.store.pending('unconfirmed-task'))

    def test_invalidating_browser_session_preserves_current_token(self):
        self.store.save_token('private-token')
        self.store.save_browser_session('private-token', 'student', self.cookies)
        self.store.clear_browser_session()
        self.assertEqual(self.store.token(), 'private-token')
        self.assertIsNone(self.store.browser_session('private-token'))

    def test_session_decryption_failure_reports_no_secrets(self):
        self.store.save_browser_session('private-token', 'student', self.cookies)
        broken = Store(self.store.root, protector=Mock(unprotect=Mock(side_effect=ValueError('private-cookie'))))
        with self.assertRaises(CheckinError) as caught:
            broken.browser_session('private-token')
        self.assertEqual(caught.exception.state, 'login_required')
        self.assertNotIn('private-cookie', str(caught.exception))


@unittest.skipUnless(PRESENT, 'Engine is not implemented')
class TransientRetryTests(unittest.TestCase):
    """2026-09-28 现场：一次瞬时失败之后白等了 4.5 分钟（interval=300，冷却 21:06:30 就允许了）。

    瞬时失败要提前重试；等人的状态（登录/定位）不许被加密节奏。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name), protector=Mock(
            protect=lambda b: b[::-1], unprotect=lambda b: b[::-1]))
        self.store.save_settings(Settings(enabled=True, start='21:00', end='23:30',
                                          interval=300, location_source='simulation'))
        self.store.save_token('private-token')
        self.now = dt.datetime(2026, 9, 21, 22, 0, tzinfo=SHANGHAI)
        self.task = Task('task-1', 'form-1', 'publish-1', 'student',
                         '2026-09-21', '今日查寝', '21:00', '23:30', False, '宿舍', '800米')
        self.api = Mock()
        self.api.user.return_value = 'student'
        self.api.today.return_value = self.task
        self.api.is_signed.return_value = False
        self.engine = Engine(self.store, self.api, Mock(return_value={'latitude': 29.8}),
                             clock=lambda: self.now)

    def delay(self):
        seconds = self.engine.next_tick - time.monotonic()
        self.assertGreater(seconds, 0)
        return seconds

    def failing_submit(self):
        self.api.submit.side_effect = CheckinError(
            'network_error', '学校接口响应超时（20 秒未回应），请稍后重试',
            detail='响应超时 20.0s／直连')

    def test_an_unrecorded_submission_comes_back_after_the_cooldown_not_the_interval(self):
        self.failing_submit()
        result = self.engine.tick()
        self.assertEqual(result.state, 'ready')
        self.assertTrue(result.retry_soon)
        self.assertGreater(self.delay(), float(SUBMIT_COOLDOWN_SECONDS))
        self.assertLess(self.delay(), 200, '还在等满一个 interval 就等于没修')

    def test_an_unknown_submission_outcome_is_read_back_sooner(self):
        self.store.set_pending(self.task.key)
        self.api.is_signed.side_effect = CheckinError(
            'network_error', '与学校接口的连接被中断，请稍后重试', detail='连接被中断 0.1s／直连')
        result = self.engine.tick()
        self.assertEqual(result.state, 'uncertain')
        self.assertTrue(result.retry_soon)
        self.assertLess(self.delay(), 200)

    def test_a_transient_read_failure_is_retried_sooner(self):
        self.api.today.side_effect = CheckinError(
            'network_error', '学校接口响应超时（20 秒未回应），请稍后重试')
        result = self.engine.tick()
        self.assertEqual(result.state, 'network_error')
        self.assertTrue(result.retry_soon)
        self.assertLess(self.delay(), 200)

    def test_a_lost_login_waits_for_the_user_instead_of_being_retried_faster(self):
        self.api.user.side_effect = CheckinError('login_required', '登录已失效，请重新登录')
        result = self.engine.tick()
        self.assertEqual(result.state, 'login_required')
        self.assertFalse(result.retry_soon)
        self.assertGreater(self.delay(), 250)

    def test_a_successful_check_in_still_waits_the_full_interval(self):
        self.api.submit.return_value = True
        result = self.engine.tick()
        self.assertEqual(result.state, 'signed')
        self.assertFalse(result.retry_soon)
        self.assertGreater(self.delay(), 250)

    def test_the_failure_class_reaches_history_and_status(self):
        self.failing_submit()
        self.engine.tick()
        history = self.store.history()
        self.assertIn('[ready] 提交未生效：学校接口响应超时（20 秒未回应），请稍后重试', history)
        self.assertIn('响应超时 20.0s／直连', history)
        status = json.loads((self.store.root / 'status.json').read_text(encoding='utf-8'))
        self.assertEqual(status['detail'], '响应超时 20.0s／直连')
        self.assertTrue(status['retry_soon'])

    def test_a_clean_history_line_has_no_diagnostic_suffix(self):
        self.api.submit.return_value = True
        self.engine.tick()
        line = self.store.history().splitlines()[-1]
        self.assertIn('[signed] 服务器已确认今日任务完成', line)
        self.assertNotIn(' · ', line)


@unittest.skipUnless(PRESENT, 'Engine is not implemented')
class RepeatedFailureTests(unittest.TestCase):
    """2026-10-04 现场：Clash 全局模式把学校流量送到境外，TLS 握手连续 58 次被切断。

    一晚 58 行一模一样的记录挤掉了有用历史，通知也只弹过第一条 —— 那次是用户自己
    手动打卡才补上的。连续同类失败要折叠、计数，并在第 5 次把处置建议写进文案。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name), protector=Mock(
            protect=lambda b: b[::-1], unprotect=lambda b: b[::-1]))
        self.store.save_settings(Settings(enabled=True, start='21:00', end='23:30',
                                          interval=300, location_source='simulation'))
        self.store.save_token('private-token')
        self.now = dt.datetime(2026, 10, 4, 22, 0, tzinfo=SHANGHAI)
        self.api = Mock()
        self.api.user.side_effect = CheckinError(
            'network_error', '与学校接口的 TLS 握手被切断（链路中间有设备没让它谈完），请稍后重试',
            detail='TLS 握手被切断 5.3s／直连')
        self.api.today.return_value = None
        self.engine = Engine(self.store, self.api, Mock(return_value={}),
                             clock=lambda: self.now)

    def failing_runs(self, times):
        results = []
        for _ in range(times):
            results.append(self.engine.run(automatic=True, submit=True))
        return results

    def test_repeats_collapse_into_one_line_with_a_counter(self):
        self.failing_runs(4)
        lines = [line for line in self.store.history().splitlines() if 'network_error' in line]
        self.assertEqual(len(lines), 1, f'应折叠成一行，实际 {len(lines)} 行')
        self.assertIn('连续第 4 次', lines[0])
        self.assertIn('TLS 握手被切断 5.3s／直连', lines[0])

    def test_the_escalated_text_is_its_own_line_and_then_also_collapses(self):
        self.failing_runs(7)
        lines = [line for line in self.store.history().splitlines() if 'network_error' in line]
        self.assertEqual(len(lines), 2, f'升级前后各一行，实际 {len(lines)} 行')
        self.assertNotIn('规则', lines[0])
        self.assertIn('「规则」模式', lines[1])
        self.assertIn('连续第 7 次', lines[1])

    def test_a_streak_that_changes_gets_its_own_line(self):
        self.failing_runs(2)
        self.api.user.side_effect = CheckinError(
            'network_error', '学校接口响应超时（20 秒未回应），请稍后重试', detail='响应超时 20.0s／直连')
        self.failing_runs(1)
        lines = [line for line in self.store.history().splitlines() if 'network_error' in line]
        self.assertEqual(len(lines), 2)

    def test_the_fifth_failure_adds_the_thing_to_actually_check(self):
        results = self.failing_runs(5)
        self.assertNotIn('连续失败', results[0].message)
        self.assertIn('已连续多次失败', results[4].message)
        self.assertIn('「规则」模式', results[4].message)
        self.assertIn('连续第 5 次', results[4].detail)
        # 第 6 次仍然是升级后的文案（通知去重键才不会再变来变去）
        self.assertIn('「规则」模式', self.failing_runs(1)[0].message)

    def test_a_success_clears_the_streak(self):
        self.failing_runs(4)
        self.api.user.side_effect = None
        self.api.user.return_value = 'student'
        signed = self.engine.run(automatic=True, submit=True)
        self.assertNotEqual(signed.state, 'network_error')
        self.assertEqual(self.engine.transient_streak, 0)
        self.api.user.side_effect = CheckinError('network_error', '无法连接学校接口，请检查网络后重试')
        self.assertNotIn('连续失败', self.failing_runs(1)[0].message)


if __name__ == '__main__':
    unittest.main()
