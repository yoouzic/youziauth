"""idm_http / idm_login 的单元测试。

重点覆盖本轮定位到的两个真实缺陷：
  1. 浏览器发起的 POST 被站点 WAF 拦成 400 空白页 —— 提交必须走纯 HTTP 客户端；
  2. X-AuthErrorCode 对「验证码错」与「密码错」都是 -1，只能靠正文文案区分，
     而密码错时重试毫无意义、只会白烧登录尝试 —— 必须立刻停手。
"""

import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import idm_http  # noqa: E402
import idm_login  # noqa: E402

# 实测抓取的服务端原文（见 .tools/auth_error_report.txt）
CAPTCHA_FAILURE_TEXT = "UAMS 验证失败。动态口令验证失败 返回至登录页面"
CREDENTIAL_FAILURE_TEXT = "UAMS 验证失败。用户名或密码错误。 返回至登录页面"

# 真实验证码是 100x30 JPEG（约 2 KB）。submit_login 会拒绝过小的响应
# （防止把「看不清，换一张」这类占位文本当成图片），所以替身也要够长。
JPEG = b"\xff\xd8\xff" + b"j" * 400

FORM_INFO = {
    "fields": {"goto": "aHR0cA==", "encoded": "true", "SunQueryParamsString": "cmVhbG0=",
               "gx_charset": "UTF-8", "IDToken1": "", "IDToken2": "", "IDToken3": "",
               "validateCode": ""},
    "action": "https://idm.swu.edu.cn/am/UI/Login",
    "pageurl": "https://idm.swu.edu.cn/am/UI/Login?realm=%2F",
}


class FakeHeaders:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, name, default=None):
        return self.values.get(name, default)

    def get_all(self, name):
        return self.values.get(name)


class FakeSolver:
    def __init__(self, results):
        self.results = list(results)

    def solve(self, _image):
        return self.results.pop(0) if self.results else (None, 0.0)


class FakeClient:
    """替换 idm_http.HttpClient：按脚本返回响应，并记录发出去的请求。"""

    instances = []

    def __init__(self, responses, cookies=None, **_kwargs):
        self.responses = list(responses)
        self.cookies = dict(cookies or {})
        self.calls = []
        FakeClient.instances.append(self)

    def request(self, method, url, *, data=None, accept="", timeout=30):
        self.calls.append({"method": method, "url": url, "data": data})
        if not self.responses:
            raise AssertionError(f"unexpected extra request: {method} {url}")
        status, headers, body = self.responses.pop(0)
        if isinstance(body, str):
            body = body.encode("utf-8")
        return status, headers, body


def install_client(responses, cookies=None):
    FakeClient.instances = []
    return patch.object(idm_http, "HttpClient",
                        side_effect=lambda *_a, **_k: FakeClient(responses, cookies))


class FailureClassification(unittest.TestCase):
    def test_credential_and_captcha_failures_are_distinguished(self):
        """两种失败 X-AuthErrorCode 都是 -1，但正文文案不同，必须能区分。"""
        self.assertEqual(idm_http.classify_failure(CREDENTIAL_FAILURE_TEXT), "credentials")
        self.assertEqual(idm_http.classify_failure(CAPTCHA_FAILURE_TEXT), "captcha")

    def test_empty_or_unknown_text_is_unknown(self):
        for text in ("", "系统繁忙", None):
            self.assertEqual(idm_http.classify_failure(text), "unknown")

    def test_credential_marker_wins_when_both_appear(self):
        """宁可误判成 credentials（停手）也不要误判成 captcha（白烧一次尝试）。"""
        mixed = "用户名或密码错误 动态口令验证失败"
        self.assertEqual(idm_http.classify_failure(mixed), "credentials")


class SubmitLogin(unittest.TestCase):
    def submit(self, responses, solver, tries_log=None):
        with install_client(responses, cookies={"AMAuthCookie": "x"}):
            return idm_http.submit_login(FORM_INFO, {"AMAuthCookie": "x"},
                                         "20230001", "secret-pw", solver,
                                         log=(tries_log.append if tries_log is not None else None))

    def test_success_is_decided_by_x_autherrorcode_zero(self):
        outcome = self.submit(
            [(200, FakeHeaders(), JPEG), (302, FakeHeaders({"X-AuthErrorCode": "0"}), b"")],
            FakeSolver([("1234", 0.99)]),
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.auth_error, "0")
        self.assertEqual(outcome.code, "1234")

    def test_credentials_are_submitted_in_plaintext_not_str_encrypted(self):
        """页面那套 strEnc 提交反而会被判「用户名或密码错误」，必须明文。"""
        outcome = self.submit(
            [(200, FakeHeaders(), JPEG), (302, FakeHeaders({"X-AuthErrorCode": "0"}), b"")],
            FakeSolver([("1234", 0.99)]),
        )
        self.assertTrue(outcome.ok)
        body = FakeClient.instances[0].calls[1]["data"].decode()
        from urllib.parse import parse_qs
        parsed = parse_qs(body)
        self.assertEqual(parsed["IDToken1"], ["20230001"])
        self.assertEqual(parsed["IDToken2"], ["secret-pw"])
        self.assertEqual(parsed["validateCode"], ["1234"])
        # SSO 回跳所需的页面字段必须原样带上（不能丢）
        self.assertEqual(parsed["goto"], ["aHR0cA=="])
        self.assertEqual(parsed["SunQueryParamsString"], ["cmVhbG0="])
        self.assertEqual(parsed["encoded"], ["true"])

    def test_credential_failure_is_classified_and_returns_auth_error(self):
        outcome = self.submit(
            [(200, FakeHeaders(), JPEG),
             (200, FakeHeaders({"X-AuthErrorCode": "-1"}), CREDENTIAL_FAILURE_TEXT.encode())],
            FakeSolver([("1234", 0.99)]),
        )
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.submitted)
        self.assertEqual(outcome.auth_error, "-1")
        self.assertEqual(outcome.failure_kind, "credentials")

    def test_captcha_failure_is_classified_as_captcha(self):
        outcome = self.submit(
            [(200, FakeHeaders(), JPEG),
             (200, FakeHeaders({"X-AuthErrorCode": "-1"}), CAPTCHA_FAILURE_TEXT.encode())],
            FakeSolver([("0000", 0.99)]),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.failure_kind, "captcha")

    def test_low_confidence_captcha_never_posts_and_retries_in_place(self):
        """低置信时不提交（不消耗登录尝试），换一张再来。"""
        log = []
        outcome = self.submit(
            [(200, FakeHeaders(), JPEG), (200, FakeHeaders(), JPEG),
             (302, FakeHeaders({"X-AuthErrorCode": "0"}), b"")],
            FakeSolver([(None, 0.4), ("5678", 0.99)]),
            tries_log=log,
        )
        self.assertTrue(outcome.ok)
        client = FakeClient.instances[0]
        self.assertEqual([c["method"] for c in client.calls], ["GET", "GET", "POST"])
        self.assertTrue(any("换一张" in m for m in log))

    def test_captcha_endpoint_failure_is_not_a_submitted_attempt(self):
        outcome = self.submit([(502, FakeHeaders(), b"")], FakeSolver([("1234", 0.99)]))
        self.assertFalse(outcome.ok)
        self.assertFalse(outcome.submitted)   # 没发出 POST -> 不算一次登录尝试

    def test_missing_login_form_is_reported_without_any_request(self):
        with install_client([]):
            outcome = idm_http.submit_login({"fields": {}}, {}, "u", "p", FakeSolver([]))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.detail, "no_login_form")
        self.assertEqual(FakeClient.instances, [])


class FakePage:
    """最小可用的 IDM 登录页替身。"""

    def __init__(self, url="https://idm.swu.edu.cn/am/UI/Login?realm=%2F", fields=None):
        self.url = url
        self._fields = FORM_INFO["fields"] if fields is None else fields
        self.context = self
        self.selector_waits = 0

    def wait_for_selector(self, _selector, timeout=None, state=None):
        self.selector_waits += 1
        return object()

    def locator(self, _selector):
        outer = self

        class _Locator:
            def count(self):
                return 1

            @property
            def first(self):
                return outer

        return _Locator()

    def cookies(self, _url):
        return [{"name": "AMAuthCookie", "value": "x"}, {"name": "SESSION", "value": "y"}]

    def evaluate(self, _script):
        return {"fields": dict(self._fields), "action": FORM_INFO["action"],
                "pageurl": FORM_INFO["pageurl"]}


class ResponseJudgement(unittest.TestCase):
    """提交判据必须按证据强弱分层（`judge_response`）。

    背景（2026-09-26 用户实测）：自动登录第一次**已经在服务端认证通过**，
    程序却因为判据缺失把它判成失败，于是把正在走的登录页强行导航走
    （`fresh_session()`）并**又登了一次** —— 用户看到的就是
    「第一次登录成功、学校登录页被跳走、程序又登了一次」。
    """

    GOTO = "http://idm.swu.edu.cn/am/oauth2/authorize?service=initService"
    GOTO_ON_FAIL = "http://idm.swu.edu.cn/am/UI/Login?realm=%2F"

    def fields(self, **overrides):
        fields = dict(FORM_INFO["fields"], goto=self.GOTO, gotoOnFail=self.GOTO_ON_FAIL)
        fields.update(overrides)
        return fields

    def test_header_zero_is_accepted(self):
        self.assertEqual(idm_http.judge_response(302, "0", "", "", self.fields()),
                         (True, "header", ""))

    def test_redirect_to_the_declared_success_target_is_accepted_without_the_header(self):
        """判据缺失（响应头被中间层吞掉）时，落点等于表单声明的 goto 也算通过。"""
        self.assertEqual(idm_http.judge_response(302, None, self.GOTO, "", self.fields()),
                         (True, "redirect", ""))

    def test_redirect_back_to_the_login_page_is_a_rejection(self):
        self.assertEqual(idm_http.judge_response(302, None, self.GOTO_ON_FAIL, "", self.fields()),
                         (False, "", "rejected"))
        self.assertEqual(
            idm_http.judge_response(302, None, "https://uaaap.swu.edu.cn/cas/login?service=x",
                                    "", self.fields()),
            (False, "", "rejected"))

    def test_redirect_off_campus_is_never_accepted(self):
        """落点跑出学校域名：不可信，按明确失败处理（既不信它的会话，也不重试）。"""
        self.assertEqual(idm_http.judge_response(302, None, "https://evil.test/ok", "", self.fields()),
                         (False, "", "rejected"))

    def test_missing_judgement_is_unconfirmed_not_a_rejection(self):
        """说不清 ≠ 失败：既没有错误码也没有错误文案时，绝不能当成「再登一次」的理由。"""
        for status, error, location, text in (
                (302, None, "https://idm.swu.edu.cn/am/oauth2/other", ""),
                (400, None, "", "\r\n\r\n\r\n"),
                (502, None, "", ""),
                (200, None, "", "")):
            self.assertEqual(idm_http.judge_response(status, error, location, text, self.fields()),
                             (False, "", "unconfirmed"), f'HTTP {status}')


class SubmissionOutcome(unittest.TestCase):
    """`submit_login` 的结果分类：通过 / 明确失败 / 未确认，以及是否值得重试。"""

    def submit(self, responses, solver):
        with install_client(responses, cookies={"AMAuthCookie": "x"}):
            return idm_http.submit_login(FORM_INFO, {"AMAuthCookie": "x"},
                                         "20230001", "secret-pw", solver)

    def submit_with_lost_response(self, error):
        """让 POST 抛异常（响应丢失），验证码请求照常返回。"""
        client = FakeClient([(200, FakeHeaders(), JPEG)], {"AMAuthCookie": "x"})
        real_request = client.request

        def flaky(method, url, **kwargs):
            if method == "POST":
                raise error
            return real_request(method, url, **kwargs)

        client.request = flaky
        with patch.object(idm_http, "HttpClient", return_value=client):
            outcome = idm_http.submit_login(FORM_INFO, {"AMAuthCookie": "x"},
                                            "20230001", "secret-pw", FakeSolver([("1234", 0.99)]))
        self.assertTrue(client.calls, "验证码必须真的取过")
        return outcome

    def test_lost_response_is_unconfirmed_not_a_failure(self):
        """POST 发出去了但没拿到响应：服务端可能已经认证成功 —— 归「未确认」。"""
        outcome = self.submit_with_lost_response(TimeoutError('proxy reset'))
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.sent, "POST 已经发出去过")
        self.assertFalse(outcome.submitted, "没拿到响应")
        self.assertEqual(outcome.failure_kind, "unconfirmed")
        self.assertTrue(outcome.unconfirmed)
        self.assertFalse(outcome.retryable, "未确认绝不能再登一次")

    def test_blank_or_unknown_answer_is_unconfirmed_and_not_retryable(self):
        """WAF 的 400 空白页 / 无文案的 5xx：同样说不清，不该换来第二次登录。"""
        for status in (400, 502):
            outcome = self.submit([(200, FakeHeaders(), JPEG), (status, FakeHeaders(), b"\r\n\r\n\r\n")],
                                  FakeSolver([("1234", 0.99)]))
            self.assertFalse(outcome.ok)
            self.assertTrue(outcome.sent)
            self.assertEqual(outcome.failure_kind, "unconfirmed", f'HTTP {status}')
            self.assertFalse(outcome.retryable)

    def test_unconfirmed_result_hands_back_session_for_verification(self):
        """未确认时要能把会话与下一跳交回上层去验证，而不是自己重登。"""
        outcome = self.submit(
            [(200, FakeHeaders(), JPEG),
             (302, FakeHeaders({"Location": "https://of.swu.edu.cn/somewhere"}), b"")],
            FakeSolver([("1234", 0.99)]))
        self.assertEqual(outcome.failure_kind, "unconfirmed")
        self.assertEqual(outcome.location, "https://of.swu.edu.cn/somewhere")
        self.assertTrue(outcome.cookies)

    def test_only_captcha_failures_are_retryable(self):
        """重试只留给「服务端明说验证码错」；密码错/文案不明/未确认一律不重试。"""
        captcha = self.submit([(200, FakeHeaders(), JPEG),
                               (200, FakeHeaders({"X-AuthErrorCode": "-1"}),
                                CAPTCHA_FAILURE_TEXT.encode())],
                              FakeSolver([("0000", 0.99)]))
        self.assertTrue(captcha.retryable)
        credentials = self.submit([(200, FakeHeaders(), JPEG),
                                   (200, FakeHeaders({"X-AuthErrorCode": "-1"}),
                                    CREDENTIAL_FAILURE_TEXT.encode())],
                                  FakeSolver([("0000", 0.99)]))
        self.assertFalse(credentials.retryable)
        unknown = self.submit([(200, FakeHeaders(), JPEG),
                               (200, FakeHeaders({"X-AuthErrorCode": "-1"}), "系统维护中".encode())],
                              FakeSolver([("0000", 0.99)]))
        self.assertEqual(unknown.failure_kind, "unknown")
        self.assertFalse(unknown.retryable, "文案不明不该白烧一次登录尝试")

    def test_captcha_pipeline_failure_is_retryable_but_low_confidence_is_not(self):
        """没发出 POST 时：取图/识别链路自身出错值得换会话，纯低置信不值得。"""
        blocked = self.submit([(403, FakeHeaders(), b"")], FakeSolver([("1234", 0.99)]))
        self.assertFalse(blocked.sent)
        self.assertTrue(blocked.retryable, "会话可能已被风控挡住，换会话还有救")
        low = self.submit([(200, FakeHeaders(), JPEG)] * 4, FakeSolver([(None, 0.4)]))
        self.assertEqual(low.detail, "captcha_unavailable")
        self.assertFalse(low.retryable, "模型读不了这几张图，换会话只是白跳一次页面")


class BrowserCookieJar(unittest.TestCase):
    """HTTP 客户端不许带「页面 JS 读得到的 cookie」（站点动态防护那些）。

    实测（2026-09-26）：带上它们一律 400 + 空白页（换 UA 也一样）；只带 HttpOnly
    会话 cookie 则 200，POST 真的到达服务端。
    """

    class Context:
        def cookies(self, _url):
            return [{"name": "61zqTsrO93nzO", "value": "session"},
                    {"name": "61zqTsrO93nzP", "value": "dynamic"},
                    {"name": "AMAuthCookie", "value": "auth"}]

    class Page:
        def __init__(self, raw="61zqTsrO93nzP=dynamic; AMAuthCookie=auth"):
            self.raw = raw

        def evaluate(self, _script):
            return self.raw

    def test_js_visible_cookies_are_dropped(self):
        jar = idm_http.browser_cookies(self.Context(), self.Page())
        self.assertEqual(jar, {"61zqTsrO93nzO": "session"})

    def test_without_a_page_the_jar_is_unchanged(self):
        jar = idm_http.browser_cookies(self.Context())
        self.assertEqual(sorted(jar), ["61zqTsrO93nzO", "61zqTsrO93nzP", "AMAuthCookie"])

    def test_unreadable_document_cookie_falls_back_to_everything(self):
        class Broken:
            def evaluate(self, _script):
                raise RuntimeError("page closed")

        jar = idm_http.browser_cookies(self.Context(), Broken())
        self.assertEqual(len(jar), 3, "读不到就照旧全带，别凭空少带")


class SilentLoginPolicy(unittest.TestCase):
    def run_attempts(self, responses, solver, max_attempts=2):
        fresh_calls = []
        page = FakePage()

        def fresh_session():
            fresh_calls.append(1)

        with install_client(responses):
            result = idm_login.attempt_silent_login(
                page, "20230001", "secret-pw", solver=solver, cancel=threading.Event(),
                fresh_session=fresh_session, max_attempts=max_attempts, log=lambda _m: None)
        return result, fresh_calls

    def one_attempt_responses(self, headers, body=b""):
        return [(200, FakeHeaders(), JPEG), (200, headers, body)]

    def test_cancel_during_captcha_recognition_does_not_send_login(self):
        cancel = threading.Event()

        class CancellingSolver:
            def solve(self, _image):
                cancel.set()
                return "1234", 0.99

        with install_client(self.one_attempt_responses(FakeHeaders({"X-AuthErrorCode": "0"}))):
            result = idm_login.attempt_silent_login(
                FakePage(), "20230001", "fixture-password", solver=CancellingSolver(),
                cancel=cancel)

        self.assertEqual(result.reason, "cancelled")
        self.assertEqual([call["method"] for call in FakeClient.instances[0].calls], ["GET"])

    def test_bad_credentials_stop_immediately_without_a_second_attempt(self):
        """密码错时重试必然再错：必须立刻停手，不白烧第 2 次登录尝试。"""
        result, fresh_calls = self.run_attempts(
            self.one_attempt_responses(FakeHeaders({"X-AuthErrorCode": "-1"}),
                                       CREDENTIAL_FAILURE_TEXT.encode()),
            FakeSolver([("1234", 0.99)]),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "bad_credentials")
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(fresh_calls, [], "密码错时不应重走 SSO 再试一次")

    def test_captcha_failure_retries_with_a_fresh_session(self):
        """验证码错值得重试，且必须重走 SSO 换新会话（原地重试无效）。"""
        responses = (self.one_attempt_responses(FakeHeaders({"X-AuthErrorCode": "-1"}),
                                                CAPTCHA_FAILURE_TEXT.encode())
                     + self.one_attempt_responses(FakeHeaders({"X-AuthErrorCode": "-1"}),
                                                  CAPTCHA_FAILURE_TEXT.encode()))
        result, fresh_calls = self.run_attempts(responses, FakeSolver([("1111", 0.99), ("2222", 0.99)]))
        self.assertFalse(result.ok)
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(fresh_calls, [1], "第二次尝试前必须重走 SSO 换新会话")
        self.assertEqual([a.captcha_code for a in result.attempts], ["1111", "2222"])

    def test_unconfirmed_submission_is_not_retried_and_hands_back_the_session(self):
        """★ 判据缺失/响应丢失：绝不再登一次，把会话交回上层去验证。

        这是用户实测那条「第一次登录成功、学校登录页被跳走、程序又登了一次」的回归点：
        重试一旦发生，`fresh_session()` 就会把正在走的登录页导航走。
        """
        page = FakePage()
        fresh_calls = []
        client = FakeClient([(200, FakeHeaders(), JPEG)], {"AMAuthCookie": "maybe"})
        real_request = client.request

        def flaky(method, url, **kwargs):
            if method == "POST":
                raise TimeoutError("proxy reset")
            return real_request(method, url, **kwargs)

        client.request = flaky
        with patch.object(idm_http, "HttpClient", return_value=client):
            result = idm_login.attempt_silent_login(
                page, "20230001", "secret-pw", solver=FakeSolver([("1234", 0.99)]),
                cancel=threading.Event(), fresh_session=lambda: fresh_calls.append(1),
                log=lambda _m: None)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "unconfirmed")
        self.assertEqual(len(result.attempts), 1, "未确认绝不能触发第二次登录")
        self.assertEqual(fresh_calls, [], "未确认不得重走 SSO 再登一次")
        self.assertEqual(result.cookies, {"AMAuthCookie": "maybe"},
                         "会话必须交回上层去验证，而不是丢掉重登")

    def test_unknown_server_answer_is_not_retried_either(self):
        result, fresh_calls = self.run_attempts(
            self.one_attempt_responses(FakeHeaders(), b"\r\n\r\n\r\n"),
            FakeSolver([("1234", 0.99)]))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "unconfirmed")
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(fresh_calls, [])

    def test_success_returns_authenticated_cookies_for_browser_injection(self):
        page = FakePage()
        with install_client(
            [(200, FakeHeaders(), JPEG),
             (302, FakeHeaders({"X-AuthErrorCode": "0"}), b"")],
            cookies={"AMAuthCookie": "new-session"},
        ):
            result = idm_login.attempt_silent_login(
                page, "20230001", "secret-pw", solver=FakeSolver([("1234", 0.99)]),
                cancel=threading.Event(), fresh_session=lambda: None, log=lambda _m: None)
        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "authenticated")
        self.assertEqual(result.cookies, {"AMAuthCookie": "new-session"})
        self.assertEqual(result.accepted_by, "header")

    def test_redirect_acceptance_carries_the_next_hop_for_the_browser(self):
        """判据来自 302 时，要把服务端给的下一跳带出去（浏览器顺着它走完 SSO）。"""
        target = "http://idm.swu.edu.cn/am/oauth2/authorize?service=initService"
        fields = dict(FORM_INFO["fields"], goto=target, gotoOnFail="")
        page = FakePage(fields=fields)
        with install_client(
            [(200, FakeHeaders(), JPEG),
             (302, FakeHeaders({"Location": target}), b"")],
            cookies={"AMAuthCookie": "new-session"},
        ):
            result = idm_login.attempt_silent_login(
                page, "20230001", "secret-pw", solver=FakeSolver([("1234", 0.99)]),
                cancel=threading.Event(), fresh_session=lambda: None, log=lambda _m: None)
        self.assertTrue(result.ok)
        self.assertEqual(result.accepted_by, "redirect")
        self.assertEqual(result.resume_url, target)

    def test_off_login_page_returns_without_touching_the_form(self):
        page = FakePage(url="https://of.swu.edu.cn/#/appCenter")
        with install_client([]):
            result = idm_login.attempt_silent_login(
                page, "u", "p", solver=FakeSolver([]), cancel=threading.Event(),
                fresh_session=lambda: None, log=lambda _m: None)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "not_on_idm_form")

    def test_missing_form_fields_stop_instead_of_blindly_posting(self):
        page = FakePage(fields={})
        with install_client([]):
            result = idm_login.attempt_silent_login(
                page, "u", "p", solver=FakeSolver([]), cancel=threading.Event(),
                fresh_session=lambda: None, log=lambda _m: None)
        self.assertFalse(result.ok)
        self.assertEqual(result.attempts[0].note, "form_fields_unavailable")


if __name__ == "__main__":
    unittest.main()
