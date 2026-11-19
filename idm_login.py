"""西南大学统一认证（IDM）静默登录：自动填账号密码与验证码。

背景与设计取舍
--------------
本项目原先刻意不保存密码，IDM 登录完全由人工在小窗里完成。
启用静默登录后，程序需要自动填写，因此必须持久化 IDM 凭据
（见 idm_credentials.py，DPAPI 加密、仅本机当前用户可解密）。

这张表单的**完整地址链**（曾经搞错的根源，务必记住）
--------------------------------------------------
    of.swu.edu.cn/cas/login?service=...        ← 三段式跳转的起点
      ↓
    uaaap.swu.edu.cn/cas/login?service=...     ← 「推荐登录」三选一选择页
      ↓  点「统一认证登录」（onclick = _goLogin()，给 URL 追加 federalEnable=true）
    idm.swu.edu.cn/am/UI/Login?realm=...       ← 本模块处理的表单页

两个坑：
  * 中间那次跳转**必须由浏览器点击触发**。用 page.goto 重新导航同一个 URL
    （原样或带 federalEnable=true）会被站点动态防护判为异常并返回 HTTP 400。
    落点推进见 dorm_login.enter_idm_login()。
  * 「统一认证登录」按钮上的文字画在 img/unified_button.png 里，DOM 中
    没有任何文本节点（全文档搜「统一认证登录」命中 0 次），只能用 onclick 定位。

★ 提交为什么走纯 HTTP 客户端，而不是在浏览器里点「登录」
------------------------------------------------------
idm.swu.edu.cn 前面有一层瑞数（RiverSecurity）类动态 WAF，它把
**浏览器内一切「带 body 的 POST 到 /am/」拦成 HTTP 400**，响应体只有一个
空白页。也就是说在浏览器里 `form.submit()` 根本到不了服务端 —— 表单一填完
页面就卡在空白的 `idm.swu.edu.cn/am/UI/Login`，既不跳转也不报错，
自动化只能判失败并反复重试（表现为「连续登录」）。

绕行办法：浏览器只负责走 SSO 链路与导出表单字段，**取验证码与提交账号密码
交给纯 HTTP 客户端**（见 idm_http.py），它不被这层 WAF 拦截。
成功判据也从「是否离开 idm 主机」换成了服务端明示的响应头
`X-AuthErrorCode == "0"` —— 比原先的间接推断可靠得多，而且能拿到真实错误码。

关于重试（重要）
----------------
**提交失败后该会话即失效，同一会话内重试没有意义**，必须重走 SSO 换新会话。
因此：

    * 只有在「验证码没识别出来、根本没发出 POST」时才原地换一张重试；
    * **只有服务端明确说「验证码错」时才换新会话再提交一次** —— 这是唯一值得的
      重试（换会话能换到一张新验证码）；
    * 密码错 → 立刻停手；判据缺失 / 响应丢失 / 落点不认识（unconfirmed）→ **不重登**，
      把会话交回上层先走完跳转链、等 exchange-token 定论；
    * 最多 MAX_AUTO_ATTEMPTS(=2) 次自动尝试，超过后交由人工接管。

最坏情况只消耗 2 次登录尝试，配合 2 次成功率 99.99%+，
既拿到绝大部分自动化收益，又把账号锁定风险控制在很低水平。

★ 为什么「不重登」和「重试」一样重要（2026-09-26 修复）
----------------------------------------------------
旧策略是「只要不是密码错就重走 SSO 再试一次」，而成功判据只有响应头一条。
两者叠加的后果：判据一旦缺失（响应头被中间层吞掉、响应丢失），一个**服务端已经
认证成功**的登录会被判成失败 —— 正在走的登录页被 `fresh_session()` 强行导航走，
紧接着又发一次登录。用户看到的就是「第一次登录成功后页面被跳走、程序又登了一次」，
而原本那条跳转链（POST 的 302 → oauth2/authorize → 联邦 → exchange-token）被打断。
现在：判据分层（见 idm_http.judge_response），重试只留给验证码错。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

MAX_AUTO_ATTEMPTS = 2

# 页面元素选择器（实测自 idm.swu.edu.cn 登录页）
SEL_USERNAME = "#loginName"
SEL_PASSWORD = "#password"
SEL_CODE = "#validateCode"
SEL_CODE_IMAGE = "#kaptchaImage"
SEL_LOGIN_FORM = "form[name=Login], #loginForm"

IDM_HOSTS = ("idm.swu.edu.cn",)


@dataclass
class SilentAttempt:
    """一次自动尝试的结果记录，用于诊断与日志（不记录密码明文）。"""

    attempt: int
    captcha_needed: bool = False
    captcha_code: str = ""
    captcha_conf: float = 0.0
    submitted: bool = False
    note: str = ""
    auth_error: str | None = None


@dataclass
class SilentResult:
    ok: bool
    attempts: list[SilentAttempt] = field(default_factory=list)
    reason: str = ""
    cookies: dict[str, str] = field(default_factory=dict)
    resume_url: str = ""     # 服务端给的下一跳：浏览器应顺着它走完 SSO
    accepted_by: str = ""    # ok=True 时的判据来源：header / redirect


def on_idm_login_page(page, wait_ms: int = 0) -> bool:
    """判断当前页面是否为需要填写的 IDM 登录表单页。

    wait_ms>0 时先等待表单渲染完成 —— 实测 `page.goto(..., domcontentloaded)`
    返回时页面的 JS 尚未把表单渲染出来，直接 count() 会误判为"不在登录页"。
    """
    try:
        url = urlsplit(page.url)
    except Exception:  # noqa: BLE001
        return False
    if url.scheme != "https" or url.hostname not in IDM_HOSTS:
        return False
    try:
        if wait_ms > 0:
            page.wait_for_selector(SEL_USERNAME, timeout=wait_ms, state="attached")
        return page.locator(SEL_USERNAME).count() > 0 and page.locator(SEL_PASSWORD).count() > 0
    except Exception:  # noqa: BLE001
        return False


def left_idm_host(page) -> bool:
    """当前页面是否已不在 IDM 主机上。

    注意：自从提交改走纯 HTTP 客户端后，**成功判据不再依赖它**
    （见 idm_http.SubmitOutcome.auth_error），这里仅用于辅助诊断与日志。
    """
    try:
        url = urlsplit(page.url)
    except Exception:  # noqa: BLE001
        return False
    return not (url.scheme == "https" and url.hostname in IDM_HOSTS)


def attempt_silent_login(
    page,
    username: str,
    password: str,
    *,
    solver=None,
    cancel=None,
    fresh_session=None,
    max_attempts: int = MAX_AUTO_ATTEMPTS,
    log=None,
) -> SilentResult:
    """在已打开的 IDM 登录页尝试自动登录（提交走纯 HTTP 客户端，绕开 WAF）。

    参数
    ----
    fresh_session : 可选的无参回调。每次重试前调用，由调用方**重走 SSO 链路**
        以获得新会话（清 Cookie 后重新导航 INIT_URL → 联邦中转页 → 点「统一认证登录」）。
        实测：提交失败后会话即失效，原地重试没有意义；而直接 reload 登录页会被
        WAF 打成 400 空白页，所以只能重走链路。

    返回 SilentResult；ok=True 表示服务端返回 X-AuthErrorCode == "0"，
    且 result.cookies 里是认证后的新会话 cookie（调用方需灌回浏览器并重走 SSO 收尾）。
    """
    def _log(message: str) -> None:
        if log:
            log(message)

    result = SilentResult(ok=False)

    if not on_idm_login_page(page, wait_ms=8000):
        result.reason = "not_on_idm_form"
        return result

    if solver is None:
        try:
            from captcha_ocr import default_solver  # noqa: PLC0415

            solver = default_solver()
        except Exception as exc:  # noqa: BLE001
            result.reason = f"solver_unavailable: {exc}"
            _log(f"验证码识别不可用：{exc}")
            return result

    try:
        import idm_http  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        result.reason = f"http_client_unavailable: {exc}"
        _log(f"HTTP 登录组件不可用：{exc}")
        return result

    for n in range(1, max_attempts + 1):
        if cancel is not None and getattr(cancel, "is_set", lambda: False)():
            result.reason = "cancelled"
            return result

        record = SilentAttempt(attempt=n)

        # 第一轮之外，每次都重走 SSO 换新会话（提交失败即会话失效，原地重试无意义）
        if n > 1:
            if fresh_session is None:
                result.reason = "exhausted"
                break
            _log(f"第 {n} 次：重走 SSO 链路换取新会话")
            try:
                fresh_session()
            except Exception as exc:  # noqa: BLE001
                record.note = f"fresh_session_failed: {type(exc).__name__}"
                result.attempts.append(record)
                _log(f"第 {n} 次：重走链路失败 {exc}")
                break
            if not on_idm_login_page(page, wait_ms=8000):
                record.note = "not_on_login_form"
                result.attempts.append(record)
                _log(f"第 {n} 次：重走链路后未回到登录表单页")
                break

        record.captcha_needed = True

        info = idm_http.export_login_form(page)
        if not info.get("fields"):
            record.note = "form_fields_unavailable"
            result.attempts.append(record)
            _log(f"第 {n} 次：当前页面没有登录表单字段")
            break

        cookies = idm_http.browser_cookies(page.context, page)
        _log(f"第 {n} 次：用会话 cookie 提交（{len(cookies)} 枚；站点动态防护那几枚"
             f"已剔除，带上它们会被判重放并回 400）")
        try:
            outcome = idm_http.submit_login(info, cookies, username, password, solver,
                                            log=_log, cancel=cancel)
        except Exception as exc:  # noqa: BLE001
            record.note = f"submit_error: {type(exc).__name__}"
            result.attempts.append(record)
            _log(f"第 {n} 次：提交异常 {exc}")
            continue

        if outcome.failure_kind == "cancelled":
            result.reason = "cancelled"
            return result
        record.captcha_code = outcome.code
        record.captcha_conf = float(outcome.confidence)
        record.submitted = outcome.submitted
        record.auth_error = outcome.auth_error

        if outcome.ok:
            result.ok = True
            result.reason = "authenticated"
            result.accepted_by = outcome.accepted_by
            result.cookies = dict(outcome.cookies)
            result.resume_url = outcome.location
            result.attempts.append(record)
            _log(f"第 {n} 次：认证通过（判据={outcome.accepted_by}，验证码 {outcome.code}，"
                 f"置信度 {outcome.confidence:.3f}，HTTP {outcome.status}，"
                 f"X-AuthErrorCode={outcome.auth_error}）")
            return result

        record.note = outcome.detail
        result.attempts.append(record)

        if outcome.failure_kind == "credentials":
            # 服务端明确说「用户名或密码错误」：重试必然再错，只会白烧一次登录尝试、
            # 白白逼近账号锁定阈值。立刻停手并把这个事实报上去，让人去改凭据。
            result.reason = "bad_credentials"
            _log(f"第 {n} 次：服务端判为「用户名或密码错误」—— 重试无意义，停止自动尝试")
            return result

        if outcome.retryable:
            # ★ 唯一值得重试的失败：服务端明说「验证码错」→ 换新会话能换一张新验证码。
            # （取图/识别链路自身出错也算：那不是一次登录尝试，换会话还有救。）
            _log(f"第 {n} 次：未通过（{outcome.detail or outcome.failure_kind}）"
                 f"—— 换新会话再试一次")
            continue

        # ★★ 其余情形一律**不再自动重登**（这是「第一次登录成功、页面却被跳走、
        # 紧接着又登一次」的修复点）：
        #   unconfirmed —— 判据缺失/响应丢失/落点不认识：服务端**可能已经认证成功**。
        #       把会话交回上层，先让浏览器走完跳转链等 exchange-token 定论；
        #       在这里再登一次只会把一次已经成功的登录踩掉。
        #   rejected / unknown（4xx、5xx、文案读不出、非验证码的服务端异常）：
        #       重试要么同样失败，要么把一次可能成功的登录踩掉 —— 停手，交回人工/上层。
        result.reason = outcome.failure_kind or ("not_sent" if not outcome.sent else "unconfirmed")
        if outcome.unconfirmed:
            result.cookies = dict(outcome.cookies)
            result.resume_url = outcome.location
        _log(f"第 {n} 次：{'未确认（可能已认证）' if outcome.unconfirmed else '未通过'}"
             f"（HTTP {outcome.status}，X-AuthErrorCode={outcome.auth_error}，"
             f"归类={result.reason}，已发出 POST={outcome.sent}，{outcome.detail or '-'}）"
             f"—— 不再自动重登，交上层验证会话")
        return result

    if not result.reason:
        result.reason = "exhausted"
    return result
