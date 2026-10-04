import datetime as dt
import importlib.util
import json
import os
import ssl
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from dorm_checkin import CheckinError, SHANGHAI, Task

PRESENT = importlib.util.find_spec('dorm_api') is not None
if PRESENT:
    import dorm_api
    from dorm_api import SwuApi


class FixtureHandler(BaseHTTPRequestHandler):
    """只会答话、或者按指令卡住/断线/回 503 的假学校接口。"""

    def log_message(self, *args):
        pass

    def do_GET(self):
        mode = self.server.mode
        if mode == 'stall':
            time.sleep(self.server.stall_seconds)
        elif mode == 'drop':
            self.close_connection = True
            return
        elif mode == 'status':
            self.send_response(503)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'code': 200, 'data': {'ok': True}}).encode())


class QuietServer(ThreadingHTTPServer):
    """客户端超时/主动断线是这些用例的预期行为，别把栈刷进测试输出。"""

    def handle_error(self, request, client_address):
        pass


class FeatureExists(unittest.TestCase):
    def test_api_exists(self):
        self.assertTrue(PRESENT, 'School API adapter missing')


@unittest.skipUnless(PRESENT, 'API adapter missing')
class ApiTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.record = dict(id='id-1', formId='form-1', tsrq='2026-09-21', cqzmc='晚间查寝',
                           qdkssj='21:00', qdjssj='23:30')
        self.selected = dict(xh='student', cqfbid='pub-1', qdjg='0', formId='dorm-1',
                             qsqddd='宿舍', qdbj='800米', qdsj=['21:00', '23:30'])
        self.replies = [dict(records=[self.record], total=1), self.selected,
                        dict(columnList=[])]
        self.api = SwuApi(transport=self.transport)
        self.current = dt.datetime(2026, 9, 21, 22, tzinfo=SHANGHAI)

    def transport(self, method, path, token, **kwargs):
        self.calls.append((method, path, token, kwargs))
        return self.replies.pop(0)

    def test_discovers_task_without_historical_fallback(self):
        task = self.api.today('secret', 'student', self.current)
        self.assertEqual(task.id, 'id-1')
        self.assertEqual(task.publish_id, 'pub-1')
        self.assertEqual(task.start, '21:00')
        self.assertEqual(self.calls[0][3]['form']['pageNum'], '1')

    def test_ambiguous_tasks_are_not_chosen_by_score(self):
        self.replies[0]['records'].append(dict(self.record, id='id-2'))
        with self.assertRaisesRegex(CheckinError, '多条'):
            self.api.today('secret', 'student', self.current)

    def test_old_tasks_are_ignored(self):
        self.record['tsrq'] = '2026-09-20'
        self.assertIsNone(self.api.today('secret', 'student', self.current))

    def test_missing_publish_id_stops_instead_of_defaulting(self):
        del self.selected['cqfbid']
        with self.assertRaises(CheckinError):
            self.api.today('secret', 'student', self.current)

    def test_mismatched_student_stops(self):
        self.selected['xh'] = 'another-student'
        with self.assertRaises(CheckinError):
            self.api.today('secret', 'student', self.current)

    def test_verify_false_stops(self):
        task = self.api.today('secret', 'student', self.current)
        self.replies = [dict(isArea=False)]
        with self.assertRaises(CheckinError):
            self.api.verify('secret', task, {})

    def test_verify_string_false_is_not_truthy_success(self):
        task = self.api.today('secret', 'student', self.current)
        self.replies = [dict(isArea='false')]
        with self.assertRaises(CheckinError):
            self.api.verify('secret', task, {})

    def test_readback_does_not_accept_save_message(self):
        task = self.api.today('secret', 'student', self.current)
        self.replies = [dict(self.selected, msg='保存成功')]
        self.assertFalse(self.api.is_signed('secret', task))
        self.replies = [dict(self.selected, qdjg='1')]
        self.assertTrue(self.api.is_signed('secret', task))

    def test_dormitory_reference_point_is_captured_for_the_map(self):
        self.replies[2] = dict(columnList=[dict(address='宿舍', latitude='29.821186',
                                                longitude='106.426239', qdbj=800)])
        task = self.api.today('secret', 'student', self.current)
        self.assertEqual(task.latitude, '29.821186')
        self.assertEqual(task.longitude, '106.426239')
        self.assertEqual(task.address, '宿舍')
        self.assertEqual(task.radius, '800米')

    def test_a_form_without_a_reference_point_still_produces_a_task(self):
        task = self.api.today('secret', 'student', self.current)
        self.assertEqual((task.latitude, task.longitude), ('', ''))

    def test_submit_reports_the_confirmation_the_school_writes_back(self):
        task = self.api.today('secret', 'student', self.current)
        self.replies = [dict(qdjg='1')]
        self.assertTrue(self.api.submit('secret', task, {}))
        self.replies = [dict(qdjg='0'), {}]
        self.assertFalse(self.api.submit('secret', task, {}))
        self.assertFalse(self.api.submit('secret', task, {}))

    def test_submit_uses_current_task_identifiers(self):
        task = self.api.today('secret', 'student', self.current)
        self.replies = [{}]
        self.api.submit('secret', task, {'latitude': 29.8, 'longitude': 106.4})
        body = self.calls[-1][3]['body']
        self.assertEqual(body['businessKey'], 'id-1')
        self.assertEqual(body['formId'], 'form-1')
        self.assertEqual(body['cqfbid'], 'pub-1')

    def test_user_response_must_contain_identity(self):
        self.replies = [{}]
        with self.assertRaises(CheckinError) as caught:
            self.api.user('secret')
        self.assertEqual(caught.exception.state, 'login_required')


@unittest.skipUnless(PRESENT, 'API adapter missing')
class FailureReportTests(unittest.TestCase):
    """2026-09-28 复盘：一句"请检查网络"曾经同时代表超时/被重置/DNS，事后无从下手。"""

    def setUp(self):
        self.server = QuietServer(('127.0.0.1', 0), FixtureHandler)
        self.server.mode = 'ok'
        self.server.stall_seconds = 2.0
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.origin = patch.object(
            dorm_api, 'ORIGIN', f'http://127.0.0.1:{self.server.server_port}')
        self.origin.start()
        self.addCleanup(self.origin.stop)
        # 线上超时是 20 秒，测试里压到 0.3 秒，语义不变。
        self.fast_timeout = patch.object(dorm_api, 'REQUEST_TIMEOUT_SECONDS', 0.3)
        self.fast_timeout.start()
        self.addCleanup(self.fast_timeout.stop)

    def call(self):
        return dorm_api.request('GET', '/gateway/fighter-baida/api/probe', 'token',
                                query={'appType': 'fighter-portal'})

    def test_a_stalled_school_answer_is_reported_as_a_timeout(self):
        self.server.mode = 'stall'
        with self.assertRaises(CheckinError) as caught:
            self.call()
        self.assertEqual(caught.exception.state, 'network_error')
        self.assertIn('响应超时', str(caught.exception))
        self.assertIn('响应超时', caught.exception.detail)
        self.assertIn('直连', caught.exception.detail)

    def test_a_dropped_connection_is_not_reported_as_a_timeout(self):
        self.server.mode = 'drop'
        with self.assertRaises(CheckinError) as caught:
            self.call()
        self.assertEqual(caught.exception.state, 'network_error')
        self.assertIn('连接被中断', str(caught.exception))
        self.assertIn('连接被中断', caught.exception.detail)

    def test_an_http_status_error_says_the_school_answered(self):
        self.server.mode = 'status'
        with self.assertRaises(CheckinError) as caught:
            self.call()
        self.assertEqual(caught.exception.state, 'network_error')
        self.assertIn('HTTP 503', str(caught.exception))
        self.assertIn('HTTP 503', caught.exception.detail)

    def test_a_working_answer_carries_no_diagnostic(self):
        self.assertEqual(self.call(), {'ok': True})


@unittest.skipUnless(PRESENT, 'API adapter missing')
class TlsFailureClassTests(unittest.TestCase):
    """2026-10-04 现场：TLS 握手被对端/中间设备在完成前切断（SSLEOFError，每次约 5 秒）。"""

    def test_a_cut_handshake_is_not_reported_as_a_plain_timeout(self):
        exc = ssl.SSLEOFError(8, 'UNEXPECTED_EOF_WHILE_READING')
        self.assertEqual(dorm_api.failure_kind(exc), 'TLS 握手被切断')
        self.assertIn('TLS 握手被切断', dorm_api.transport_message('TLS 握手被切断'))

    def test_the_same_classification_survives_being_wrapped_by_urllib(self):
        wrapped = urllib.error.URLError(ssl.SSLEOFError(8, 'UNEXPECTED_EOF_WHILE_READING'))
        self.assertEqual(dorm_api.failure_kind(wrapped), 'TLS 握手被切断')

    def test_a_certificate_problem_stays_a_generic_tls_failure(self):
        self.assertEqual(dorm_api.failure_kind(ssl.SSLCertVerificationError(1, 'bad cert')),
                         'TLS 握手失败')


@unittest.skipUnless(PRESENT, 'API adapter missing')
class OutboundRouteTests(unittest.TestCase):
    """本机开着 Clash 时 urllib 会静默继承系统代理：那一跳必须被摘掉并如实记录。"""

    def setUp(self):
        self.server = QuietServer(('127.0.0.1', 0), FixtureHandler)
        self.server.mode = 'ok'
        self.server.stall_seconds = 0.0
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.origin = patch.object(
            dorm_api, 'ORIGIN', f'http://127.0.0.1:{self.server.server_port}')
        self.origin.start()
        self.addCleanup(self.origin.stop)
        self.fast_timeout = patch.object(dorm_api, 'REQUEST_TIMEOUT_SECONDS', 0.3)
        self.fast_timeout.start()
        self.addCleanup(self.fast_timeout.stop)
        # 一个必定连不上的"系统代理"：真走了它，请求必然失败。
        self.proxies = patch.object(dorm_api.urllib.request, 'getproxies',
                                    return_value={'http': 'http://127.0.0.1:1',
                                                  'https': 'http://127.0.0.1:1'})
        self.proxies.start()
        self.addCleanup(self.proxies.stop)

    def call(self):
        return dorm_api.request('GET', '/gateway/fighter-baida/api/probe', 'token')

    def test_school_calls_do_not_go_through_the_local_proxy(self):
        self.assertEqual(dorm_api.system_proxy(), '127.0.0.1:1')
        self.assertEqual(dorm_api.pick_route(), dorm_api.DIRECT)
        self.assertEqual(self.call(), {'ok': True})

    def test_a_bypassed_proxy_is_named_in_the_failure_note(self):
        self.server.mode = 'drop'
        with self.assertRaises(CheckinError) as caught:
            self.call()
        self.assertIn('本机系统代理 127.0.0.1:1 已绕过', caught.exception.detail)

    def test_a_proxy_only_network_can_be_opted_back_in(self):
        with patch.dict(os.environ, {dorm_api.SYSTEM_PROXY_ENV: '1'}):
            self.assertEqual(dorm_api.pick_route(), dorm_api.SYSTEM)
            with self.assertRaises(CheckinError) as caught:
                self.call()
        self.assertEqual(caught.exception.state, 'network_error')
        self.assertIn('经系统代理 127.0.0.1:1', caught.exception.detail)


@unittest.skipUnless(PRESENT, 'API adapter missing')
class ReadRetryTests(unittest.TestCase):
    """一个抖动不该拖成 5 分钟：只读调用本轮重试，落库那次 POST 仍然只发一次。"""

    def api(self, transport, retry_delays=(0, 0)):
        return SwuApi(transport=transport, retry_delays=retry_delays)

    def test_a_transient_read_failure_is_retried_within_the_run(self):
        calls = []

        def transport(method, path, token, **kwargs):
            calls.append(path)
            if len(calls) < 3:
                raise CheckinError('network_error', '无法连接学校接口，请检查网络后重试')
            return {'subject': {'username': 'student'}}

        self.assertEqual(self.api(transport).user('token'), 'student')
        self.assertEqual(len(calls), 3)

    def test_the_retry_count_reaches_the_diagnostic(self):
        def transport(method, path, token, **kwargs):
            raise CheckinError('network_error', '学校接口响应超时（20 秒未回应），请稍后重试',
                               detail='响应超时 20.0s／直连')

        with self.assertRaises(CheckinError) as caught:
            self.api(transport).user('token')
        self.assertEqual(caught.exception.state, 'network_error')
        self.assertIn('本轮已重试 2 次', caught.exception.detail)
        self.assertIn('响应超时 20.0s／直连', caught.exception.detail)

    def test_a_read_failure_that_is_not_transient_is_not_retried(self):
        calls = []

        def transport(method, path, token, **kwargs):
            calls.append(path)
            raise CheckinError('login_required', '登录已失效，请重新登录')

        with self.assertRaises(CheckinError):
            self.api(transport).user('token')
        self.assertEqual(len(calls), 1)

    def test_the_save_post_is_never_retried_within_the_run(self):
        calls = []

        def transport(method, path, token, **kwargs):
            calls.append(path)
            raise CheckinError('network_error', '无法连接学校接口，请检查网络后重试')

        task = Task('task-1', 'form-1', 'publish-1', 'student', '2026-09-21', '查寝',
                    '21:00', '23:30', False, '宿舍', '800米')
        with self.assertRaises(CheckinError):
            self.api(transport).submit('token', task, {})
        self.assertEqual(calls, [dorm_api.SAVE])


if __name__ == '__main__':
    unittest.main()
