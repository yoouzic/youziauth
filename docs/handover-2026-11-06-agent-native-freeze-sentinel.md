# 交接：2026-11-06 agent 主线程卡在内核调用约 5 小时，v1.8.41 加自愈

## 0. 一句话现状

2026-11-06 10:28:16，SYSTEM agent（`youziauth-agent.exe`，PID 24024，11-05 22:48:59
启动）写完最后一份 `runtime.json` 后再没有任何动作：进程还活着、CPU 0、没有 TCP 端点，
唯一的 named pipe 实例一直是 BUSY；界面于是显示「后台检测已停止响应（最近一次检测于
10:28）」，点「立即检测」提示「后台认证服务没有响应」。15:33 用提权脚本先抓线程栈、
再结束进程并重新运行计划任务，新 agent（PID 28792）15:33:19 启动并立刻恢复。源码已修，
目标版本 **v1.8.41**。

## 1. 现场证据（2026-11-06）

| 证据 | 数值 |
|---|---|
| `runtime.json` mtime | `2026-11-06 10:28:16`（state `online_external`），之后 5 小时未变 |
| `agent.log` 最后一行 | `2026-11-06 10:24:14 uplink is up; background check running (every 60s)` |
| 进程 | PID 24024，创建于 11-05 22:48:59；5–6 线程全部 Wait，CPU 0，无 TCP 连接 |
| 命名管道 | `\\.\pipe\youziauth-agent` 存在；`CreateFile` 持续 `winerror 231`（ERROR_PIPE_BUSY），`WaitNamedPipe` 2s 超时（121） |
| 崩溃/电源 | Application 日志 10:00 后无 youziauth 相关错误、无 WER；last boot 11-05 16:47，24h 内无 Kernel-Power 睡眠/恢复事件 → 不是休眠或重启造成 |
| 提权 py-spy dump（重启前） | 见下 |

```
Thread 45764 (idle): "MainThread"                 # 没有任何 Python 帧：卡在 native 调用里
Thread 14868 (idle): "youziauth-agent-ipc"
    NamedPipeServer.serve_once (agent_ipc.py:294)  # ConnectNamedPipe
Thread 42444 (idle): "youziauth-agent-stop"        # Event.wait
Thread 2852 (idle): "youziauth-agent-watchdog"     # Event.wait（巡检正在 15s 等待里）
```

主线程卡在一个没有 Python 帧的 native 调用里；巡检线程并没有锁在发布逻辑上，但
`runtime.json` 五小时没更新，说明卡死点带走（或堵住）了主循环的存活。具体是哪个 native
调用没有拿到 native/内核帧，无法百分百指认；最可疑的是 **DNS 解析**（Windows 的
`getaddrinfo` 没有超时参数，1.8.40 的登录路径新增了 msftconnecttest/msftncsi 主机名，
本仓库 2026-10-24 的事故就是同一类卡死），其次是文件 I/O。

## 2. 修复（v1.8.41）

| 文件 | 改动 |
|---|---|
| `campus_auth_agent.py` | ① 认证尝试加 **90s 墙钟死线**（daemon 线程 + `join`，超时发布「认证请求长时间没有返回」并按退避重试；挂住线程限量 + 冷却，避免 DNS 卡住主循环）。② 新增**存活哨兵**：不碰日志锁/快照锁，只读主循环心跳；心跳停 300s 就把全部线程栈 dump 到 `%ProgramData%\youziauth-system\logs\agent-failsafe.log`，然后 `os._exit(75)` —— 计划任务带 RestartOnFailure（1 分钟、最多 3 次），1 分钟内自动拉起 agent，不需要重启电脑。③ 主循环单轮异常不再静默终结：写日志、发布「后台检测本轮异常」、5s 后继续。④ 快照写失败（如与界面读文件竞争）只降级为警告，不再可能带走主循环。⑤ 等待下一轮时分段更新心跳（check_interval 可达 1 小时，不能被误判为卡死）。 |
| `tests/test_campus_auth_agent.py` | 新增 4 项：卡住的认证在死线后放弃且不挂主循环；心跳停 → 哨兵 dump + 退出（注入 exit hook 验证）；心跳正常 → 哨兵不动作；单轮异常后主循环继续。 |

## 3. 验证

- `python -m unittest discover -s tests -q` → **Ran 1099 tests, OK (skipped=1)**
- `node --test tests/test_desktop_ui.cjs` → **130 passed**
- 恢复实测：15:33:19 新 agent 启动；15:34:26 快照恢复更新；pipe `status` 15ms 内回复；
  `agent.log` 心跳恢复。
- 下次卡死时应能看到 `agent-failsafe.log` 里的线程栈与退出码 75，随后计划任务自动重启
  （预期 1–2 分钟内恢复）。

## 4. 已知限制

- 哨兵依赖计划任务的 RestartOnFailure；任务被删除/禁用时退出后无人拉起（需重装或开机）。
- 若卡死是「进程被外部整体暂停」而不是线程阻塞，哨兵线程同样被暂停，无法自救；这类
  情况仍要手动重启。
- 本轮没拿到 native/内核帧；faulthandler 的 failsafe 栈仍只含 Python 线程栈，但足以在下
  次指认卡在哪个 Python 调用所进入的 native 边界。

## 5. 这台机器怎么恢复

已经恢复：提权脚本按「py-spy dump → `taskkill /F /PID 24024` →
`schtasks /Run /TN \youziauth\SystemAgent`」执行，新 agent PID 28792 健康运行。
v1.8.41 安装后，同类卡死会自愈，不再需要人工重启电脑。
