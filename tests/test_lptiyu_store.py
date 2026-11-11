"""乐跑存储层: 设置、凭证加密、状态历史、每日计数、研究库同步。"""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lptiyu_store import Settings, Store


class SyncLedgerTests(unittest.TestCase):
    def test_record_sync_stamps_the_local_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            store.record_sync('本机凭证已是最新')
            record = store.sync_record()
            self.assertEqual(record['message'], '本机凭证已是最新')
            stamped = dt.datetime.fromisoformat(record['at'])
            self.assertLess(abs((dt.datetime.now() - stamped).total_seconds()), 5)

    def test_sync_record_tolerates_a_corrupt_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            (Path(tmp) / 'sync.json').write_text('{bad', encoding='utf-8')
            self.assertEqual(store.sync_record(), {})


class SettingsTests(unittest.TestCase):
    def test_defaults_validate(self):
        self.assertEqual(Settings().validate().run_time, '06:30')

    def test_inverted_distance_rejected(self):
        with self.assertRaises(ValueError):
            Settings(dist_lo=3.0, dist_hi=2.0).validate()

    def test_run_time_outside_validity_window_rejected(self):
        with self.assertRaises(ValueError):
            Settings(run_time='05:30').validate()

    def test_pace_out_of_range_rejected(self):
        with self.assertRaises(ValueError):
            Settings(pace_target=60).validate()

    def test_pace_range_rejects_inverted_bounds(self):
        with self.assertRaises(ValueError):
            Settings(pace_lo=300, pace_hi=250).validate()

    def test_pace_range_rejects_implausible_speed(self):
        with self.assertRaises(ValueError):
            Settings(pace_lo=60, pace_hi=90).validate()

    def test_pace_range_needs_both_bounds(self):
        with self.assertRaises(ValueError):
            Settings(pace_lo=245, pace_hi=0).validate()

    def test_pace_range_treats_the_legacy_single_value_as_a_point(self):
        self.assertEqual(Settings().pace_range(), (0, 0))
        self.assertEqual(Settings(pace_target=240).pace_range(), (240, 240))
        self.assertEqual(Settings(pace_lo=245, pace_hi=280).pace_range(), (245, 280))

    def test_pace_range_survives_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            store.save_settings(Settings(pace_lo=245, pace_hi=280))
            self.assertEqual(store.settings().pace_range(), (245, 280))

    def test_settings_survive_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            store.save_settings(Settings(enabled=True, run_time='07:00', dist_hi=3.0))
            loaded = store.settings()
            self.assertTrue(loaded.enabled)
            self.assertEqual(loaded.run_time, '07:00')
            self.assertEqual(loaded.dist_hi, 3.0)


class TokenTests(unittest.TestCase):
    def test_token_roundtrip_is_encrypted_at_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            self.assertFalse(store.has_token())
            store.save_token_record('ABC123', issued_at='2026-11-11T01:07:18', source='test')
            self.assertTrue(store.has_token())
            # 密文文件里绝不出现明文 token
            blob = (store.root / 'session' / 'credential.dat').read_bytes()
            self.assertNotIn(b'ABC123', blob)
            record = store.token_record()
            self.assertEqual(record['token'], 'ABC123')
            self.assertEqual(record['issued_at'], '2026-11-11T01:07:18')
            self.assertEqual(record['source'], 'test')

    def test_has_token_never_decrypts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            store.save_token_record('SECRET')
            # 只读文件存在性，不触碰 DPAPI
            self.assertTrue(store.has_token())

    def test_invalid_token_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            with self.assertRaises(ValueError):
                store.save_token_record('bad\nmultiline')

    def test_clear_token_removes_ciphertext(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            store.save_token_record('ABC123')
            store.clear_token()
            self.assertFalse(store.has_token())


class StatusTests(unittest.TestCase):
    def test_record_writes_status_and_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            store.record('signed', '今日跑步已完成', '第二运动场')
            self.assertEqual(store.status()['state'], 'signed')
            self.assertIn('[signed] 今日跑步已完成 · 第二运动场', store.history())

    def test_history_is_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            for index in range(150):
                store.record('running', f'第 {index} 次运行')
            lines = store.history().splitlines()
            self.assertEqual(len(lines), 99)
            self.assertIn('第 149 次运行', lines[-1])
            self.assertNotIn('第 0 次运行', store.history())

    def test_corrupt_settings_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            (store.root / 'settings.json').write_text('{broken', encoding='utf-8')
            with self.assertRaises(Exception):
                store.settings()

    def test_bom_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            (store.root).mkdir(parents=True, exist_ok=True)
            (store.root / 'settings.json').write_bytes(
                b'\xef\xbb\xbf' + json.dumps({'enabled': True, 'run_time': '06:30',
                                              'dist_lo': 2.03, 'dist_hi': 2.35,
                                              'anchor_max_m': 20, 'max_per_day': 1,
                                              'time_lo': '06:00', 'time_hi': '23:00'}
                                             ).encode('utf-8'))
            self.assertTrue(store.settings().enabled)

    def test_daily_counters_pruned_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            self.assertEqual(store.daily().get('run', 0), 0)
            store.bump_daily(kind='run')
            store.bump_daily(kind='run')
            self.assertEqual(store.daily().get('run'), 2)


class LabSyncTests(unittest.TestCase):
    def _lab(self, root: Path, token='LABTOKEN', issued='2026-11-11T01:07:18',
             consumed=False):
        lab = root / 'lab'
        (lab / 'data').mkdir(parents=True, exist_ok=True)
        (lab / 'lpctl.py').write_text('# lab stub\n', encoding='utf-8')
        config = {
            'schema': 'lptiyu-lab/1', 'active': '222025321102104',
            'members': {'222025321102104': {
                'uid': 17987537, 'school_id': 837,
                'student_num': '222025321102104', 'card_id': '222025321102104',
                'name': '陈同学', 'alias': '陈同学', 'enabled': True,
                'auth': {'token': token, 'issued_at': issued,
                         'source': 'mitm_capture', 'consumed_at': 'x' if consumed else ''},
            }},
        }
        (lab / 'config.json').write_text(json.dumps(config, ensure_ascii=False),
                                         encoding='utf-8')
        return lab

    def test_sync_adopts_identity_and_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lab = self._lab(root)
            store = Store(root / 'store')
            store.save_settings(Settings(lab_dir=str(lab)))
            changes, message = store.sync_from_lab()
            self.assertIn('token', changes)
            identity = store.identity()
            self.assertEqual(identity['student_num'], '222025321102104')
            self.assertEqual(identity['uid'], '17987537')
            self.assertEqual(store.token(), 'LABTOKEN')
            # 目录被记住，后续不再依赖设置
            self.assertEqual(store.settings().lab_dir, str(lab))

    def test_sync_keeps_newer_local_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lab = self._lab(root, issued='2026-11-11T01:07:18')
            store = Store(root / 'store')
            store.save_settings(Settings(lab_dir=str(lab)))
            store.save_token_record('LOCALNEW', issued_at='2026-11-11T09:00:00',
                                    source='manual')
            changes, _ = store.sync_from_lab()
            self.assertNotIn('token', changes)
            self.assertEqual(store.token(), 'LOCALNEW')

    def test_sync_skips_consumed_lab_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lab = self._lab(root, consumed=True)
            store = Store(root / 'store')
            store.save_settings(Settings(lab_dir=str(lab)))
            changes, _ = store.sync_from_lab()
            self.assertNotIn('token', changes)
            self.assertFalse(store.has_token())

    def test_renewal_can_adopt_a_consumed_token_that_is_still_alive(self):
        # 研究库把「提交完」记成 consumed, 但实测 token 只由下一次 loginByCode 轮换才死;
        # 续期路径允许采纳它, 由真门探活裁决, 否则每次都要把用户赶去重开小程序。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lab = self._lab(root, consumed=True)
            store = Store(root / 'store')
            store.save_settings(Settings(lab_dir=str(lab)))
            changes, _ = store.sync_from_lab(include_consumed=True)
            self.assertIn('token', changes)
            self.assertEqual(store.token(), 'LABTOKEN')
            self.assertEqual(store.token_record()['source'], 'lab-consumed')

    def test_sync_picks_the_member_matching_the_local_identity(self):
        # 研究库是多成员白名单, active 只是「上次在用的那个」; 按 active 取会把
        # 别人的号装进来。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lab = root / 'lab'
            (lab / 'data').mkdir(parents=True, exist_ok=True)
            (lab / 'lpctl.py').write_text('# lab stub\n', encoding='utf-8')
            config = {
                'schema': 'lptiyu-lab/1', 'active': 'OTHER',
                'members': {
                    'OTHER': {'uid': 111, 'school_id': 837, 'student_num': 'OTHER',
                              'name': '别人', 'alias': '别人', 'enabled': True,
                              'auth': {'token': 'OTHERTOKEN',
                                       'issued_at': '2026-11-11T09:00:00'}},
                    '222025321102104': {
                        'uid': 17987537, 'school_id': 837,
                        'student_num': '222025321102104', 'card_id': '222025321102104',
                        'name': '陈同学', 'alias': '陈同学', 'enabled': True,
                        'auth': {'token': 'MYTOKEN', 'issued_at': '2026-11-11T01:07:18'}},
                },
            }
            (lab / 'config.json').write_text(json.dumps(config, ensure_ascii=False),
                                             encoding='utf-8')
            store = Store(root / 'store')
            store.save_identity({'student_num': '222025321102104', 'uid': '17987537'})
            store.save_settings(Settings(lab_dir=str(lab)))
            changes, _ = store.sync_from_lab()
            self.assertIn('token', changes)
            self.assertEqual(store.token(), 'MYTOKEN')

    def test_sync_without_lab_reports_clearly(self):
        # 封闭性: 桌面已知文件夹的自动发现可能命中本机真实研究库, 必须钉死为空,
        # 否则这台装着研究库的机器上, 「没有 lab」的路径永远测不到。
        with patch.object(Store, '_known_desktop_dirs', lambda self: []), \
             patch.object(Store, 'LAB_HINTS', ()):
            with tempfile.TemporaryDirectory() as tmp:
                store = Store(Path(tmp))
                changes, message = store.sync_from_lab()
                self.assertEqual(changes, [])
                self.assertIn('未找到', message)


class LabStatusTests(unittest.TestCase):
    """采集组件(研究库)的状态: 没它就是采不到新凭证, 界面得能解释清楚。"""

    def _lab(self, root: Path):
        lab = root / "lab"
        lab.mkdir(parents=True, exist_ok=True)
        (lab / "lpctl.py").write_text("# lab stub\n", encoding="utf-8")
        return lab

    def test_not_configured_and_not_found(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(Store, "_known_desktop_dirs", lambda self: []):
            store = Store(Path(tmp))
            status = store.lab_status()
            self.assertFalse(status["ok"])
            self.assertEqual(status["reason"], "not_configured")

    def test_configured_but_missing_lpctl(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(Store, "_known_desktop_dirs", lambda self: []):
            root = Path(tmp)
            (root / "empty").mkdir()
            store = Store(root / "store")
            store.save_settings(Settings(lab_dir=str(root / "empty")))
            status = store.lab_status()
            self.assertFalse(status["ok"])
            self.assertEqual(status["reason"], "missing_lpctl")
            self.assertEqual(status["configured"], str(root / "empty"))

    def test_configured_and_valid(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(Store, "_known_desktop_dirs", lambda self: []):
            root = Path(tmp)
            lab = self._lab(root)
            store = Store(root / "store")
            store.save_settings(Settings(lab_dir=str(lab)))
            status = store.lab_status()
            self.assertTrue(status["ok"])
            self.assertEqual(status["found"], str(lab))
            self.assertEqual(status["reason"], "")


class ZoneCatalogTests(unittest.TestCase):
    """跑区目录: 清单、点位、每块场地的特调都各自落盘。"""

    POINTS = [{'id': '1', 'lat': 29.8237, 'lng': 106.4302},
              {'id': '2', 'lat': 29.8245, 'lng': 106.4315},
              {'id': '3', 'lat': 29.8231, 'lng': 106.4318}]

    def test_zone_list_merges_names_and_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            store.save_zone_list([{'id': '1873', 'name': '第二运动场', 'is_default': 0}])
            store.save_zone_list([{'id': '1873', 'name': '第二运动场（新）', 'is_default': 1},
                                  {'id': '1874', 'name': '第一运动场', 'is_default': 0}])
            catalog = store.zones()
            self.assertEqual(set(catalog), {'1873', '1874'})
            self.assertEqual(catalog['1873']['name'], '第二运动场（新）')
            self.assertEqual(catalog['1873']['is_default'], 1)

    def test_points_and_tuning_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            store.save_zone_points('1873', '第二运动场', self.POINTS,
                                   tuning={'direction': 'cw', 'lane_m': 5})
            entry = store.zones()['1873']
            self.assertEqual(len(entry['points']), 3)
            self.assertEqual(entry['tuning']['direction'], 'cw')
            self.assertEqual(store.zone_tuning('1873')['lane_m'], 5.0)
            summary = store.zone_summary('1873')
            self.assertEqual(summary['zone_id'], '1873')
            self.assertIn('贴点', summary['text'])
            self.assertNotIn('29.82', summary['text'], '摘要不该带坐标')

    def test_tuning_is_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            store.save_zone_tuning('1873', {'direction': 'sideways', 'lane_m': -1})
            self.assertEqual(store.zone_tuning('1873'),
                             {'direction': 'auto', 'lane_m': 0.0,
                              'anchor_m': 0.0, 'start_deg': 0.0,
                              'axis_deg': 0.0})

    def test_the_same_need_user_line_is_written_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            for _ in range(4):
                store.record_renewal('need_user', '点「开始采集」，微信打开小程序就能拿到凭证')
            lines = [line for line in store.history().splitlines() if '[need_user]' in line]
            self.assertEqual(len(lines), 1, '同一句话重复十几次只留第一行')
            store.record_renewal('need_user', '换了一句新的提示')
            lines = [line for line in store.history().splitlines() if '[need_user]' in line]
            self.assertEqual(len(lines), 2)

    def test_a_learned_axis_fills_in_but_never_overrides_the_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            store.save_zone_points('1873', '第二运动场', [
                {'lat': 29.825441, 'lng': 106.428002, 'point_id': '1'},
                {'lat': 29.825990, 'lng': 106.427020, 'point_id': '2'},
                {'lat': 29.825930, 'lng': 106.427876, 'point_id': '3'}])
            self.assertTrue(store.save_zone_axis('1873', 154.4, 0.42))
            tuning = store.zone_tuning('1873')
            self.assertEqual(tuning['axis_deg'], 154.4, '学到的朝向要能补进来')
            store.save_zone_tuning('1873', {'axis_deg': 12.0})
            self.assertEqual(store.zone_tuning('1873')['axis_deg'], 12.0,
                             '用户手填的朝向优先')

    def test_unknown_or_too_few_points_has_no_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            self.assertIsNone(store.zone_summary('nope'))
            store.save_zone_points('1873', 'x', self.POINTS[:2])
            self.assertIsNone(store.zone_summary('1873'))

    def test_corrupt_catalog_degrades_to_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            store.root.mkdir(parents=True, exist_ok=True)
            (store.root / 'zones.json').write_text('{broken', encoding='utf-8')
            self.assertEqual(store.zones(), {})


class CaptureLedgerTests(unittest.TestCase):
    def test_attempts_are_counted_per_day_and_trimmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            at = dt.datetime(2026, 11, 11, 19, 0)
            store.record_capture_attempt(at)
            store.record_capture_attempt(at)
            state = store.capture_state()
            self.assertEqual(state['attempts']['2026-11-11'], 2)
            self.assertEqual(state['last_attempt'], '2026-11-11T19:00:00')
            for day in range(1, 12):
                store.record_capture_attempt(dt.datetime(2026, 10, day, 9, 0))
            self.assertLessEqual(len(store.capture_state()['attempts']), 7,
                                 '只留最近一周, 这个文件不需要历史')

    def test_a_corrupt_ledger_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            (store.root / 'capture.json').write_text('{not json', encoding='utf-8')
            self.assertEqual(store.capture_state()['attempts'], {})
            store.record_capture_attempt(dt.datetime(2026, 11, 11, 9, 0))
            self.assertEqual(store.capture_state()['attempts']['2026-11-11'], 1)


class RenewalLedgerTests(unittest.TestCase):
    """续期台账: 只记时间与结果, 不含凭证; 坏文件不拖垮乐跑区块。"""

    def test_success_sets_next_due_and_clears_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            store.record_renewal('error', '链路故障', next_due='2026-11-11T09:00:00')
            self.assertEqual(store.renewal()['failures'], 1)
            store.record_renewal('renewed', '已自动续期', next_due='2026-11-11T12:00:00',
                                 detail='grab')
            record = store.renewal()
            self.assertEqual(record['state'], 'renewed')
            self.assertEqual(record['failures'], 0)
            self.assertEqual(record['next_due'], '2026-11-11T12:00:00')
            self.assertTrue(record['last_success'])
            self.assertIn('renewed', store.history())
            self.assertNotIn('token', json.dumps(record).lower())

    def test_events_are_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            for index in range(30):
                store.record_renewal('error', f'第 {index} 次失败')
            events = store.renewal()['events']
            self.assertEqual(len(events), 20)
            self.assertIn('第 29 次失败', json.dumps(events, ensure_ascii=False))

    def test_corrupt_ledger_degrades_to_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            store.root.mkdir(parents=True, exist_ok=True)
            (store.root / 'renewal.json').write_text('{broken', encoding='utf-8')
            self.assertEqual(store.renewal(), {})

    def test_record_renewal_never_touches_the_task_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            store.record('signed', '今日跑步已完成')
            store.record_renewal('ok', '凭证可用')
            self.assertEqual(store.status()['state'], 'signed',
                             '续期是维护动作, 不该顶掉今日跑步的状态')
            self.assertNotIn('[ok]', store.history(), '例行探活不写运行记录, 台账里有就行')
            self.assertEqual(store.renewal()['state'], 'ok')

    def test_renewal_writes_history_only_when_it_needs_a_human(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            store.record_renewal('need_user', '凭证已失效：请在小程序里开一次')
            self.assertIn('[need_user]', store.history())
            store.record_renewal('renewed', '已自动续期')
            self.assertIn('[renewed]', store.history())
            store.record_renewal('ok', '凭证可用')
            self.assertEqual(store.history().count('[ok]'), 0)


if __name__ == '__main__':
    unittest.main()
