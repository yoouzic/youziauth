"""乐跑凭证采集: 让安装包自己完成「等小程序登录 → 拿到 access_token」这一步。

背景: 凭证只能由小程序 `WpLogin/loginByCode` 产生(微信 code 换 token), 而这一步
必须在小程序真正登录时从 HTTPS 流量里读出来。研究库(lptiyu-lab)用 mitmproxy 做
这件事; 这里给发行版做一套等价、可控的最小实现, 不再依赖研究库:

  1. 本机生成一把专用 CA(`%LOCALAPPDATA%\\youziauth\\lptiyu\\capture\\ca.pem`),
     只用于签发 `*.lptiyu.com` 的站点证书;
  2. 用户显式点「安装证书」才写进 **当前用户** 的受信任根(免管理员), 随时可卸;
  3. 「开始采集」把系统代理指到本机 127.0.0.1:<随机端口>, 只解密 lptiyu 域,
     其它 TLS 一律原样隧道, 不经手;
  4. 抓到 loginByCode 的 token 就落盘(DPAPI 由调用方负责), 并立刻还原系统代理;
  5. 超时/停止/异常都走同一个收尾: 关服务、还原代理、清备份。

边界: 只处理 HTTP/1.1 与 CONNECT; 不碰 HTTP/2、WebSocket; 响应最多缓冲 8MB;
采集结束立即还原代理, 不把用户留在代理里。
"""
from __future__ import annotations

import base64
import datetime as dt
import http.client
import json
import os
import queue
import select
import socket
import socketserver
import ssl
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

from lptiyu.cipher import CipherError, unseal

CAPTURE_HOST_SUFFIX = "lptiyu.com"
MAX_HEAD_BYTES = 64 * 1024
MAX_BODY_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT_S = 300
CONNECT_TIMEOUT_S = 20
BACKUP_NAME = "proxy_backup.json"

REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"


def _now():
    return dt.datetime.now()


class CaptureError(RuntimeError):
    """给界面看的一句话。"""


# ================================================================ 流量解析(纯函数)
def _form_value(body: bytes, key: str):
    try:
        text = body.decode("utf-8", "replace")
        values = urllib.parse.parse_qs(text).get(key) or []
        return values[0] if values else ""
    except Exception:                      # noqa: BLE001
        return ""


def identity_from_request(body: bytes):
    """请求体 `data=<base64>` 解出来的身份字段。

    小程序每次调用都带着 uid/token/学号; 登录那一刻 token 还是旧的, 但身份是
    新的 —— 用它把 identity.json 填对, 后面的真门探活才有 uid 可用。
    """
    sealed = _form_value(body, "data")
    if not sealed:
        return None
    try:
        plain = json.loads(unseal(sealed))
    except (CipherError, ValueError):
        return None
    if not isinstance(plain, dict):
        return None
    out = {}
    for key in ("uid", "school_id", "student_num", "card_id", "term_id"):
        value = plain.get(key)
        if value not in (None, "", 0):
            out[key] = value
    return out or None


def credentials_from_login(body: bytes):
    """loginByCode 响应 `{"data": "<base64>"}` → access_token/refresh_token/uid。"""
    try:
        reply = json.loads(body.decode("utf-8", "replace"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(reply, dict) or not isinstance(reply.get("data"), str):
        return None
    try:
        plain = json.loads(unseal(reply["data"]))
    except (CipherError, ValueError):
        return None
    if not isinstance(plain, dict):
        return None
    cred = {}
    token_data = plain.get("token_data")
    if isinstance(token_data, dict) and token_data.get("access_token"):
        cred["token"] = str(token_data["access_token"])
        if token_data.get("refresh_token"):
            cred["refresh_token"] = str(token_data["refresh_token"])
        if token_data.get("refresh_expire"):
            cred["refresh_expire"] = int(token_data["refresh_expire"])
    if plain.get("uid"):
        cred["uid"] = plain["uid"]
    return cred if cred.get("token") else None


def host_in_scope(host: str) -> bool:
    host = (host or "").strip().lower().rstrip(".")
    return host == CAPTURE_HOST_SUFFIX or host.endswith("." + CAPTURE_HOST_SUFFIX)


# ================================================================ 证书
class CertificateAuthority:
    """本机专用 CA: 生成、签发站点证书、装卸(只动当前用户库)。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.key_path = self.root / "ca.key"
        self.cert_path = self.root / "ca.pem"
        self.der_path = self.root / "ca.der"
        self._leaf_cache = {}
        self._lock = threading.Lock()

    # ---------------------------------------------------------- 生成/读取
    def ensure(self):
        with self._lock:
            if self.key_path.is_file() and self.cert_path.is_file():
                return self.cert_path
            try:
                from cryptography import x509
                from cryptography.hazmat.primitives import hashes, serialization
                from cryptography.hazmat.primitives.asymmetric import rsa
                from cryptography.x509.oid import NameOID
            except ImportError as exc:      # pragma: no cover —— 打包时必有
                raise CaptureError("缺少证书组件 cryptography，无法生成采集证书") from exc
            self.root.mkdir(parents=True, exist_ok=True)
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, "youziauth 乐跑采集 CA"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "youziauth"),
            ])
            now = dt.datetime.now(dt.timezone.utc)
            cert = (x509.CertificateBuilder()
                    .subject_name(name).issuer_name(name)
                    .public_key(key.public_key())
                    .serial_number(x509.random_serial_number())
                    .not_valid_before(now - dt.timedelta(days=1))
                    .not_valid_after(now + dt.timedelta(days=3650))
                    .add_extension(x509.BasicConstraints(ca=True, path_length=0),
                                   critical=True)
                    .add_extension(x509.KeyUsage(
                        digital_signature=True, key_cert_sign=True, crl_sign=True,
                        content_commitment=False, key_encipherment=False,
                        data_encipherment=False, key_agreement=False,
                        encipher_only=False, decipher_only=False), critical=True)
                    .sign(key, hashes.SHA256()))
            self.key_path.write_bytes(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()))
            self.cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            self.der_path.write_bytes(cert.public_bytes(serialization.Encoding.DER))
            return self.cert_path

    def leaf(self, host: str):
        """给某个域名签一张站点证书, 返回 (cert_path, key_path)。同域名缓存。"""
        with self._lock:
            cached = self._leaf_cache.get(host)
            if cached and Path(cached[0]).is_file():
                return cached
        self.ensure()
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        ca_key = serialization.load_pem_private_key(self.key_path.read_bytes(), None)
        ca_cert = x509.load_pem_x509_certificate(self.cert_path.read_bytes())
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = dt.datetime.now(dt.timezone.utc)
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name([
                    x509.NameAttribute(NameOID.COMMON_NAME, host)]))
                .issuer_name(ca_cert.subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(days=1))
                .not_valid_after(now + dt.timedelta(days=397))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]),
                               critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                               critical=True)
                .sign(ca_key, hashes.SHA256()))
        leaf_dir = self.root / "leaf"
        leaf_dir.mkdir(exist_ok=True)
        cert_path = leaf_dir / f"{host}.pem"
        key_path = leaf_dir / f"{host}.key"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
        with self._lock:
            self._leaf_cache[host] = (cert_path, key_path)
        return cert_path, key_path

    def fingerprint(self):
        """DER 的 SHA1 大写指纹(与 Windows 证书库 Thumbprint 一致)。"""
        if not self.cert_path.is_file():
            return ""
        import hashlib
        if not self.der_path.is_file():
            return ""
        return hashlib.sha1(self.der_path.read_bytes()).hexdigest().upper()

    # ---------------------------------------------------------- 装卸
    def installed(self):
        fp = self.fingerprint()
        if not fp:
            return False
        try:
            out = subprocess.run(
                ["certutil", "-user", "-store", "Root"], capture_output=True,
                timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                text=True, errors="replace").stdout or ""
        except (OSError, subprocess.SubprocessError):
            return False
        return fp.replace(" ", "") in (out or "").replace(" ", "")

    def install(self):
        self.ensure()
        if self.installed():
            return {"ok": True, "detail": "证书已在当前用户的受信任根里",
                    "fingerprint": self.fingerprint()}
        try:
            done = subprocess.run(
                ["certutil", "-user", "-addstore", "Root", str(self.der_path)],
                capture_output=True, timeout=90, text=True, errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError) as exc:
            raise CaptureError(f"安装证书失败：{exc}") from exc
        if done.returncode != 0 or not self.installed():
            tail = ((done.stdout or "") + (done.stderr or "")).strip()[-300:]
            raise CaptureError(f"安装证书失败：{tail or 'certutil 返回异常'}")
        return {"ok": True, "detail": "证书已装入当前用户", "fingerprint": self.fingerprint()}

    def remove(self):
        fp = self.fingerprint()
        if not fp:
            return {"ok": True, "detail": "本机还没有生成采集证书"}
        try:
            subprocess.run(
                ["certutil", "-user", "-delstore", "Root", fp],
                capture_output=True, timeout=90, text=True, errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError) as exc:
            raise CaptureError(f"卸载证书失败：{exc}") from exc
        return {"ok": not self.installed(), "detail": "证书已从当前用户卸载"
                if not self.installed() else "证书仍在本机, 请重试", "fingerprint": fp}


# ================================================================ 系统代理
class SystemProxy:
    """HKCU 系统代理的读写、备份、还原。注册表路径可注入(测试用)。"""

    def __init__(self, backup_path: Path, reg_path: str = REG_PATH):
        self.backup_path = Path(backup_path)
        self.reg_path = reg_path
        self._saved = None

    def _open(self, write=False):
        import winreg
        return winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, self.reg_path, 0,
            winreg.KEY_READ | (winreg.KEY_SET_VALUE if write else 0))

    def read(self):
        import winreg
        with self._open() as key:
            try:
                enable = int(winreg.QueryValueEx(key, "ProxyEnable")[0])
            except FileNotFoundError:
                enable = 0
            try:
                server = str(winreg.QueryValueEx(key, "ProxyServer")[0])
            except FileNotFoundError:
                server = ""
            try:
                override = str(winreg.QueryValueEx(key, "ProxyOverride")[0])
            except FileNotFoundError:
                override = ""
        return enable, server, override

    def write(self, enable, server, override=None):
        import winreg
        with self._open(write=True) as key:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, int(bool(enable)))
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, str(server))
            if override is not None:
                winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, str(override))
        self._notify()

    @staticmethod
    def _notify():
        try:
            import ctypes
            ctypes.windll.wininet.InternetSetOptionW(0, 39, 0, 0)   # SETTINGS_CHANGED
            ctypes.windll.wininet.InternetSetOptionW(0, 37, 0, 0)   # REFRESH
        except Exception:                  # noqa: BLE001 —— 无 WinINet 时忽略
            pass

    def point_at(self, port):
        """把系统代理指到本机采集端口, 并先把原值备份下来。"""
        if self._saved is None:
            self._saved = self.read()
            try:
                self.backup_path.parent.mkdir(parents=True, exist_ok=True)
                self.backup_path.write_text(json.dumps(
                    {"enable": self._saved[0], "server": self._saved[1],
                     "override": self._saved[2]}), encoding="utf-8")
            except OSError:
                pass
        self.write(1, f"127.0.0.1:{port}", self._saved[2])

    def restore(self):
        """还原系统代理; 有崩溃遗留的备份也能收拾。"""
        saved = self._saved
        if saved is None and self.backup_path.is_file():
            try:
                data = json.loads(self.backup_path.read_text(encoding="utf-8"))
                saved = (int(data.get("enable") or 0), str(data.get("server") or ""),
                         str(data.get("override") or ""))
            except (OSError, ValueError):
                saved = None
        if saved is None:
            return False
        self.write(*saved)
        self._saved = None
        try:
            self.backup_path.unlink(missing_ok=True)
        except OSError:
            pass
        return True

    def stale_backup(self):
        return self.backup_path.is_file()


# ================================================================ 转发
def _read_head(conn):
    """读到 \\r\\n\\r\\n 为止; 返回 (head_bytes, rest) 或 (None, b'')。"""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            return None, b""
        buf += chunk
        if len(buf) > MAX_HEAD_BYTES:
            return None, b""
    head, _, rest = buf.partition(b"\r\n\r\n")
    return head, rest


def _parse_head(head: bytes):
    text = head.decode("latin-1")
    lines = text.split("\r\n")
    first = lines[0] if lines else ""
    headers = {}
    order = []
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        headers[key.lower()] = value.strip()
        order.append(key)
    return first, headers, order


def _read_request(conn, rest):
    method = path = version = ""
    headers = {}
    while True:
        if rest:
            head, rest = rest, b""
        else:
            head, rest = _read_head(conn)
        if head is None:
            return None
        method, path, version = (head.decode("latin-1").split("\r\n")[0].split(" ") + ["", "", ""])[:3]
        _first, headers, _order = _parse_head(head)
        if method and headers.get("expect", "").lower() == "100-continue":
            conn.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
        length = int(headers.get("content-length") or 0)
        body = rest
        while len(body) < length:
            chunk = conn.recv(min(65536, length - len(body)))
            if not chunk:
                break
            body += chunk
        rest = body[length:]
        return {"method": method, "path": path, "version": version,
                "headers": headers, "body": body[:length]}


class _ProxyHandler(socketserver.BaseRequestHandler):
    server = None                       # 由 _ProxyServer 注入

    def handle(self):                   # noqa: C901 —— 一次抓包处理的直线流程
        client = self.request
        client.settimeout(CONNECT_TIMEOUT_S)
        try:
            head, _rest = _read_head(client)
            if head is None:
                return
            first, _headers, _order = _parse_head(head)
            parts = first.split(" ")
            if len(parts) < 2 or parts[0].upper() != "CONNECT":
                client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                return
            host, _, port = parts[1].partition(":")
            host = host.strip().lower()
            port = int(port or 443)
            if not host_in_scope(host):
                self._tunnel(host, port)
                return
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            cert_path, key_path = self.server.ca.leaf(host)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(str(cert_path), str(key_path))
            tls = ctx.wrap_socket(client, server_side=True)
            try:
                self._serve_https(tls, host, port)
            finally:
                try:
                    tls.close()
                except OSError:
                    pass
        except (OSError, ssl.SSLError, ValueError):
            return
        finally:
            try:
                client.close()
            except OSError:
                pass

    # ---------------------------------------------------------- 隧道(非目标)
    def _tunnel(self, host, port):
        try:
            upstream = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S)
        except OSError:
            self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        sockets = [self.request, upstream]
        try:
            while True:
                readable, _, errored = select.select(sockets, [], sockets, 30)
                if errored or not readable:
                    return
                for sock in readable:
                    other = upstream if sock is self.request else self.request
                    data = sock.recv(65536)
                    if not data:
                        return
                    other.sendall(data)
        except OSError:
            return
        finally:
            try:
                upstream.close()
            except OSError:
                pass

    # ---------------------------------------------------------- 目标 HTTPS
    def _serve_https(self, tls, host, port):
        rest = b""
        while True:
            request = _read_request(tls, rest)
            if request is None:
                return
            rest = b""
            self._sniff_request(request, host)
            status, headers, body = self._forward(host, port, request)
            if status is None:
                tls.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n"
                            b"Connection: close\r\n\r\n")
                return
            self._sniff_response(request, body)
            keep = [f"HTTP/1.1 {status} {headers.get('reason', '')}".strip()]
            for key, value in headers.get("items", []):
                if key.lower() in ("content-length", "transfer-encoding", "connection"):
                    continue
                keep.append(f"{key}: {value}")
            keep.append(f"Content-Length: {len(body)}")
            keep.append("Connection: close")
            tls.sendall(("\r\n".join(keep) + "\r\n\r\n").encode("latin-1") + body)
            return              # 每个连接只服务一个请求, 由客户端重连

    @staticmethod
    def _forward(host, port, request):
        conn = http.client.HTTPSConnection(
            host, port, timeout=CONNECT_TIMEOUT_S,
            context=ssl.create_default_context())
        headers = {k: v for k, v in request["headers"].items()
                   if k not in ("proxy-connection", "connection", "content-length",
                                "accept-encoding")}
        headers["Connection"] = "close"
        headers["Accept-Encoding"] = "identity"
        try:
            conn.request(request["method"], request["path"] or "/",
                         body=request["body"] or None, headers=headers)
            resp = conn.getresponse()
            body = resp.read(MAX_BODY_BYTES)
            items = resp.getheaders()
            return resp.status, {"reason": resp.reason, "items": items}, body
        except (OSError, http.client.HTTPException):
            return None, None, b""
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _sniff_request(self, request, host):
        path = request["path"] or ""
        if "/v3/api.php/" not in path:
            return
        self.server.record_request(host, path, request["body"])

    def _sniff_response(self, request, body):
        path = request["path"] or ""
        if "loginByCode" not in path and "loginAuth" not in path:
            return
        self.server.record_login(body)


class _ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, ca, sink, port=0):
        self.ca = ca
        self.sink = sink
        super().__init__(("127.0.0.1", port), _ProxyHandler)

    def record_request(self, host, path, body):
        try:
            self.sink.on_request(host, path, body)
        except Exception:                  # noqa: BLE001 —— 解析失败不影响转发
            pass

    def record_login(self, body):
        try:
            self.sink.on_login(body)
        except Exception:                  # noqa: BLE001
            pass


# ================================================================ 会话
class CaptureSession:
    """一次采集: 起代理 → 等 token → 收尾。状态机对外只暴露 status()。"""

    def __init__(self, root: Path, clock=None, proxy_factory=None, ca=None,
                 timeout_s=DEFAULT_TIMEOUT_S, server_factory=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._clock = clock or _now
        self.ca = ca or CertificateAuthority(self.root)
        self.proxy = (proxy_factory or
                      (lambda: SystemProxy(self.root / BACKUP_NAME)))()
        self.timeout_s = int(timeout_s)
        self._server_factory = server_factory or _ProxyServer
        self._server = None
        self._thread = None
        self._tokens = queue.Queue()
        self._lock = threading.Lock()
        self._state = "idle"
        self._message = ""
        self._port = 0
        self._deadline = 0.0
        self.identity = {}

    # ---------------------------------------------------------- 状态
    def status(self):
        with self._lock:
            return {"state": self._state, "message": self._message,
                    "port": self._port,
                    "remaining_s": max(0, int(self._deadline - time.time()))
                    if self._state == "waiting" else 0}

    def _set(self, state, message):
        with self._lock:
            self._state, self._message = state, message

    def cert_status(self):
        return {"installed": self.ca.installed(), "fingerprint": self.ca.fingerprint()}

    # ---------------------------------------------------------- 生命周期
    def start(self):
        if self.proxy.stale_backup():
            self.proxy.restore()           # 上次崩溃留下的代理, 先还原
        if not self.ca.installed():
            # 装证书是明确的用户动作(界面上有按钮), 采集本身不替用户做主。
            raise CaptureError('还没有安装采集证书：先点「安装采集证书」，再开始采集')
        server = self._server_factory(self.ca, self)
        self._server = server
        port = server.server_address[1]
        self._port = port
        self.proxy.point_at(port)
        self._deadline = time.time() + self.timeout_s
        self._thread = threading.Thread(target=server.serve_forever,
                                        name="youziauth-lptiyu-capture", daemon=True)
        self._thread.start()
        self._set("waiting", "等待小程序登录：在微信里打开「数体智慧体育」并进入跑步首页")
        return self.status()

    def stop(self, state=None, message=None):
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except OSError:
                pass
            self._server = None
        try:
            self.proxy.restore()
        except Exception:                  # noqa: BLE001 —— 收尾必须尽力
            pass
        if state:
            self._set(state, message or "")

    def poll(self):
        """取到 token 就返回它(并收尾); 超时则返回 None 并置 timeout。"""
        try:
            cred = self._tokens.get_nowait()
        except queue.Empty:
            cred = None
        if cred is not None:
            self.stop("captured", "已抓到凭证")
            return cred
        with self._lock:
            waiting = self._state == "waiting"
            expired = waiting and time.time() >= self._deadline
        if expired:
            self.stop("timeout", "等待超时：没有看到小程序登录")
        return None

    def wait(self, timeout=None):
        deadline = time.time() + (timeout if timeout is not None else self.timeout_s)
        while time.time() < deadline:
            cred = self.poll()
            if cred:
                return cred
            with self._lock:
                if self._state not in ("waiting", "idle"):
                    return None
            time.sleep(0.2)
        self.stop("timeout", "等待超时：没有看到小程序登录")
        return None

    # ---------------------------------------------------------- 流量回调
    def on_request(self, host, path, body):
        identity = identity_from_request(body)
        if identity and identity.get("uid"):
            self.identity.update(identity)

    def on_login(self, body):
        cred = credentials_from_login(body)
        if not cred:
            return
        merged = dict(self.identity)
        merged.update(cred)
        merged["source"] = "capture"
        merged["issued_at"] = self._clock().isoformat(timespec="seconds")
        self._tokens.put(merged)
