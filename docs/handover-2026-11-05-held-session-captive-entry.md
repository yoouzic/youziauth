# 交接：2026-11-05 门户留下失效会话记录时，客户端只会让用户「去自助服务厅释放」

## 0. 一句话现状

22:15–22:22 校园网未认证；ePortal 仍保留一条不再授权本机的会话记录，根路径因此只肯
跳 `success.jsp`。程序据此报「门户保留了会话记录……请在门户自助服务厅释放」，但用户点
Windows 的「打开浏览器并连接」，在门户登录页重新认证一次就能正常上网。根因是客户端拿
不到登录 `queryString` 就收工；浏览器走的是网络的 captive-portal 跳转，登录页
`index.jsp?wlanuserip=…&mac=…` 的查询串**就是**登录要用的 `queryString`。源码已修，
**v1.8.40 已于 2026-11-05 22:40 发布并自动更新到本机**（见 §7）。

## 1. 现场证据（本机，2026-11-05 22:15–22:22）

| 证据 | 数值 |
|---|---|
| `agent.log` 22:15–22:21 | 反复 `portal success page contradicted by getOnlineUserInfo; treating session as stale` → `current auth status: unauthenticated` → 强制登录 → `portal kept a session record that no longer authorises this machine; only the portal self-service can release it` |
| 界面 22:20:43 | 「门户保留了会话记录，但该会话已不再授权本机；请在门户自助服务厅「查看和退出我的登录设备」释放后重试」 |
| Windows WLAN 面板 | `swu-wifi`：需要操作，没有 Internet；按钮「打开浏览器并连接」 |
| 用户操作 | 点「打开浏览器并连接」后，在门户登录页（见附图）正常认证，网络恢复 |
| 用户给的门户入口 URL | `http://222.198.127.170/eportal/index.jsp?wlanuserip=10.65.104.219&wlanacname=NAS&ssid=Ruijie&nasip=172.28.255.4&mac=9c67d6cf0d5f&t=wireless-v2-plain&url=http://www.msftconnecttest.com/redirect` |
| 只读实测（22:2x，已认证状态） | 门户根 `302 → redirectortosuccess.jsp → success.jsp?userIndex=…`；上述入口 URL 返回 48 KB 登录页，页面 JS 用 `getQueryString()` 作为 `pageInfo` / 登录参数 —— 入口 URL 的查询串就是 `queryString` |

## 2. 根因链

1. `CampusAuthClient.get_query_string()` 只从门户根取登录页。会话记录存在时根跳
   `success.jsp`，其 `userIndex` 不是登录 `queryString`；客户端拒绝复用它是正确的，
   但随后只会抛 `PortalSessionHeld`。
2. `held_session_attempt()` 向门户核对那条会话 → `getOnlineUserInfo` 返回 fail →
   报「会话须释放」。可门户自己的登录页其实仍接受一次新登录（浏览器已经证明）。
3. 网络拦截普通 HTTP 时会把浏览器引到 ePortal 入口 URL（Windows「打开浏览器并连接」
   走的就是这条）；该 URL 的查询串正是登录要用的 `queryString` —— 这条浏览器路径
   客户端完全没用过。

## 3. 修复（v1.8.40）

| 文件 | 改动 |
|---|---|
| `campus_auth.py` | 新增 `CAPTIVE_PORTAL_TRIGGER_URLS`（`www.msftconnecttest.com/redirect`、`/connecttest.txt`、`www.msftncsi.com/ncsi.txt`）与 `captive_portal_entry()`：请求这些 URL（不跟随重定向），从 `302 Location` / meta refresh / JS 跳转里找出**指回本校园门户**且带 `wlanuserip` 等登录参数的入口 URL；`get_query_string()` 在根路径拿不到 `queryString` 时用它，再取入口页最终 URL 的查询串（或页面里的 `queryString`）。只接受 host 与配置 `portal_url` 相同的入口，避免把别的服务器当门户。 |
| `campus_auth.py` | `held_session_attempt()` 的失败文案补上「可在浏览器里点『打开浏览器并连接』重新认证」，作为入口也拿不到时的可行替代。 |
| `tests/test_campus_auth.py` | 新增 `CaptivePortalEntryTests`（5 项）：302 入口、meta refresh 入口、非门户跳转被拒、无入口时仍拒绝复用 `userIndex`、以及本地 mock 门户的端到端测试（根跳 success.jsp → 入口页 → 登录 POST 携带入口 queryString → 校验成功）。 |

## 4. 验证

- `python -m unittest discover -s tests -q` → **Ran 1095 tests, OK (skipped=1)**
- `node --test tests/test_desktop_ui.cjs` → **130 passed**
- 本机只读实测（已认证）：`captive_portal_entry()` = `None`（三个触发 URL 都到达真实
  互联网，不误判）；`attempt_authentication(force_login=True)` = `already_online`
  「门户仍持有有效会话，无需重新登录（会话 IP 10.65.104.219）」—— 健康会话上不会乱发登录。
- 真实门户的「失效记录」状态当下无法复现（已认证）；用本地 mock 门户覆盖了
  「根被 success.jsp 挡住 → 入口页 → 登录成功」的完整链路。

## 5. 已知限制

- 入口 URL 依赖网络继续拦截常规 HTTP 探测。若校园网放行这些探测（白名单），仍会回到
  「会话须释放」提示（文案已给出浏览器替代）。
- 只接受与配置 `portal_url` 同 host 的入口；门户换 IP/域名的重定向会被保守拒绝。
- 入口页若要求浏览器 cookie 才能完成登录，仍可能失败（本门户实测不需要：程序长期在没有
  cookie jar 的情况下正常登录）。

## 6. 这台机器怎么恢复

装的是 1.8.39。v1.8.40 发布后系统代理会自动更新；在那之前若再出现同样提示：点
Windows 的「打开浏览器并连接」手动认证一次即可立即恢复（脚本无法在无提权、不打断现有
会话的前提下替你做这一步）。

## 7. 发布与装机验证（v1.8.40，2026-11-05 22:34–22:49）

- `v1.8.40` tag → Signed Windows release 工作流 **5m45s 全部成功**（测试、前端测试、
  VERSION/tag 一致、构建、Ed25519 签名、签名校验、发布）。
- Release：<https://github.com/yoouzic/youziauth/releases/tag/v1.8.40>；资产齐全
  （`youziauth.msi` 66,151,388 B、`.ed25519`、`SHA256SUMS.txt`、`release-notes.md`、
  `release-provenance.json`）。
- `release-provenance.json`：`git_commit = cdc00d1f…`（本修复后的 version commit），
  `git_tag = v1.8.40`。
- 本机系统代理自动更新：`checking → downloading(21%…90%) → installing → installed`
  （22:41–22:49），注册表 `DisplayVersion = 1.8.40`、`_internal/VERSION = 1.8.40`，
  `youziauth.exe` / `youziauth-agent.exe` 于 22:49:00 重启，新 agent 快照
  `update.detail = "agent restarted"`。
- **装机二进制核对**：用 PyInstaller 归档读取器解出已安装
  `youziauth-agent.exe` 里的 `campus_auth` 模块，确认包含
  `captive_portal_entry`、`CAPTIVE_PORTAL_TRIGGER_URLS` 与新增日志字符串
  （“captive-portal redirect found the ePortal login page”）—— 修复确实进了这台机器。
- 健康会话下实测：`captive_portal_entry()` = `None`（不误判），强制重试返回
  `already_online`「门户仍持有有效会话，无需重新登录」。
- 失效记录状态下的真实复测要等它下次发生；届时 `agent.log` 应出现
  `captive-portal redirect found the ePortal login page` 并直接完成登录。若仍失败，
  日志会保留“未找到入口”的证据，文案也会提示用浏览器认证。
