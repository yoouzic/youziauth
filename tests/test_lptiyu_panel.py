"""乐跑控制器: 忙闸、自动流水线门控、结果状态映射、凭证保号与续期接入。

全部网络面打桩: _probe_alive 钉死为存活, subprocess 钉死, 续期器换成假的 ——
单测绝不真连服务器, 也绝不触碰本机研究库(这台机器上研究库真实存在)。
续期本身的阶梯/配额/冷却在 test_lptiyu_renew.py 里测。
"""
import datetime as dt
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import lptiyu_panel
from lptiyu_panel import LptiyuController, LptiyuError, TokenSlot, MAX_DAILY_AUTO_ATTEMPTS
from lptiyu.client import ApiError
from lptiyu_renew import CredentialRenewer, RenewOutcome
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


class FakeRenewer:
    """续期器替身: 决定「此刻到不到期」与「续期结果」, 绝不碰网络。"""

    def __init__(self, due=False, outcome=None):
        self.is_due = due
        self.outcome = outcome or RenewOutcome('ok', '凭证可用')
        self.calls = []

    def due(self, settings=None):
        self.calls.append('due')
        return self.is_due

    def renew(self, force=False):
        self.calls.append(('renew', force))
        return self.outcome


class PanelTestCase(unittest.TestCase):
    """共享的隔离环境: 临时存储 + 无网探测 + 不会触发的采集 + 假续期器。"""

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
        self.renewer = FakeRenewer()
        probe = patch.object(LptiyuController, '_probe_alive', lambda self, client: True)
        grab = patch('lptiyu_panel.subprocess.run')
        lab = patch.object(Store, 'lab_dir', lambda self: '')
        probe.start(); grab.start(); lab.start()
        self.addCleanup(probe.stop); self.addCleanup(grab.stop); self.addCleanup(lab.stop)

    def make_controller(self, code=0, settings=None, clock=None, renewer=None,
                        capture=None, open_applet=None):
        if settings is not None:
            self.store.save_settings(settings)
        if renewer is not None:
            self.renewer = renewer

        def factory(slot, flow_settings):
            flow = FakeFlow(code)
            self.flows.append(flow)
            return flow

        controller = LptiyuController(store=self.store, clock=clock or CLOCK,
                                      flow_factory=factory, renewer=self.renewer,
                                      capture=capture,
                                      open_applet=open_applet or (lambda: True))
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
        # 这条要看真续期器的文案: 假续期器只会回放预设结果。
        controller.renewer = CredentialRenewer(
            controller.store, clock=CLOCK,
            probe=controller._probe_record, grab=controller._grab_via_lab)
        with self.assertRaises(LptiyuError) as caught:
            controller._execute(submit=False)
        self.assertEqual(caught.exception.state, 'need_token')
        self.assertIn('研究库', caught.exception.message,
                      '没有采集组件时要直说, 不能让人去点必然失败的「同步凭证」')


class PollTests(PanelTestCase):
    def test_poll_fires_after_run_time_when_enabled(self):
        started = threading.Event()
        actions = []
        original = LptiyuController.start

        def spy(self, action):
            actions.append(action)
            if action == 'run':
                started.set()
            return original(self, action)

        controller = self.make_controller(settings=Settings(enabled=True, run_time='06:30'))
        with patch.object(LptiyuController, 'start', spy):
            controller.poll()
        self.assertTrue(started.wait(3.0), '到点且开启时必须自动起跑')
        controller._wait_for_work()
        self.assertEqual(actions, ['run'])

    def test_poll_renews_before_the_run_when_due(self):
        started = threading.Event()
        actions = []
        original = LptiyuController.start

        def spy(self, action):
            actions.append(action)
            if action == 'renew_auto':
                started.set()
            return original(self, action)

        controller = self.make_controller(settings=Settings(enabled=True, run_time='06:30'),
                                          renewer=FakeRenewer(due=True))
        with patch.object(LptiyuController, 'start', spy):
            controller.poll()
        self.assertTrue(started.wait(3.0), '凭证到期时必须先保活')
        controller._wait_for_work()
        self.assertEqual(actions, ['renew_auto'], '保活那一拍不先起跑')

    def test_poll_skips_before_run_time(self):
        controller = self.make_controller(settings=Settings(enabled=True, run_time='08:00'))
        controller.poll()
        controller._wait_for_work()
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


class RenewActionTests(PanelTestCase):
    """手动/后台续期在控制器这一层的映射: 成功不打扰, 要人动手才冒泡。"""

    def test_manual_renew_maps_ok_to_token_ok(self):
        controller = self.make_controller(
            renewer=FakeRenewer(outcome=RenewOutcome('ok', '凭证可用')))
        result = controller._renew()
        self.assertEqual(result['state'], 'token_ok')
        self.assertEqual(result['message'], '凭证可用')

    def test_manual_renew_asks_the_user_when_needed(self):
        controller = self.make_controller(
            renewer=FakeRenewer(outcome=RenewOutcome('need_user', '请在小程序里开一次')))
        self.assertEqual(controller._renew()['state'], 'need_token')

    def test_manual_renew_forces_past_the_ledger(self):
        renewer = FakeRenewer()
        controller = self.make_controller(renewer=renewer)
        controller._renew()
        self.assertIn(('renew', True), renewer.calls)

    def test_auto_renew_stays_quiet_when_healthy(self):
        renewer = FakeRenewer(outcome=RenewOutcome('ok', '凭证可用'))
        controller = self.make_controller(renewer=renewer)
        before = controller.store.status()
        self.assertIsNone(controller._renew_auto())
        self.assertIn(('renew', False), renewer.calls)
        self.assertEqual(controller.store.status(), before,
                         '续期成功不该把今日跑步的状态顶掉')

    def test_auto_renew_bubbles_up_when_it_needs_the_user(self):
        controller = self.make_controller(
            renewer=FakeRenewer(outcome=RenewOutcome('need_user', '请在小程序里开一次')))
        self.assertEqual(controller._renew_auto()['state'], 'need_token')

    def test_auto_renew_skips_the_day_that_already_ran(self):
        renewer = FakeRenewer(outcome=RenewOutcome('need_user', 'x'))
        controller = self.make_controller(renewer=renewer)
        controller.store.mark_signed(controller._clock().date().isoformat())
        self.assertIsNone(controller._renew_auto())
        self.assertNotIn(('renew', False), renewer.calls)


class ProbePolicyTests(PanelTestCase):
    """提交后探测: 只有 101 才是死亡判据。"""

    def test_non_101_error_keeps_the_token(self):
        controller = self.make_controller()

        class Flaky:
            def term_list(self):
                raise ApiError(4, 'transient')

        controller.slot.after_submission(Flaky())
        self.assertTrue(controller.store.has_token(), '链路抖动不该丢掉手上的凭证')

    def test_101_clears_the_token(self):
        controller = self.make_controller()

        class Dead:
            def term_list(self):
                raise ApiError(101, 'dead')

        controller.slot.after_submission(Dead())
        self.assertFalse(controller.store.has_token())


class FakeZoneClient:
    """切跑区 + 读点位的最小替身; 记下每次 setRunZone。"""

    BASE = [(29.8237, 106.4302), (29.8245, 106.4315),
            (29.8231, 106.4318), (29.8223, 106.4305)]

    def __init__(self, zones, current=None):
        self.zones = zones
        self.current = current or zones[0]['id']
        self.set_calls = []

    def set_zone(self, zone_id):
        self.set_calls.append(zone_id)
        self.current = zone_id
        return {'status': 1}

    def before_run(self):
        name = next(zone['name'] for zone in self.zones if zone['id'] == self.current)
        return {'run_zone_id': self.current, 'run_zone_name': name,
                'run_zone_list': self.zones,
                'run_line_info': {'point_list': [
                    {'id': index + 1, 'jingwei': f'{lat},{lng}'}
                    for index, (lat, lng) in enumerate(self.BASE)]},
                'time_rule_arr': [{'min_distance': 2.0, 'max_distance': 10.0}]}


class ZoneApplyTests(PanelTestCase):
    """我选的跑区要推给服务端: 小程序那边也得切过去, 否则下次还跑旧场地。"""

    def apply(self, chosen, current):
        controller = self.make_controller(
            settings=Settings(enabled=True, zone_id=chosen))
        client = FakeZoneClient(ZoneSyncTests.ZONES, current=current)
        with patch.object(TokenSlot, 'make_client', lambda self, pace=0: client), \
                patch.object(LptiyuController, '_ensure_credentials',
                             lambda self: {'token': 'T'}):
            result = controller._apply_zone()
        return controller, client, result

    def test_it_switches_the_server_to_my_choice(self):
        controller, client, result = self.apply('1874', '1873')
        self.assertIn('1874', client.set_calls, '要把 1874 推上去')
        self.assertEqual(client.current, '1874')
        self.assertEqual(result['state'], 'zones')
        self.assertIn('第一运动场', result['message'])
        self.assertEqual(controller.store.settings().zone_id, '1874')

    def test_a_zone_already_in_place_is_not_switched_again(self):
        _controller, client, _result = self.apply('1874', '1874')
        self.assertEqual(client.set_calls, [], '已经在这块场地就别多余切一次')

    def test_a_refused_switch_is_reported(self):
        controller = self.make_controller(settings=Settings(enabled=True, zone_id='1874'))

        class Stubborn(FakeZoneClient):
            def set_zone(self, zone_id):
                self.set_calls.append(zone_id)
                return {'status': 1}          # 假装成功, 但 current 不变

        client = Stubborn(ZoneSyncTests.ZONES, current='1873')
        with patch.object(TokenSlot, 'make_client', lambda self, pace=0: client), \
                patch.object(LptiyuController, '_ensure_credentials',
                             lambda self: {'token': 'T'}):
            with self.assertRaises(LptiyuError) as caught:
                controller._apply_zone()
        self.assertIn('切换没生效', caught.exception.message)

    def test_without_a_choice_it_says_so(self):
        controller = self.make_controller(settings=Settings(enabled=True))
        with self.assertRaises(LptiyuError) as caught:
            controller._apply_zone()
        self.assertIn('先', caught.exception.message)


class ZoneSyncAdoptsTests(PanelTestCase):
    def test_syncing_keeps_the_zone_i_chose(self):
        # 用户选过场地就以他为准: 同步顺手把服务器切回他选的那块, 不能反被
        # 小程序里的旧场地带跑。
        controller = self.make_controller(
            settings=Settings(enabled=True, zone_id='1873', zone_name='第二运动场'))
        client = FakeZoneClient(ZoneSyncTests.ZONES, current='1874')
        with patch.object(TokenSlot, 'make_client', lambda self, pace=0: client):
            controller._sync_zones()
        current = controller.store.settings()
        self.assertEqual(current.zone_id, '1873', '我选的 1873 不能被同步改掉')
        self.assertEqual(client.current, '1873', '收尾要还原到我选的那块')

    def test_syncing_follows_the_server_when_i_never_chose(self):
        controller = self.make_controller(settings=Settings(enabled=True))
        client = FakeZoneClient(ZoneSyncTests.ZONES, current='1874')
        with patch.object(TokenSlot, 'make_client', lambda self, pace=0: client):
            controller._sync_zones()
        current = controller.store.settings()
        self.assertEqual(current.zone_id, '1874', '没选过就跟服务器走')
        self.assertEqual(current.zone_name, '第一运动场')

    def test_the_sink_writes_the_selection_too(self):
        controller = self.make_controller()
        controller._zone_sink('1875', '第四运动场',
                              [{'lat': 29.81, 'lng': 106.42}], [])
        self.assertEqual(controller.store.settings().zone_id, '1875')
        self.assertEqual(controller.store.settings().zone_name, '第四运动场')


class ZoneSyncTests(PanelTestCase):
    ZONES = [{'id': '1873', 'name': '第二运动场', 'is_default': 0},
             {'id': '1874', 'name': '第一运动场', 'is_default': 1},
             {'id': '1876', 'name': '南区操场', 'is_default': 0}]

    def test_sync_collects_every_field_and_restores_the_choice(self):
        controller = self.make_controller(settings=Settings(enabled=True, zone_id='1873'))
        client = FakeZoneClient(self.ZONES)
        with patch.object(TokenSlot, 'make_client', lambda self, pace=0: client):
            result = controller._sync_zones()
        self.assertEqual(result['state'], 'zones')
        self.assertIn('3', result['message'])
        catalog = self.store.zones()
        self.assertEqual(set(catalog), {'1873', '1874', '1876'})
        for zone_id, entry in catalog.items():
            self.assertEqual(len(entry['points']), 4, zone_id)
        self.assertEqual(client.set_calls[-1], '1873', '同步完要把账号跑区切回用户选的')
        self.assertEqual(self.store.settings().zone_id, '1873')

    def test_sink_writes_points_without_touching_the_selection(self):
        controller = self.make_controller(settings=Settings(enabled=True, zone_id='1874'))
        controller._zone_sink('1874', '第一运动场',
                              [{'id': '1', 'lat': 29.8237, 'lng': 106.4302},
                               {'id': '2', 'lat': 29.8245, 'lng': 106.4315},
                               {'id': '3', 'lat': 29.8231, 'lng': 106.4318}],
                              self.ZONES)
        catalog = self.store.zones()
        self.assertEqual(len(catalog['1874']['points']), 3)
        self.assertEqual(self.store.settings().zone_id, '1874')
        self.assertIsNotNone(self.store.zone_summary('1874'))

    def test_sink_swallows_a_broken_catalog(self):
        controller = self.make_controller()
        with patch.object(Store, 'save_zone_points', side_effect=OSError('disk')):
            controller._zone_sink('1874', 'x', [{'id': '1', 'lat': 1.0, 'lng': 2.0}], [])


class FakeCaptureCA:
    def __init__(self, installed=False):
        self.installed_flag = installed
        self.install_calls = 0
        self.remove_calls = 0

    def installed(self):
        return self.installed_flag

    def fingerprint(self):
        return "ABCDEF" * 6 + "ABCD"

    def install(self):
        self.install_calls += 1
        self.installed_flag = True
        return {"ok": True, "detail": "证书已装入当前用户"}

    def remove(self):
        self.remove_calls += 1
        self.installed_flag = False
        return {"ok": True, "detail": "证书已从当前用户卸载"}


class FakeCapture:
    def __init__(self, cred=None, installed=True, final=None):
        self.cred = cred
        self.ca = FakeCaptureCA(installed)
        self.final = final or {}
        self.started = 0
        self.stops = []
        self.logs = []
        self.state = "idle"
        self.message = ""
        self.connects = 0

    def log(self, message):
        self.logs.append(message)
        self.state = "idle"
        self.message = ""

    def start(self):
        from lptiyu_capture import CaptureError
        if not self.ca.installed():
            raise CaptureError("还没有安装采集证书：先点「安装采集证书」，再开始采集")
        self.started += 1
        self.state, self.message = "waiting", "等待小程序登录"

    def wait(self, timeout=None, on_tick=None):
        if on_tick is not None:
            on_tick()
        self.state = self.final.get("state", "captured" if self.cred else "timeout")
        self.message = self.final.get("message", "等待超时")
        return self.cred

    def stop(self, state=None, message=None):
        self.stops.append((state, message))
        self.state = state or "idle"
        self.message = message or ""

    def status(self):
        return {"state": self.state, "message": self.message,
                "port": 18888, "remaining_s": 42}

    def cert_status(self):
        return {"installed": self.ca.installed(),
                "fingerprint": self.ca.fingerprint()}


class DiagnoseTests(PanelTestCase):
    def diagnose(self, installed=True, running=True, proxy_ok=True, connects=0):
        controller = self.make_controller(capture=FakeCapture(installed=installed))
        controller.capture.connects = connects
        with patch.object(lptiyu_panel, 'wechat_protocol_registered', lambda: installed), \
                patch.object(lptiyu_panel, 'wechat_installed', lambda: installed), \
                patch.object(lptiyu_panel, '_wechat_running', lambda: running), \
                patch('lptiyu_capture.proxy_self_test',
                      lambda ca, **_kw: (proxy_ok, '代理链可用（200）')):
            return controller.capture_diagnose()

    def test_a_missing_wechat_is_the_verdict(self):
        report = self.diagnose(installed=False)
        self.assertIn('没有找到微信', report['verdict'])
        self.assertFalse(report['wechat_installed'])

    def test_wechat_not_running_is_the_verdict(self):
        report = self.diagnose(running=False)
        self.assertIn('没在运行', report['verdict'])

    def test_a_broken_proxy_chain_is_the_verdict(self):
        report = self.diagnose(proxy_ok=False)
        self.assertIn('代理链不通', report['verdict'])

    def test_zero_connections_with_a_healthy_chain_points_at_wechat(self):
        report = self.diagnose(connects=0)
        self.assertIn('没把流量交出来', report['verdict'])

    def test_a_healthy_chain_with_traffic_says_capture_is_fine(self):
        report = self.diagnose(connects=3)
        self.assertIn('采集链正常', report['verdict'])


class AutoCaptureTests(PanelTestCase):
    CRED = {"token": "SELF-HEALED", "uid": 17980002, "student_num": "2025003",
            "school_id": "837", "card_id": "2025003",
            "issued_at": "2026-11-11T19:00:00", "source": "capture"}

    def test_auto_capture_needs_the_certificate(self):
        controller = self.make_controller(capture=FakeCapture(installed=False))
        self.assertFalse(controller.capture_ready())
        self.assertFalse(controller.auto_capture())

    def test_auto_capture_runs_without_being_asked(self):
        capture = FakeCapture(cred=self.CRED, installed=True)
        controller = self.make_controller(capture=capture, open_applet=lambda: True)
        self.assertTrue(controller.auto_capture())
        controller._wait_for_work()
        self.assertEqual(capture.started, 1)
        self.assertEqual(controller.store.token(), "SELF-HEALED")
        today = CLOCK().date().isoformat()
        self.assertEqual(controller.store.capture_state()['attempts'][today], 1)

    def test_auto_capture_respects_its_cooldown(self):
        capture = FakeCapture(cred=self.CRED, installed=True)
        controller = self.make_controller(capture=capture, open_applet=lambda: True)
        self.assertTrue(controller.auto_capture())
        controller._wait_for_work()
        self.assertFalse(controller.auto_capture(), '20 分钟内不该反复弹小程序')

    def test_auto_capture_stops_at_the_daily_cap(self):
        controller = self.make_controller(capture=FakeCapture(installed=True))
        today = CLOCK().date().isoformat()
        for _ in range(3):
            controller.store.record_capture_attempt(CLOCK())
        self.assertEqual(controller.store.capture_state()['attempts'][today], 3)
        self.assertFalse(controller._auto_capture_due())

    def test_a_missing_token_asks_for_a_silent_capture(self):
        controller = self.make_controller(capture=FakeCapture(installed=True))
        controller.store.clear_token()
        with self.assertRaises(LptiyuError) as caught:
            controller._execute(submit=False)
        self.assertEqual(caught.exception.state, 'need_token')
        self.assertIn('自动采集', caught.exception.message)
        self.assertTrue(controller._capture_requested)

    def test_poll_picks_the_request_up_by_itself(self):
        capture = FakeCapture(cred=self.CRED, installed=True)
        controller = self.make_controller(capture=capture, open_applet=lambda: True)
        controller._capture_requested = True
        controller.poll()
        controller._wait_for_work()
        self.assertEqual(capture.started, 1, '下一拍就该把采集接过来')


class MiniProgramTests(unittest.TestCase):
    def test_a_missing_wechat_is_explained(self):
        with patch.object(lptiyu_panel, 'wechat_installed', lambda: False):
            self.assertFalse(lptiyu_panel.open_mini_program('weixin://x'))
        self.assertIn('没有找到微信', lptiyu_panel.LAST_LAUNCH_REASON)

class MiniProgramTests(unittest.TestCase):
    def test_launcher_reports_whether_it_started(self):
        # 先假装微信装着: 构建机上没有 weixin://, 探针会直接返回 False
        with patch.object(lptiyu_panel, 'wechat_installed', lambda: True), \
                patch.object(lptiyu_panel.os, 'startfile') as started:
            self.assertTrue(lptiyu_panel.open_mini_program('weixin://x'))
            started.assert_called_once_with('weixin://x')
        with patch.object(lptiyu_panel, 'wechat_installed', lambda: True), \
                patch.object(lptiyu_panel.os, 'startfile', side_effect=OSError('no handler')):
            self.assertFalse(lptiyu_panel.open_mini_program('weixin://x'))


class CaptureFlowTests(PanelTestCase):
    CRED = {"token": "CAPTURED-1", "uid": 17980001, "student_num": "2025001",
            "school_id": "837", "card_id": "2025001",
            "issued_at": "2026-11-11T18:00:00", "source": "capture"}

    def test_capture_opens_the_mini_program_for_the_user(self):
        opened = []
        controller = self.make_controller(
            capture=FakeCapture(cred=self.CRED),
            open_applet=lambda: opened.append(1) or True)
        controller._capture_flow()
        self.assertEqual(len(opened), 1, '点一次采集就该替用户把小程序拉起来')

    def test_capture_still_waits_when_wechat_cannot_be_launched(self):
        controller = self.make_controller(
            capture=FakeCapture(cred=self.CRED), open_applet=lambda: False)
        result = controller._capture_flow()
        self.assertEqual(result['state'], 'synced', '唤不起微信也要继续等, 不能直接失败')

    def test_capture_saves_the_token_and_the_captured_identity(self):
        controller = self.make_controller(capture=FakeCapture(cred=self.CRED))
        result = controller._capture_flow()
        self.assertEqual(result["state"], "synced")
        self.assertEqual(controller.store.token(), "CAPTURED-1")
        identity = controller.store.identity()
        self.assertEqual(identity["uid"], "17980001")
        self.assertEqual(identity["student_num"], "2025001")
        self.assertEqual(controller.store.token_record()["source"], "capture")

    def test_capture_timeout_asks_for_another_try(self):
        controller = self.make_controller(capture=FakeCapture(
            cred=None, final={"state": "timeout", "message": "等待超时：没有看到小程序登录"}))
        with self.assertRaises(LptiyuError) as caught:
            controller._capture_flow()
        self.assertEqual(caught.exception.state, "need_token")
        self.assertIn("超时", caught.exception.message)

    def test_stopping_the_capture_returns_to_idle(self):
        controller = self.make_controller(capture=FakeCapture(
            cred=None, final={"state": "idle", "message": "已停止采集"}))
        self.assertEqual(controller._capture_flow()["state"], "idle")

    def test_capture_refuses_without_the_certificate(self):
        controller = self.make_controller(capture=FakeCapture(installed=False))
        with self.assertRaises(LptiyuError) as caught:
            controller._capture_flow()
        self.assertIn("证书", caught.exception.message)

    def test_failed_probe_marks_the_token_dead(self):
        controller = self.make_controller(capture=FakeCapture(cred=self.CRED))
        with patch.object(LptiyuController, "_probe_alive", lambda self, client: False):
            with self.assertRaises(LptiyuError) as caught:
                controller._capture_flow()
        self.assertEqual(caught.exception.state, "need_token")
        self.assertFalse(controller.store.has_token())

    def test_cert_actions_go_through_the_cached_status(self):
        capture = FakeCapture(installed=False)
        controller = self.make_controller(capture=capture)
        self.assertFalse(controller.capture_status()["cert_installed"])
        controller.capture_cert_install()
        self.assertEqual(capture.ca.install_calls, 1)
        self.assertTrue(controller.capture_status()["cert_installed"])
        controller.capture_cert_remove()
        self.assertEqual(capture.ca.remove_calls, 1)
        self.assertFalse(controller.capture_status()["cert_installed"])

    def test_capture_status_carries_progress_for_the_ui(self):
        controller = self.make_controller(capture=FakeCapture(cred=self.CRED))
        controller.capture.start()
        status = controller.capture_status()
        self.assertEqual(status["state"], "waiting")
        self.assertEqual(status["remaining_s"], 42)
        self.assertTrue(status["available"])

    def test_closing_stops_an_active_capture(self):
        capture = FakeCapture(cred=self.CRED)
        controller = self.make_controller(capture=capture)
        controller.capture.start()
        controller.close()
        self.assertTrue(capture.stops, "退出时必须把采集停掉, 否则代理留在用户身上")


class FlowPolicyTests(PanelTestCase):
    """配速区间要原样落进流水线的 policy, 否则界面改了也白改。"""

    def flow_for(self, settings):
        # 不注入 flow_factory: 这里要看的就是正式路径现场构造的那只 DailyFlow。
        self.store.save_settings(settings)
        controller = LptiyuController(store=self.store, clock=CLOCK,
                                      renewer=self.renewer)
        self.addCleanup(controller.close)
        return controller._flow(settings)

    def test_flow_policy_carries_the_pace_window(self):
        flow = self.flow_for(Settings(enabled=True, pace_lo=245, pace_hi=280))
        self.assertEqual((flow.policy['pace_lo'], flow.policy['pace_hi']), (245, 280))

    def test_legacy_single_pace_reads_as_a_point_window(self):
        flow = self.flow_for(Settings(enabled=True, pace_target=240))
        self.assertEqual((flow.policy['pace_lo'], flow.policy['pace_hi']), (240, 240))

    def test_flow_carries_the_selected_zone_tuning(self):
        self.store.save_zone_tuning('1873', {'direction': 'cw', 'anchor_m': 12, 'lane_m': 5})
        flow = self.flow_for(Settings(enabled=True, zone_id='1873'))
        self.assertEqual(flow.zone_tuning['direction'], 'cw')
        self.assertEqual(flow.zone_tuning['anchor_m'], 12.0)
        self.assertEqual(flow.zone_tuning['lane_m'], 5.0)
        self.assertIsNotNone(flow.zone_sink, '流水线要把点位交给调用方留档')

    def test_flow_without_a_selected_zone_still_tunes_from_live_points(self):
        flow = self.flow_for(Settings(enabled=True, zone_id=''))
        self.assertEqual(flow.zone_tuning['direction'], 'auto', '没选跑区就是纯自动特调')
        self.assertEqual(flow.zone_tuning['anchor_m'], 0.0)
        self.assertEqual(flow.zone_tuning['lane_m'], 0.0)


if __name__ == '__main__':
    unittest.main()
