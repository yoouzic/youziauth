import contextlib
import importlib.util
import inspect
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import threading

import dorm_login
from dorm_checkin import CheckinError, Store

PRESENT = importlib.util.find_spec('dorm_login') is not None
if PRESENT:
    from dorm_login import extract_token


class FeatureExists(unittest.TestCase):
    def test_login_exists(self):
        self.assertTrue(PRESENT, 'Dorm login module missing')


@unittest.skipUnless(PRESENT, 'Login module missing')
class LoginTests(unittest.TestCase):
    def test_owned_browser_uses_native_explicit_port_and_closes_only_owned_process(self):
        import dorm_login
        from pathlib import Path
        process = Mock()
        process.poll.return_value = None
        playwright = Mock()
        browser = playwright.chromium.connect_over_cdp.return_value
        with patch.object(dorm_login, 'browser_executable', return_value=Path('C:/Browser/chrome.exe')), \
             patch.object(dorm_login.subprocess, 'Popen', return_value=process) as popen, \
             patch.object(dorm_login.socket, 'create_connection', return_value=MagicMock()):
            with dorm_login.owned_browser(playwright, threading.Event()) as actual:
                self.assertIs(actual, browser)
            command = popen.call_args.args[0]
            port_arg = next(x for x in command if x.startswith('--remote-debugging-port='))
            self.assertGreater(int(port_arg.split('=')[1]), 0)
            self.assertIn('--remote-debugging-address=127.0.0.1', command)
            self.assertTrue(any(x.startswith('--user-data-dir=') and 'youziauth-login-' in x for x in command))
            browser.close.assert_called_once()
            process.terminate.assert_called_once()

    def test_login_navigation_primes_cas_then_follows_federation(self):
        """导航顺序：先初始页，再联邦中转页；中转页会自动重定向到 CAS 登录页。"""
        import dorm_login
        self.assertTrue(hasattr(dorm_login, 'open_login_page'), 'Federation navigation missing')
        page = Mock()
        page.url = 'https://uaaap.swu.edu.cn/cas/login?service=example'
        urls = []
        def navigate(url, **kwargs):
            urls.append(url)
            return Mock(status=200)
        page.goto.side_effect = navigate
        dorm_login.open_login_page(page, threading.Event())
        self.assertEqual(urls[0], dorm_login.INIT_URL)
        self.assertEqual(urls[1], dorm_login.LOGIN_URL)
        # 落点由服务端重定向决定，不再由客户端追加 federalEnable 触发
        self.assertEqual(len(urls), 2)

    def test_navigation_http_error_is_reported_not_swallowed(self):
        import dorm_login
        self.assertTrue(hasattr(dorm_login, 'open_login_page'))
        page = Mock()
        page.goto.return_value = Mock(status=400)
        from dorm_checkin import CheckinError
        with self.assertRaisesRegex(CheckinError, 'HTTP 400'):
            dorm_login.open_login_page(page, threading.Event())

    def test_cas_page_is_reachable_without_extra_navigation(self):
        """落点即 CAS 登录页，不再做多余的重复导航。

        实测背景：对已落地的 CAS URL 再导航一次（无论是否追加 federalEnable=true）
        都会被服务端返回 HTTP 400，导致登录失败并在自动打卡中反复重试
        （表现为「连续登录」）。因此修复后只做两次必要导航，落点校验改为信任集合。
        """
        import dorm_login
        original = 'https://uaaap.swu.edu.cn/cas/login?service=https%3a%2f%2fexample&state=a%20b%2f~'
        page = Mock(url=original)
        urls = []
        def navigate(url, **kwargs):
            urls.append(url)
            return Mock(status=200)
        page.goto.side_effect = navigate
        dorm_login.open_login_page(page, threading.Event())
        # 只做 INIT_URL → LOGIN_URL 两次导航，不再追加第三次
        self.assertEqual(len(urls), 2)
        self.assertEqual(urls[0], dorm_login.INIT_URL)
        self.assertEqual(urls[1], dorm_login.LOGIN_URL)
        # 不得把已经落地的 CAS URL 原样或改写后再导航
        self.assertFalse(any('federalEnable' in u for u in urls))
        self.assertNotIn(original, urls)

    def test_uaaap_cas_page_is_a_trusted_landing(self):
        """uaaap.swu.edu.cn 必须被视为合法落点（实际登录页就在这里）。"""
        import dorm_login
        self.assertIn('uaaap.swu.edu.cn', dorm_login.TRUSTED_AUTH_HOSTS)

    def test_unified_login_is_entered_by_clicking_not_by_renavigating(self):
        """推进到 IDM 表单页只能靠**点击**「统一认证登录」，不能重新导航。

        实测背景：该按钮的 onclick 是 _goLogin()，给当前 URL 追加 federalEnable=true
        后由浏览器自身发起同源导航，落到 idm.swu.edu.cn/am/UI/Login。
        用 page.goto 重新导航同一 URL（原样或带 federalEnable）会被站点动态防护
        返回 HTTP 400 —— 这就是「连续登录」的根因。按钮文字又画在图片里
        （DOM 中搜「统一认证登录」命中 0 次），所以只能靠 onclick 定位。
        """
        import dorm_login
        stages = iter(['cas', 'cas', 'idm'])
        page = Mock()
        page.evaluate.side_effect = lambda js, *a: (
            next(stages) if js is dorm_login.JS_PROBE_LOGIN_STAGE else True)
        self.assertTrue(dorm_login.enter_idm_login(page, threading.Event()))
        page.goto.assert_not_called()
        clicks = [c for c in page.evaluate.call_args_list
                  if c.args and c.args[0] is dorm_login.JS_CLICK_UNIFIED_LOGIN]
        self.assertEqual(len(clicks), 1, '必须恰好点击一次「统一认证登录」')

    def test_unified_login_entry_skips_work_when_already_on_idm_form(self):
        import dorm_login
        page = Mock()
        page.evaluate.return_value = 'idm'
        self.assertTrue(dorm_login.enter_idm_login(page, threading.Event()))
        page.goto.assert_not_called()
        page.evaluate.assert_called_once()  # 已在表单页，连探测都不必再点

    def test_unified_login_entry_is_a_noop_off_the_cas_choice_page(self):
        """落点不是 CAS 选择页时不得乱点，也不得导航。"""
        import dorm_login
        page = Mock()
        page.evaluate.return_value = 'other'
        self.assertFalse(dorm_login.enter_idm_login(page, threading.Event()))
        page.goto.assert_not_called()
        page.evaluate.assert_called_once()

    def test_unified_login_entry_returns_early_when_form_never_appears(self):
        import dorm_login
        page = Mock()
        page.evaluate.return_value = 'cas'   # 点了但始终没进 IDM
        self.assertFalse(dorm_login.enter_idm_login(page, threading.Event(), timeout=0))
        page.goto.assert_not_called()

    def test_navigation_does_not_modify_untrusted_redirect(self):
        import dorm_login
        self.assertTrue(hasattr(dorm_login, 'open_login_page'))
        page = Mock(url='https://other.test/cas/login')
        page.goto.return_value = Mock(status=200)
        from dorm_checkin import CheckinError
        with self.assertRaises(CheckinError):
            dorm_login.open_login_page(page, threading.Event())
        self.assertEqual(page.goto.call_count, 2)

    def test_cancelled_navigation_does_not_open_school_page(self):
        import dorm_login
        self.assertTrue(hasattr(dorm_login, 'open_login_page'))
        cancel = threading.Event()
        cancel.set()
        page = Mock()
        from dorm_checkin import CheckinError
        with self.assertRaises(CheckinError):
            dorm_login.open_login_page(page, cancel)
        page.goto.assert_not_called()

    def test_resume_target_upgrades_scheme_and_rejects_untrusted(self):
        """服务端给的下一跳只能在学校域名内；http 同主机升级成 https。"""
        self.assertEqual(
            dorm_login.resume_target('http://idm.swu.edu.cn/am/oauth2/authorize?service=x'),
            'https://idm.swu.edu.cn/am/oauth2/authorize?service=x')
        self.assertEqual(
            dorm_login.resume_target('https://of.swu.edu.cn/#/casLogin?from=%2FappCenter'),
            'https://of.swu.edu.cn/#/casLogin?from=%2FappCenter')
        for value in ('https://evil.test/ok', 'javascript:alert(1)', '', None,
                      'https://idm.swu.edu.cn/am/UI/Login?realm=%2F'):
            self.assertEqual(dorm_login.resume_target(value), '', value)

    def test_attempt_log_records_controlled_lines_and_never_raises(self):
        """静默登录必须留下可诊断的记录（这次事故本地零痕迹，只能读代码推）。"""
        import tempfile
        from pathlib import Path
        from dorm_checkin import Store
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory), protector=Mock(protect=lambda b: b, unprotect=lambda b: b))
            log = dorm_login.attempt_log(store)
            log('第 1 次：认证通过（判据=header）')
            text = (Path(directory) / 'login.log').read_text(encoding='utf-8')
            self.assertIn('判据=header', text)
            with patch.object(Path, 'write_text', side_effect=OSError('denied')):
                log('写不进去也不能抛')          # 诊断日志绝不影响登录
        self.assertIsNotNone(dorm_login.attempt_log(Mock(spec=[])))   # 没有 root 时退化为空实现

    def response(self, url='https://of.swu.edu.cn/gateway/auth/exchange-token', **kwargs):
        return Mock(url=url, status=200, headers={}, json=lambda: {'code': 200, 'data': 'secret'}, **kwargs)

    def test_only_school_https_exchange_response_is_accepted(self):
        self.assertEqual(extract_token(self.response()), 'secret')
        for url in ['https://of.swu.edu.cn.evil.test/gateway/auth/exchange-token',
                    'http://of.swu.edu.cn/gateway/auth/exchange-token',
                    'https://of.swu.edu.cn:8443/gateway/auth/exchange-token',
                    'https://other.test/?exchange-token',
                    'https://of.swu.edu.cn/gateway/other?exchange-token']:
            self.assertIsNone(extract_token(self.response(url)))

    def test_dict_body_is_not_a_token(self):
        r = self.response()
        r.json = lambda: {'code': 200, 'data': {'token': 'secret'}}
        self.assertIsNone(extract_token(r))

    def test_header_token_supported(self):
        r = self.response()
        r.headers = {'fighter-auth-token': 'header-token'}
        self.assertEqual(extract_token(r), 'header-token')

    def test_newline_token_rejected(self):
        r = self.response()
        r.headers = {'fighter-auth-token': 'bad\r\ntoken'}
        r.json = lambda: {}
        self.assertIsNone(extract_token(r))


class SessionLoginTests(unittest.TestCase):
    def setUp(self):
        self.assertIn('interactive', inspect.signature(dorm_login.login).parameters,
                      'Non-interactive session renewal missing')
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name), protector=Mock(protect=lambda b: b[::-1], unprotect=lambda b: b[::-1]))
        self.cookies = [dict(name='SSO', value='session-cookie', domain='idm.swu.edu.cn',
                             path='/', expires=-1, httpOnly=True, secure=True, sameSite='Lax')]
        self.page = Mock(url='https://of.swu.edu.cn/')
        self.page.locator.return_value.first.is_visible.return_value = False
        self.context = Mock(pages=[self.page])
        self.context.cookies.return_value = self.cookies
        self.browser = Mock(contexts=[self.context])
        self.api = Mock()
        self.api.user.return_value = 'student'
        self.capture = None
        self.issue_token = True
        self.navigations = []
        self.restored = []
        self.context.on.side_effect = self.listen
        self.context.add_cookies.side_effect = self.restored.extend
        self.page.goto.side_effect = self.navigate
        driver = MagicMock()
        module = patch.dict(sys.modules, {'playwright.sync_api': driver})
        module.start()
        self.addCleanup(module.stop)
        owned = patch.object(dorm_login, 'owned_browser')
        self.owned = owned.start()
        self.owned.return_value.__enter__.return_value = self.browser
        self.addCleanup(owned.stop)

    def listen(self, name, capture):
        if name == 'response':
            self.capture = capture

    def navigate(self, url, **kwargs):
        self.navigations.append(url)
        if self.issue_token:
            self.capture(Mock(url='https://of.swu.edu.cn/gateway/auth/exchange-token', status=200,
                              headers={'fighter-auth-token': 'new-token'}))
        return Mock(status=200)

    def seed_session(self):
        self.store.save_token('old-token')
        self.store.save_browser_session('old-token', 'student', self.cookies)

    def test_interactive_login_saves_only_school_cookies(self):
        self.context.cookies.return_value = self.cookies + [dict(self.cookies[0], domain='unrelated.test')]
        self.assertEqual(dorm_login.login(self.store, self.api, threading.Event()), 'student')
        self.assertEqual(self.store.token(), 'new-token')
        self.assertEqual(self.store.browser_session('new-token')['cookies'], self.cookies)
        self.assertEqual(self.navigations, [dorm_login.INIT_URL])

    def test_credentials_trigger_unified_login_entry_before_silent_attempt(self):
        """配了凭据时，login() 必须先点「统一认证登录」把页面推进到 IDM 表单页。

        没有这一步，落点永远停在 uaaap 的 CAS 选择页，on_idm_login_page 不成立，
        静默登录形同虚设（这正是「静默登录从未生效」的原因）。
        """
        import dorm_login
        self.issue_token = False
        entries = []

        def fake_enter(page, cancel, **kwargs):
            entries.append(page)
            self.capture(Mock(url='https://of.swu.edu.cn/gateway/auth/exchange-token',
                              status=200, headers={'fighter-auth-token': 'new-token'}))
            return True

        credentials = Mock(username='u', password='p')
        with patch.object(dorm_login, 'resolve_idm_credentials', return_value=credentials), \
             patch.object(dorm_login, 'enter_idm_login', side_effect=fake_enter):
            self.assertEqual(dorm_login.login(self.store, self.api, threading.Event()), 'student')
        self.assertEqual(entries, [self.page])

    def test_manual_login_does_not_click_for_the_user(self):
        """没配凭据时不得替用户点「统一认证登录」。

        该落地页是「统一认证登录 / 钉钉扫码登录 / 特定账号登录」三选一的选择页，
        纯人工登录必须把选择权留给用户。
        """
        import dorm_login
        with patch.object(dorm_login, 'resolve_idm_credentials', return_value=None), \
             patch.object(dorm_login, 'enter_idm_login') as enter:
            self.assertEqual(dorm_login.login(self.store, self.api, threading.Event()), 'student')
        enter.assert_not_called()

    def test_renewal_restores_cookies_and_rotates_bound_token(self):
        self.seed_session()
        self.assertEqual(dorm_login.login(self.store, self.api, threading.Event(), interactive=False), 'student')
        self.assertEqual(self.restored, self.cookies)
        self.assertEqual(self.store.token(), 'new-token')
        self.assertIsNone(self.store.browser_session('old-token'))
        self.assertEqual(self.store.browser_session('new-token')['student'], 'student')
        self.assertTrue(self.owned.call_args.kwargs['headless'])

    def test_token_only_install_does_not_open_a_browser_for_renewal(self):
        self.store.save_token('old-token')
        with patch.object(dorm_login, 'resolve_idm_credentials', return_value=None):
            with self.assertRaises(CheckinError) as caught:
                dorm_login.login(self.store, self.api, threading.Event(), interactive=False)
        self.assertEqual(caught.exception.state, 'login_required')
        self.assertIn('统一认证凭据', str(caught.exception))
        self.owned.assert_not_called()
        self.assertEqual(self.store.token(), 'old-token')

    def test_unattended_login_can_start_from_scratch_with_saved_credentials(self):
        """用户选择：没有会话、但存了统一认证凭据时，允许 headless 自己登一次。

        走的是既有的静默登录链路（自动填学号密码 + 本地模型识别验证码），
        所以这里只需要证明：没有 cookie 时不再提前拒绝，而是真的开浏览器并保存新会话。
        """
        entries = []

        def fake_enter(page, cancel, **kwargs):
            entries.append(page)
            self.capture(Mock(url='https://of.swu.edu.cn/gateway/auth/exchange-token',
                              status=200, headers={'fighter-auth-token': 'fresh-token'}))
            return True

        self.issue_token = False        # 令牌只能来自「静默登录成功」这条路
        credentials = Mock(username='u', password='p')
        with patch.object(dorm_login, 'resolve_idm_credentials', return_value=credentials), \
             patch.object(dorm_login, 'enter_idm_login', side_effect=fake_enter):
            self.assertEqual(dorm_login.login(self.store, self.api, threading.Event(),
                                              interactive=False), 'student')
        self.assertEqual(entries, [self.page])
        self.assertEqual(self.owned.call_args.kwargs, {'headless': True})
        self.assertEqual(self.store.token(), 'fresh-token')
        self.assertEqual(self.store.browser_session('fresh-token')['student'], 'student')
        log = (self.store.root / 'login.log').read_text(encoding='utf-8')
        self.assertIn('完整登录流程', log)
        self.assertNotIn('登录凭据', log)          # 只记受控字段，绝不写凭据本身

    def test_unattended_first_login_reports_bad_credentials(self):
        """无人值守提交凭据后被判「密码错」时，必须把这句话报出来。"""
        silent = Mock(ok=False, reason='bad_credentials', cookies={}, resume_url=None,
                      accepted_by=None)
        with patch.object(dorm_login, 'resolve_idm_credentials',
                          return_value=Mock(username='u', password='p')), \
             patch.object(dorm_login, 'enter_idm_login', return_value=True), \
             patch('idm_login.attempt_silent_login', return_value=silent):
            self.issue_token = False
            with self.assertRaises(CheckinError) as caught:
                dorm_login.login(self.store, self.api, threading.Event(), timeout=0,
                                 interactive=False)
        self.assertIn('用户名或密码错误', str(caught.exception))

    def test_captcha_timeout_discards_unusable_session_not_token(self):
        self.seed_session()
        self.issue_token = False
        self.page.locator.return_value.first.is_visible.return_value = True
        with self.assertRaises(CheckinError) as caught:
            dorm_login.login(self.store, self.api, threading.Event(), timeout=0, interactive=False)
        self.assertEqual(caught.exception.state, 'login_required')
        self.assertIn('手动', str(caught.exception))
        self.assertEqual(self.store.token(), 'old-token')
        self.assertIsNone(self.store.browser_session('old-token'))
        self.owned.reset_mock()
        with self.assertRaises(CheckinError):
            dorm_login.login(self.store, self.api, threading.Event(), interactive=False)
        self.owned.assert_not_called()

    def test_renewal_never_switches_accounts(self):
        self.seed_session()
        self.api.user.return_value = 'another-student'
        with self.assertRaises(CheckinError) as caught:
            dorm_login.login(self.store, self.api, threading.Event(), interactive=False)
        self.assertEqual(caught.exception.state, 'login_required')
        self.assertEqual(self.store.token(), 'old-token')
        self.assertIsNone(self.store.browser_session('old-token'))

    def test_network_failure_preserves_session_for_later(self):
        self.seed_session()
        self.page.goto.side_effect = TimeoutError('private-cookie')
        with self.assertRaises(CheckinError) as caught:
            dorm_login.login(self.store, self.api, threading.Event(), interactive=False)
        self.assertEqual(caught.exception.state, 'network_error')
        self.assertNotIn('private-cookie', str(caught.exception))
        self.assertIsNotNone(self.store.browser_session('old-token'))

    def test_exchange_service_failure_preserves_session(self):
        self.seed_session()
        self.issue_token = False
        def unavailable(url, **kwargs):
            self.capture(Mock(url='https://of.swu.edu.cn/gateway/auth/exchange-token', status=503, headers={}))
            return Mock(status=200)
        self.page.goto.side_effect = unavailable
        with self.assertRaises(CheckinError) as caught:
            dorm_login.login(self.store, self.api, threading.Event(), timeout=0, interactive=False)
        self.assertEqual(caught.exception.state, 'network_error')
        self.assertIsNotNone(self.store.browser_session('old-token'))

    def test_exchange_auth_rejection_clears_session(self):
        self.seed_session()
        self.issue_token = False
        def rejected(url, **kwargs):
            self.capture(Mock(url='https://of.swu.edu.cn/gateway/auth/exchange-token', status=200,
                              headers={}, json=lambda: {'code': 401, 'data': None}))
            return Mock(status=200)
        self.page.goto.side_effect = rejected
        with self.assertRaises(CheckinError) as caught:
            dorm_login.login(self.store, self.api, threading.Event(), timeout=0, interactive=False)
        self.assertEqual(caught.exception.state, 'login_required')
        self.assertIsNone(self.store.browser_session('old-token'))

    def test_silent_exchange_timeout_preserves_session(self):
        self.seed_session()
        self.issue_token = False
        with self.assertRaises(CheckinError) as caught:
            dorm_login.login(self.store, self.api, threading.Event(), timeout=0, interactive=False)
        self.assertEqual(caught.exception.state, 'network_error')
        self.assertIsNotNone(self.store.browser_session('old-token'))

    def test_callback_page_waits_for_delayed_exchange_without_navigating_again(self):
        self.seed_session()
        self.issue_token = False
        self.page.url = 'https://of.swu.edu.cn/#/casLogin?from=%2FappCenter'
        self.page.wait_for_timeout.side_effect = lambda delay: self.capture(Mock(
            url='https://of.swu.edu.cn/gateway/auth/exchange-token', status=200,
            headers={'fighter-auth-token': 'new-token'}))
        self.assertEqual(dorm_login.login(self.store, self.api, threading.Event(), interactive=False), 'student')
        # 页面已经在收尾落点：**一次导航都不该发生** —— 导航就是把正在走的跳转打断
        # （「第一次登录成功后页面被跳走」就是这么来的）。只等 SPA 触发 exchange-token。
        self.assertEqual(self.navigations, [])

    def test_silent_success_follows_the_server_next_hop_instead_of_restarting(self):
        """★ 认证通过后必须顺着服务端给的下一跳走完 SSO，而不是从 CAS 初始页重走。

        旧行为正是「打断原先的登录跳转逻辑」：明明已经认证成功，却把正在走的登录页
        导航掉、从初始页重走一遍（用户看到「学校登录页被跳走/替换」）。
        """
        import idm_login
        target = 'http://idm.swu.edu.cn/am/oauth2/authorize?service=initService'
        silent = idm_login.SilentResult(ok=True, reason='authenticated',
                                        cookies={'AMAuthCookie': 'fresh'},
                                        resume_url=target, accepted_by='redirect')
        credentials = Mock(username='20230001', password='secret-pw')
        self.issue_token = False
        self.page.wait_for_timeout.side_effect = lambda delay: self.capture(Mock(
            url='https://of.swu.edu.cn/gateway/auth/exchange-token', status=200,
            headers={'fighter-auth-token': 'new-token'}))
        with patch.object(dorm_login, 'resolve_idm_credentials', return_value=credentials), \
             patch.object(dorm_login, 'enter_idm_login', return_value=True), \
             patch.object(idm_login, 'attempt_silent_login', return_value=silent) as attempt:
            self.assertEqual(dorm_login.login(self.store, self.api, threading.Event()), 'student')
        self.assertEqual(attempt.call_count, 1)
        self.assertIn('https://idm.swu.edu.cn/am/oauth2/authorize?service=initService',
                      self.navigations, '必须顺着服务端给的下一跳走')
        self.assertEqual(self.navigations[-1],
                         'https://idm.swu.edu.cn/am/oauth2/authorize?service=initService',
                         '认证成功后不得再从 CAS 初始页重走')
        self.assertEqual(self.store.token(), 'new-token')

    def test_unconfirmed_silent_attempt_verifies_the_session_without_logging_in_again(self):
        """★ 判据缺失/响应丢失时先验证会话，**绝不**再登一次。"""
        import idm_login
        silent = idm_login.SilentResult(ok=False, reason='unconfirmed',
                                        cookies={'AMAuthCookie': 'maybe'})
        credentials = Mock(username='20230001', password='secret-pw')
        self.issue_token = False
        self.page.wait_for_timeout.side_effect = lambda delay: self.capture(Mock(
            url='https://of.swu.edu.cn/gateway/auth/exchange-token', status=200,
            headers={'fighter-auth-token': 'new-token'}))
        with patch.object(dorm_login, 'resolve_idm_credentials', return_value=credentials), \
             patch.object(dorm_login, 'enter_idm_login', return_value=True), \
             patch.object(idm_login, 'attempt_silent_login', return_value=silent) as attempt:
            self.assertEqual(dorm_login.login(self.store, self.api, threading.Event()), 'student')
        self.assertEqual(attempt.call_count, 1, '未确认绝不能再自动登一次')
        adopted = [c for c in self.restored if c.get('value') == 'maybe']
        self.assertTrue(adopted, '未确认时必须把会话接回浏览器去验证')
        self.assertEqual(adopted[0]['domain'], '.swu.edu.cn')

    def test_transient_identity_failure_keeps_the_new_session(self):
        """交换已成功 = 登录已成功：身份核验的偶发抖动不得把这次登录丢掉。"""
        self.seed_session()
        self.api.user.side_effect = CheckinError('network_error', '学校接口暂不可用')
        with patch.object(dorm_login, 'IDENTITY_RETRY_SECONDS', 0):
            with self.assertRaises(CheckinError) as caught:
                dorm_login.login(self.store, self.api, threading.Event(), interactive=False)
        self.assertEqual(caught.exception.state, 'network_error')
        self.assertEqual(self.store.token(), 'new-token', '登录成果必须保住')
        self.assertEqual(self.store.browser_session('new-token')['student'], 'student')

    def test_identity_check_retries_transient_failures(self):
        self.seed_session()
        self.api.user.side_effect = [CheckinError('network_error', '抖动'), 'student']
        with patch.object(dorm_login, 'IDENTITY_RETRY_SECONDS', 0):
            self.assertEqual(dorm_login.login(self.store, self.api, threading.Event()), 'student')
        self.assertEqual(self.store.token(), 'new-token')

    def test_explicit_silent_failure_refreshes_the_form_before_handing_over(self):
        """★ 交回人工前必须刷新出一张**可用**的登录表单。

        失败的那个 POST 会让会话失效，而且我们取过的验证码会让页面上显示的那张作废
        （服务端只认最后一张）—— 不刷新的话，用户照着图输入也必然失败。
        真人手动登录本身是可行的（实测：被 WAF 拦的只是脚本发起的 POST）。
        """
        import idm_login
        silent = idm_login.SilentResult(ok=False, reason='bad_credentials')
        credentials = Mock(username='20230001', password='wrong-pw')
        self.issue_token = False
        with patch.object(dorm_login, 'resolve_idm_credentials', return_value=credentials), \
             patch.object(dorm_login, 'enter_idm_login', return_value=True) as enter, \
             patch.object(idm_login, 'attempt_silent_login', return_value=silent):
            with self.assertRaises(CheckinError):
                dorm_login.login(self.store, self.api, threading.Event(), timeout=0)
        self.assertEqual(enter.call_count, 2, '自动尝试 + 交回人工前的刷新，各一次')
        text = (self.store.root / 'login.log').read_text(encoding='utf-8')
        self.assertIn('交回人工', text)

    def test_unconfirmed_result_is_verified_without_refreshing_the_form(self):
        """未确认时先验证会话：不得为了「交回人工」提前把页面刷新掉。"""
        import idm_login
        silent = idm_login.SilentResult(ok=False, reason='unconfirmed',
                                        cookies={'AMAuthCookie': 'maybe'})
        credentials = Mock(username='20230001', password='secret-pw')
        self.issue_token = False
        with patch.object(dorm_login, 'resolve_idm_credentials', return_value=credentials), \
             patch.object(dorm_login, 'enter_idm_login', return_value=True) as enter, \
             patch.object(idm_login, 'attempt_silent_login', return_value=silent):
            with self.assertRaises(CheckinError):
                dorm_login.login(self.store, self.api, threading.Event(), timeout=0)
        self.assertEqual(enter.call_count, 1, '只有最初那一次，没有「交回人工」的刷新')
        text = (self.store.root / 'login.log').read_text(encoding='utf-8')
        self.assertNotIn('交回人工', text)
        self.assertIn('未确认', text)

    def test_automatic_failure_does_not_refresh_for_a_human_who_is_not_there(self):
        """非交互（夜间无人值守）不刷新：没人可交回，别白跑一趟链路。"""
        import idm_login
        silent = idm_login.SilentResult(ok=False, reason='bad_credentials')
        credentials = Mock(username='20230001', password='wrong-pw')
        self.issue_token = False
        self.seed_session()
        with patch.object(dorm_login, 'resolve_idm_credentials', return_value=credentials), \
             patch.object(dorm_login, 'enter_idm_login', return_value=True) as enter, \
             patch.object(idm_login, 'attempt_silent_login', return_value=silent):
            with self.assertRaises(CheckinError):
                dorm_login.login(self.store, self.api, threading.Event(),
                                 timeout=0, interactive=False)
        self.assertEqual(enter.call_count, 1)

    def test_handover_refresh_is_skipped_while_the_human_is_typing(self):
        """★ 用户已经开始手动输入时不许刷新页面 —— 那会把他的输入冲掉。

        （也就不会出现「程序把我的登录页面跳走」这种体验问题。）
        """
        import idm_login
        silent = idm_login.SilentResult(ok=False, reason='bad_credentials')
        credentials = Mock(username='20230001', password='wrong-pw')
        self.issue_token = False
        with patch.object(dorm_login, 'resolve_idm_credentials', return_value=credentials), \
             patch.object(dorm_login, 'enter_idm_login', return_value=True) as enter, \
             patch.object(dorm_login, 'form_in_use', return_value=True), \
             patch.object(idm_login, 'attempt_silent_login', return_value=silent):
            with self.assertRaises(CheckinError):
                dorm_login.login(self.store, self.api, threading.Event(), timeout=0)
        self.assertEqual(enter.call_count, 1, '不得为了交回人工去刷新用户正在用的页面')
        text = (self.store.root / 'login.log').read_text(encoding='utf-8')
        self.assertIn('已开始手动输入', text)
        self.assertNotIn('刷新登录页', text)

    def test_form_in_use_reads_the_visible_fields_only(self):
        page = Mock()
        page.evaluate.return_value = True
        self.assertTrue(dorm_login.form_in_use(page))
        page.evaluate.return_value = Mock()          # 替身/读不到 -> 当作没人输入
        self.assertFalse(dorm_login.form_in_use(page))
        page.evaluate.side_effect = RuntimeError('page closed')
        self.assertFalse(dorm_login.form_in_use(page))

    def test_dead_restored_session_is_cleared_before_the_new_login(self):
        """★ 旧会话没接上时必须**清空学校 cookie** 再登，否则动态防护一律 400。

        实机根因（2026-09-26）：上一次登录保存的学校 cookie 被灌进新浏览器后，
        新旧两代「动态防护 cookie」混在一起，站点对 /am/ 下所有请求回 400 空白页
        （同一批 cookie 取验证码 → 400；全新会话 → 200）。链路因此死在
        `idm.swu.edu.cn/am/oauth2/authorize` 上，界面卡住不动。
        """
        self.seed_session()                      # 旧会话（cookie 已失效）
        self.issue_token = False
        real_navigate = self.page.goto.side_effect

        def navigate(url, **kwargs):
            # 第一次走链路（INIT/LOGIN）不发 token，模拟"旧会话接不上"；
            # 清干净重走时才发 —— 对应实机里"干净会话一切正常"。
            if len(self.navigations) >= 2:
                self.issue_token = True
            return real_navigate(url, **kwargs)

        self.page.goto.side_effect = navigate
        self.assertEqual(dorm_login.login(self.store, self.api, threading.Event(),
                                          interactive=False), 'student')
        self.context.clear_cookies.assert_called_once()
        self.assertEqual(self.store.token(), 'new-token')

    def test_valid_restored_session_is_not_cleared(self):
        """旧会话还能接上（拿到 token）时不得多此一举清 cookie。"""
        self.seed_session()
        self.assertEqual(dorm_login.login(self.store, self.api, threading.Event(),
                                          interactive=False), 'student')
        self.context.clear_cookies.assert_not_called()
        self.assertEqual(self.navigations, [dorm_login.INIT_URL])

    def test_blocked_authorize_hop_fails_fast_instead_of_waiting_five_minutes(self):
        """认证后的授权跳转被 400 时立刻报错 —— 不让人对着卡住的页面干等。"""
        self.seed_session()
        self.issue_token = False
        real_navigate = self.page.goto.side_effect

        def navigate(url, **kwargs):
            self.capture(Mock(
                url='https://idm.swu.edu.cn/am/oauth2/authorize?service=initService&decision=Allow',
                status=400, headers={}, request=Mock(method='GET')))
            return real_navigate(url, **kwargs)

        self.page.goto.side_effect = navigate
        started = time.monotonic()
        with self.assertRaises(CheckinError) as caught:
            dorm_login.login(self.store, self.api, threading.Event(), interactive=False)
        self.assertIn('授权跳转', str(caught.exception))
        self.assertLess(time.monotonic() - started, 5, '必须立刻失败，不能等满超时')

    def test_authorize_hop_helper_is_precise(self):
        self.assertTrue(dorm_login.is_authorize_hop(
            'https://idm.swu.edu.cn/am/oauth2/authorize?service=initService'))
        for value in ('https://idm.swu.edu.cn/am/UI/Login', 'https://evil.test/am/oauth2/authorize',
                      'https://of.swu.edu.cn/gateway/auth/exchange-token'):
            self.assertFalse(dorm_login.is_authorize_hop(value), value)

    def test_headless_disguise_swaps_ua_and_viewport(self):
        """★ 无人值守（headless）必须伪装成普通 Chrome。

        实测根因（2026-09-26 晚）：headless 的 UA 含 `HeadlessChrome/153.0.0.0`、
        屏幕 800x600 → `uaaap/cas/login` 那一跳被判 **400**，进不了 IDM 表单页，
        续期三次全败；有头（手动登录）同一链路逐跳正常。
        """
        page = Mock()
        page.evaluate.return_value = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                                      '(KHTML, like Gecko) HeadlessChrome/153.0.0.0 Safari/537.36')
        session = page.context.new_cdp_session.return_value
        self.assertTrue(dorm_login.disguise_headless(page))
        sent = {call.args[0]: call.args[1] for call in session.send.call_args_list}
        ua = sent['Network.setUserAgentOverride']['userAgent']
        self.assertNotIn('HeadlessChrome', ua)
        self.assertIn('Chrome/153.0.0.0', ua)
        self.assertEqual(sent['Emulation.setDeviceMetricsOverride']['screenWidth'], 1536)
        self.assertEqual(sent['Emulation.setDeviceMetricsOverride']['width'], 1280)

    def test_headful_page_is_left_alone(self):
        page = Mock()
        page.evaluate.return_value = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                                      '(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36')
        self.assertFalse(dorm_login.disguise_headless(page))
        page.context.new_cdp_session.assert_not_called()

    def test_disguise_failure_never_breaks_the_login(self):
        closed = Mock()
        closed.evaluate.side_effect = RuntimeError('page closed')
        self.assertFalse(dorm_login.disguise_headless(closed))
        no_cdp = Mock()
        no_cdp.evaluate.return_value = 'HeadlessChrome/1'
        no_cdp.context.new_cdp_session.side_effect = RuntimeError('cdp gone')
        self.assertFalse(dorm_login.disguise_headless(no_cdp))
        weird = Mock()
        weird.evaluate.return_value = Mock()          # 读不到字符串也不能炸
        self.assertFalse(dorm_login.disguise_headless(weird))

    def test_adopting_a_session_replaces_browser_cookies_instead_of_duplicating(self):
        """★ 灌会话前必须先清：同名两代 cookie 并存会让授权跳转被判重放（400）。

        实机链路（2026-09-26 15:27）：`X-AuthErrorCode=0` 认证通过之后，
        浏览器带着「自己的 + 灌进来的」两套同名 cookie 走授权跳转 → 400 → 拿不到 token。
        """
        context = Mock()
        self.assertTrue(dorm_login.adopt_idm_cookies(context, {'61zqO': 'session'}))
        context.clear_cookies.assert_called_once()
        self.assertEqual(context.add_cookies.call_args.args[0],
                         [{'name': '61zqO', 'value': 'session',
                           'domain': '.swu.edu.cn', 'path': '/'}])
        # 清不掉也要把会话灌进去，不能因为清 cookie 失败就放弃
        context.reset_mock()
        context.clear_cookies.side_effect = RuntimeError('busy')
        self.assertTrue(dorm_login.adopt_idm_cookies(context, {'61zqO': 'session'}))
        context.add_cookies.assert_called_once()
        # 没有 cookie 就什么都不做
        context.reset_mock()
        self.assertFalse(dorm_login.adopt_idm_cookies(context, {}))
        context.add_cookies.assert_not_called()

    def test_failure_reason_is_written_to_the_diagnostics_log(self):
        self.seed_session()
        self.issue_token = False
        with self.assertRaises(CheckinError):
            dorm_login.login(self.store, self.api, threading.Event(), timeout=0)
        text = (self.store.root / 'login.log').read_text(encoding='utf-8')
        self.assertIn('登录未完成', text)

    def test_cancellation_on_last_wait_does_not_invalidate_session(self):
        self.seed_session()
        self.issue_token = False
        cancel = threading.Event()
        def stop(delay):
            cancel.set()
            time.sleep(.002)
        self.page.wait_for_timeout.side_effect = stop
        with self.assertRaises(CheckinError) as caught:
            dorm_login.login(self.store, self.api, cancel, timeout=.001, interactive=False)
        self.assertEqual(caught.exception.state, 'cancelled')
        self.assertIsNotNone(self.store.browser_session('old-token'))

    def test_cancellation_during_cookie_capture_does_not_commit_new_session(self):
        self.seed_session()
        cancel = threading.Event()
        def stop():
            cancel.set()
            return self.cookies
        self.context.cookies.side_effect = stop
        with self.assertRaises(CheckinError) as caught:
            dorm_login.login(self.store, self.api, cancel, interactive=False)
        self.assertEqual(caught.exception.state, 'cancelled')
        self.assertEqual(self.store.token(), 'old-token')
        self.assertIsNotNone(self.store.browser_session('old-token'))

    def test_cancelled_renewal_does_not_open_browser_or_clear_session(self):
        self.seed_session()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(CheckinError) as caught:
            dorm_login.login(self.store, self.api, cancel, interactive=False)
        self.assertEqual(caught.exception.state, 'cancelled')
        self.owned.assert_not_called()
        self.assertIsNotNone(self.store.browser_session('old-token'))

    def test_expired_cookies_do_not_start_renewal(self):
        self.store.save_token('old-token')
        self.store.save_browser_session('old-token', 'student', [dict(self.cookies[0], expires=1)])
        # 没有凭据时：过期会话不能拿去续期，也不必开浏览器。
        # （有凭据时的全流程登录是另一条路，见 test_unattended_login_can_start_from_scratch_…）
        with patch.object(dorm_login, 'resolve_idm_credentials', return_value=None):
            with self.assertRaises(CheckinError):
                dorm_login.login(self.store, self.api, threading.Event(), interactive=False)
        self.owned.assert_not_called()

    def test_failed_session_save_preserves_previous_matching_credentials(self):
        self.seed_session()
        with patch.object(self.store, 'save_browser_session', side_effect=OSError('disk full')):
            with self.assertRaises(CheckinError):
                dorm_login.login(self.store, self.api, threading.Event(), interactive=False)
        self.assertEqual(self.store.token(), 'old-token')
        self.assertIsNotNone(self.store.browser_session('old-token'))

    def test_interactive_login_recovers_from_unreadable_saved_session(self):
        self.seed_session()
        with patch.object(self.store, 'browser_session', side_effect=CheckinError('login_required', '损坏')):
            self.assertEqual(dorm_login.login(self.store, self.api, threading.Event()), 'student')
        self.assertEqual(self.store.token(), 'new-token')

    def test_cancel_after_identity_check_does_not_replace_saved_session(self):
        self.seed_session()
        cancel = threading.Event()
        def identify(token):
            cancel.set()
            return 'student'
        self.api.user.side_effect = identify
        with self.assertRaises(CheckinError) as caught:
            dorm_login.login(self.store, self.api, cancel, interactive=False)
        self.assertEqual(caught.exception.state, 'cancelled')
        self.assertEqual(self.store.token(), 'old-token')
        self.assertIsNotNone(self.store.browser_session('old-token'))


@unittest.skipUnless(os.environ.get('YOUZIAUTH_BROWSER_TESTS') == '1', 'Opt-in native browser test')
class BrowserSessionRuntimeTests(unittest.TestCase):
    def test_native_browser_restores_encrypted_session_after_process_restart(self):
        from urllib.parse import urlsplit
        from dorm_api import SwuApi
        from dorm_panel import DormController
        native_browser = dorm_login.owned_browser
        launches, restored_requests = [], []
        accept_session = True
        reject_old_token = False

        @contextlib.contextmanager
        def offline_browser(playwright, cancel, *, headless=False):
            launches.append(headless)
            generation = len(launches)
            with native_browser(playwright, cancel, headless=True) as browser:
                def respond(route):
                    request = route.request
                    url = urlsplit(request.url)
                    if url.hostname != 'of.swu.edu.cn':
                        route.abort()
                        return
                    if url.path == '/gateway/auth/exchange-token':
                        route.fulfill(status=200, content_type='application/json',
                                      body=json.dumps({'code': 200, 'data': f'offline-token-{generation}'}))
                        return
                    if url.path.startswith('/cas/'):
                        restored = 'SSO=offline-session-cookie' in request.all_headers().get('cookie', '')
                        if generation > 1:
                            restored_requests.append(restored)
                        authenticated = generation == 1 or (accept_session and restored)
                        html = ('<script>history.replaceState(null,"","/#/casLogin");'
                                'setTimeout(() => fetch("/gateway/auth/exchange-token"), 150)</script>' if authenticated else
                                '<form><input name="password" type="password"><input name="captcha"></form>')
                        headers = {'Set-Cookie': 'SSO=offline-session-cookie; Path=/; Secure; HttpOnly; SameSite=Lax'} if authenticated else {}
                        route.fulfill(status=200, content_type='text/html', headers=headers, body=html)
                        return
                    route.abort()
                browser.contexts[0].route('**/*', respond)
                yield browser

        def identity(method, path, token, **kwargs):
            self.assertTrue(token.startswith('offline-token-'))
            if reject_old_token and token == 'offline-token-1':
                raise CheckinError('login_required', '测试令牌已失效')
            if path == '/gateway/fighter-baida/api/cqtj/getTransitionByToday':
                return {'records': [], 'total': 0}
            self.assertEqual(path, '/gateway/fighter-middle/api/auth/user')
            return {'subject': {'username': 'offline-student'}}

        with tempfile.TemporaryDirectory() as directory, patch.object(dorm_login, 'owned_browser', offline_browser):
            store = Store(Path(directory))
            api, cancel = SwuApi(transport=identity), threading.Event()
            self.assertEqual(dorm_login.login(store, api, cancel, timeout=5), 'offline-student')
            self.assertEqual(store.token(), 'offline-token-1')
            restarted = Store(Path(directory))
            reject_old_token = True
            controller = DormController(restarted)
            controller.api = controller.engine.api = api
            try:
                self.assertTrue(controller.start('query'))
                deadline = time.monotonic() + 60
                while controller.busy and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertFalse(controller.busy)
                self.assertEqual(controller.drain()[-1].state, 'no_task')
            finally:
                controller.close()
            self.assertEqual(restarted.token(), 'offline-token-2')
            self.assertTrue(restored_requests and all(restored_requests))
            for path in Path(directory).rglob('*'):
                if path.is_file():
                    self.assertNotIn(b'offline-session-cookie', path.read_bytes())
                    self.assertNotIn(b'offline-token-', path.read_bytes())
            accept_session = False
            with self.assertRaises(CheckinError) as caught:
                dorm_login.login(restarted, api, cancel, timeout=.2, interactive=False)
            self.assertEqual(caught.exception.state, 'login_required')
            self.assertIsNone(restarted.browser_session(restarted.token()))
            with self.assertRaises(CheckinError):
                dorm_login.login(restarted, api, cancel, interactive=False)
            self.assertEqual(launches, [False, True, True])
            restarted.clear_token()
            self.assertEqual(restarted.token(), '')


if __name__ == '__main__':
    unittest.main()
