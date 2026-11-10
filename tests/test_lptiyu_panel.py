"""乐跑控制器: 忙闸、自动流水线门控、结果状态映射、凭证保号。

全部网络面打桩: _probe_alive 钉死为存活, subprocess 钉死 —— 单测绝不真连
服务器, 也绝不触碰本机研究库(这台机器上研究库真实存在)。
"""
import datetime as dt
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from lptiyu_panel import LptiyuController, LptiyuError, TokenSlot, MAX_DAILY_AUTO_ATTEMPTS
from lptiyu_store import Settings, Store

CLOCK = lambda: dt.datetime(2026, 11, 11, 7, 0)  # noqa: E731 —— 有效时段内、run_time 之后


class FakeFlow:
    def __init__(self, code):
        self.code = code
        self.summary = {'record_id': 'R1', 'distance': 2.98, 'used_time': 722,
                        'zone_name': '第二运动场', 'record_failed_reason': '自动确认有效'}
        self.calls = []

    def run(self, submit, zone_override=None):
        self.calls.append({'submit': submit, 'zone': zone_override})
        return (self.summary if self.code in (0, 5) else None), self.code


class PanelTestCase(unittest.TestCase):
    """共享的隔离环境: 临时存储 + 无网探测 + 不会触发的采集。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / 'lptiyu')
        self.store.save_identity({'name': '测试成员', 'student_num': '222025321102104',
                                  'uid': '17987537', 'school_id': '837',
                                  'card_id': '222025321102104'})
        self.store.save_token_record('TOKEN', issued_at='2026-11-11T01:07:18',
                                     source='test')
        self.flows = []
        probe = patch.object(LptiyuController, '_probe_alive', lambda self, client: True)
        grab = patch('lptiyu_panel.subprocess.run')
        lab = patch.object(Store, 'lab_dir', lambda self: '')
        probe.start(); grab.start(); lab.start()
        self.addCleanup(probe.stop); self.addCleanup(grab.stop); self.addCleanup(lab.stop)

    def make_controller(self, code=0, settings=None, clock=None):
        if settings is not None:
            self.store.save_settings(settings)

        def factory(slot, flow_settings):
            flow = FakeFlow(code)
            self.flows.append(flow)
            return flow

        controller = LptiyuController(store=self.store, clock=clock or CLOCK,
                                      flow_factory=factory)
        self.addCleanup(controller.close)
        return controller


class ExecuteTests(PanelTestCase):
    def test_successful_run_records_signed_and_keeps_token(self):
        controller = self.make_controller(code=0)
        result = controller._execute(submit=True)
        self.assertEqual(result['state'], 'signed')
        self.assertIn('2.98km', result['message'])
        self.assertIn("4'02\"", result['message'])
        self.assertTrue(controller.store.is_signed_today())
        self.assertTrue(controller.store.has_token(), '提交成功后凭证必须保留给下一次')
        self.assertEqual(controller.store.daily().get('run'), 1)

    def test_zone_override_comes_from_settings(self):
        controller = self.make_controller(code=0, settings=Settings(zone_id='1873'))
        controller._execute(submit=False)
        self.assertEqual(self.flows[0].calls[0]['zone'], '1873')

    def test_quota_full_marks_day_done(self):
        controller = self.make_controller(code=3)
        result = controller._execute(submit=True)
        self.assertEqual(result['state'], 'quota')
        self.assertTrue(controller.store.is_signed_today())
        self.assertIsNone(controller.store.daily().get('run'),
                          '额度满那一拍没有真实提交, 不计入 run')

    def test_dead_token_maps_to_need_token(self):
        controller = self.make_controller(code=2)
        with self.assertRaises(LptiyuError) as caught:
            controller._execute(submit=True)
        self.assertEqual(caught.exception.state, 'need_token')

    def test_cancelled_maps_to_cancelled(self):
        controller = self.make_controller(code=9)
        self.assertEqual(controller._execute(submit=True)['state'], 'cancelled')

    def test_uncertain_records_school_verdict(self):
        controller = self.make_controller(code=5)
        self.assertEqual(controller._execute(submit=True)['state'], 'uncertain')

    def test_generic_failure_maps_to_error(self):
        controller = self.make_controller(code=4)
        self.assertEqual(controller._execute(submit=True)['state'], 'error')

    def test_dry_run_never_marks_signed(self):
        controller = self.make_controller(code=0)
        result = controller._execute(submit=False)
        self.assertEqual(result['state'], 'checked')
        self.assertFalse(controller.store.is_signed_today())
        self.assertTrue(controller.store.has_token())


class GateTests(PanelTestCase):
    def test_busy_rejects_second_action(self):
        controller = self.make_controller()
        controller.busy = True
        self.assertFalse(controller.start('run'))
        self.assertFalse(controller.start('sync'))

    def test_unknown_action_rejected(self):
        controller = self.make_controller()
        self.assertFalse(controller.start('purge'))

    def test_missing_identity_reports_need_token(self):
        controller = self.make_controller()
        controller.store.save_identity({})
        with self.assertRaises(LptiyuError) as caught:
            controller._execute(submit=False)
        self.assertEqual(caught.exception.state, 'need_token')

    def test_missing_token_reports_need_token_without_lab(self):
        controller = self.make_controller()
        controller.store.clear_token()
        with self.assertRaises(LptiyuError) as caught:
            controller._execute(submit=False)
        self.assertEqual(caught.exception.state, 'need_token')


class PollTests(PanelTestCase):
    def test_poll_fires_after_run_time_when_enabled(self):
        started = threading.Event()
        original = LptiyuController.start

        def spy(self, action):
            started.set()
            return original(self, action)

        controller = self.make_controller(settings=Settings(enabled=True, run_time='06:30'))
        with patch.object(LptiyuController, 'start', spy):
            controller.poll()
        self.assertTrue(started.wait(3.0), '到点且开启时必须自动起跑')
        controller._wait_for_work()

    def test_poll_skips_before_run_time(self):
        controller = self.make_controller(settings=Settings(enabled=True, run_time='08:00'))
        controller.poll()
        self.assertFalse(controller.busy)

    def test_poll_skips_when_disabled(self):
        controller = self.make_controller(settings=Settings(enabled=False))
        controller.poll()
        self.assertFalse(controller.busy)

    def test_poll_skips_when_signed_today(self):
        controller = self.make_controller(settings=Settings(enabled=True))
        controller.store.mark_signed(controller._clock().date().isoformat())
        controller.poll()
        self.assertFalse(controller.busy)

    def test_poll_stops_after_daily_attempt_cap(self):
        controller = self.make_controller(settings=Settings(enabled=True))
        for _ in range(MAX_DAILY_AUTO_ATTEMPTS):
            controller.store.bump_daily(kind='auto')
        controller.poll()
        self.assertFalse(controller.busy, '每天自动尝试有硬上限')


if __name__ == '__main__':
    unittest.main()
