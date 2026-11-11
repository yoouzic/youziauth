"""轨迹样本导出: 内容完整、坐标可复用、且绝不带账号信息。"""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from lptiyu_share import build_markdown, sample_line, track_axis, zone_passes
from lptiyu_store import Store


class FakeStore:
    def __init__(self, samples, zones=None):
        self._samples = samples
        self._zones = zones or {}

    def samples(self):
        return self._samples

    def zones(self):
        return self._zones


def a_sample(**over):
    base = {
        'at': '2026-11-11T06:31:00', 'record_id': '775156', 'zone_id': '1873',
        'zone_name': '第二运动场', 'distance': 3.01, 'used_time': 1080,
        'record_status': 1, 'record_failed_reason': '', 'style': 'ring',
        'zone_axis_deg': 160.1, 'zone_axis_source': 'prior',
        'zone_points': [{'lat': 29.825441, 'lng': 106.428002},
                        {'lat': 29.825990, 'lng': 106.427020},
                        {'lat': 29.825930, 'lng': 106.427876},
                        {'lat': 29.825292, 'lng': 106.427264}],
        'passed_points': ['29.825500,106.428100', '29.825980,106.427100',
                          '29.825900,106.427900', '29.825300,106.427300'],
    }
    base.update(over)
    return base


class ReportTests(unittest.TestCase):
    def test_it_carries_the_geometry_and_the_run(self):
        store = FakeStore([a_sample()], {'1873': {'name': '第二运动场', 'axis_deg': 160.1,
                                                 'axis_source': 'prior',
                                                 'points': [{'lat': 29.825441, 'lng': 106.428002}]}})
        text = build_markdown(store, version='1.9.7', now=dt.datetime(2026, 11, 11, 21, 0))
        self.assertIn('# 乐跑轨迹样本', text)
        self.assertIn('1.9.7', text)
        self.assertIn('1873 第二运动场', text)
        self.assertIn('29.825441, 106.428002', text)
        self.assertIn('160.1°', text)
        self.assertIn('| 2026-11-11 | 3.01 | 1080 | 1 |', text)
        self.assertIn('29.825500, 106.428100', text, '实际通过点要能被机器读回')

    def test_it_never_leaks_identity_or_token(self):
        store = FakeStore([a_sample(uid=17987537, student_num='222025321102104',
                                    token='SECRET-TOKEN', name='张三')])
        text = build_markdown(store)
        for secret in ('SECRET-TOKEN', '222025321102104', '17987537', '张三'):
            self.assertNotIn(secret, text)

    def test_a_failed_run_is_written_down_too(self):
        store = FakeStore([a_sample(record_status=0, record_failed_reason='未在打卡点打卡')])
        text = build_markdown(store)
        self.assertIn('未在打卡点打卡', text)

    def test_the_capture_log_rides_along(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'capture').mkdir()
            (root / 'capture' / 'capture.log').write_text(
                '[21:00:01] 采集开始 127.0.0.1:5100\n[21:00:09] CONNECT api2.lptiyu.com:443\n',
                encoding='utf-8')
            store = FakeStore([a_sample()])
            store.root = root
            text = build_markdown(store)
            self.assertIn('采集诊断', text)
            self.assertIn('CONNECT api2.lptiyu.com:443', text)

    def test_an_empty_store_says_what_to_do(self):
        text = build_markdown(FakeStore([]))
        self.assertIn('还没有跑区数据', text)

    def test_sample_line_reports_the_worst_checkpoint_gap(self):
        cell = sample_line(a_sample())
        self.assertTrue(cell['worst'].endswith('m'), cell['worst'])
        self.assertEqual(cell['status'], '1')


class TrackAxisTests(unittest.TestCase):
    """从真实通过点估朝向: 点太少不猜, 点够且主方向明确才给值。"""

    def stadium_points(self, axis_deg=15.5, n=30):
        import math
        theta = math.radians(axis_deg)
        pts = []
        for i in range(n):
            x = -150 + 10.0 * i              # 沿长轴每 10m 一个点(约 300m 直道)
            y = 0.4 * math.sin(i)            # 真人轨迹的横向抖动(~0.4m)
            lat = 29.816 + (x * math.cos(theta) - y * math.sin(theta)) / 110570.0
            lng = 106.421 + (x * math.sin(theta) + y * math.cos(theta)) / (
                111320.0 * math.cos(math.radians(29.816)))
            pts.append((round(lat, 6), round(lng, 6)))
        return pts

    def test_four_points_are_not_enough(self):
        deg, share = track_axis([(29.82, 106.42)] * 4)
        self.assertIsNone(deg)
        self.assertEqual(share, 0.0)

    def test_a_straight_run_reports_its_bearing(self):
        deg, share = track_axis(self.stadium_points(15.5))
        self.assertIsNotNone(deg)
        delta = abs(deg - 15.5) % 180
        self.assertLess(min(delta, 180 - delta), 6.0, f'学到 {deg}°, 应接近 15.5°')
        self.assertGreater(share, 0.2)

    def test_zone_passes_aggregate_and_deduplicate(self):
        samples = [
            {'zone_id': '1873', 'passed_points': ['29.8255,106.4281', '29.8256,106.4282']},
            {'zone_id': '1873', 'passed_points': ['29.8255,106.4281', '29.8257,106.4283']},
            {'zone_id': '1875', 'passed_points': ['29.8169,106.4210']},
        ]
        points = zone_passes(samples, '1873')
        self.assertEqual(len(points), 3, '同一点不该重复计入')


class StoreTests(unittest.TestCase):
    def test_samples_are_kept_newest_first_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            self.assertTrue(store.record_sample({'record_id': 'A', 'distance': 3}))
            self.assertFalse(store.record_sample({'record_id': 'A', 'distance': 4}))
            self.assertTrue(store.record_sample({'record_id': 'B', 'distance': 3}))
            rows = store.samples()
            self.assertEqual([row['record_id'] for row in rows], ['B', 'A'])

    def test_the_ledger_is_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            for index in range(Store.MAX_SAMPLES + 5):
                store.record_sample({'record_id': f'R{index}'})
            self.assertEqual(len(store.samples()), Store.MAX_SAMPLES)


if __name__ == '__main__':
    unittest.main()
