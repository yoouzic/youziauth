"""乐跑流水线: 干跑零写入、退出码语义、token 保号生命周期。"""
import datetime
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lptiyu import flow as lptiyu_flow
from lptiyu.client import ApiError


def make_template(root: Path) -> str:
    """最小真人模板形态: 与测试跑区同构的 ~3km 闭环。

    前 4 个点就是跑区 4 个锚点(相似变换的源环与目标环逐点对应 → 旋转/尺度 ≈ 1,
    锚点覆盖只剩抖动量), 三条直边 + 一条大弯补足 ~2.95km 周长, 使距离缩放因子 ≈ 1。
    """
    anchors = [(29.8237, 106.4302), (29.8245, 106.4315),
               (29.8231, 106.4318), (29.8223, 106.4305)]

    def detour_points(h):
        """a3 → a0 的外侧大弯; h 的单位是米。"""
        a3, a0 = anchors[3], anchors[0]
        dx, dy = a0[0] - a3[0], a0[1] - a3[1]
        dist_deg = math.hypot(dx, dy)
        px, py = -dy / dist_deg, dx / dist_deg
        bulge_deg = h / 111000.0          # 米 → 度
        points = []
        prev = a3
        length = 0.0
        for i in range(0, 61):
            t = i / 60
            point = (a3[0] + dx * t + px * bulge_deg * math.sin(math.pi * t),
                     a3[1] + dy * t + py * bulge_deg * math.sin(math.pi * t))
            length += math.hypot((point[0] - prev[0]) * 111000.0,
                                 (point[1] - prev[1]) * 111000.0
                                 * math.cos(math.radians(point[0])))
            points.append(point)
            prev = point
        return points, length

    bulge = 100.0
    points, length = detour_points(bulge)
    while length < 2125.0 and bulge < 4000:
        bulge += 10.0
        points, length = detour_points(bulge)

    points_out = list(anchors[:3]) + points
    template = []
    for index, (lat, lng) in enumerate(points_out):
        entry = {'a': lat, 'o': lng, 's': round(3.4 + 0.3 * math.sin(index), 2),
                 'c': '0.05'}
        if index % 13 == 0:
            entry['b'] = 1
        template.append(entry)
    path = root / 'template.json'
    path.write_text(json.dumps(template), encoding='utf-8')
    return str(path)


class FakeSlot:
    def __init__(self, uid=17987537):
        self.uid = uid
        self.label = '测试成员'
        self.school_id = 837
        self.student_num = '222025321102104'
        self.card_id = self.student_num
        self.token = 'TOKEN'
        self.after_calls = 0
        self.dead_calls = 0

    def require_uid(self):
        if not self.uid:
            raise RuntimeError('no uid')
        return self.uid

    def make_client(self, pace=0.0):
        return self.client

    def after_submission(self, client):
        self.after_calls += 1

    def mark_dead(self):
        self.dead_calls += 1


class FakeClient:
    """按脚本回放的假客户端; calls 记录每个端点是否被触碰。"""

    def __init__(self, script):
        self.script = dict(script)
        self.calls = []

    def _play(self, name, default):
        self.calls.append(name)
        result = self.script.get(name, default)
        if isinstance(result, Exception):
            raise result
        return result

    def term_list(self):
        return self._play('term_list', [{'id': 13103}])

    def school_info(self):
        return self._play('school_info', {'school_name': '西南大学'})

    def before_run(self):
        return self._play('before_run', {
            'run_zone_id': 1874, 'run_zone_name': '第一运动场', 'term_id': 13103,
            'run_line_info': {'point_list': [
                {'id': 1, 'jingwei': '29.8237,106.4302'},
                {'id': 2, 'jingwei': '29.8245,106.4315'},
                {'id': 3, 'jingwei': '29.8231,106.4318'},
                {'id': 4, 'jingwei': '29.8223,106.4305'}]},
            'time_rule_arr': [{'min_distance': 2.0, 'max_distance': 10.0}]})

    def set_zone(self, zone_id):
        self.calls.append(('set_zone', zone_id))
        return {'status': 1}

    def term_records(self, term_id, page=1):
        return self._play(('term_records', page), {'list': []})

    def server_time(self):
        record = self.script.get('server_time')
        if record is not None:
            return record
        # 默认给一个落在有效时段内的时间
        today = datetime.date.today()
        return int(datetime.datetime.combine(
            today, datetime.time(7, 0)).timestamp())

    def oss_sts(self):
        return self._play('oss_sts', {'AccessKeyId': 'k', 'AccessKeySecret': 's',
                                      'SecurityToken': 't'})

    def stop_run(self, biz):
        return self._play('stop_run', {'record_id': 'R1', 'status': 1,
                                       'distance': biz['distance']})

    def record_detail(self, record_id, year=None):
        return self._play('record_detail',
                          {'record_status': 1, 'record_failed_reason': '自动确认有效',
                           'distance': 2.31})


def make_flow(slot, policy=None, template_path=''):
    flow = lptiyu_flow.DailyFlow(slot, policy=policy, log_fn=lambda message: None,
                                 template_path=template_path)
    flow._sleep = lambda sec: None          # 测试不等真实间隔
    return flow


class DryRunTests(unittest.TestCase):
    def test_dry_run_never_touches_write_endpoints(self):
        slot = FakeSlot()
        slot.client = FakeClient({})
        flow = make_flow(slot)
        out, code = flow.run(submit=False)
        self.assertEqual(code, 0)
        names = [call for call in slot.client.calls
                 if call not in ('term_list', 'school_info', 'before_run')
                 and call != ('term_records', 1) and call != ('term_records', 2)]
        self.assertNotIn('oss_sts', names)
        self.assertNotIn('stop_run', names)
        self.assertEqual(slot.after_calls, 0)
        self.assertIn('oss_record_file', out)
        self.assertGreater(out['track_points'], 50)

    def test_dry_run_outside_valid_hours_still_runs(self):
        slot = FakeSlot()
        slot.client = FakeClient({'server_time': 0})   # 1970 → 时段外
        flow = make_flow(slot)
        _out, code = flow.run(submit=False)
        self.assertEqual(code, 0)

    def test_dead_token_returns_code_2_and_marks_dead(self):
        slot = FakeSlot()
        slot.client = FakeClient({'term_list': ApiError(101, 'expired')})
        flow = make_flow(slot)
        _out, code = flow.run(submit=False)
        self.assertEqual(code, 2)
        self.assertEqual(slot.dead_calls, 1)

    def test_quota_full_returns_code_3(self):
        slot = FakeSlot()
        today = int(datetime.datetime.combine(datetime.date.today(),
                                              datetime.time(7, 0)).timestamp())
        slot.client = FakeClient({('term_records', 1): {'list': [
            {'start_time': today, 'distance': 2.31}]}})
        flow = make_flow(slot)
        _out, code = flow.run(submit=False)
        self.assertEqual(code, 0)          # 干跑不限额
        flow = make_flow(slot)
        _out, code = flow.run(submit=True)
        self.assertEqual(code, 3)

    def test_ring_style_used_when_template_missing(self):
        slot = FakeSlot()
        slot.client = FakeClient({})
        flow = make_flow(slot, template_path=str(
            Path(tempfile.gettempdir()) / 'no-such-template.json'))
        out, code = flow.run(submit=False)
        self.assertEqual(code, 0)
        self.assertGreater(out['track_points'], 50)


class SubmitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.template = make_template(Path(self._tmp.name))
        # OSS 上传必须打桩: 真实实现会连阿里云, 测试绝不出网。
        patcher = patch('lptiyu.flow.UP.upload_to_oss', return_value=(200, ''))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_successful_submit_keeps_token_for_next_day(self):
        slot = FakeSlot()
        slot.client = FakeClient({})
        flow = make_flow(slot, template_path=self.template)
        summary, code = flow.run(submit=True)
        self.assertEqual(code, 0)
        self.assertEqual(slot.after_calls, 1)
        self.assertEqual(slot.dead_calls, 0)
        self.assertEqual(summary['record_id'], 'R1')

    def test_invalid_judgement_returns_code_5(self):
        slot = FakeSlot()
        slot.client = FakeClient({'record_detail': {
            'record_status': 5, 'record_failed_reason': '打卡点异常',
            'distance': 2.31}})
        flow = make_flow(slot, template_path=self.template)
        _summary, code = flow.run(submit=True)
        self.assertEqual(code, 5)
        self.assertEqual(slot.after_calls, 1)

    def test_stop_run_failure_returns_code_4(self):
        slot = FakeSlot()
        slot.client = FakeClient({'stop_run': {'status': 0}})
        flow = make_flow(slot, template_path=self.template)
        _summary, code = flow.run(submit=True)
        self.assertEqual(code, 4)

    def test_pace_target_shrinks_used_time(self):
        slot = FakeSlot()
        slot.client = FakeClient({})
        flow = make_flow(slot, policy={'pace_target': 240, 'dist_lo': 2.9,
                                       'dist_hi': 3.1},
                         template_path=self.template)
        out, code = flow.run(submit=False)
        self.assertEqual(code, 0)
        used = out['stopRun_biz']['used_time']
        distance = out['stopRun_biz']['distance']
        self.assertTrue(0.95 <= used / (distance * 240) <= 1.05,
                        'pace_target=240 时全程用时应贴近 4\'00"/km')


class PaceTextTests(unittest.TestCase):
    def test_pace_text_formats(self):
        self.assertEqual(lptiyu_flow.pace_text(240), "4'00\"")
        self.assertEqual(lptiyu_flow.pace_text(245), "4'05\"")
        self.assertEqual(lptiyu_flow.pace_text(0), '')
        self.assertEqual(lptiyu_flow.pace_text(None), '')


if __name__ == '__main__':
    unittest.main()
