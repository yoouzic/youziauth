"""凭证自动续期: 探活判活、只在 101 判死、补号阶梯、配额与冷却、跑步前保活。

全部网络面与子进程打桩, 研究库路径钉死 —— 单测绝不真连服务器, 也绝不碰本机
真实研究库(这台机器上研究库真实存在)。
"""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lptiyu_renew import (BACKOFF_BASE_MINUTES, COOLDOWN_MINUTES, MAX_DAILY_GRABS,
                          MAX_DAILY_PROBES, CredentialRenewer)
from lptiyu_store import Settings, Store

START = dt.datetime(2026, 11, 11, 5, 50)   # 默认 06:30 起跑, 提前 40 分钟: 落在 45 分钟保活窗


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at

    def tick(self, minutes):
        self.at += dt.timedelta(minutes=minutes)
        return self.at


class Probe:
    """探活替身: 按顺序返回预设结果, 用完后一律判活。"""

    def __init__(self, *sequence):
        self.sequence = list(sequence)
        self.calls = 0

    def __call__(self, record):
        self.calls += 1
        if not self.sequence:
            return True, ''
        item = self.sequence.pop(0)
        if isinstance(item, tuple):
            return item
        return item, ''


class Grab:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return True, ''


class RenewTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = Store(self.root / 'lptiyu')
        self.clock = Clock(START)
        self.probe = Probe()
        self.grab = Grab()
        self.lab_path = None
        # 这台机器上真有研究库: 自动发现必须钉死, 否则「没有研究库」的路径永远测不到。
        lab = patch.object(Store, 'lab_dir',
                           lambda _store: str(self.lab_path) if self.lab_path else '')
        lab.start()
        self.addCleanup(lab.stop)

    # ---------------------------------------------------------------- 工具
    def renewer(self):
        return CredentialRenewer(self.store, clock=self.clock,
                                 probe=self.probe, grab=self.grab)

    def token(self, value='TOK', issued='2026-11-11T01:00:00'):
        self.store.save_token_record(value, issued_at=issued, source='test')

    def save_lab(self, token='LABTOKEN', issued='2026-11-11T05:00:00', consumed=False,
                 member='222025321102104'):
        lab = self.root / 'lab'
        (lab / 'data').mkdir(parents=True, exist_ok=True)
        (lab / 'lpctl.py').write_text('# stub\n', encoding='utf-8')
        config = {'schema': 'lptiyu-lab/1', 'active': member,
                  'members': {member: {'uid': 17987537, 'school_id': 837,
                                       'student_num': member, 'card_id': member,
                                       'name': '测试成员', 'alias': '测试成员',
                                       'auth': {'token': token, 'issued_at': issued,
                                                'consumed_at': '2026-11-11T05:10:00'
                                                               if consumed else ''}}}}
        (lab / 'config.json').write_text(json.dumps(config, ensure_ascii=False),
                                         encoding='utf-8')
        self.lab_path = lab
        return lab

    def seed_ledger(self, **fields):
        record = {'state': 'ok', 'message': '', 'last_attempt': '', 'last_success': '',
                  'failures': 0, 'next_due': '', 'events': []}
        record.update(fields)
        self.store.root.mkdir(parents=True, exist_ok=True)
        (self.store.root / 'renewal.json').write_text(json.dumps(record), encoding='utf-8')
        return record


class RenewLadderTests(RenewTestCase):
    def test_alive_token_is_left_alone(self):
        self.token('TOK')
        outcome = self.renewer().renew()
        self.assertEqual(outcome.state, 'ok')
        self.assertEqual(self.store.token(), 'TOK')
        self.assertEqual(self.grab.calls, 0, '活着就不该惊动采集链')
        record = self.store.renewal()
        self.assertEqual(record['state'], 'ok')
        self.assertEqual(record['failures'], 0)
        self.assertEqual(record['next_due'],
                         (START + dt.timedelta(minutes=180)).isoformat(timespec='seconds'))
        self.assertEqual(self.store.status(), {}, '续期是维护动作, 不写任务状态')
        self.assertNotIn('[ok]', self.store.history(), '例行探活只进台账, 不进运行记录')

    def test_dead_token_adopts_the_lab_token_without_capture(self):
        self.token('OLD')
        self.save_lab(token='NEW', issued='2026-11-11T05:40:00')
        self.probe = Probe((False, '101'), True)
        outcome = self.renewer().renew()
        self.assertEqual(outcome.state, 'renewed')
        self.assertEqual(self.store.token(), 'NEW')
        self.assertEqual(self.grab.calls, 0, '研究库有现成新号就不该让用户再开小程序')

    def test_consumed_lab_token_is_adopted_and_probed(self):
        # 研究库把提交后的号记成 consumed, 但实测只由 loginByCode 轮换才死。
        self.save_lab(token='CONSUMED-BUT-ALIVE', consumed=True)
        outcome = self.renewer().renew()
        self.assertEqual(outcome.state, 'renewed')
        self.assertEqual(self.store.token(), 'CONSUMED-BUT-ALIVE')
        self.assertEqual(self.probe.calls, 1)

    def test_without_a_lab_it_names_the_missing_capture_component(self):
        outcome = self.renewer().renew()
        self.assertEqual(outcome.state, 'need_user')
        self.assertIn('研究库', outcome.message,
                      '没有采集组件时要说清缺的是什么, 而不是让人去开小程序')
        self.assertEqual(self.grab.calls, 0)

    def test_capture_runs_at_most_twice_a_day(self):
        self.save_lab(token='')           # 研究库在, 但还没有号
        outcomes = [self.renewer().renew(force=True) for _ in range(MAX_DAILY_GRABS + 1)]
        self.assertEqual(self.grab.calls, MAX_DAILY_GRABS)
        self.assertEqual(outcomes[-1].state, 'need_user')
        self.assertIn('次数已用完', outcomes[-1].message)

    def test_network_error_keeps_the_token_and_backs_off(self):
        self.token('TOK')
        self.probe = Probe((None, '链路故障'))
        outcome = self.renewer().renew()
        self.assertEqual(outcome.state, 'error')
        self.assertEqual(self.store.token(), 'TOK', '探不通不等于死了')
        record = self.store.renewal()
        self.assertEqual(record['failures'], 1)
        self.assertEqual(record['next_due'],
                         (START + dt.timedelta(minutes=BACKOFF_BASE_MINUTES))
                         .isoformat(timespec='seconds'))

    def test_cooldown_blocks_the_next_automatic_attempt_but_not_a_manual_one(self):
        self.token('TOK')
        self.probe = Probe((None, '链路故障'), True)
        self.assertEqual(self.renewer().renew().state, 'error')
        renewer = self.renewer()
        self.assertEqual(renewer.renew().state, 'cooldown')
        self.assertIn('分钟后再试', renewer.renew().message)
        self.assertEqual(renewer.renew(force=True).state, 'ok', '手动点击越过冷却')

    def test_probe_budget_stops_hammering(self):
        self.token('TOK')
        for _ in range(MAX_DAILY_PROBES):
            self.assertEqual(self.renewer().renew(force=True).state, 'ok')
        outcome = self.renewer().renew(force=True)
        self.assertEqual(outcome.state, 'capped')
        self.assertEqual(self.probe.calls, MAX_DAILY_PROBES, '探活也有每日硬上限')

    def test_unknown_lab_token_falls_back_to_capture(self):
        self.save_lab(token='DEAD', issued='2026-11-11T05:40:00')
        self.probe = Probe((False, '101'), (False, '101'), (False, '101'))
        outcome = self.renewer().renew()
        self.assertEqual(self.grab.calls, 1, '研究库那枚也死了就该去采集一次')
        self.assertEqual(outcome.state, 'need_user')

    def test_auto_renew_off_is_reported_not_executed(self):
        self.token('TOK')
        self.store.save_settings(Settings(auto_renew=False))
        self.assertEqual(self.renewer().renew().state, 'skipped')
        self.assertEqual(self.probe.calls, 0)


class DueTests(RenewTestCase):
    """什么时候该在后台跑一次保活。"""

    def test_due_when_never_renewed(self):
        self.store.save_settings(Settings(enabled=True))
        self.assertTrue(self.renewer().due())

    def test_not_due_when_auto_run_is_off(self):
        self.store.save_settings(Settings(enabled=False))
        self.assertFalse(self.renewer().due())

    def test_not_due_when_auto_renew_is_off(self):
        self.store.save_settings(Settings(enabled=True, auto_renew=False))
        self.assertFalse(self.renewer().due())

    def test_not_due_before_next_due(self):
        # 07:00 已经在保活窗之外, 这时只有台账的 next_due 说了算。
        self.clock = Clock(dt.datetime(2026, 11, 11, 7, 0))
        self.store.save_settings(Settings(enabled=True))
        self.seed_ledger(next_due=(self.clock.at + dt.timedelta(minutes=30))
                         .isoformat(timespec='seconds'))
        self.assertFalse(self.renewer().due())

    def test_due_once_next_due_passed(self):
        self.clock = Clock(dt.datetime(2026, 11, 11, 7, 0))
        self.store.save_settings(Settings(enabled=True))
        self.seed_ledger(next_due=(self.clock.at - dt.timedelta(minutes=1))
                         .isoformat(timespec='seconds'))
        self.assertTrue(self.renewer().due())

    def test_lead_window_renews_even_if_the_interval_is_still_far(self):
        self.store.save_settings(Settings(enabled=True, run_time='06:30', renew_lead_min=45))
        self.seed_ledger(next_due=(START + dt.timedelta(hours=3)).isoformat(timespec='seconds'),
                         last_success='2026-11-10T05:46:00')
        self.assertTrue(self.renewer().due())

    def test_lead_window_already_confirmed_today_is_not_due(self):
        self.store.save_settings(Settings(enabled=True, run_time='06:30', renew_lead_min=45))
        self.seed_ledger(next_due=(START + dt.timedelta(hours=3)).isoformat(timespec='seconds'),
                         last_success=(START + dt.timedelta(minutes=1)).isoformat(timespec='seconds'))
        self.assertFalse(self.renewer().due())

    def test_outside_the_lead_window_the_interval_decides(self):
        self.clock = Clock(dt.datetime(2026, 11, 11, 7, 0))
        self.store.save_settings(Settings(enabled=True, run_time='06:30', renew_lead_min=45))
        self.seed_ledger(next_due=(START + dt.timedelta(hours=3)).isoformat(timespec='seconds'))
        self.assertFalse(self.renewer().due())
        self.seed_ledger(next_due=(START + dt.timedelta(minutes=60)).isoformat(timespec='seconds'))
        self.assertTrue(self.renewer().due())

    def test_broken_settings_read_as_not_due(self):
        self.store.root.mkdir(parents=True, exist_ok=True)
        (self.store.root / 'settings.json').write_text('{broken', encoding='utf-8')
        self.assertFalse(self.renewer().due())


if __name__ == '__main__':
    unittest.main()
