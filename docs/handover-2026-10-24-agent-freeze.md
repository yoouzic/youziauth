# 交接：2026-10-24 认证代理冻死 + 界面「连接正常」假象

## 0. 一句话现状

系统认证代理（`youziauth-agent.exe`）在 **2026-10-24 14:08** 之后再没更新过
`runtime.json`、也没写过一行日志；界面照着最后一份快照一直显示「连接正常」，而那台机器
的校园网其实需要网页认证。用户点「立即检测 / 重新认证」时，界面桥接线程**永久**卡住，
屏幕上没有任何新日志。三个进程间的链路各有缺陷；源码已修复，目标版本 **v1.8.38**。

## 1. 现场证据（用户机器，故障当天 21:30 左右采集）

| 证据 | 数值 |
|---|---|
| `%ProgramData%\youziauth-system\runtime.json` mtime | `2026-10-24 14:08:04`，状态 `online_external`（最后一条写着"互联网连接正常"） |
| `%ProgramData%\youziauth-system\logs\agent.log` 最后一行 | `2026-10-24 13:39:55 uplink is up; background check running (every 60s)` |
| 机器是否休眠 | 否。当天只有 10:24 一次开机，无 Kernel-Power 睡眠/唤醒事件 |
| `youziauth-agent.exe` 进程 | PID 3728，自 10:24 起一直活着；3 个线程全部在 Wait，CPU 0 |
| 命名管道 `\\.\pipe\youziauth-agent` | 存在，但 `CreateFile` 持续返回 `ERROR_PIPE_BUSY`（唯一实例被一个已连接、永不回复的客户端占着） |
| 该进程的网络端点 | `netstat` 里没有任何 TCP/UDP 端点：不是卡在 HTTP socket 上 |
| GUI `youziauth.exe` | PID 13504，自 10:24 起活着，窗口消息循环正常（`Responding=True`），但 JS 桥接线程卡死 |
| 用户手动网页认证后 | 网络恢复，代理与界面仍然卡着（冻结的线程不会自愈） |

## 2. 复现路径（用户总结）

校园网已认证 → 切到手机热点（外网正常）→ 再切回校园网（ePortal 需要网页认证）。
在这个窗口里点一次「重新认证 / 立即检测」就会踩中；之后界面一直停在点击前的状态。

## 3. 根因链（三处缺陷叠在一起）

1. **手动指令在唯一的 pipe handler 里做网络 I/O。** `handle_command("retry")` 直接调用
   `retry_now()` → `run_cycle(force_login=True)`。`run_cycle` 第一件事就是网络探测；
   探测用 `urllib`，而 `urllib` 的 `timeout=` 只管 socket 读写，**管不了**
   `socket.getaddrinfo`/DNS 解析（Windows 没有给解析超时参数）。Wi-Fi 刚切换时解析
   可以永远不返回：handler 于是永不返回，pipe 实例一直 busy。
2. **界面读 pipe 没有超时上界。** `agent_ipc.send_command()` 的 `timeout_ms` 只用于
   连接重试；连接成功后是阻塞 `ReadFile`（`PIPE_WAIT`），没有截止时间。代理不回复，
   pywebview 的 dispatch 线程就永久阻塞，`_mutation` 锁不放，前端 `act()` 的 pending
   一直为真 —— 所有按钮禁用，也就是用户说的「卡死」。
3. **界面无条件相信快照。** `DesktopBridge._tick()` 把 `runtime.json` 里的状态直接映射
   成界面状态，不看 `updated_at` 有多旧。冻在 `online_external` 的快照于是一直显示
   「连接正常」，而系统右下角 Windows 自己显示「需要执行的操作」。
4. 上述三件事同时发生，造成「没有任何日志」：探测卡住发生在任何日志之前，冻结后主循环
   也不再写快照，界面也没有把失败写出来。

## 4. 修复（v1.8.38）

| 文件 | 改动 |
|---|---|
| `agent_ipc.py` | `send_command()` 的 `timeout_ms` 改为**连接+写入+读取**的总预算：用 `PeekNamedPipe` 轮询，到点抛 `TimeoutError`（`timed out waiting for a reply...`）；对端关管抛有界 `OSError`。正常回复最多多等 15ms。 |
| `campus_auth_agent.py` | `retry` / `reload-config` 指令**立即返回**：清阻塞状态、置「下一轮强制登录」标记、`set()` 唤醒事件，网络 I/O 只允许发生在主循环线程；主循环等待 stop/唤醒事件，醒来立刻跑一轮。探测加**墙钟死线**（默认 `max(30, 5×request_timeout)`，默认 40s）：超时发布 `waiting_for_network` + 「网络检测长时间没有返回（已等待约 Ns），仍在重试」并按正常节奏重试。到顶（8 个挂住线程）后每 15 分钟仍放行一次新探测（DNS 恢复后能自愈），硬上限 32 个。新增巡检线程（15s 一次）：一轮超过 120s 未返回就发布一条**新鲜**的「后台检测已超过约 N 分钟没有返回；仍在重试」并写 ERROR（300s 节流），界面不可能再把过期状态当当前状态。快照发布加 `RLock`。 |
| `desktop_bridge.py` | `_tick()` 增加新鲜度判定：`updated_at` 缺失/无时区/超过 `max(180, 3×检测间隔)` 秒未更新 → 报 `error` + 「后台检测已停止响应（最近一次检测于 HH:MM）」，且不再用旧快照触发通知；新鲜快照把真实检测时间显示在「最近检测」。`network_check/network_start/update_check` 的 pipe 超时翻译成「后台认证服务没有响应；请稍后重试，或重启电脑后再试。」，不再退化成笼统文案。 |
| `tests/` | 新增 43 项：`test_agent_ipc`（挂死 handler 0.5s 超时、不重放、关管有界错误）、`test_campus_auth_agent`（指令不碰网络、主循环补跑强制轮、探测死线、到顶/冷却/硬上限、巡检发布与节流）、`test_desktop_bridge`（冻结快照报 error、时间戳不可信、超时文案、通知不触发）。 |

验证：`python -m unittest discover -s tests -q` → **Ran 1083 tests, OK (skipped=1)**；
`node --test tests/test_desktop_ui.cjs` → 130 passed；`compileall`、`git diff --check` 干净。
用本机冻结快照做实测：`agent_snapshot_is_stale(...)` 为 True，文案为
「后台检测已停止响应（最近一次检测于 14:08）」。

## 5. 当前这台机器怎么恢复

代理是 **SYSTEM** 进程，交互用户对 `\youziauth\SystemAgent` 任务连 `/Query`、`/Run`
都会被拒绝（实测 `Access is denied`），所以界面无法在无提权的情况下重启它。

1. **重启电脑**：开机任务会重新拉起代理，界面即可恢复（但装的仍是 1.8.37，旧缺陷还在）。
2. 要真正修复请安装 **v1.8.38**（本次修复随 `VERSION`、`docs/release-notes/v1.8.38.md`
   一起提交，由 `v1.8.38` tag 触发签名发布流程）。自动更新推到这台机器之前，也可以
   本地 `build_msi.ps1` 出包后手动 `/fa` 安装（本地包未签名，自动更新不会认）。
3. 新版本上线前，如果再遇到界面显示「连接正常」但网页要认证：直接重启电脑恢复，
   不要反复点按钮（旧版本会继续卡）。

## 6. 已知限制

- 卡在 `getaddrinfo` 里的线程**杀不掉**：只能按上限放弃（最多 8 个常驻，冷却期后缓慢
  增加，硬上限 32），进程重启才彻底清空。修复保证的是「不再静默、不再卡界面、网络恢复
  后能自愈」，不是「永远不产生卡住的线程」。
- 巡检发布的是 `waiting_for_network` 快照（沿用既有状态机：这类状态不带 incident_id），
  如果卡住前恰好是 `auth_failed`，复位时可能重新生成一次告警事件；与「门户短暂不可达」
  的既有行为一致。
- 触发现场（Wi-Fi 切换窗口里的 DNS 挂死）不可控，无法在真机上稳定复现；本轮用阻塞
  fake probe + 冻结快照做了等价验证。
