import configparser
import logging
import os
import unittest

try:
    import campus_auth
except ModuleNotFoundError as exc:
    campus_auth = None
    import_error = exc
else:
    import_error = None


class QueryStringParsingTests(unittest.TestCase):
    def test_extracts_query_string_from_eportal_redirect_script(self):
        self.assertIsNotNone(campus_auth, import_error)
        html = (
            "<script>"
            "top.self.location.href='http://222.198.127.170/eportal/index.jsp?"
            "wlanuserip=1.2.3.4&wlanacname=ac01&ssid=&nasip=10.0.0.1&url=http%3A%2F%2Fexample.com'"
            "</script>"
        )

        query = campus_auth.extract_query_string(html)

        self.assertEqual(
            query,
            "wlanuserip=1.2.3.4&wlanacname=ac01&ssid=&nasip=10.0.0.1&url=http%3A%2F%2Fexample.com",
        )

    def test_extracts_query_string_when_html_escapes_ampersands(self):
        self.assertIsNotNone(campus_auth, import_error)
        html = (
            "location.href=\"/eportal/index.jsp?"
            "wlanuserip=1.2.3.4&amp;wlanacname=ac01&amp;ssid=\""
        )

        query = campus_auth.extract_query_string(html)

        self.assertEqual(query, "wlanuserip=1.2.3.4&wlanacname=ac01&ssid=")

    def test_returns_none_when_login_page_has_no_query_string(self):
        self.assertIsNotNone(campus_auth, import_error)

        self.assertIsNone(campus_auth.extract_query_string("<html>already online</html>"))

    def test_ignores_non_login_jsp_query_on_success_page(self):
        self.assertIsNotNone(campus_auth, import_error)

        query = campus_auth.extract_query_string("<a href='success_mab.jsp?ms2g='>ok</a>")

        self.assertIsNone(query)


class LoginResultTests(unittest.TestCase):
    def test_success_json_is_treated_as_login_success(self):
        self.assertIsNotNone(campus_auth, import_error)

        result = campus_auth.parse_login_result('{"result":"success","message":"认证成功"}')

        self.assertTrue(result.ok)
        self.assertEqual(result.message, "认证成功")

    def test_failed_json_keeps_server_message(self):
        self.assertIsNotNone(campus_auth, import_error)

        result = campus_auth.parse_login_result('{"result":"fail","message":"账号或密码错误"}')

        self.assertFalse(result.ok)
        self.assertEqual(result.message, "账号或密码错误")

    def test_non_json_success_text_is_treated_as_success(self):
        self.assertIsNotNone(campus_auth, import_error)

        result = campus_auth.parse_login_result("callback({result:'success'})")

        self.assertTrue(result.ok)


class PasswordEncryptionTests(unittest.TestCase):
    def test_encrypts_password_with_eportal_rsa_public_key(self):
        self.assertIsNotNone(campus_auth, import_error)

        encrypted = campus_auth.rsa_encrypt_password("pw", "11", "10001")

        self.assertEqual(encrypted, "00f043")

    def test_build_login_payload_uses_encrypted_password_when_requested(self):
        self.assertIsNotNone(campus_auth, import_error)
        config = campus_auth.AuthConfig(
            username="student",
            password="plain",
            service="",
            password_encrypt=True,
        )

        payload = campus_auth.build_login_payload(
            config,
            "wlanuserip=1.2.3.4",
            password_value="encrypted-value",
        )

        self.assertEqual(payload["password"], "encrypted-value")
        self.assertEqual(payload["passwordEncrypt"], "true")

    def test_rejects_encryption_when_public_key_is_missing(self):
        self.assertIsNotNone(campus_auth, import_error)

        with self.assertRaisesRegex(ValueError, "publicKeyExponent"):
            campus_auth.extract_rsa_public_key({"publicKeyModulus": "ca1"})


class ConfigTests(unittest.TestCase):
    def test_loads_required_config_values(self):
        self.assertIsNotNone(campus_auth, import_error)
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_dict(
            {
                "auth": {
                    "portal_url": "http://222.198.127.170/",
                    "username": "student",
                    "password": "secret",
                    "login_url": "http://222.198.127.170/eportal/index.jsp?wlanuserip=1.2.3.4",
                    "service": "%E9%BB%98%E8%AE%A4",
                    "check_interval_seconds": "30",
                    "request_timeout_seconds": "5",
                    "password_encrypt": "true",
                }
            }
        )

        config = campus_auth.load_config_from_parser(parser)

        self.assertEqual(config.portal_url, "http://222.198.127.170")
        self.assertEqual(config.username, "student")
        self.assertEqual(config.password, "secret")
        self.assertEqual(
            config.login_url,
            "http://222.198.127.170/eportal/index.jsp?wlanuserip=1.2.3.4",
        )
        self.assertEqual(config.service, "%E9%BB%98%E8%AE%A4")
        self.assertEqual(config.check_interval_seconds, 30)
        self.assertEqual(config.request_timeout_seconds, 5)
        self.assertIs(config.password_encrypt, True)

    def test_loads_auto_password_encryption_mode(self):
        self.assertIsNotNone(campus_auth, import_error)
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_dict(
            {
                "auth": {
                    "portal_url": "http://222.198.127.170/",
                    "username": "student",
                    "password": "secret",
                    "password_encrypt": "auto",
                }
            }
        )

        config = campus_auth.load_config_from_parser(parser)

        self.assertIsNone(config.password_encrypt)

    def test_loads_password_from_environment_variable(self):
        self.assertIsNotNone(campus_auth, import_error)
        old_value = os.environ.get("CAMPUS_AUTH_TEST_PASSWORD")
        os.environ["CAMPUS_AUTH_TEST_PASSWORD"] = "secret-from-env"
        try:
            parser = configparser.ConfigParser(interpolation=None)
            parser.read_dict(
                {
                    "auth": {
                        "portal_url": "http://222.198.127.170/",
                        "username": "student",
                        "password": "",
                        "password_env": "CAMPUS_AUTH_TEST_PASSWORD",
                    }
                }
            )

            config = campus_auth.load_config_from_parser(parser)

            self.assertEqual(config.password, "secret-from-env")
        finally:
            if old_value is None:
                os.environ.pop("CAMPUS_AUTH_TEST_PASSWORD", None)
            else:
                os.environ["CAMPUS_AUTH_TEST_PASSWORD"] = old_value

    def test_rejects_missing_password_environment_variable(self):
        self.assertIsNotNone(campus_auth, import_error)
        os.environ.pop("CAMPUS_AUTH_MISSING_PASSWORD", None)
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_dict(
            {
                "auth": {
                    "portal_url": "http://222.198.127.170/",
                    "username": "student",
                    "password": "",
                    "password_env": "CAMPUS_AUTH_MISSING_PASSWORD",
                }
            }
        )

        with self.assertRaisesRegex(ValueError, "CAMPUS_AUTH_MISSING_PASSWORD"):
            campus_auth.load_config_from_parser(parser)

    def test_rejects_missing_credentials(self):
        self.assertIsNotNone(campus_auth, import_error)
        parser = configparser.ConfigParser()
        parser.read_dict({"auth": {"portal_url": "http://222.198.127.170/"}})

        with self.assertRaisesRegex(ValueError, "username"):
            campus_auth.load_config_from_parser(parser)


class StatusParsingTests(unittest.TestCase):
    def test_online_user_info_success_means_authenticated(self):
        self.assertIsNotNone(campus_auth, import_error)

        status = campus_auth.parse_online_user_info('{"result":"success","userId":"student"}')

        self.assertEqual(status, campus_auth.AuthStatus.AUTHENTICATED)

    def test_online_user_info_fail_means_unauthenticated(self):
        self.assertIsNotNone(campus_auth, import_error)

        status = campus_auth.parse_online_user_info('{"result":"fail","message":"not online"}')

        self.assertEqual(status, campus_auth.AuthStatus.UNAUTHENTICATED)

    def test_unparseable_online_user_info_is_unknown(self):
        self.assertIsNotNone(campus_auth, import_error)

        status = campus_auth.parse_online_user_info("<html>bad gateway</html>")

        self.assertEqual(status, campus_auth.AuthStatus.UNKNOWN)


class StatusFlowTests(unittest.TestCase):
    def test_success_page_still_wins_when_the_api_cannot_name_the_session(self):
        self.assertIsNotNone(campus_auth, import_error)

        class FakeClient(campus_auth.CampusAuthClient):
            def __init__(self):
                super().__init__(campus_auth.AuthConfig(username="student", password="pw"))

            def request(self, url, data=None, allow_redirects=True, referer=None):
                if url == self.config.portal_url:
                    return campus_auth.HttpResponse(
                        200,
                        {},
                        "<html><title>login success</title></html>",
                        self.portal_url("/eportal/success_mab.jsp"),
                    )
                if url.endswith("method=getOnlineUserInfo"):
                    return campus_auth.HttpResponse(
                        200, {}, '{"result":"fail","message":"not online"}', url
                    )
                return campus_auth.HttpResponse(200, {}, "", url)

        status = FakeClient().check_status()

        self.assertEqual(status, campus_auth.AuthStatus.AUTHENTICATED)

    def test_named_session_refused_by_api_beats_the_success_page(self):
        self.assertIsNotNone(campus_auth, import_error)
        user_index = "64323438356532646337343838356233353333323330323566656136613838345f31302e302e302e31"

        class FakeClient(campus_auth.CampusAuthClient):
            def __init__(self):
                super().__init__(campus_auth.AuthConfig(username="student", password="pw"))

            def request(self, url, data=None, allow_redirects=True, referer=None):
                if url == self.config.portal_url:
                    return campus_auth.HttpResponse(
                        200,
                        {},
                        "<html><title>登录成功</title></html>",
                        self.portal_url(f"/eportal/./success.jsp?userIndex={user_index}"),
                    )
                if "method=getOnlineUserInfo" in url:
                    self.asked = url
                    return campus_auth.HttpResponse(
                        200,
                        {},
                        '{"result":"fail","message":"获取用户信息失败，用户可能已经下线"}',
                        url,
                    )
                return campus_auth.HttpResponse(200, {}, "", url)

        client = FakeClient()
        status = client.check_status()

        # This is the false-positive case: the portal serves success.jsp from a
        # stale session record while its own API refuses the session we named.
        self.assertEqual(status, campus_auth.AuthStatus.UNAUTHENTICATED)
        self.assertIn(f"userIndex={user_index}", client.asked)


class OnlineUserInfoTests(unittest.TestCase):
    def test_user_index_is_read_out_of_the_success_redirect(self):
        self.assertIsNotNone(campus_auth, import_error)

        index = campus_auth.extract_user_index(
            "http://portal/eportal/./success.jsp?userIndex=abc123",
            "",
        )

        self.assertEqual(index, "abc123")

    def test_user_index_is_read_from_a_configured_login_url(self):
        self.assertIsNotNone(campus_auth, import_error)

        index = campus_auth.extract_user_index(
            "",
            "http://portal/eportal/index.jsp?userIndex=deadbeef&wlanuserip=1.2.3.4",
        )

        self.assertEqual(index, "deadbeef")

    def test_fail_without_a_named_session_stays_unknown(self):
        self.assertIsNotNone(campus_auth, import_error)

        status = campus_auth.parse_online_user_info(
            '{"result":"fail","message":"not online"}', user_index_supplied=False
        )

        self.assertEqual(status, campus_auth.AuthStatus.UNKNOWN)

    def test_wait_is_retried_because_the_portal_settles(self):
        self.assertIsNotNone(campus_auth, import_error)

        class FakeClient(campus_auth.CampusAuthClient):
            def __init__(self):
                super().__init__(campus_auth.AuthConfig(username="student", password="pw"))
                self.responses = [
                    '{"result":"wait","message":"用户信息不完整，请稍后重试"}',
                    '{"result":"success","message":"获取用户信息成功"}',
                ]

            def request(self, url, data=None, allow_redirects=True, referer=None):
                return campus_auth.HttpResponse(200, {}, self.responses.pop(0), url)

        status = FakeClient().check_online_user_info()

        self.assertEqual(status, campus_auth.AuthStatus.AUTHENTICATED)

    def test_success_page_template_noise_is_not_authentication(self):
        self.assertIsNotNone(campus_auth, import_error)

        # The portal ships this CSS/JS on pages that are not the success page.
        class FakeClient(campus_auth.CampusAuthClient):
            def request(self, url, data=None, allow_redirects=True, referer=None):
                return campus_auth.HttpResponse(
                    200,
                    {},
                    "<style>.toLogOut_1{float:right}</style>"
                    "<script>var userIndex = getQueryStringByName(\"userIndex\");</script>",
                    self.portal_url("/eportal/index.jsp?wlanuserip=1.2.3.4&wlanacname=ac01"),
                )

        status = FakeClient(
            campus_auth.AuthConfig(username="student", password="pw")
        ).check_login_page_status()

        self.assertEqual(status, campus_auth.AuthStatus.UNAUTHENTICATED)

    def test_portal_requests_bypass_the_system_proxy(self):
        self.assertIsNotNone(campus_auth, import_error)
        import http.server
        import threading
        import urllib.request

        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        original = urllib.request.getproxies
        # A dead proxy address: anything that still honours the system proxy
        # (which on Windows means whatever Clash/v2ray left in the registry)
        # cannot reach the portal at all.
        urllib.request.getproxies = lambda: {"http": "http://127.0.0.1:1"}
        try:
            config = campus_auth.AuthConfig(portal_url=f"http://127.0.0.1:{port}")
            client = campus_auth.CampusAuthClient(config)
            self.assertEqual(
                [
                    handler
                    for handler in client.opener.handlers
                    if isinstance(handler, urllib.request.ProxyHandler)
                ],
                [],
            )
            response = client.request(config.portal_url)
        finally:
            urllib.request.getproxies = original
            server.shutdown()
            server.server_close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(received, ["/"])

    def test_success_page_url_wins_over_non_login_jsp_query(self):
        self.assertIsNotNone(campus_auth, import_error)

        class FakeClient(campus_auth.CampusAuthClient):
            def __init__(self):
                super().__init__(campus_auth.AuthConfig(username="student", password="pw"))

            def request(self, url, data=None, allow_redirects=True, referer=None):
                if url == self.config.portal_url:
                    return campus_auth.HttpResponse(
                        200,
                        {},
                        "<a href='success_mab.jsp?ms2g='>ok</a>",
                        self.portal_url("/eportal/success_mab.jsp"),
                    )
                return campus_auth.HttpResponse(200, {}, "", url)

        status = FakeClient().check_login_page_status()

        self.assertEqual(status, campus_auth.AuthStatus.AUTHENTICATED)


class LoginFlowTests(unittest.TestCase):
    def test_get_query_string_prefers_configured_login_url(self):
        self.assertIsNotNone(campus_auth, import_error)

        class FakeClient(campus_auth.CampusAuthClient):
            def request(self, url, data=None, allow_redirects=True, referer=None):
                raise AssertionError("configured login_url should avoid probing portal home")

        config = campus_auth.AuthConfig(
            username="student",
            password="pw",
            login_url=(
                "http://222.198.127.170/eportal/index.jsp?"
                "wlanuserip=10.135.155.137&wlanacname=NAS&ssid=Ruijie"
            ),
        )

        query = FakeClient(config).get_query_string()

        self.assertEqual(
            query,
            "wlanuserip=10.135.155.137&wlanacname=NAS&ssid=Ruijie",
        )

    def test_login_fetches_page_info_before_encrypted_login(self):
        self.assertIsNotNone(campus_auth, import_error)

        class FakeClient(campus_auth.CampusAuthClient):
            def __init__(self):
                super().__init__(
                    campus_auth.AuthConfig(
                        username="student",
                        password="pw",
                        password_encrypt=True,
                    )
                )
                self.requests = []

            def get_query_string(self):
                return "wlanuserip=1.2.3.4"

            def request(self, url, data=None, allow_redirects=True, referer=None):
                self.requests.append((url, data))
                if url.endswith("method=pageInfo"):
                    return campus_auth.HttpResponse(
                        200,
                        {},
                        '{"publicKeyExponent":"11","publicKeyModulus":"10001"}',
                        url,
                    )
                if url.endswith("method=login"):
                    self.login_payload = dict(data)
                    return campus_auth.HttpResponse(
                        200, {}, '{"result":"success","message":"ok"}', url
                    )
                raise AssertionError(f"unexpected url: {url}")

        client = FakeClient()

        result = client.login()

        self.assertTrue(result.ok)
        self.assertEqual(client.login_payload["password"], "00f043")
        self.assertEqual(client.login_payload["passwordEncrypt"], "true")
        self.assertTrue(any(url.endswith("method=pageInfo") for url, _ in client.requests))

    def test_auto_encryption_keeps_plain_password_when_page_info_disables_it(self):
        self.assertIsNotNone(campus_auth, import_error)

        class FakeClient(campus_auth.CampusAuthClient):
            def __init__(self):
                super().__init__(
                    campus_auth.AuthConfig(
                        username="student",
                        password="pw",
                        password_encrypt=None,
                    )
                )
                self.requests = []

            def get_query_string(self):
                return "wlanuserip=1.2.3.4"

            def request(self, url, data=None, allow_redirects=True, referer=None):
                self.requests.append((url, data))
                if url.endswith("method=pageInfo"):
                    return campus_auth.HttpResponse(
                        200,
                        {},
                        (
                            '{"passwordEncrypt":"false",'
                            '"publicKeyExponent":"10001",'
                            '"publicKeyModulus":"94dd"}'
                        ),
                        url,
                    )
                if url.endswith("method=login"):
                    self.login_payload = dict(data)
                    return campus_auth.HttpResponse(
                        200, {}, '{"result":"success","message":"ok"}', url
                    )
                raise AssertionError(f"unexpected url: {url}")

        client = FakeClient()

        result = client.login()

        self.assertTrue(result.ok)
        self.assertEqual(client.login_payload["password"], "pw")
        self.assertEqual(client.login_payload["passwordEncrypt"], "false")
        self.assertTrue(any(url.endswith("method=pageInfo") for url, _ in client.requests))

    def test_auto_encryption_sets_payload_flag_when_page_info_enables_it(self):
        self.assertIsNotNone(campus_auth, import_error)

        class FakeClient(campus_auth.CampusAuthClient):
            def __init__(self):
                super().__init__(
                    campus_auth.AuthConfig(
                        username="student",
                        password="pw",
                        password_encrypt=None,
                    )
                )
                self.requests = []

            def get_query_string(self):
                return "wlanuserip=1.2.3.4"

            def request(self, url, data=None, allow_redirects=True, referer=None):
                self.requests.append((url, data))
                if url.endswith("method=pageInfo"):
                    return campus_auth.HttpResponse(
                        200,
                        {},
                        (
                            '{"passwordEncrypt":"true",'
                            '"publicKeyExponent":"11",'
                            '"publicKeyModulus":"10001"}'
                        ),
                        url,
                    )
                if url.endswith("method=login"):
                    self.login_payload = dict(data)
                    return campus_auth.HttpResponse(
                        200, {}, '{"result":"success","message":"ok"}', url
                    )
                raise AssertionError(f"unexpected url: {url}")

        client = FakeClient()

        result = client.login()

        self.assertTrue(result.ok)
        self.assertEqual(client.login_payload["password"], "00f043")
        self.assertEqual(client.login_payload["passwordEncrypt"], "true")
        self.assertTrue(any(url.endswith("method=pageInfo") for url, _ in client.requests))

    def test_forced_plaintext_login_skips_page_info(self):
        self.assertIsNotNone(campus_auth, import_error)

        class FakeClient(campus_auth.CampusAuthClient):
            def __init__(self):
                super().__init__(
                    campus_auth.AuthConfig(
                        username="student",
                        password="pw",
                        password_encrypt=False,
                    )
                )
                self.requests = []

            def get_query_string(self):
                return "wlanuserip=1.2.3.4"

            def request(self, url, data=None, allow_redirects=True, referer=None):
                self.requests.append((url, data))
                if url.endswith("method=pageInfo"):
                    raise AssertionError("pageInfo should not be requested")
                if url.endswith("method=login"):
                    self.login_payload = dict(data)
                    return campus_auth.HttpResponse(
                        200, {}, '{"result":"success","message":"ok"}', url
                    )
                raise AssertionError(f"unexpected url: {url}")

        client = FakeClient()

        result = client.login()

        self.assertTrue(result.ok)
        self.assertEqual(client.login_payload["password"], "pw")
        self.assertEqual(client.login_payload["passwordEncrypt"], "false")


class StructuredAttemptTests(unittest.TestCase):
    def test_already_authenticated_returns_structured_success(self):
        class FakeClient:
            def check_status(self):
                return campus_auth.AuthStatus.AUTHENTICATED

        attempt = campus_auth.attempt_authentication(FakeClient(), logging.getLogger("test"))

        self.assertEqual(attempt.kind.value, "already_online")

    def test_server_rejection_returns_rejected_without_raw_response(self):
        class FakeClient:
            def check_status(self):
                return campus_auth.AuthStatus.UNAUTHENTICATED

            def login(self):
                return campus_auth.LoginResult(
                    ok=False,
                    message="账号或密码错误",
                    raw='{"password":"should-not-leak"}',
                )

        attempt = campus_auth.attempt_authentication(FakeClient(), logging.getLogger("test"))

        self.assertEqual(attempt.kind.value, "rejected")
        self.assertEqual(attempt.message, "账号或密码错误")
        self.assertNotIn("password", attempt.message)

    def test_pre_response_exception_returns_transient_error(self):
        class FakeClient:
            def check_status(self):
                raise OSError("network unavailable")

        attempt = campus_auth.attempt_authentication(FakeClient(), logging.getLogger("test"))

        self.assertEqual(attempt.kind.value, "transient_error")
        self.assertEqual(attempt.message, "network unavailable")

    def test_force_login_skips_the_already_authenticated_short_circuit(self):
        class FakeClient:
            def check_status(self):
                return campus_auth.AuthStatus.AUTHENTICATED

            def login(self):
                return campus_auth.LoginResult(ok=True, message="success")

        attempt = campus_auth.attempt_authentication(
            FakeClient(), logging.getLogger("test"), force_login=True
        )

        self.assertEqual(attempt.kind.value, "login_succeeded")


if __name__ == "__main__":
    unittest.main()
