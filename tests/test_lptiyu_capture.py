"""凭证采集: 流量解析、CA 签发、系统代理备份还原、会话状态机。

真实 TLS 拦截需要连线到 lptiyu, 单测只覆盖纯逻辑与本地回环:
  * 用 lptiyu.cipher.seal 造出与小程序同构的请求/响应信封;
  * CA 在本机真的生成一次(只落临时目录, 不装库);
  * 旧版代理用 HKCU 下的临时键; 默认会话的注册表、看门狗调用全部拦截并断言未使用;
  * CONNECT 和 HTTPS 采集用本地回环、合成报文验证。
"""
import datetime as dt
import http.client
import json
import os
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from unittest.mock import patch

from lptiyu.cipher import seal

import lptiyu_capture as cap


def sealed_body(payload: dict) -> bytes:
    return urllib.parse.urlencode({"ostype": 5, "data": seal(json.dumps(payload))}).encode()


def sealed_reply(payload: dict) -> bytes:
    return json.dumps({"status": 1, "data": seal(json.dumps(payload))}).encode()


class SniffTests(unittest.TestCase):
    def test_identity_comes_from_the_request_envelope(self):
        body = sealed_body({"uid": 17987537, "token": "", "school_id": 837,
                            "student_num": "222025321102104",
                            "card_id": "222025321102104", "sign": "x"})
        identity = cap.identity_from_request(body)
        self.assertEqual(identity["uid"], 17987537)
        self.assertEqual(identity["student_num"], "222025321102104")
        self.assertNotIn("sign", identity)

    def test_a_request_envelope_carries_the_live_token(self):
        # 打开跑步首页就会发 getSchoolInfo, 信封里就是当前 token —— 采集不必等登录。
        body = sealed_body({"uid": 17987537, "token": "LIVE-TOKEN",
                            "student_num": "222025321102104"})
        cred = cap.credential_from_request(body)
        self.assertEqual(cred["token"], "LIVE-TOKEN")
        self.assertEqual(cred["uid"], 17987537)
        self.assertNotIn("token", cap.identity_from_request(body),
                         'identity.json 不该存 token')

    def test_identity_ignores_garbage(self):
        self.assertIsNone(cap.identity_from_request(b"data=not-base64"))
        self.assertIsNone(cap.identity_from_request(b"nickname=x"))

    def test_login_response_yields_the_access_token(self):
        body = sealed_reply({
            "uid": 17987537,
            "token_data": {"access_token": "TOKEN-1", "refresh_token": "R1",
                           "refresh_expire": 3600}})
        cred = cap.credentials_from_login(body)
        self.assertEqual(cred["token"], "TOKEN-1")
        self.assertEqual(cred["refresh_token"], "R1")
        self.assertEqual(cred["refresh_expire"], 3600)
        self.assertEqual(cred["uid"], 17987537)

    def test_login_without_token_data_is_not_a_credential(self):
        self.assertIsNone(cap.credentials_from_login(sealed_reply({"uid": 1})))
        self.assertIsNone(cap.credentials_from_login(b'{"status":101}'))
        self.assertIsNone(cap.credentials_from_login(b"\xff\xfe"))

    def test_only_lptiyu_hosts_are_intercepted(self):
        self.assertTrue(cap.host_in_scope("api2.lptiyu.com"))
        self.assertTrue(cap.host_in_scope("LPTIYU.COM"))
        self.assertFalse(cap.host_in_scope("lptiyu.evil.com"))
        self.assertFalse(cap.host_in_scope("example.com"))
        self.assertFalse(cap.host_in_scope(""))


class CertificateTests(unittest.TestCase):
    def test_certificates_carry_the_extensions_chromium_requires(self):
        # 缺 AKI/SKI 时 Chromium(微信小程序渲染层)会直接 CERTIFICATE_VERIFY_FAILED,
        # 表现是「小程序里一切正常, 采集端什么都没看到」。
        from cryptography import x509
        with tempfile.TemporaryDirectory() as tmp:
            ca = cap.CertificateAuthority(Path(tmp))
            ca.ensure()
            root_cert = x509.load_pem_x509_certificate(ca.cert_path.read_bytes())
            self.assertTrue(root_cert.extensions.get_extension_for_class(
                x509.SubjectKeyIdentifier))
            self.assertTrue(root_cert.extensions.get_extension_for_class(
                x509.AuthorityKeyIdentifier))
            cert_path, _key = ca.leaf("api2.lptiyu.com")
            leaf = x509.load_pem_x509_certificate(cert_path.read_bytes())
            aki = leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
            ski = root_cert.extensions.get_extension_for_class(
                x509.SubjectKeyIdentifier).value
            self.assertEqual(aki.key_identifier, ski.digest,
                             'AKI 必须对得上签发者的 SKI')
            self.assertTrue(leaf.extensions.get_extension_for_class(
                x509.ExtendedKeyUsage))

    def test_a_legacy_ca_without_ski_is_replaced(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "old ca")])
            now = dt.datetime.now(dt.timezone.utc)
            old = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now - dt.timedelta(days=1))
                   .not_valid_after(now + dt.timedelta(days=365))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=0),
                                  critical=True)
                   .sign(key, hashes.SHA256()))
            (root / "ca.key").write_bytes(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()))
            (root / "ca.pem").write_bytes(old.public_bytes(serialization.Encoding.PEM))
            (root / "ca.der").write_bytes(old.public_bytes(serialization.Encoding.DER))
            ca = cap.CertificateAuthority(root)
            before = ca.fingerprint()
            ca.ensure()
            self.assertNotEqual(ca.fingerprint(), before, '没有 SKI 的旧 CA 要换掉')
            self.assertTrue((root / "superseded_ca.txt").is_file())
            x509.load_pem_x509_certificate(ca.cert_path.read_bytes()).extensions \
                .get_extension_for_class(x509.SubjectKeyIdentifier)

    def test_ca_and_leaf_are_generated_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            ca = cap.CertificateAuthority(Path(tmp))
            self.assertEqual(ca.fingerprint(), "", "还没生成就没有指纹")
            ca.ensure()
            first = ca.cert_path.read_bytes()
            ca.ensure()
            self.assertEqual(first, ca.cert_path.read_bytes(), "第二次不该重签")
            fp = ca.fingerprint()
            self.assertEqual(len(fp), 40)
            self.assertTrue(all(c in "0123456789ABCDEF" for c in fp))

            cert_path, key_path = ca.leaf("api2.lptiyu.com")
            self.assertTrue(cert_path.is_file() and key_path.is_file())
            from cryptography import x509
            leaf = x509.load_pem_x509_certificate(cert_path.read_bytes())
            san = leaf.extensions.get_extension_for_class(
                x509.SubjectAlternativeName).value
            self.assertIn("api2.lptiyu.com", san.get_values_for_type(x509.DNSName))
            self.assertFalse(leaf.extensions.get_extension_for_class(
                x509.BasicConstraints).value.ca)

    def test_store_listing_is_compared_case_insensitively(self):
        # certutil 打印小写指纹, 我们按 Windows 习惯存大写; 只比大写就永远认不出
        # 自己刚装进去的证书(界面: 装成功却报失败, 采集按钮一直灰着)。
        with tempfile.TemporaryDirectory() as tmp:
            ca = cap.CertificateAuthority(Path(tmp))
            ca.ensure()
            fp = ca.fingerprint()
            listing = f"证书哈希(sha1): {fp.lower()}\r\n"
            done = subprocess.CompletedProcess(args=[], returncode=0,
                                               stdout=listing, stderr="")
            with patch.object(cap.subprocess, "run", return_value=done):
                self.assertTrue(ca.installed())

    def test_install_reports_success_when_the_store_shows_lowercase(self):
        with tempfile.TemporaryDirectory() as tmp:
            ca = cap.CertificateAuthority(Path(tmp))
            ca.ensure()
            fp = ca.fingerprint()
            add = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=f"Root ... 添加到存储。\r\nCertUtil: -addstore 命令成功完成。\r\n",
                stderr="")
            listing = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=f"证书哈希(sha1): {fp.lower()}", stderr="")
            calls = {"n": 0}

            def fake_run(args, **kwargs):
                calls["n"] += 1
                return add if "-addstore" in args else listing

            with patch.object(cap.subprocess, "run", side_effect=fake_run):
                result = ca.install()
            self.assertTrue(result["ok"])
            self.assertIn("证书", result["detail"])

    def test_install_says_so_when_the_store_still_does_not_show_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            ca = cap.CertificateAuthority(Path(tmp))
            ca.ensure()
            add = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
            listing = subprocess.CompletedProcess(args=[], returncode=0,
                                                  stdout="no certs here", stderr="")

            def fake_run(args, **kwargs):
                return add if "-addstore" in args else listing

            with patch.object(cap.subprocess, "run", side_effect=fake_run):
                with self.assertRaises(cap.CaptureError) as caught:
                    ca.install()
            self.assertIn("受信任根", str(caught.exception))

    def test_removal_timeout_explains_the_confirmation_dialog(self):
        with tempfile.TemporaryDirectory() as tmp:
            ca = cap.CertificateAuthority(Path(tmp))
            ca.ensure()
            with patch.object(cap.subprocess, "run",
                              side_effect=cap.subprocess.TimeoutExpired(cmd="certutil", timeout=1)):
                with self.assertRaises(cap.CaptureError) as caught:
                    ca.remove()
            self.assertIn("弹窗", str(caught.exception))

    def test_uninstall_without_a_certificate_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            ca = cap.CertificateAuthority(Path(tmp))
            self.assertEqual(ca.remove(), {"ok": True, "detail": "本机还没有生成采集证书"})


class SystemProxyTests(unittest.TestCase):
    REG = r"Software\youziauth-capture-test"

    def setUp(self):
        import winreg
        self.winreg = winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.REG) as key:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, "127.0.0.1:7890")
            winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, "<local>")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.backup = Path(tmp.name) / cap.BACKUP_NAME
        self.proxy = cap.SystemProxy(self.backup, reg_path=self.REG)
        self.addCleanup(self._drop_key)

    def _drop_key(self):
        try:
            self.winreg.DeleteKey(self.winreg.HKEY_CURRENT_USER, self.REG)
        except OSError:
            pass

    def test_point_at_backs_up_and_restore_puts_it_back(self):
        before = self.proxy.read()
        self.assertEqual(before[1], "127.0.0.1:7890")
        self.proxy.point_at(18888)
        self.assertEqual(self.proxy.read()[1], "127.0.0.1:18888")
        self.assertTrue(self.backup.is_file(), "备份要落盘, 崩溃后才能收拾")
        self.proxy.restore()
        self.assertEqual(self.proxy.read()[1], "127.0.0.1:7890")
        self.assertEqual(self.proxy.read()[2], "<local>", "绕过列表也要还原")
        self.assertFalse(self.backup.exists())

    def test_stale_backup_can_be_recovered_after_a_crash(self):
        self.proxy.point_at(18888)
        # 模拟崩溃: 进程没了, 只有备份留在盘上
        self.backup.write_text(json.dumps(
            {"enable": 1, "server": "127.0.0.1:7890", "override": "<local>"}),
            encoding="utf-8")
        fresh = cap.SystemProxy(self.backup, reg_path=self.REG)
        self.assertTrue(fresh.stale_backup())
        self.assertTrue(fresh.restore())
        self.assertEqual(fresh.read()[1], "127.0.0.1:7890")
        self.assertFalse(self.backup.exists())

    def test_restore_without_backup_is_harmless(self):
        self.assertFalse(cap.SystemProxy(self.backup, reg_path=self.REG).restore())

    def test_a_second_session_cannot_replace_an_active_backup(self):
        self.proxy.point_at(18888)
        try:
            second = cap.SystemProxy(self.backup, reg_path=self.REG)
            with self.assertRaises(cap.CaptureError):
                second.point_at(18889)
            self.assertEqual(self.proxy.read()[1], "127.0.0.1:18888")
            self.assertEqual(json.loads(self.backup.read_text())["port"], 18888)
        finally:
            self.proxy.restore()
        self.assertEqual(self.proxy.read()[1], "127.0.0.1:7890")

    def test_normal_restore_preserves_an_external_proxy_change(self):
        self.proxy.point_at(18888)
        self.proxy.write(1, "new-proxy.example:8080", "<local>")
        self.assertFalse(self.proxy.restore())
        self.assertEqual(self.proxy.read()[1], "new-proxy.example:8080")
        self.assertFalse(self.backup.exists())

    def test_stop_requests_watchdog_recovery_after_transient_restore_failure(self):
        session = cap.CaptureSession(self.backup.parent, ca=FakeCA(),
                                     proxy_factory=lambda: self.proxy, server_factory=FakeServer,
                                     guard_factory=lambda *_: SimpleNamespace(pid=4321))
        session.start()
        capture_id = self.proxy.capture_id
        with patch.object(cap.proxy_guard, "write_proxy", side_effect=PermissionError("temporarily locked")):
            session.stop("idle", "已停止采集")
        self.assertEqual(session.status()["state"], "error")
        self.assertTrue(cap.proxy_guard.read_backup(self.backup)["release_requested"])
        self.assertEqual(cap.proxy_guard.main([str(self.backup), str(os.getpid()), self.REG, capture_id]), 0)
        self.assertEqual(self.proxy.read()[1], "127.0.0.1:7890")
        session.stop()


class FakeServer:
    def __init__(self, ca, sink, port=0):
        self.ca = ca
        self.sink = sink
        self.server_address = ("127.0.0.1", 18888)
        self.stopped = False

    def serve_forever(self):
        while not self.stopped:
            threading.Event().wait(0.02)

    def shutdown(self):
        self.stopped = True

    def server_close(self):
        self.stopped = True


class HangingServer(FakeServer):
    """模拟 shutdown() 卡住的采集服务: 停采集不能被它挂住。"""

    def shutdown(self):
        threading.Event().wait(30)


class FakeProxy:
    def __init__(self):
        self.pointed = 0
        self.restored = 0
        self.backup = False

    def stale_backup(self):
        return self.backup

    def point_at(self, port):
        self.pointed = port

    def restore(self):
        self.restored += 1
        self.backup = False
        return True


class BrokenBackupProxy(FakeProxy):
    """备份写不进去时必须直接放弃采集, 而不是先改代理再说。"""

    def point_at(self, port):
        self.restored = 0
        raise cap.CaptureError("无法备份系统代理")


class FakeCA:
    def __init__(self, installed=True):
        self._installed = installed
        self.installs = 0

    def installed(self):
        return self._installed

    def fingerprint(self):
        return "F" * 40

    def install(self):
        self.installs += 1
        self._installed = True
        return {"ok": True}

    def leaf(self, host):
        raise AssertionError("tunnel 测试不该签站点证书")


class SessionTests(unittest.TestCase):
    def session(self, installed=True, timeout=5, server_factory=FakeServer,
                guard_factory=None, proxy_factory=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        proxy = proxy_factory() if proxy_factory is not None else FakeProxy()
        ca = FakeCA(installed)
        session = cap.CaptureSession(
            Path(tmp.name), proxy_factory=lambda: proxy, ca=ca, timeout_s=timeout,
            server_factory=server_factory, guard_factory=guard_factory)
        return session, proxy, ca

    def test_start_refuses_until_the_certificate_is_installed(self):
        session, proxy, ca = self.session(installed=False)
        with self.assertRaises(cap.CaptureError) as caught:
            session.start()
        self.assertIn("安装采集证书", str(caught.exception))
        self.assertEqual(proxy.pointed, 0, "证书没装就不该动系统代理")

    def test_start_points_the_system_proxy_and_waits(self):
        session, proxy, ca = self.session()
        status = session.start()
        self.assertEqual(status["state"], "waiting")
        self.assertEqual(proxy.pointed, 18888)
        self.assertIn("小程序", status["message"])
        session.stop("idle", "done")
        self.assertGreaterEqual(proxy.restored, 1)

    def test_login_is_delivered_then_the_proxy_is_restored(self):
        session, proxy, _ca = self.session()
        session.start()
        session.on_request("api2.lptiyu.com", "/v3/api.php/Run2/beforeRunV260",
                           sealed_body({"uid": 42, "student_num": "2025001",
                                        "school_id": 837}))
        session.on_login(sealed_reply({"uid": 42, "token_data": {
            "access_token": "NEW", "refresh_token": "r", "refresh_expire": 60}}))
        cred = session.poll()
        self.assertEqual(cred["token"], "NEW")
        self.assertEqual(cred["uid"], 42)
        self.assertEqual(cred["student_num"], "2025001", "身份要和 token 一起带回")
        self.assertEqual(cred["source"], "capture")
        self.assertEqual(session.status()["state"], "captured")
        self.assertEqual(proxy.restored, 1)

    def test_a_request_alone_is_enough_to_capture(self):
        session, proxy, _ca = self.session()
        session.start()
        session.on_request("api2.lptiyu.com", "/v3/api.php/WpLogin/getSchoolInfo",
                           sealed_body({"uid": 7, "token": "FROM-REQUEST",
                                        "student_num": "2025002"}))
        cred = session.poll()
        self.assertEqual(cred["token"], "FROM-REQUEST")
        self.assertEqual(cred["student_num"], "2025002")
        self.assertEqual(proxy.restored, 1)

    def test_the_session_writes_a_capture_log(self):
        session, _proxy, _ca = self.session()
        session.start()
        session.on_request("api2.lptiyu.com", "/v3/api.php/WpLogin/getSchoolInfo",
                           sealed_body({"uid": 7, "token": "T"}))
        session.poll()
        log = (session.root / "capture.log").read_text(encoding="utf-8")
        self.assertIn("采集开始", log)
        self.assertIn("凭证已捕获", log)
        self.assertIn("采集结束", log)

    def test_timeout_stops_and_restores(self):
        session, proxy, _ca = self.session(timeout=0)
        session.start()
        self.assertIsNone(session.wait(timeout=0.6))
        self.assertEqual(session.status()["state"], "timeout")
        self.assertEqual(proxy.restored, 1)

    def test_an_external_proxy_change_stops_capture(self):
        session, proxy, _ca = self.session()
        proxy.read = lambda: (1, "127.0.0.1:7890", "")     # Clash 抢回去了
        session.start()
        proxy.pointed = 0
        session.ensure_proxy()
        self.assertEqual(proxy.pointed, 0, "不应再覆盖另一个程序设置的代理")
        self.assertEqual(session.status()["state"], "idle")
        self.assertEqual(proxy.restored, 1)

    def test_stop_clears_port_and_cancels_the_wait_tick(self):
        session, proxy, _ca = self.session()
        proxy.read = lambda: (0, "", "")
        session.start()
        session.stop("idle", "已停止采集")
        proxy.pointed = 0
        session.ensure_proxy()
        ticks = []
        session.wait(timeout=0.01, on_tick=lambda: ticks.append(1))
        self.assertEqual(ticks, [])
        self.assertEqual(proxy.pointed, 0)
        self.assertEqual(session.status()["port"], 0)
        self.assertEqual(session.status()["message"], "已停止采集")

    def test_wait_releases_proxy_when_cancelled(self):
        session, proxy, _ca = self.session()
        stop_event = threading.Event()
        session.start(stop_event=stop_event)
        try:
            self.assertIsNone(session.wait(on_tick=stop_event.set, stop_event=stop_event))
            self.assertEqual(session.status()["state"], "idle")
            self.assertEqual(session.status()["port"], 0)
            self.assertEqual(proxy.restored, 1)
        finally:
            session.stop()

    def test_cancel_during_guard_setup_does_not_switch_proxy(self):
        stop_event = threading.Event()

        def start_guard(*_):
            stop_event.set()
            return SimpleNamespace(pid=4321)

        session, proxy, _ca = self.session(guard_factory=start_guard)
        with self.assertRaisesRegex(cap.CaptureError, "已取消"):
            session.start(stop_event=stop_event)
        self.assertEqual(proxy.pointed, 0)
        self.assertEqual(session.status()["port"], 0)

    def test_an_old_wait_does_not_stop_a_new_capture(self):
        session, proxy, _ca = self.session()
        session.start()

        def restart():
            session.stop("idle", "")
            session.start()

        try:
            self.assertIsNone(session.wait(timeout=0.01, on_tick=restart))
            self.assertEqual(session.status()["state"], "waiting")
            self.assertEqual(proxy.restored, 1)
        finally:
            session.stop()

    def test_listener_start_failure_does_not_switch_the_proxy(self):
        session, proxy, _ca = self.session()
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("can't start thread")):
            with self.assertRaises(cap.CaptureError):
                session.start()
        self.assertEqual(proxy.pointed, 0)
        self.assertEqual(session.status()["port"], 0)

    def test_watchdog_start_failure_does_not_switch_the_proxy(self):
        session, proxy, _ca = self.session(guard_factory=lambda *_: None)
        with self.assertRaises(cap.CaptureError):
            session.start()
        self.assertEqual(proxy.pointed, 0)
        self.assertEqual(session.status()["port"], 0)

    def test_wait_reports_progress_on_every_tick(self):
        session, _proxy, _ca = self.session(timeout=3.0)
        session.start()
        ticks = []
        session.wait(timeout=1.4, on_tick=lambda: ticks.append(1))
        self.assertGreaterEqual(len(ticks), 1, '等的时候要能回钩, 采集流程靠它做体检')

    def test_stale_proxy_backup_is_restored_before_starting(self):
        session, proxy, _ca = self.session()
        proxy.backup = True
        session.start()
        self.assertGreaterEqual(proxy.restored, 1, "上次崩溃留下的代理先还原再开始")
        session.stop("idle", "")

    def test_stop_is_bounded_when_the_server_will_not_shut_down(self):
        session, proxy, _ca = self.session(server_factory=HangingServer)
        session.start()
        started = time.monotonic()
        session.stop("idle", "")
        self.assertLess(time.monotonic() - started, 8.0,
                        "shutdown 卡住时也必须尽快收尾, 否则界面退不出去、代理留在用户身上")
        self.assertEqual(proxy.restored, 1)

    def test_a_capture_that_cannot_back_up_the_proxy_never_starts(self):
        broken = BrokenBackupProxy()
        session, _proxy, _ca = self.session(proxy_factory=lambda: broken)
        with self.assertRaises(cap.CaptureError):
            session.start()
        self.assertEqual(broken.pointed, 0, "备份没落盘就不该改系统代理")

    def test_start_arms_a_proxy_guard(self):
        spawned = []
        session, _proxy, _ca = self.session(
            guard_factory=lambda *args: spawned.append(args) or SimpleNamespace(pid=4321))
        session.start()
        self.assertEqual(len(spawned), 1, "采集开始就要有独立看门狗等父进程退出")
        path, parent_pid, _reg = spawned[0]
        self.assertEqual(Path(path).name, cap.BACKUP_NAME)
        self.assertEqual(parent_pid, os.getpid())
        session.stop("idle", "")


class ScopedTransportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for action in ("read_proxy", "write_proxy", "recover_stale_backup", "spawn_guard"):
            forbidden = patch.object(cap.proxy_guard, action, side_effect=AssertionError(action))
            mocked = forbidden.start()
            self.addCleanup(forbidden.stop)
            self.addCleanup(mocked.assert_not_called)

    def open_session(self, ca=None, **options):
        session = cap.CaptureSession(self.root, ca=ca or FakeCA(), timeout_s=5)
        self.addCleanup(session.stop)
        session.start(**options)
        return session

    def test_scoped_session_binds_the_client_port_without_touching_system_proxy(self):
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        session = self.open_session(port=port)
        self.assertEqual(session.status()["port"], port)
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            self.assertFalse(session.ensure_proxy())
        session.stop()
        self.assertEqual(session.status()["port"], 0)
        self.assertFalse((self.root / cap.BACKUP_NAME).exists())

    @unittest.skipUnless(hasattr(socket, "SO_EXCLUSIVEADDRUSE"), "Winsock option")
    def test_windows_listener_reserves_its_address_exclusively(self):
        session = self.open_session()
        active_socket = session._server.socket
        self.assertNotEqual(active_socket.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE), 0)
        self.assertEqual(active_socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR), 0)

    def test_non_target_and_non_https_connects_are_rejected_before_outbound_connection(self):
        session = self.open_session()
        cases = ["example.test:443", "api2.lptiyu.com.example.test:443",
                 "api2.lptiyu.com:80", "user@fake.lptiyu.com:443"]
        with patch.object(cap.socket, "create_connection", side_effect=AssertionError("upstream")) as dial:
            for authority in cases:
                with self.subTest(authority=authority), socket.socket() as client:
                    client.settimeout(1)
                    client.connect(("127.0.0.1", session.status()["port"]))
                    client.sendall(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
                    response, _rest = cap._read_head(client)
                    self.assertTrue(response and response.startswith(b"HTTP/1.1 403"))
            dial.assert_not_called()
        self.assertEqual(session.connects, 0)

    def test_scoped_https_transports_matching_request_and_delivers_data(self):
        ca = cap.CertificateAuthority(self.root)
        ca.ensure()
        payload = {"uid": "synthetic-uid", "token": "synthetic-token", "school_id": "100"}
        with patch.object(ca, "installed", return_value=True):
            session = self.open_session(ca=ca)
        upstream_reply = (200, {"reason": "OK", "items": []}, b"synthetic upstream reply")
        with patch.object(cap._ProxyHandler, "_forward", return_value=upstream_reply) as forward:
            context = ssl.create_default_context(cafile=str(ca.cert_path))
            conn = http.client.HTTPSConnection("127.0.0.1", session.status()["port"], timeout=2,
                                               context=context)
            with closing(conn):
                conn.set_tunnel("api2.lptiyu.com", 443)
                conn.request("POST", "/v3/api.php/WpLogin/getSchoolInfo", body=sealed_body(payload))
                with conn.getresponse() as response:
                    self.assertEqual((response.status, response.read()), (200, upstream_reply[2]))
            self.assertEqual(forward.call_args.args[:2], ("api2.lptiyu.com", 443))
        token = session.wait(timeout=1)
        self.assertEqual({key: token[key] for key in payload}, payload)
        self.assertEqual(session.status()["state"], "captured")

    def test_tls_request_cannot_send_another_origin_or_host_through_proxy(self):
        ca = cap.CertificateAuthority(self.root)
        ca.ensure()
        with patch.object(ca, "installed", return_value=True):
            session = self.open_session(ca=ca)
        context = ssl.create_default_context(cafile=str(ca.cert_path))
        with patch.object(cap._ProxyHandler, "_forward", side_effect=AssertionError("upstream")) as forward:
            for path, host in (("/path", "example.test"), ("https://example.test/", "api2.lptiyu.com")):
                with self.subTest(path=path, host=host):
                    conn = http.client.HTTPSConnection("127.0.0.1", session.status()["port"],
                                                       timeout=2, context=context)
                    with closing(conn):
                        conn.set_tunnel("api2.lptiyu.com", 443)
                        conn.request("GET", path, headers={"Host": host})
                        with conn.getresponse() as response:
                            self.assertEqual(response.status, 403)
            forward.assert_not_called()
        self.assertEqual(session.requests, 0)

    def test_stopping_closes_open_plain_and_tls_clients_and_allows_same_port_restart(self):
        ca = cap.CertificateAuthority(self.root)
        ca.ensure()
        with patch.object(ca, "installed", return_value=True):
            session = self.open_session(ca=ca)
            port = session.status()["port"]
            with socket.create_connection(("127.0.0.1", port), timeout=2) as plain:
                plain.sendall(b"CONNECT ")
                context = ssl.create_default_context(cafile=str(ca.cert_path))
                conn = http.client.HTTPSConnection("127.0.0.1", port, timeout=2, context=context)
                with closing(conn):
                    conn.set_tunnel("api2.lptiyu.com", 443)
                    conn.connect()
                    session.stop()
                    for client in (plain, conn.sock):
                        with self.subTest(client=type(client).__name__):
                            client.settimeout(0.5)
                            try:
                                self.assertEqual(client.recv(1), b"")
                            except (ConnectionResetError, ssl.SSLEOFError):
                                pass
                    self.assertEqual(session.start(port=port)["port"], port)

    def test_closed_listener_callbacks_cannot_contaminate_a_new_capture(self):
        session = self.open_session()
        old_listener = session._server
        old_listener.record_request("api2.lptiyu.com", "/path",
                                    sealed_body({"uid": "first-session", "student_num": "100"}))
        session.stop()
        session.start()
        old_listener.record_connect()
        old_listener.record_request("api2.lptiyu.com", "/path",
                                    sealed_body({"uid": "late-session", "token": "late-token"}))
        old_listener.record_login(sealed_reply({"token_data": {"access_token": "late-reply"}}))
        self.assertEqual((session.connects, session.requests, session.identity), (0, 0, {}))
        self.assertIsNone(session.poll())

    def test_capture_does_not_end_while_its_response_is_still_being_forwarded(self):
        ca = cap.CertificateAuthority(self.root)
        ca.ensure()
        with patch.object(ca, "installed", return_value=True):
            session = self.open_session(ca=ca)
        forwarding = threading.Event()
        finish = threading.Event()

        def held_upstream(*_args):
            forwarding.set()
            if not finish.wait(2):
                raise RuntimeError("isolated upstream was not released")
            return 200, {"reason": "OK", "items": []}, b"response"

        context = ssl.create_default_context(cafile=str(ca.cert_path))
        conn = http.client.HTTPSConnection("127.0.0.1", session.status()["port"], timeout=2,
                                           context=context)
        with closing(conn), patch.object(cap._ProxyHandler, "_forward", side_effect=held_upstream):
            conn.set_tunnel("api2.lptiyu.com", 443)
            conn.request("POST", "/v3/api.php/WpLogin/getSchoolInfo",
                         body=sealed_body({"uid": "test-uid", "token": "test-token"}))
            try:
                self.assertTrue(forwarding.wait(1))
                self.assertIsNone(session.poll())
            finally:
                finish.set()
            with conn.getresponse() as response:
                self.assertEqual(response.read(), b"response")
            self.assertEqual(session.wait(timeout=1)["token"], "test-token")


class TemporaryGlobalTransportTests(unittest.TestCase):
    REG = r"Software\youziauth-manual-global-fixture"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.original = (0, "", "<local>")
        self.current = self.original
        self.writes = []

        def read(reg_path):
            self.assertEqual(reg_path, self.REG)
            return self.current

        def write(enable, server, override, reg_path):
            self.assertEqual(reg_path, self.REG)
            self.current = (enable, server, override)
            self.writes.append(self.current)

        for name, function in (("read_proxy", read), ("write_proxy", write)):
            dependency = patch.object(cap.proxy_guard, name, side_effect=function)
            dependency.start()
            self.addCleanup(dependency.stop)

    def make_session(self, guard=lambda *_: SimpleNamespace(pid=4321), ca=None):
        session = cap.CaptureSession(self.root, ca=ca or FakeCA(), system_proxy=True,
                                     proxy_factory=lambda: cap.SystemProxy(self.root / cap.BACKUP_NAME, self.REG),
                                     guard_factory=guard)
        self.addCleanup(session.stop)
        return session

    def make_listener(self):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(2)
        return listener

    def test_watches_a_tagged_lease_before_switching_and_restores_on_cancel(self):
        original = self.current = (1, "old-proxy.example:8090", "<local>;example.test")

        def guard(path, pid, reg):
            backup = cap.proxy_guard.read_backup(path)
            self.assertEqual(self.current, original)
            self.assertEqual(reg, self.REG)
            self.assertEqual(backup["pid"], pid)
            self.assertTrue(backup["monitor_listener"])
            self.assertGreater(backup["expires_at"], time.time())
            return SimpleNamespace(pid=4321)

        session = self.make_session(guard)
        status = session.start()
        self.assertEqual(self.current, (1, f"127.0.0.1:{status['port']}", original[2]))
        cancel = threading.Event()
        cancel.set()
        self.assertIsNone(session.wait(stop_event=cancel))
        self.assertEqual(self.current, original)
        self.assertFalse((self.root / cap.BACKUP_NAME).is_file())

    def test_a_missing_watchdog_does_not_change_the_original_settings(self):
        session = self.make_session(lambda *_: None)
        with self.assertRaisesRegex(cap.CaptureError, "看门狗"):
            session.start()
        self.assertEqual(self.writes, [])

    def test_an_untagged_backup_is_never_restored_when_a_manual_session_starts(self):
        backup = self.root / cap.BACKUP_NAME
        backup.write_text(json.dumps({"enable": 1, "server": "other.example:9000"}), encoding="utf-8")
        session = self.make_session()
        with self.assertRaisesRegex(cap.CaptureError, "备份"):
            session.start()
        self.assertEqual(self.writes, [])
        self.assertTrue(backup.is_file())

    def test_a_new_uncommitted_record_with_a_dead_owner_does_not_block_capture(self):
        backup = self.root / cap.BACKUP_NAME
        prior = {"enable": 0, "server": "", "override": "", "port": 18888,
                 "pid": 1234, "capture_id": "abcd" * 8, "monitor_listener": True,
                 "expires_at": time.time() + 15}
        backup.write_text(json.dumps(prior), encoding="utf-8")
        with patch.object(cap.proxy_guard, "pid_alive", return_value=False):
            session = self.make_session()
            self.assertEqual(session.start()["state"], "waiting")
        self.assertNotEqual(json.loads(backup.read_text())["capture_id"], prior["capture_id"])

    def test_an_active_legacy_extractor_keeps_its_proxy_and_backup(self):
        legacy = self.root / "old-exporter.json"
        legacy.write_text(json.dumps({"enable": 0, "server": "", "override": "", "pid": 1234,
                                       "port": 18888}), encoding="utf-8")
        self.current = (1, "127.0.0.1:18888", "<local>")
        session = self.make_session()
        with patch.object(cap.proxy_guard, "shared_backup_path", return_value=self.root / cap.BACKUP_NAME), \
                patch.object(cap.proxy_guard, "legacy_extractor_backup_path", return_value=legacy), \
                patch.object(cap.proxy_guard, "pid_alive", return_value=True):
            with self.assertRaisesRegex(cap.CaptureError, "旧版提取器"):
                session.start()
        self.assertEqual(self.writes, [])
        self.assertTrue(legacy.is_file())

    def test_global_connect_passes_other_tls_bytes_without_a_capture_certificate(self):
        listener = self.make_listener()
        port = listener.getsockname()[1]

        def peer():
            with listener.accept()[0] as conn:
                conn.settimeout(2)
                conn.sendall(conn.recv(64).upper())

        worker = threading.Thread(target=peer, daemon=True)
        worker.start()
        session = self.make_session()
        status = session.start()
        with socket.create_connection(("127.0.0.1", status["port"]), timeout=2) as client:
            client.sendall(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode())
            head, _ = cap._read_head(client)
            self.assertTrue(head and head.startswith(b"HTTP/1.1 200"))
            client.sendall(b"opaque traffic")
            self.assertEqual(client.recv(64), b"OPAQUE TRAFFIC")
        worker.join(2)
        self.assertEqual(session.connects, 0)
        self.assertEqual(session.requests, 0)

    def test_global_http_preserves_an_existing_http_upstream(self):
        listener = self.make_listener()
        port = listener.getsockname()[1]
        self.current = (1, f"http=127.0.0.1:{port};https=127.0.0.1:{port}", "<local>")
        received = []

        def peer():
            with listener.accept()[0] as conn:
                conn.settimeout(2)
                head, _ = cap._read_head(conn)
                received.append(head.split(b"\r\n", 1)[0])
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\nweb")

        worker = threading.Thread(target=peer, daemon=True)
        worker.start()
        session = self.make_session()
        status = session.start()
        with closing(http.client.HTTPConnection("127.0.0.1", status["port"], timeout=2)) as client:
            client.request("GET", "http://fixture.example/path")
            with client.getresponse() as response:
                self.assertEqual((response.status, response.read()), (200, b"web"))
        worker.join(2)
        self.assertEqual(received, [b"GET http://fixture.example/path HTTP/1.1"])
        self.assertEqual(session.requests, 0)

    def test_global_tls_capture_restores_settings_after_delivery(self):
        self.current = (1, "127.0.0.1:1234", "<local>")
        original = self.current
        ca = cap.CertificateAuthority(self.root)
        ca.ensure()
        session = self.make_session(ca=ca)
        with patch.object(ca, "installed", return_value=True):
            status = session.start()
        context = ssl.create_default_context(cafile=str(ca.cert_path))
        payload = {"uid": "synthetic-user", "token": "synthetic-credential"}
        conn = http.client.HTTPSConnection("127.0.0.1", status["port"], timeout=2, context=context)
        with closing(conn), patch.object(cap._ProxyHandler, "_forward", return_value=(
                200, {"reason": "OK", "items": []}, b"fixture")) as forward:
            conn.set_tunnel("api2.lptiyu.com", 443)
            conn.request("POST", "/v3/api.php/WpLogin/getSchoolInfo", body=sealed_body(payload))
            with conn.getresponse() as response:
                self.assertEqual(response.read(), b"fixture")
        self.assertEqual(forward.call_args.kwargs["upstream"], ("127.0.0.1", 1234))
        self.assertEqual(session.wait(timeout=2)["token"], payload["token"])
        self.assertEqual(self.current, original)


if __name__ == "__main__":
    unittest.main()
