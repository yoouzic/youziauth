"""传输层: 连接 + 真机指纹头组 + 编码解码。

连接形态: 直连或 CONNECT 隧道(proxy='host:port')二选一, 由调用方按需选择。
2026-11-11 实测修正: 直连同样可用(token 与出口无绑定), 不再强制经链。
"""
import gzip
import http.client
import zlib

from . import spec, tls

try:
    import brotli as _br
except ImportError:                    # pragma: no cover
    try:
        import brotlicffi as _br       # type: ignore
    except ImportError:
        _br = None

ACCEPT_ENC = "gzip, deflate, br" if _br else "gzip, deflate"


def connect(host: str, port: int = 443, timeout: int = 15,
            proxy: str = None, ca_bundle: str = "") -> http.client.HTTPSConnection:
    """建 TLS 连接; proxy='host:port' 走 CONNECT 隧道。默认头清空, 指纹由 post_form 锁定。"""
    if proxy and ":" in proxy:
        ph, pp = proxy.rsplit(":", 1)
        conn = http.client.HTTPSConnection(ph, int(pp), timeout=timeout,
                                           context=tls.build_context(ca_bundle))
        conn.set_tunnel(host, port)
    else:
        conn = http.client.HTTPSConnection(host, port, timeout=timeout,
                                           context=tls.build_context(ca_bundle))
    try:
        conn.default_headers.clear()
    except Exception:                  # pragma: no cover —— 版本差异防御
        pass
    return conn


def post_form(conn: http.client.HTTPSConnection, path: str, body: bytes,
              flag: bool = False, accept_encoding: str = None):
    """真机指纹头组发送 form POST。

    头序锁定(逐字节实录): content-length, content-type, [flag], charset,
    referer, user-agent, accept-encoding; flag 仅两个端点出现, 值为 JS
    String(Boolean) 的产物字面量。
    """
    conn.putrequest("POST", path, skip_accept_encoding=True)
    conn.putheader("content-length", str(len(body)))
    conn.putheader("content-type", "application/x-www-form-urlencoded")
    if flag:
        conn.putheader("flag", spec.FLAG_LITERAL)
    conn.putheader("charset", "utf-8")
    conn.putheader("referer", spec.WXHR_REFERER)
    conn.putheader("user-agent", spec.UA_ANDROID)
    conn.putheader("accept-encoding", accept_encoding or ACCEPT_ENC)
    conn.endheaders(body)
    return conn.getresponse()


def decode(raw: bytes, content_encoding: str) -> bytes:
    """按 Content-Encoding 解码; 未知/失败原样返回(上层自行容错)。"""
    enc = (content_encoding or "").lower().strip()
    try:
        if "gzip" in enc:
            return gzip.decompress(raw)
        if "br" in enc and _br:
            return _br.decompress(raw)
        if "deflate" in enc:
            try:
                return zlib.decompress(raw, -15)
            except zlib.error:
                return zlib.decompress(raw)
    except Exception:                  # noqa: BLE001
        return raw
    return raw
