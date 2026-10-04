# 打卡接口传输层失败的复盘与诊断（2026-09-28 现场）

## 现场

两台独立机器、两个账号，同一分钟出现同一类报错（用户与同学各提供一份运行记录截图）：

| | 本机 | 同学机器 |
| --- | --- | --- |
| 隔夜会话失效 | 21:04:19 `login_required` | 21:04:13 `login_required` |
| 静默重登 | 21:04:24–21:04:29 成功（学号 …104，11 个 cookie） | 同期完成 |
| **传输层失败** | **21:05:30** `[ready] 提交未生效：无法连接学校接口，请检查网络后重试` | **21:05:21** `[network_error] 无法连接学校接口，请检查网络后重试` |
| 结果待确认 | — | 21:10:36 `[uncertain] 提交结果待确认；将仅回查` |
| 服务器确认 | 21:10:00 `signed` | 21:12:31 `signed` |

两次打卡最终都被服务器确认，且各只记录一次（写前标记 + 回查契约生效，无重复）。

## 结论（区分直证与推断）

**直证**（本机日志与代码可验证）：

1. 报错只可能来自 `dorm_api.request` 的传输层分支（`dorm_api.py`）：HTTP 状态异常是另一句
   「学校接口暂不可用（HTTP n）」，登录失效是「登录已失效，请重新登录」。
2. 不是断网：失败发生在 `save` POST 上，而**紧接其后的回查 GET 成功**（`_readback` 返回 `None`
   需要 `is_signed` 正常作答 → `pending.json` 在 21:05:30 被清空）。
3. 不是程序自身的确定性缺陷：同一次运行里 `api.user` / `api.today` / `api.verify` 都成功；
   两台机器失败的**请求还不是同一个**（本机是 save，同学那行 `network_error` 只能来自 save 之前的调用）。
4. 不是账号、凭据、定位或 payload：21:04 的静默重登成功（cookie 11 个、验证码置信度 1.000），
   失败发生在其后的接口调用上；`save_browser_session` 是单块原子写入，不存在"token 存了 cookie 没存"的窗口。
5. 校园网链路当时是通的：SYSTEM 身份的认证代理（直连 `222.198.127.170`）21:00–21:10 每分钟都是
   `authenticated`（21:05 那一拍 63 s，常规 61 s）。
6. 当下实测同一条链路健康：`of.swu.edu.cn` 接口 0.17 s 返回、`idm` 登录页 0.25 s 返回（412 是站点动态防护）。

**推断**（日志无法直证，需要下一次现场来区分）：

- 失败落在"程序逻辑之外的共享链路"上：学校网关／校园网出口／本机代理链三者之一。两台机器只差 9 秒，
  共同的只能是它们共享的那一段。
- 最可能是**学校接口在开窗峰值 20 秒内没回应**：两台机器失败那晚的耗时都比各自前一晚成功时多约
  20 秒，正好等于写死的 `timeout=20`（实测：卡住的连接在 20.03 秒报同一句话；被立刻重置则在 0.02 秒报同一句话）。
- 两台机器会把当晚第一次真实写入压在 21:03–21:06，是程序节奏决定的：21:00 开窗 + `interval=300` 决定
  第一拍落在 21:00–21:05，而隔夜会话必然失效 → 先静默重登（10–60 秒）→ 才发第一次提交。
- 修复前**错误类型被抹平**（`except (URLError, TimeoutError, OSError)` → 一句话）、**按设计不记录原始原因**，
  所以上述区分在事故现场是不可能的 —— 这正是本次改动的第一动机。

## 改动

| # | 改动 | 位置 |
| --- | --- | --- |
| 1 | 受控诊断字段 `Result.detail` / `CheckinError.detail`：`失败类别 耗时／链路／本轮重试次数`，进 `history.log` 与 `status.json`；仍不含报文、token、坐标 | `dorm_checkin.py`、`dorm_api.py` |
| 2 | 学校 API 默认直连（`ProxyHandler({})` 明确忽略系统代理），诊断里记录「本机系统代理 X 已绕过」；`YOUZIAUTH_DORM_SYSTEM_PROXY=1` 可切回系统代理 | `dorm_api.py` |
| 3 | 文案分层：超时 / 域名解析失败 / 连接被拒绝 / 连接被中断 / TLS 失败 / HTTP 状态码 各自成句，状态名仍是 `network_error`（界面不用改） | `dorm_api.py` |
| 4 | 瞬时失败提前重试：只读调用同一轮重试 2 次（1 s／3 s）；POST 失败且回查确认未签到后，下一次自动检查提前到 `TRANSIENT_RETRY_SECONDS`（= 冷却 + 10 秒 ≈ 70 秒）而不是等满 `interval`。落库那次 POST 永远只发一次 | `dorm_api.py`、`dorm_checkin.py` |

不改的东西：`timeout=20` 保持（只提成命名常量 `REQUEST_TIMEOUT_SECONDS`，便于调整）；
`SUBMIT_MAX_ATTEMPTS = 3` 与冷却语义不变；登录浏览器仍然走系统代理（那条链路的指纹实验很敏感，不动）。

## 验证

- `python -m unittest discover -s tests -q` → **591 passed, 1 skipped**（新增 19 项）。
- `node --test tests/test_desktop_ui.cjs` → **91 passed**。
- 新增回归：
  - 卡住 → 「学校接口响应超时（20 秒未回应）」+ 诊断含「响应超时／直连」；立刻断线 → 「与学校接口的连接被中断」（`tests/test_dorm_api.py::FailureReportTests`）
  - 系统代理指向死端口时学校调用仍然成功（证明那一跳真的被绕过）；`YOUZIAUTH_DORM_SYSTEM_PROXY=1` 时又确实走代理（`OutboundRouteTests`）
  - 只读调用重试 2 次并把次数写进诊断；`login_required` 不重试；`save` 只发一次（`ReadRetryTests`）
  - 瞬时失败后 `next_tick` 介于冷却与 interval 之间；`login_required` / 成功不加密节奏；诊断进入 `history.log` 与 `status.json`（`tests/test_dorm_checkin.py::TransientRetryTests`）
  - 端到端：真实本地 HTTP 服务端卡住 save → `ready` + 超时文案 + `retry_soon`，学校恢复后同一引擎补上提交（`tests/test_dorm_integration.py`）

## 复现 / 诊断命令

```powershell
.\.tools\desktop-python\Scripts\python.exe -m unittest discover -s tests -q
node --test tests/test_desktop_ui.cjs
```

现场只读诊断（不提交、不写任何状态）：`.scratch/route_probe.py`（看学校流量被判给哪条出站）、
`.scratch/repro_transport_error.py`（对比"卡住"与"被重置"在界面上的差异）。

## 2026-10-04 现场：代理「全局」模式杀掉了学校 TLS 握手（自动打卡整晚未完成）

现象：21:04:13（开窗第一拍）到 22:30:41，**连续 58 次** `[network_error]`，每次都是
`TLS 握手失败 5.3s／直连／本机系统代理 127.0.0.1:7897 已绕过／本轮已重试 2 次`；
当晚最终是用户 22:32 自己手动打卡才补上（22:32:36 `signed`）。

定位（全部只读）：

| 证据 | 结果 |
| --- | --- |
| 直连 TLS 握手（`ssl.create_default_context`） | `SSLEOFError: UNEXPECTED_EOF_WHILE_READING`，5.23s（of.swu）/0.64s（idm.swu） |
| 同一时刻 `www.baidu.com` | 正常（TLSv1.2，0.21s）→ 不是断网 |
| Clash 生成的运行配置 `clash-verge.yaml`（21:53:53 订阅更新时重写） | **`mode: global`** |
| Clash Verge 日志 | `[22:32:11] Switch Proxy Mode To: rule` |
| 切模式前后同一分钟的探针 | 22:32:05–22:32:10 失败 → 22:32:12 起同一主机全部成功 |
| 订阅配置本身 `profiles/REHaEEs67K9T.yaml` | `mode: rule`；合并模板 `mCF3il8OCVYp.yaml` 为空 → 全局模式来自 Verge 的运行状态 |

结论：**Clash 处于「全局」模式时，学校流量（含本程序"直连"的请求 —— TUN 仍在接管出站）
被送到境外节点，学校入口在 TLS 握手完成前直接切断**；切回「规则」模式后
`DomainSuffix(cn) → DIRECT`，立刻恢复。程序侧无法自愈这种模式（用户态绕不过 TUN），
所以这一轮的改善方向是"让人一眼看懂该去改什么"，外加日志卫生：

| # | 改善 | 位置 |
| --- | --- | --- |
| 1 | `SSLEOFError` 单独归类为 `TLS 握手被切断`（对应文案：链路中间有设备没让它谈完），不再与证书错误混在一起 | `dorm_api.failure_kind` / `transport_message` |
| 2 | 连续同类 `network_error` 计数进诊断（`连续第 N 次`）；到第 `TRANSIENT_ESCALATE_AT = 5` 次把处置建议写进文案：「若本机开着代理…请确认它处于「规则」模式而不是「全局」模式」 | `dorm_checkin.Engine._track_transient` |
| 3 | 文案必须稳定（不带次数），否则每次都会当成"新消息" | 同上 |
| 4 | 通知去重键加入文案：状态没变但文案升级时会**再弹一次**（原来一晚 58 次失败只弹过第一条） | `desktop_bridge._tick` |
| 5 | 连续失败在 `history.log` 里折叠成一行、计数递增，不再用 58 行一模一样的记录挤掉有用历史 | `dorm_checkin.Store.record` / `_same_failure` |

回归：`tests/test_dorm_checkin.py::RepeatedFailureTests`（折叠、换类别另起一行、第 5 次升级文案、
成功后清零）、`tests/test_dorm_api.py::TlsFailureClassTests`（切断 vs 证书错误）。

**给用户的处置建议**：把 Clash Verge 切回「规则」模式并保持（订阅更新不会把它改回 rule，
但 Verge 会记住上次选择）；如果在全局模式下需要打卡，那就得先切回规则模式，
或者接受当晚手动打卡。程序现在会在第 5 次失败时把这句话直接弹出来。
