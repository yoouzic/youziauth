"""乐跑流水线: 干跑零写入、退出码语义、token 保号生命周期。"""
import datetime
import json
import math
import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from lptiyu import flow as lptiyu_flow
from lptiyu.client import ApiClient, ApiError
from lptiyu.tracks import TemplateShift


def track_winding(track):
    """轨迹绕质心的累计转角(弧度): 正=逆时针, 负=顺时针。独立实现, 不复用引擎。"""
    clat = sum(p['a'] for p in track) / len(track)
    clng = sum(p['o'] for p in track) / len(track)
    k = math.cos(math.radians(clat))
    total, prev = 0.0, None
    for p in track:
        ang = math.atan2(p['a'] - clat, (p['o'] - clng) * k)
        if prev is not None:
            d = ang - prev
            while d > math.pi:
                d -= 2 * math.pi
            while d < -math.pi:
                d += 2 * math.pi
            total += d
        prev = ang
    return total


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


class TermTotalClient(FakeClient):
    def __init__(self, total, script=None):
        super().__init__(script or {})
        self.total = total

    def before_run(self):
        return dict(super().before_run(), total_num=self.total)


def make_flow(slot, policy=None, template_path='', zone_tuning=None, zone_sink=None):
    flow = lptiyu_flow.DailyFlow(slot, policy=policy, log_fn=lambda message: None,
                                 template_path=template_path,
                                 zone_tuning=zone_tuning, zone_sink=zone_sink)
    flow._sleep = lambda sec: None          # 测试不等真实间隔
    return flow


class DryRunTests(unittest.TestCase):
    def test_cancellation_while_connecting_does_not_send_a_request(self):
        client = ApiClient(uid=1, token='synthetic', school_id=1)
        client.stop_event = threading.Event()
        with (
            patch('lptiyu.client.wire.connect') as connect,
            patch('lptiyu.client.wire.post_form') as post,
        ):
            connection = connect.return_value

            def connect_and_cancel(*args, **kwargs):
                client.stop_event.set()
                return connection

            connect.side_effect = connect_and_cancel
            with self.assertRaises(ApiError) as caught:
                client.term_list()
            self.assertEqual(caught.exception.code, 9)
            post.assert_not_called()
            connection.close.assert_called_once()

    def test_term_list_cancellation_while_opening_transport_prevents_post(self):
        client = ApiClient(uid=1, token='synthetic', school_id=1)
        client.stop_event = threading.Event()
        with (
            patch('lptiyu.client.wire.connect') as create_connection,
            patch('lptiyu.client.wire.post_form') as post,
        ):
            connection = create_connection.return_value
            connection.connect.side_effect = client.stop_event.set
            post.return_value.read.return_value = b'[]'
            post.return_value.status = 200
            post.return_value.getheader.return_value = None

            with self.assertRaises(ApiError) as caught:
                client.term_list()

            self.assertEqual(caught.exception.code, 9)
            connection.connect.assert_called_once()
            post.assert_not_called()
            connection.close.assert_called_once()

    def test_cancellation_before_start_prevents_api_calls(self):
        slot = FakeSlot()
        slot.client = FakeClient({})
        flow = make_flow(slot)
        flow.stop_event.set()
        self.assertEqual(flow.run(submit=False), (None, 9))
        self.assertEqual(slot.client.calls, [])

    def test_cancellation_after_first_query_prevents_followup_query(self):
        slot = FakeSlot()
        flow = make_flow(slot)

        class CancelAfterQuery(FakeClient):
            def term_list(self):
                result = super().term_list()
                flow.stop_event.set()
                return result

        slot.client = CancelAfterQuery({})
        self.assertEqual(flow.run(submit=False), (None, 9))
        self.assertEqual(slot.client.calls, ['term_list'])

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


class TermRunLimitTests(unittest.TestCase):
    def test_term_count_query_uses_the_school_aggregate_without_zone_data(self):
        client = FakeClient({'before_run': {'term_id': '13103', 'total_num': '24'}})

        self.assertEqual(lptiyu_flow.query_term_count(client, threading.Event()), 24)
        self.assertEqual(client.calls, ['term_list', 'before_run'])

    def test_term_count_query_falls_back_to_confirmed_records(self):
        client = FakeClient({
            'before_run': {'term_id': '13103', 'total_num': None},
            ('term_records', 1): {'list': [
                {'id': 1, 'record_status': '1', 'distance': '2.3'},
                {'id': 2, 'record_status': 2, 'distance': '2.3'},
            ]},
        })

        self.assertEqual(lptiyu_flow.query_term_count(client, threading.Event()), 1)
        self.assertEqual(client.calls, ['term_list', 'before_run',
                                        ('term_records', 1), ('term_records', 2)])

    def test_reaching_the_limit_uses_the_schools_aggregate_before_writes(self):
        slot = FakeSlot()
        slot.client = TermTotalClient('28')
        flow = make_flow(slot, policy={'term_run_limit': 28})

        summary, code = flow.run(submit=True, zone_override='1875')

        self.assertEqual(code, lptiyu_flow.TERM_QUOTA_REACHED)
        self.assertEqual(summary, {'term_id': '13103', 'term_count': 28,
                                   'term_run_limit': 28})
        self.assertEqual(slot.client.calls, ['term_list', 'school_info', 'before_run'])

    def test_counts_distinct_confirmed_records_across_pages_before_writes(self):
        valid = {'id': 11, 'record_status': 1, 'distance': 2.3}
        second = {'id': 22, 'record_status': '1', 'distance': '2.3'}
        third = {'id': 33, 'record_status': 1, 'distance': 2.4}
        slot = FakeSlot()
        slot.client = FakeClient({
            ('term_records', 1): {'list': [
                valid,
                {'id': 12, 'record_status': 3, 'distance': 2.3},
            ]},
            ('term_records', 2): {'list': [
                dict(valid, id='11'),
                second,
                {'id': 23, 'record_status': 1, 'distance': 2.3,
                 'record_failed_reason': '打卡点异常'},
            ]},
            ('term_records', 3): {'list': [
                third,
                {'id': 34, 'record_status': 0, 'distance': 2.4},
                {'id': 35, 'record_status': 1, 'distance': 0},
            ]},
        })
        flow = make_flow(slot, policy={'term_run_limit': 2})

        summary, code = flow.run(submit=True, zone_override='1875')

        self.assertEqual(code, 7)
        self.assertEqual(summary, {'term_id': '13103', 'term_count': 3,
                                   'term_run_limit': 2})
        self.assertEqual(slot.client.calls, [
            'term_list', 'school_info', 'before_run',
            ('term_records', 1), ('term_records', 2),
            ('term_records', 3), ('term_records', 4),
        ])

    def test_below_limit_still_checks_the_daily_quota(self):
        now = int(datetime.datetime.now().timestamp())
        valid = {'id': 11, 'record_status': 1, 'distance': 2.3, 'start_time': now}
        slot = FakeSlot()
        slot.client = FakeClient({('term_records', 1): {'list': [valid]}})
        flow = make_flow(slot, policy={'term_run_limit': 2})

        self.assertEqual(flow.run(submit=True), (None, 3))
        self.assertNotIn('oss_sts', slot.client.calls)

    def test_dry_run_does_not_query_the_full_term(self):
        slot = FakeSlot()
        slot.client = FakeClient({
            ('term_records', 1): {'list': [
                {'id': 11, 'record_status': 1, 'distance': 2.3},
            ]},
            ('term_records', 2): {'list': [
                {'id': 22, 'record_status': 1, 'distance': 2.3},
            ]},
        })
        flow = make_flow(slot, policy={'term_run_limit': 1})

        self.assertEqual(flow.run(submit=False)[1], 0)
        self.assertNotIn(('term_records', 3), slot.client.calls)

    def test_unknown_count_prevents_writes(self):
        for reply in ({'list': 'invalid'}, {'status': 503, 'list': []},
                      {'list': [{'id': 123, 'record_status': 1}]},
                      {'list': [{'id': 123, 'distance': 2.3}]},
                      OSError('offline'), ApiError(3, 'service rejected')):
            with self.subTest(reply=reply):
                slot = FakeSlot()
                slot.client = FakeClient({('term_records', 1): reply})
                flow = make_flow(slot, policy={'term_run_limit': 28})

                self.assertEqual(flow.run(submit=True, zone_override='1875'), (None, 8))
                self.assertEqual(slot.client.calls[-1], ('term_records', 1))

    def test_repeated_page_prevents_writes(self):
        page = {'list': [{'id': 123, 'record_status': 1, 'distance': 2.3}]}
        slot = FakeSlot()
        slot.client = FakeClient({
            ('term_records', 1): page, ('term_records', 2): page,
        })
        flow = make_flow(slot, policy={'term_run_limit': 28})

        self.assertEqual(flow.run(submit=True, zone_override='1875'), (None, 8))
        self.assertEqual(slot.client.calls[-1], ('term_records', 2))

    def test_expiration_during_count_marks_token_dead(self):
        for reply in (ApiError(101, 'expired'), {'status': '101'}):
            with self.subTest(reply=reply):
                slot = FakeSlot()
                slot.client = FakeClient({('term_records', 1): reply})
                flow = make_flow(slot, policy={'term_run_limit': 28})

                self.assertEqual(flow.run(submit=True, zone_override='1875'), (None, 2))
                self.assertEqual(slot.dead_calls, 1)
                self.assertEqual(slot.client.calls[-1], ('term_records', 1))

    def test_missing_term_id_prevents_writes(self):
        class MissingTerm(TermTotalClient):
            def before_run(self):
                result = super().before_run()
                del result['term_id']
                return result

        slot = FakeSlot()
        slot.client = MissingTerm(28)
        flow = make_flow(slot, policy={'term_run_limit': 28})

        self.assertEqual(flow.run(submit=True, zone_override='1875'), (None, 8))
        self.assertEqual(slot.client.calls, ['term_list', 'school_info', 'before_run'])

    def test_invalid_aggregate_falls_back_to_confirmed_records(self):
        records = {('term_records', 1): {'list': [
            {'id': 11, 'record_status': '1', 'distance': '2.3'},
        ]}}
        for value in (None, -1, False, 1.4, 'invalid'):
            with self.subTest(value=value):
                slot = FakeSlot()
                slot.client = TermTotalClient(value, records)
                flow = make_flow(slot, policy={'term_run_limit': 1})

                summary, code = flow.run(submit=True)

                self.assertEqual(code, lptiyu_flow.TERM_QUOTA_REACHED)
                self.assertEqual(summary['term_count'], 1)
                self.assertIn(('term_records', 2), slot.client.calls)

    def test_cancellation_after_read_prevents_followup_requests(self):
        slot = FakeSlot()
        flow = make_flow(slot, policy={'term_run_limit': 28})

        class CancelOnRecords(FakeClient):
            def term_records(self, term_id, page=1):
                result = super().term_records(term_id, page)
                flow.stop_event.set()
                return result

        slot.client = CancelOnRecords({})

        self.assertEqual(flow.run(submit=True, zone_override='1875'), (None, 9))
        self.assertEqual(slot.client.calls[-1], ('term_records', 1))


class ZoneSwitchClient(FakeClient):
    """模拟服务端跑区状态: setRunZone 成功才换 beforeRun 的点位; refuse=True 时拒绝切换。"""

    CATALOG = {
        '1874': ('第一运动场', [(29.8237, 106.4302), (29.8245, 106.4315),
                               (29.8231, 106.4318), (29.8223, 106.4305)]),
        '1873': ('第二运动场', [(29.8257, 106.4282), (29.8265, 106.4295),
                               (29.8251, 106.4298), (29.8243, 106.4285)]),
    }

    def __init__(self, script=None, refuse=False):
        super().__init__(script or {})
        self.current = '1874'
        self.refuse = refuse

    def before_run(self):
        self.calls.append('before_run')
        name, pts = self.CATALOG[self.current]
        return {'run_zone_id': int(self.current), 'run_zone_name': name,
                'term_id': 13103,
                'run_line_info': {'point_list': [
                    {'id': int(self.current) * 10 + i,
                     'jingwei': f'{lat},{lng}'}
                    for i, (lat, lng) in enumerate(pts)]},
                'time_rule_arr': [{'min_distance': 2.0, 'max_distance': 10.0}]}

    def set_zone(self, zone_id):
        self.calls.append(('set_zone', str(zone_id)))
        if not self.refuse:
            self.current = str(zone_id)
        return {'status': 1}


class ZoneSwitchTests(unittest.TestCase):
    """切换跑区必须真的换掉提交的场地: 服务器拒绝时不得拿旧点位冒充新跑区。"""

    def test_term_change_after_switch_prevents_submission(self):
        class TermChangingClient(ZoneSwitchClient):
            def before_run(self):
                result = super().before_run()
                if self.current == '1873':
                    result['term_id'] = 13104
                return result

        slot = FakeSlot()
        slot.client = TermChangingClient()
        flow = make_flow(slot, policy={'term_run_limit': 28})

        self.assertEqual(flow.run(submit=True, zone_override='1873'),
                         (None, lptiyu_flow.TERM_COUNT_UNAVAILABLE))
        self.assertNotIn('oss_sts', slot.client.calls)
        self.assertNotIn('stop_run', slot.client.calls)

    def test_rechecks_the_aggregate_after_switching_zones(self):
        class TotalChangingClient(ZoneSwitchClient):
            def before_run(self):
                total = 28 if self.current == '1873' else 27
                return dict(super().before_run(), total_num=total)

        slot = FakeSlot()
        slot.client = TotalChangingClient()
        flow = make_flow(slot, policy={'term_run_limit': 28})

        summary, code = flow.run(submit=True, zone_override='1873')

        self.assertEqual(code, lptiyu_flow.TERM_QUOTA_REACHED)
        self.assertEqual(summary['term_count'], 28)
        self.assertEqual(slot.client.calls, [
            'term_list', 'school_info', 'before_run', ('set_zone', '1873'), 'before_run',
        ])

    def test_refused_switch_aborts_before_generating_or_submitting(self):
        slot = FakeSlot()
        slot.client = ZoneSwitchClient(refuse=True)
        flow = make_flow(slot)
        out, code = flow.run(submit=True, zone_override='1873')
        self.assertEqual(code, 4, '切换被拒必须中止, 不能拿旧跑区点位冒充新跑区提交')
        self.assertIsNone(out)
        self.assertNotIn('oss_sts', slot.client.calls)
        self.assertNotIn('stop_run', slot.client.calls)

    def test_successful_switch_submits_the_new_zone_points(self):
        slot = FakeSlot()
        slot.client = ZoneSwitchClient()
        flow = make_flow(slot)
        out, code = flow.run(submit=False, zone_override='1873')
        self.assertEqual(code, 0)
        biz = out['stopRun_biz']
        self.assertEqual(biz['game_id'], '1873')
        self.assertEqual(sorted(row['point_id'] for row in out['log_data']),
                         ['18730', '18731', '18732', '18733'],
                         '打卡点必须来自新跑区, 而不是只改 game_id')


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

    def test_aggregate_below_28_only_needs_the_daily_query(self):
        for total in (27, 0, '0'):
            with self.subTest(total=total):
                slot = FakeSlot()
                slot.client = TermTotalClient(total)
                flow = make_flow(slot, policy={'term_run_limit': 28}, template_path=self.template)

                summary, code = flow.run(submit=True)

                self.assertEqual(code, 0)
                self.assertEqual(summary['record_id'], 'R1')
                self.assertEqual(slot.client.calls.count(('term_records', 1)), 1)

    def test_27_valid_historical_runs_still_allow_one_submission_under_28_limit(self):
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        start_time = int(datetime.datetime.combine(yesterday, datetime.time(7)).timestamp())
        records = [
            {'id': number, 'record_status': 1, 'distance': 2.31, 'start_time': start_time}
            for number in range(1, 28)
        ]
        records.append({'id': 28, 'record_status': 2, 'distance': 2.31, 'start_time': start_time})
        slot = FakeSlot()
        slot.client = FakeClient({
            ('term_records', page): {'list': records[start:start + 10]}
            for page, start in enumerate(range(0, len(records), 10), 1)
        })
        flow = make_flow(slot, policy={'term_run_limit': 28}, template_path=self.template)

        summary, code = flow.run(submit=True)

        self.assertEqual(code, 0)
        self.assertEqual(summary['record_id'], 'R1')
        self.assertIn(('term_records', 3), slot.client.calls)
        self.assertIn('stop_run', slot.client.calls)

    def test_cancelling_after_submission_keeps_the_record_id(self):
        slot = FakeSlot()
        slot.client = FakeClient({})
        flow = make_flow(slot, template_path=self.template)

        def cancel_when_acknowledged(stage, _message):
            if stage == 4:
                flow.stop_event.set()

        flow.progress_fn = cancel_when_acknowledged
        summary, code = flow.run(submit=True)
        self.assertEqual(code, lptiyu_flow.SUBMITTED_REVIEW_CANCELLED)
        self.assertEqual(summary['record_id'], 'R1')
        self.assertNotIn('record_detail', slot.client.calls)
        self.assertEqual(slot.after_calls, 0)

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

    def test_pace_window_lands_inside_the_range(self):
        slot = FakeSlot()
        slot.client = FakeClient({})
        flow = make_flow(slot, policy={'pace_lo': 245, 'pace_hi': 255, 'dist_lo': 2.9,
                                       'dist_hi': 3.1},
                         template_path=self.template)
        out, code = flow.run(submit=False)
        self.assertEqual(code, 0)
        used = out['stopRun_biz']['used_time']
        distance = out['stopRun_biz']['distance']
        pace = used / distance
        self.assertTrue(245 <= pace <= 255,
                        f'配速区间 4\'05"–4\'15" 要真的落在区间里, 实际 {pace:.1f}s/km')

    def test_ring_fallback_still_honours_the_pace_window(self):
        # 没有模板时走 ring: 引擎有自己的速度曲线, 靠缩放时间轴把配速拉进区间。
        slot = FakeSlot()
        slot.client = FakeClient({})
        flow = make_flow(slot, policy={'pace_lo': 300, 'pace_hi': 320, 'dist_lo': 2.9,
                                       'dist_hi': 3.1})
        out, code = flow.run(submit=False)
        self.assertEqual(code, 0)
        used = out['stopRun_biz']['used_time']
        distance = out['stopRun_biz']['distance']
        self.assertTrue(300 <= used / distance <= 320,
                        f'ring 路径也要按区间跑, 实际 {used / distance:.1f}s/km')
        start = out['stopRun_biz']['start_time']
        self.assertLessEqual(max(row['time'] for row in out['log_data']), start + used,
                             '缩放后日志时间不能超出本次用时')


class ZoneProfileTests(unittest.TestCase):
    """跑区特调进流水线: 点位留档、场地几何进日志、方向真的按跑区走。"""

    ANCHORS = [(29.8237, 106.4302, '1'), (29.8245, 106.4315, '2'),
               (29.8231, 106.4318, '3'), (29.8223, 106.4305, '4')]

    def test_live_points_are_reported_to_the_sink(self):
        slot = FakeSlot()
        slot.client = FakeClient({})
        seen = []
        flow = make_flow(slot, policy={'dist_lo': 2.9, 'dist_hi': 3.1},
                         zone_tuning={'direction': 'cw', 'anchor_m': 15, 'lane_m': 6},
                         zone_sink=lambda *args: seen.append(args))
        out, code = flow.run(submit=False)
        self.assertEqual(code, 0)
        self.assertTrue(seen, '点位与跑区清单要交给调用方留档')
        zone_id, zone_name, points, zone_list = seen[0]
        self.assertEqual(zone_id, '1874')
        self.assertEqual(len(points), 4)
        self.assertEqual(points[0]['id'], '1')
        self.assertIsInstance(zone_list, list)
        self.assertTrue(any('场地' in line or '长轴' in line for line in flow.lines),
                        '这次跑的是哪块场地、什么朝向, 日志里要看得见')

    def test_sink_failure_never_blocks_the_run(self):
        slot = FakeSlot()
        slot.client = FakeClient({})

        def broken(*_args):
            raise OSError('disk full')

        flow = make_flow(slot, policy={'dist_lo': 2.9, 'dist_hi': 3.1},
                         zone_tuning={}, zone_sink=broken)
        _out, code = flow.run(submit=False)
        self.assertEqual(code, 0, '留档失败不该拦住跑步')

    def test_ring_track_follows_the_zone_direction(self):
        from lptiyu import tracks

        def winding(track):
            clat = sum(p['a'] for p in track) / len(track)
            clng = sum(p['o'] for p in track) / len(track)
            total, prev = 0.0, None
            for point in track:
                angle = math.atan2(point['a'] - clat,
                                   (point['o'] - clng) * math.cos(math.radians(clat)))
                if prev is not None:
                    total += (angle - prev + math.pi) % (2 * math.pi) - math.pi
                prev = angle
            return total

        ccw = tracks.ring_track(self.ANCHORS, 2000, random.Random(7),
                                profile={'direction': 'ccw', 'lane_m': 5})
        cw = tracks.ring_track(self.ANCHORS, 2000, random.Random(7),
                               profile={'direction': 'cw', 'lane_m': 5})
        self.assertGreater(winding(ccw[0]), 0, 'ccw 跑区必须逆时针')
        self.assertLess(winding(cw[0]), 0, 'cw 跑区必须顺时针')
        self.assertEqual(sorted(ccw[1]), sorted(cw[1]), '两个方向都要经过全部打卡点')

    def test_zone_lane_keeps_the_field_clear_of_the_anchors(self):
        from lptiyu import tracks

        track, crossings, _used, _dist = tracks.ring_track(
            self.ANCHORS, 1600, random.Random(3),
            profile={'direction': 'ccw', 'lane_m': 9})
        self.assertEqual(sorted(crossings), ['1', '2', '3', '4'])
        for lat, lng, _pid in self.ANCHORS:
            nearest = min(tracks.haversine_m(p['a'], p['o'], lat, lng) for p in track)
            self.assertLess(nearest, 20.0, '再大的车道也要靠到打卡点')


class TemplateScaleTests(unittest.TestCase):
    """模板迁移的各向异性缩放: 场地长宽比不同时不能只用一把尺子。"""

    def test_scales_differ_when_the_target_is_squarer(self):
        from lptiyu.tracks import TemplateShift, geo_of

        lat0, lng0 = 29.82, 106.43

        def box(width_m, height_m):
            return [geo_of(-width_m / 2, -height_m / 2, lat0, lng0),
                    geo_of(width_m / 2, -height_m / 2, lat0, lng0),
                    geo_of(width_m / 2, height_m / 2, lat0, lng0),
                    geo_of(-width_m / 2, height_m / 2, lat0, lng0)]

        shift = TemplateShift(anchors=[], template=[{'a': lat0, 'o': lng0,
                                                     's': 2.6, 'c': '0.05'}])
        pairs = list(zip(box(100, 50), box(100, 100)))
        sx, sy = shift._axis_scales(pairs, 0.0, 1.0, (lat0, lng0), (lat0, lng0))
        self.assertGreater(abs(sx - sy), 0.2, '长宽比不同就该两个方向分别缩放')
        self.assertLessEqual(max(sx, sy), 1.6 + 1e-6, '缩放比要有上限, 免得把场地拉变形')

    def test_generate_reports_both_scales(self):
        slot = FakeSlot()
        slot.client = FakeClient({})
        flow = make_flow(slot, policy={'dist_lo': 2.9, 'dist_hi': 3.1})
        # 本测试只关心双尺度上报; 贴点放到判定半径40m, 免得随机距离缩放耗尽重试落到 ring
        flow.zone_profile = {'direction': 'ccw', 'lane_m': 4.0, 'anchor_m': 40.0}
        # 用现成的迷你模板走一遍真实模板路径
        with tempfile.TemporaryDirectory() as tmp:
            flow.template_path = make_template(Path(tmp))
            out, code = flow.run(submit=False)
        self.assertEqual(code, 0)
        scale_xy = out['meta'].get('scale_xy')
        self.assertIsInstance(scale_xy, tuple)
        self.assertEqual(len(scale_xy), 2, '两个方向的缩放都要报出来')


class TemplateDirectionTests(unittest.TestCase):
    """模板绕向要服从跑区特调: 场地要求顺时针时, 逆时针模板必须镜像过来。"""

    ANCHORS = [{'point_id': '1', 'latitude': 29.8237, 'longtitude': 106.4302},
               {'point_id': '2', 'latitude': 29.8245, 'longtitude': 106.4315},
               {'point_id': '3', 'latitude': 29.8231, 'longtitude': 106.4318},
               {'point_id': '4', 'latitude': 29.8223, 'longtitude': 106.4305}]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.template_path = make_template(Path(self._tmp.name))

    def test_cw_zone_mirrors_the_ccw_template(self):
        gen = TemplateShift(self.ANCHORS, distance_km=2.95, seed=7,
                            template_path=self.template_path,
                            profile={'direction': 'cw'})
        track, meta = gen.generate(return_meta=True)
        self.assertLess(track_winding(track), 0,
                        '顺时针跑区必须生成顺时针轨迹, 不能沿用模板的逆时针')
        self.assertTrue(meta.get('mirrored'), '镜像发生了就要在 meta 里看得见')
        for anchor in self.ANCHORS:
            nearest = min(lptiyu_flow.TRK.haversine_m(
                p['a'], p['o'], anchor['latitude'], anchor['longtitude'])
                for p in track)
            self.assertLess(nearest, 20.0,
                            '镜像之后仍要贴住每个打卡点(默认验收半径20m, 判定半径40m)')

    def test_matching_direction_keeps_the_template_unmirrored(self):
        gen = TemplateShift(self.ANCHORS, distance_km=2.95, seed=7,
                            template_path=self.template_path,
                            profile={'direction': 'ccw'})
        track, meta = gen.generate(return_meta=True)
        self.assertGreater(track_winding(track), 0)
        self.assertFalse(meta.get('mirrored'))

    def test_flow_honours_zone_direction_with_template_engine(self):
        slot = FakeSlot()
        slot.client = FakeClient({})
        flow = make_flow(slot, policy={'dist_lo': 2.9, 'dist_hi': 3.1},
                         template_path=self.template_path,
                         zone_tuning={'direction': 'cw'})
        out, code = flow.run(submit=False)
        self.assertEqual(code, 0)
        self.assertLess(track_winding(out['track']), 0,
                        '跑区特调的方向必须真的落到模板引擎生成的轨迹上')


class PaceTextTests(unittest.TestCase):
    def test_pace_text_formats(self):
        self.assertEqual(lptiyu_flow.pace_text(240), "4'00\"")
        self.assertEqual(lptiyu_flow.pace_text(245), "4'05\"")
        self.assertEqual(lptiyu_flow.pace_text(0), '')
        self.assertEqual(lptiyu_flow.pace_text(None), '')


class LinkFailureFlagTests(unittest.TestCase):
    """link_failure 只标「提交之前就断了」: 那时重跑是安全的。

    提交(POST stopRun)发出去之后的断链不算 —— 记录可能已经落库, 再跑一次就是
    重复提交。这个区别决定了面板敢不敢把这一拍当成「等网回来重试」。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.template = make_template(Path(self._tmp.name))
        patcher = patch('lptiyu.flow.UP.upload_to_oss', return_value=(200, ''))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_oserror_before_submit_marks_the_link_failure(self):
        slot = FakeSlot()
        slot.client = FakeClient({'before_run': OSError(11001, 'getaddrinfo failed')})
        flow = make_flow(slot, template_path=self.template)

        self.assertEqual(flow.run(submit=True), (None, 4))
        self.assertTrue(flow.link_failure)

    def test_oserror_after_submit_is_not_an_outage(self):
        slot = FakeSlot()
        slot.client = FakeClient({'stop_run': OSError(10054, 'connection reset')})
        flow = make_flow(slot, template_path=self.template)

        self.assertEqual(flow.run(submit=True), (None, 4))
        self.assertFalse(flow.link_failure, '提交已发出去, 不能当作「没网, 再跑一次」')

    def test_flag_is_cleared_on_the_next_successful_run(self):
        slot = FakeSlot()
        slot.client = FakeClient({'before_run': OSError('offline')})
        flow = make_flow(slot, template_path=self.template)
        flow.run(submit=True)
        self.assertTrue(flow.link_failure)

        slot.client = FakeClient({})
        self.assertEqual(flow.run(submit=True)[1], 0)
        self.assertFalse(flow.link_failure, '下一拍正常了就不能还挂着上一拍的断网标记')


if __name__ == '__main__':
    unittest.main()
