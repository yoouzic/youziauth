"""TLS 策略: 默认全量校验(系统信任库 + 可选追加 CA)。

采集链(mitm)重签的 lptiyu 证书只在「业务请求经采集链隧道发出」时出现,
那时调用方把链的 CA 文件路径传进来即可; 直连时系统信任库足够 —— 2026-11-11
实测: 直连(无代理)请求真门 getTermList 返回 200 且解密出有效学期列表。
"""
import ssl


def build_context(ca_bundle: str = "") -> ssl.SSLContext:
    """返回系统信任库 + 可选 CA 的 SSLContext; 主机名校验始终开启。"""
    ctx = ssl.create_default_context()
    if ca_bundle:
        ctx.load_verify_locations(cafile=ca_bundle)
    return ctx
