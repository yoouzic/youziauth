"""Updater routing is bound to its configured user, independently of other sessions."""

import inspect
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

import app_update


USER_A = 'S-1-5-21-1-2-3-1001'
USER_B = 'S-1-5-21-1-2-3-1002'
ENTRA_USER = 'S-1-12-1-1-2-3-4'


class FakeRegistry:
    HKEY_USERS = object()

    def __init__(self, users, values):
        self.users = users
        self.values = values
        self.opened = []

    def OpenKey(self, root, path):
        self.opened.append(path)
        if not path:
            return 'root'
        sid = path.split('\\', 1)[0]
        if sid not in self.values:
            raise OSError('no loaded hive')
        return sid

    def EnumKey(self, key, index):
        if index >= len(self.users):
            raise OSError('end')
        return self.users[index]

    def QueryValueEx(self, key, name):
        try:
            return self.values[key][name], 1
        except KeyError:
            raise OSError('no value') from None

    def CloseKey(self, key): pass


class ProxyIdentityTests(unittest.TestCase):
    def proxy(self, registry, user_sid=None):
        self.assertIn('user_sid', inspect.signature(app_update.interactive_user_proxy).parameters,
                      'proxy detection must accept the task owner SID')
        with patch.object(app_update.sys, 'platform', 'win32'), \
             patch.dict('sys.modules', {'winreg': registry}):
            return app_update.interactive_user_proxy(user_sid)

    def test_bound_detection_never_uses_the_first_other_users_proxy(self):
        registry = FakeRegistry([USER_B, USER_A], {
            USER_A: {'ProxyEnable': 1, 'ProxyServer': '127.0.0.1:7897'},
            USER_B: {'ProxyEnable': 1, 'ProxyServer': '127.0.0.1:8888'}})
        self.assertEqual(self.proxy(registry, USER_A), 'http://127.0.0.1:7897')
        self.assertEqual(len(registry.opened), 1, 'bound detection must not enumerate other profiles')

    def test_bound_user_with_no_proxy_cannot_fall_back_to_another_account(self):
        registry = FakeRegistry([USER_B], {USER_B: {'ProxyEnable': 1, 'ProxyServer': '127.0.0.1:8888'}})
        self.assertIsNone(self.proxy(registry, USER_A))
        self.assertIsNone(self.proxy(registry, USER_A + '_Classes'))

    def test_unbound_single_user_detection_accepts_entra_and_excludes_classes_hives(self):
        registry = FakeRegistry([ENTRA_USER + '_Classes', ENTRA_USER, 'S-1-5-18'], {
            ENTRA_USER: {'ProxyEnable': 1, 'ProxyServer': '127.0.0.1:7897'},
            ENTRA_USER + '_Classes': {'ProxyEnable': 1, 'ProxyServer': 'evil:8888'}})
        self.assertEqual(self.proxy(registry), 'http://127.0.0.1:7897')

    def test_unbound_multiple_users_have_no_arbitrary_winner(self):
        registry = FakeRegistry([USER_A, USER_B], {
            USER_A: {'ProxyEnable': 1, 'ProxyServer': '127.0.0.1:7897'},
            USER_B: {'ProxyEnable': 1, 'ProxyServer': '127.0.0.1:8888'}})
        self.assertIsNone(self.proxy(registry))

    def test_real_controller_workers_keep_their_own_sid_and_reset_afterward(self):
        parameters = inspect.signature(app_update.UpdateController).parameters
        self.assertIn('proxy_user_sid', parameters, 'the real worker must carry its task identity')
        with tempfile.TemporaryDirectory() as directory:
            barrier = threading.Barrier(2)
            selected = {}

            def detect(user_sid=None):
                selected[threading.current_thread().name] = user_sid
                return 'http://127.0.0.1:7897'

            controllers = [app_update.UpdateController('1.9.0', Path(directory), proxy_user_sid=sid)
                           for sid in (USER_A, USER_B)]

            def work():
                barrier.wait(timeout=5)
                app_update.configured_proxy()

            def run(controller):
                controller._gate.acquire()
                controller._run(work)
                app_update.configured_proxy()

            with patch.dict(os.environ, {}, clear=True), \
                 patch.object(app_update, 'interactive_user_proxy', side_effect=detect):
                # Record each thread's bound value before its context resets.
                inside = {}
                original = detect

                def detect_once(user_sid=None):
                    name = threading.current_thread().name
                    if name not in inside:
                        inside[name] = user_sid
                    return original(user_sid)

                with patch.object(app_update, 'interactive_user_proxy', side_effect=detect_once):
                    threads = [threading.Thread(target=run, args=(controller,), name=f'worker-{index}')
                               for index, controller in enumerate(controllers)]
                    for thread in threads: thread.start()
                    for thread in threads: thread.join(10)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertEqual(inside, {'worker-0': USER_A, 'worker-1': USER_B})
                self.assertEqual(selected, {'worker-0': None, 'worker-1': None}, 'context must reset')


class ExplicitProxyRoutingTests(unittest.TestCase):
    def test_no_proxy_cannot_bypass_an_explicit_clash_proxy(self):
        proxy = app_update.ProxyHandler({'https': 'http://127.0.0.1:7897'})
        request = Request('https://api.github.com/repos/x/y')
        with patch.dict(os.environ, {'HTTP_PROXY': 'http://other:99', 'NO_PROXY': '*'}, clear=True):
            proxy.proxy_open(request, proxy.proxies['https'], 'https')
        self.assertEqual(request.host, '127.0.0.1:7897')
        self.assertEqual(request._tunnel_host, 'api.github.com', 'HTTPS must still use CONNECT')

    def test_https_proxy_and_proxy_auth_preserve_the_standard_connect_semantics(self):
        proxy = app_update.ProxyHandler({'https': 'https://user:p%40ss@127.0.0.1:7897'})
        request = Request('https://api.github.com/repos/x/y')
        with patch.dict(os.environ, {'NO_PROXY': '*'}, clear=True):
            proxy.proxy_open(request, proxy.proxies['https'], 'https')
        self.assertEqual(request.host, '127.0.0.1:7897')
        self.assertEqual(request._tunnel_host, 'api.github.com')
        self.assertEqual(request.get_header('Proxy-authorization'), 'Basic dXNlcjpwQHNz')

    def test_empty_proxy_override_disables_every_environment_proxy(self):
        with patch.dict(os.environ, {'YOUZIAUTH_UPDATE_PROXY': '', 'HTTP_PROXY': 'http://other:99',
                                     'HTTPS_PROXY': 'http://other:99'}, clear=True), \
             patch.object(app_update, 'build_opener') as builder:
            app_update.open_url('https://api.github.com/repos/x/y')
        handlers = [handler for handler in builder.call_args.args
                    if isinstance(handler, app_update.ProxyHandler)]
        self.assertEqual(len(handlers), 1)
        self.assertEqual(handlers[0].proxies, {})


if __name__ == '__main__':
    unittest.main()
