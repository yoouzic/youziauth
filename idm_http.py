"""用纯 HTTP 客户端提交 IDM 登录表单 —— 绕开「瑞数」类 WAF 对浏览器 POST 的拦截。

根因（参考上游 dan-cun/swu-daka 提交 29957ff 的实测结论，本机复现一致）
----------------------------------------------------------------------
idm.swu.edu.cn 前面挂了一层瑞数（RiverSecurity）类动态 WAF。它会把
**浏览器内一切「带 body 的 POST 到 /am/」直接拦成 HTTP 400**，响应体只有
``\\r\\n\\r\\n\\r\\n``（就是一个空白页）。被拦的包括：

    POST /am/validatecode/verify.do    页面自己的验证码预校验
    POST /am/UI/Login                  登录表单的正式提交（form.submit() 与 fetch 都一样）

后果：点「登录」之后浏览器只会停在一个**空白页**（``idm.swu.edu.cn/am/UI/Login``），
既不跳转也不报错；自动化脚本据此判定失败并反复重试，表现为「连续登录」。
**问题不在脚本填得多快，而在这个 POST 根本到不了服务端。**

解决办法
--------
纯 HTTP 客户端（不经浏览器）**不被这层 WAF 拦截**：

    GET  /am/validate.code   -> 200 image/jpeg（100x30，4 位数字，本地模型可读）
    POST /am/UI/Login        -> 302，响应头 X-AuthErrorCode 给出真实鉴权结果

于是把职责拆开：

    浏览器   只负责走 SSO 链路（CAS -> uaaap -> 联邦认证）、持有会话、导出表单字段
    本模块   只负责「取验证码 + 提交账号密码」这一步

两个必须遵守的实测结论：

1. **账号密码明文提交即可**，页面那套 ``strEnc`` 加密是历史包袱；
   用 strEnc 提交反而会被服务端判「用户名或密码错误」（会让人误以为密码错了）。
2. **提交失败后会话即失效，同一会话内重试没有意义**，必须重走 SSO 换新会话。
   因此本模块只在「验证码没识别出来、根本没发出 POST」时才原地换一张重试。

★ 判定必须分层，不能只看一个响应头（2026-09-26 修复）
--------------------------------------------------
旧判据是「``X-AuthErrorCode == "0"`` 才算成功，其余都算失败」，而上层又对
「非密码错」的失败一律换会话重试 —— 于是一旦判据缺失（响应头被中间层吞掉、
连接在响应回来前断掉），**一个服务端已经认过的登录会被判成失败**：
页面被强行导航走、紧接着又登一次，原本那条跳转链（POST 的 302 →
oauth2/authorize → 联邦 → exchange-token）就此被打断。
现在按证据强弱分层（见 `judge_response`），并把「说不清」单列成 unconfirmed：
它由上层「先接上会话、等 token」来定论，**不触发重登**。

对外只暴露 `export_login_form` 与 `submit_login` 两个函数，不依赖 Playwright
以外的任何第三方库（HTTP 用标准库 urllib）。
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlsplit

IDM_BASE = "https://idm.swu.edu.cn"
IDM_LOGIN_URL = IDM_BASE + "/am/UI/Login"
IDM_CAPTCHA_URL = IDM_BASE + "/am/validate.code"

# 认证链路只在学校域名内；落点跑到别处一律视为不可信。
SCHOOL_DOMAIN_SUFFIXES = ("swu.edu.cn",)
# 「又回到登录页」的路径特征：说明这次提交没被接受。
LOGIN_PATH_MARKERS = ("/am/ui/login", "/cas/login")

# 与站点上真实浏览器一致的 UA；用 Python 默认 UA 反而更容易被拦
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 只在验证码不可用时原地换图重试的次数（发出 POST 之后一律不再原地重试）
CAPTCHA_TRIES = 4

# 失败原因分类用的服务端文案特征（实测抓取，2026-09）
#   X-AuthErrorCode 对下面两种失败**都是 -1**，光看错误码无法区分；
#   但正文文案不同，可以据此判断「还有没有必要重试」：
#     验证码错       -> UAMS 验证失败。动态口令验证失败
#     账号密码错     -> UAMS 验证失败。用户名或密码错误。
#   这个区分直接决定了重试策略：密码错时重试必然再错，只会白烧一次登录尝试、
#   白白逼近账号锁定阈值，因此必须立刻停手。
CREDENTIAL_FAILURE_MARKERS = ("用户名或密码错误", "用户名错误", "密码错误")
CAPTCHA_FAILURE_MARKERS = ("动态口令验证失败", "验证码错误", "验证码不能为空")


def classify_failure(text: str) -> str:
    """把服务端失败文案归类：credentials / captcha / unknown。

    先判 credentials：它更具体，且绝不能因为文案里碰巧含「验证」二字被误判。
    """
    if not text:
        return "unknown"
    if any(marker in text for marker in CREDENTIAL_FAILURE_MARKERS):
        return "credentials"
    if any(marker in text for marker in CAPTCHA_FAILURE_MARKERS):
        return "captcha"
    return "unknown"


def _host_path(value: str) -> tuple[str, str] | None:
    """把 URL 归一成 (小写主机, 去尾斜杠的小写路径)；解析不出来返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    if not parts.hostname:
        return None
    return parts.hostname.lower(), (parts.path or "/").rstrip("/").lower()


def is_school_url(value: str) -> bool:
    """URL 是否属于学校域名（认证链路上的跳转只应发生在这里面）。"""
    parsed = _host_path(value)
    if parsed is None:
        return False
    host = parsed[0]
    return any(host == suffix or host.endswith("." + suffix) for suffix in SCHOOL_DOMAIN_SUFFIXES)


def is_login_url(value: str) -> bool:
    """URL 是否把浏览器送回登录页（= 这次提交被退回，不是成功）。"""
    parsed = _host_path(value)
    if parsed is None:
        return True
    return any(marker in parsed[1] for marker in LOGIN_PATH_MARKERS) or not is_school_url(value)


def judge_response(status, auth_error, location, text, fields) -> tuple[bool, str, str]:
    """判定一次 POST 的结果：返回 (是否认证通过, 判据来源, failure_kind)。

    ★ 为什么不能只看 `X-AuthErrorCode == "0"`（这是「第一次登录成功却又登一次」的根因）
    ------------------------------------------------------------------------------
    旧判据只有一条：响应头等于 "0" 才算成功，其余一律算失败，而上层的重试策略又是
    「只要不是密码错就换新会话再试一次」。于是**判据缺失**（响应头被中间层吞掉、
    连接在响应回来前断掉）就会把一个**服务端已经认过的登录**判成失败：
    页面被 `fresh_session()` 强行导航走（用户看到的就是「学校登录页被跳走」），
    紧接着又发一次登录 —— 而原本那条跳转链（POST 的 302 → oauth2/authorize → 联邦
    → exchange-token）就这样被打断了。

    因此这里按**证据强弱**分层判定，且把「说不清」与「明确失败」分开：

      1. `X-AuthErrorCode == "0"`            → 通过（判据 header）
      2. 3xx 且落点 == 表单自带的 `goto`      → 通过（判据 redirect）
         （`goto` 是页面自己声明的成功落点，`gotoOnFail` 是失败落点，都会随会话变化）
      3. 3xx 但落回登录页 / `gotoOnFail` / 非学校域名 → 明确失败
      4. 其余（含响应丢失、无落点、文案读不出）  → unconfirmed「未确认」

    第 4 类**绝不是失败**：调用方应先把会话接上、让浏览器走完跳转链去等
    exchange-token，只有拿不到 token 才谈得上失败 —— 绝不能原地再登一次。
    """
    if auth_error == "0":
        return True, "header", ""

    # 文案先归一：只有空白（WAF 的 \r\n\r\n\r\n 空白页）等于「什么都没说」。
    text = (text or "").strip()

    if status in (301, 302, 303, 307, 308):
        target = _host_path(location)
        if target is not None:
            if target == _host_path((fields or {}).get("goto")):
                return True, "redirect", ""
            if (target == _host_path((fields or {}).get("gotoOnFail"))
                    or is_login_url(location)):
                return False, "", "rejected"
        # 落点不认识：当作「可能已经认证」处理，交给上层验证，不自动重登。
        return False, "", "unconfirmed"

    if not text:
        # 200/4xx/5xx 且读不出任何服务端文案：同样说不清，按未确认处理
        # （站点 WAF 的 400 空白页也落在这里 —— 它同样不该换来一次新的登录）。
        return False, "", "unconfirmed"

    return False, "", classify_failure(text)

# 从页面导出登录表单的全部隐藏字段与 action。
# 这些字段（goto / encoded / SunQueryParamsString 等）是 SSO 回跳所必需的，
# **必须每次实时取，不能硬编码** —— 它们随会话变化。
EXPORT_FORM_JS = """() => {
    const f = document.forms['Login'];
    const out = {fields: {}, action: null, pageurl: location.href};
    if (!f) return out;
    Array.from(f.elements).forEach(el => { if (el.name) out.fields[el.name] = el.value; });
    out.action = f.action;
    return out;
}"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止自动跟随重定向：302 本身携带鉴权结果，跟随就看不到了。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class SubmitOutcome:
    """一次提交的结果（用于日志与诊断，绝不记录密码）。"""

    ok: bool = False
    code: str = ""              # 模型识别出的验证码
    confidence: float = 0.0
    status: int | None = None   # POST 的 HTTP 状态；None = 连响应都没拿到
    auth_error: str | None = None  # 响应头 X-AuthErrorCode；"0" 表示认证通过
    cookies: dict[str, str] = field(default_factory=dict)
    detail: str = ""            # 失败原因（人话）
    failure_kind: str = ""      # credentials / captcha / unknown / unconfirmed / rejected
    server_message: str = ""    # 服务端原文（已粗洗 HTML，截断）
    location: str = ""          # 3xx 的 Location：浏览器该顺着走的那一跳
    accepted_by: str = ""       # ok=True 时的判据来源：header / redirect
    sent: bool = False          # POST 是否**发出去过**（与「拿到响应」是两件事）

    @property
    def submitted(self) -> bool:
        return self.status is not None

    @property
    def unconfirmed(self) -> bool:
        """说不清有没有认证成功（响应丢失 / 判据缺失 / 落点不认识）。

        这类结果**必须**交给上层「先接上会话、等 exchange-token」来验证，
        绝不能当成失败再去登一次 —— 那正是「第一次成功却又登一次」的成因。
        注意它**不包含**明确被退回（failure_kind == "rejected"）：那是有据可依的失败，
        没有会话可验证，交回人工即可。
        """
        return not self.ok and self.failure_kind == "unconfirmed"

    @property
    def retryable(self) -> bool:
        """是否值得换新会话再提交一次。

        只有服务端**明确**说「验证码错」才值得：换会话能换一张新验证码。
        其它情形（密码错、服务端异常、判据缺失、连响应都没拿到）重试要么必然再错，
        要么把一次可能已经成功的登录踩掉，只会白烧登录尝试、逼近账号锁定阈值。
        """
        if self.ok:
            return False
        if self.sent:
            # 服务端明说验证码错 —— 唯一「再试一次就有意义」的失败。
            return self.failure_kind == "captcha"
        # 连 POST 都没发出去：只有「取图/识别这条链路自身出错」值得换会话重来一次
        # （会话可能已被风控挡住）。纯低置信（captcha_unavailable）说明模型读不了
        # 这几张图，换会话只是白跳一次页面，不算可重试。
        return self.detail.startswith(("captcha_fetch_error", "captcha_http_", "solve_error"))


def _plain_text(html: str) -> str:
    """粗洗 HTML 取可见文本，用于读出服务端的错误文案。"""
    import re  # noqa: PLC0415

    text = re.sub(r"<script[\s\S]*?</script>", " ", html or "", flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class HttpClient:
    """最小可用的带 Cookie 的 HTTP 客户端（标准库，无第三方依赖）。

    手工维护 Cookie 头而非用 CookieJar：浏览器导出的 cookie 域各不相同
    （``.swu.edu.cn`` / ``idm.swu.edu.cn``），手工拼头最直观、也最不容易出错；
    回灌浏览器时再由调用方统一设成 ``.swu.edu.cn``。
    """

    def __init__(self, cookies: dict[str, str], *, referer: str = IDM_LOGIN_URL):
        self.cookies = dict(cookies or {})
        self.referer = referer
        self._opener = urllib.request.build_opener(_NoRedirect())

    def request(self, method: str, url: str, *, data: bytes | None = None,
                accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                timeout: int = 30) -> tuple[int, object, bytes]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": self.referer,
            "Origin": IDM_BASE,
        }
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                status, response_headers, body = response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            # 302/400 等都会走到这里（禁用了自动重定向）
            status, response_headers, body = exc.code, exc.headers, exc.read()
        self._absorb(response_headers)
        return status, response_headers, body

    def _absorb(self, headers) -> None:
        try:
            raw_values = headers.get_all("Set-Cookie") or []
        except Exception:  # noqa: BLE001
            raw_values = []
        for raw in raw_values:
            pair = raw.split(";", 1)[0].strip()
            if "=" in pair:
                name, _, value = pair.partition("=")
                self.cookies[name.strip()] = value.strip()


def export_login_form(page) -> dict:
    """从浏览器当前页面导出 `forms['Login']` 的字段与 action。

    必须实时取：`goto` / `SunQueryParamsString` 等随会话变化，硬编码必然失效。
    """
    try:
        info = page.evaluate(EXPORT_FORM_JS)
    except Exception:  # noqa: BLE001
        return {}
    return info if isinstance(info, dict) else {}


JS_VISIBLE_COOKIES = "() => document.cookie"


def browser_cookies(context, page=None) -> dict[str, str]:
    """取浏览器里 idm.swu.edu.cn 的 cookie（服务端靠它认这是同一个 SSO 会话）。

    ★ 为什么要把「页面 JS 读得到的 cookie」剔掉（2026-09-26 实测根因）
    ------------------------------------------------------------------
    站点动态防护把自己要读的那些 cookie **必须让页面 JS 能读到**（所以它们会出现在
    `document.cookie` 里），而真正的服务端会话 cookie 是 HttpOnly 的。于是：
    一个**不是**由那个页面 JS 发起、却带着这些 JS 可见 cookie 的请求，会被动态防护
    判成重放，一律回 **HTTP 400 + 空白页（`\\r\\n\\r\\n\\r\\n`）**。实测：

        urllib + 浏览器全部 cookie              → 400（换 UA 也一样）
        urllib + 只带 HttpOnly 的会话 cookie     → 200，而且 POST 真到达服务端：
                                                  `X-AuthErrorCode=-1` +
                                                  「UAMS 验证失败。用户名或密码错误。」
        urllib 不带 cookie                       → 200

    之前那种「把浏览器 cookie 原样搬给 urllib」的写法，正是把整条链拖死的元凶：
    取验证码 400（`captcha_http_400`），认证成功后的授权跳转也会 400，
    界面就卡在 `idm.swu.edu.cn/am/oauth2/authorize` 一动不动。

    传了 `page` 就按上面的规则过滤；不传（探针脚本）则维持原样全部返回。
    """
    try:
        cookies = {cookie["name"]: cookie["value"] for cookie in context.cookies(IDM_BASE)}
    except Exception:  # noqa: BLE001
        return {}
    if page is None:
        return cookies
    try:
        raw = page.evaluate(JS_VISIBLE_COOKIES) or ""
    except Exception:  # noqa: BLE001
        return cookies          # 读不到就退回全部：宁可像以前那样试，也不要凭空少带
    visible = {part.split("=", 1)[0].strip() for part in str(raw).split(";") if part.strip()}
    return {name: value for name, value in cookies.items() if name not in visible}


def submit_login(info: dict, cookies: dict, username: str, password: str, solver,
                 *, log=None, cancel=None) -> SubmitOutcome:
    """用纯 HTTP 客户端提交 IDM 登录表单。

    返回 SubmitOutcome。`ok=True` 有两种判据来源（`accepted_by`）：
    响应头 `X-AuthErrorCode == "0"`，或 3xx 落点正是表单声明的成功落点 `goto`
    （见 `judge_response`）。两种情况下 `cookies` 都是认证后的新会话 cookie，
    调用方需灌回浏览器；`location` 是服务端给的下一跳，浏览器应顺着它走完 SSO。

    `ok=False` 时看 `failure_kind` 决定下一步（见 SubmitOutcome.retryable /
    unconfirmed）——**不要**把 ok=False 一律当成「没成功，再登一次」。
    """
    def _log(message: str) -> None:
        if log:
            log(message)

    action = (info or {}).get("action") or IDM_LOGIN_URL
    page_url = (info or {}).get("pageurl") or IDM_LOGIN_URL
    fields = dict((info or {}).get("fields") or {})
    if not fields:
        return SubmitOutcome(detail="no_login_form")

    client = HttpClient(cookies, referer=page_url)

    for _ in range(CAPTCHA_TRIES):
        if cancel is not None and cancel.is_set():
            return SubmitOutcome(failure_kind="cancelled")
        try:
            status, _headers, body = client.request(
                "GET", IDM_CAPTCHA_URL, accept="image/*,*/*;q=0.8", timeout=25)
        except Exception as exc:  # noqa: BLE001
            return SubmitOutcome(detail=f"captcha_fetch_error: {type(exc).__name__}")
        if status != 200 or len(body) < 200:
            return SubmitOutcome(detail=f"captcha_http_{status}")

        if cancel is not None and cancel.is_set():
            return SubmitOutcome(failure_kind="cancelled")
        try:
            code, confidence = solver.solve(body)
        except Exception as exc:  # noqa: BLE001
            return SubmitOutcome(detail=f"solve_error: {type(exc).__name__}: {exc}")
        if code is None:
            # 低置信：没发出 POST，本会话仍然有效，换一张再来
            _log(f"验证码置信度 {confidence:.3f} 偏低，换一张（未提交，不消耗登录尝试）")
            continue

        # ★ 明文提交即可；用页面那套 strEnc 反而会被判「用户名或密码错误」
        payload = dict(fields)
        payload["IDToken1"] = username
        payload["IDToken2"] = password
        payload["IDToken3"] = ""
        payload["validateCode"] = code
        data = urllib.parse.urlencode(payload).encode("utf-8")

        if cancel is not None and cancel.is_set():
            return SubmitOutcome(failure_kind="cancelled")
        try:
            status, headers, body = client.request("POST", action, data=data)
        except Exception as exc:  # noqa: BLE001
            # ⚠ POST **已经发出去了**，只是没拿到响应（超时/连接被重置/代理断流）。
            # 这**不等于**「没提交」：服务端可能已经认证成功。标记为未确认，
            # 由上层先把会话接上、等 exchange-token 来定论，绝不在这里再登一次。
            return SubmitOutcome(code=code, confidence=confidence, sent=True,
                                 cookies=dict(client.cookies), failure_kind="unconfirmed",
                                 detail=f"submit_error: {type(exc).__name__}")

        message = _plain_text(body.decode("utf-8", "replace"))
        location = headers.get("Location") or ""
        ok, accepted_by, failure_kind = judge_response(
            status, headers.get("X-AuthErrorCode"), location, message, fields)
        outcome = SubmitOutcome(code=code, confidence=confidence, status=status,
                                auth_error=headers.get("X-AuthErrorCode"), sent=True,
                                cookies=dict(client.cookies), location=location,
                                ok=ok, accepted_by=accepted_by, failure_kind=failure_kind,
                                server_message=message[:200])
        if ok:
            outcome.detail = "authenticated" if accepted_by == "header" else "accepted_by_redirect"
            return outcome

        # 已发出 POST：本会话已失效，原地重试没有意义，交回上层按 failure_kind 决策
        outcome.detail = f"auth_error={outcome.auth_error} status={status} kind={failure_kind}"
        if outcome.server_message:
            outcome.detail += f" | {outcome.server_message[:160]}"
        return outcome

    return SubmitOutcome(detail="captcha_unavailable")
