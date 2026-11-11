"""凭证采集: 流量解析、CA 签发、系统代理备份还原、会话状态机。

真实 TLS 拦截需要连线到 lptiyu, 单测只覆盖纯逻辑与本地回环:
  * 用 lptiyu.cipher.seal 造出与小程序同构的请求/响应信封;
  * CA 在本机真的生成一次(只落临时目录, 不装库);
  * 系统代理用 HKCU 下的临时键, 绝不碰用户的真实代理设置;
  * CONNECT 隧道用本地 echo 服务验证。
"""
import datetime as dt
import json
import socket
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

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
    def session(self, installed=True, timeout=5):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        proxy = FakeProxy()
        ca = FakeCA(installed)
        session = cap.CaptureSession(
            Path(tmp.name), proxy_factory=lambda: proxy, ca=ca, timeout_s=timeout,
            server_factory=FakeServer)
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

    def test_stale_proxy_backup_is_restored_before_starting(self):
        session, proxy, _ca = self.session()
        proxy.backup = True
        session.start()
        self.assertGreaterEqual(proxy.restored, 1, "上次崩溃留下的代理先还原再开始")
        session.stop("idle", "")


class TunnelTests(unittest.TestCase):
    """非目标域名的 CONNECT 必须原样隧道, 不解密。"""

    def test_connect_tunnels_bytes_verbatim(self):
        echo = socket.socket()
        echo.bind(("127.0.0.1", 0))
        echo.listen(1)
        port = echo.getsockname()[1]

        def serve():
            conn, _addr = echo.accept()
            data = conn.recv(1024)
            conn.sendall(b"echo:" + data)
            conn.close()

        threading.Thread(target=serve, daemon=True).start()
        server = cap._ProxyServer(FakeCA(), _NullSink())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        proxy_port = server.server_address[1]

        client = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
        client.sendall(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        head = client.recv(1024)
        self.assertIn(b"200", head)
        client.sendall(b"ping")
        self.assertEqual(client.recv(1024), b"echo:ping")
        client.close()
        echo.close()


class _NullSink:
    def on_request(self, *_args):
        pass

    def on_login(self, *_args):
        pass


if __name__ == "__main__":
    unittest.main()
