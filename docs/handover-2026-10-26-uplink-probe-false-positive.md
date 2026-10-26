# 交接：2026-10-26 「外网可通」误判让「重新认证」成了空操作

## 0. 一句话现状

装的是 1.8.38。今天 19:08–19:13 又出现「点认证没反应」：校园网其实需要网页认证，但
uplink 探测被门户放行的两个连通性端点骗成「外网可通」，`_run_cycle` 在这个分支直接返回，
用户 122 次重试没有一次去问门户、也没有一次尝试登录。19:13 探测终于判成「外网不通」，
一秒内就登录成功 —— 这前后一秒的对比把误判暴露得很清楚。源码已修，目标版本 **v1.8.39**。

## 1. 现场证据（本机，2026-10-26）

| 证据 | 数值 |
|---|---|
| 界面截图 19:12:11 | 当前连接「外网可通，但代理/VPN 不通（请检查 Clash 节点）」，最近检测 19:12:11 |
| Windows 同时 | 系统弹出「需要执行的操作」（captive portal 提示，用户附图） |
| `%ProgramData%\youziauth-system\logs\agent.log` | 19:08:47–19:12:11 共 **122** 条 `manual retry requested`（19:09:19–19:09:36 约 5 条/秒），期间没有任何 `current auth status` / `login` |
| 同一日志 19:13:22 | `current auth status: unauthenticated` |
| 同一日志 19:13:23 | `login succeeded: success` |
| `runtime.json` 19:14:23 | `online_external` /「互联网连接正常」 |
| 门户只读实测（修复后） | `CampusAuthClient.check_status()` → AUTHENTICATED，255 ms |

结论：19:12 时机器其实未认证（71 秒后门户亲口说 unauthenticated），但上行探测报了
「外网可通」。用户点的是应用里的按钮（agent.log 记到了 122 次），Windows 那个
「需要执行的操作」是系统自己的提示，恰好证明当时确实未认证。

## 2. 根因链

1. `network_probe.check_external_internet()` 在 NCSI 两个探测都失败后，接受
   `connect.rom.miui.com` / `wifi.vivo.com.cn` 的 204。这两条是 1.8.37 为「代理节点挂掉
   时别把外网误判成不通」加的；但 captive portal 常放行中文厂商的连通性端点（否则手机
   会一直提示登录），于是未认证时它们仍返回 204 → `internet_ok=True`。
2. `campus_auth_agent.Agent._run_cycle()` 的 `if observation.internet_ok:` 分支直接发布
   `online_external` 并返回：不再问门户，也不看 `force_login`。于是「重新认证 / 立即检测」
   在这类误判下是**空操作** —— 只写一条 `manual retry requested`，然后什么也没发生。
3. 界面唯一反馈是几秒的提示条；状态文字与「最近检测」都不变，所以用户看到的就是
   「点了没反应」。
4. 1.8.38 修的是「代理卡死后界面冻结」，这一条是**另一个**分支，所以 1.8.38 没有覆盖。

## 3. 修复（v1.8.39）

| 文件 | 改动 |
|---|---|
| `campus_auth_agent.py` | 新增 `Agent.session_verifier` 与 `portal_session_status()`：门户自己的 `check_status()` 才是会话的权威。`_run_cycle` 把 online 分支交给新的 `_run_online_cycle()`：只要抓取路径（gstatic 204）不通就先问门户 —— 门户说 UNAUTHENTICATED 就照常走真实登录流程（自动纠正白名单误判），说 AUTHENTICATED 就保留「代理/VPN 不通」并把会话状态说清楚，没明确答复时保持原文案。`request_retry()` 在返回前先把「正在重新检测校园网…」写进快照，点击必有可见变化（不用等一整轮探测）。 |
| `network_probe.py` | 在 `DIRECT_204_PROBES` 旁边写清它的反面：放行的端点只能证明「有一小撮流量出得去」，不能证明会话有效，所以 agent 会再问门户。 |
| `desktop_ui/app.js` | 运行记录翻译新增日志（白名单误判触发重新认证、手动检测请求）。 |
| `tests/test_campus_auth_agent.py` | 新增 7 项 `UplinkProbeFalsePositiveTests`：门户答复优先于 uplink 探测、手动重试必须真的认证或给出明确答复、抓取路径正常时不打扰门户。更新 2 项 1.8.38 测试以覆盖新的「正在重新检测」快照。 |

## 4. 验证

- `py -3.14 -m unittest discover -s tests -q` → **Ran 1090 tests, OK (skipped=1)**
- `node --test tests/test_desktop_ui.cjs` → **130 passed**
- `python -m compileall -q .`、`git diff --check` 干净
- 本机只读实测：`portal_session_status()` = AUTHENTICATED，255 ms —— 门户查询足够轻，
  不会拖慢 60 秒轮询
- 回归测试会失败在旧实现上：`NetworkObservation(True, False, proxy_path_ok=False)` +
  门户 UNAUTHENTICATED 的手动重试，旧代码直接返回（authenticator 0 次），新代码真的登录
  并发布 `online_campus`

## 5. 已知限制

- 判据是「抓取路径不通」：代理本身正常（gstatic 通）时不会去问门户 —— 这时外网确实能通，
  不需要认证。
- 门户查询与认证请求都走本机到 `http://222.198.127.170/` 的 HTTP；门户临时不可达时返回
  UNKNOWN，不会据此认定会话失效。
- 「外网可通 + 代理不通 + 门户很慢」时一轮会多花一点时间（实测 255 ms；最坏受
  request_timeout 限制），巡检阈值 120 s 仍然安全。

## 6. 这台机器怎么恢复

装的是 1.8.38，仍然有这个缺陷。v1.8.39 发布后系统代理会自动更新；在那之前如果再看到
「外网可通，但代理/VPN 不通」同时 Windows 弹「需要执行的操作」，**不要反复点按钮**（旧
版本不会去认证）：可手动打开 <http://222.198.127.170/> 完成认证，或稍等自动检测 —— 探测
判成「外网不通」的那一轮会自己登录（今天 19:13:22 就是这样恢复的）。
