"""乐跑存储层: 设置、凭证加密、状态历史、每日计数、研究库同步。"""
import json
import tempfile
import unittest
from pathlib import Path

from lptiyu_store import Settings, Store


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

    def test_sync_without_lab_reports_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            changes, message = store.sync_from_lab()
            self.assertEqual(changes, [])
            self.assertIn('未找到', message)


if __name__ == '__main__':
    unittest.main()
