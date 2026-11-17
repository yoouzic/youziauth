# 交接：2026-11-18 采集代理残留导致浏览器断网

## 现场

用户报告两件事：

1. 校园网自动认证的后台（SYSTEM 代理 `youziauth-agent.exe`）卡死/消失；
2. 乐跑登录/凭证采集期间，浏览器被系统代理带到 `127.0.0.1:<端口>`；如果采集失败、
   程序卡死或被结束，代理没有被释放，浏览器就整台机器断网。
   （用户记忆里的「3081」不是固定端口 —— 采集代理每次由系统分配随机回环端口，
   任何 `127.0.0.1:<port>` 都可能出现。）

本机取证（2026-11-18 00:57 左右）：

| 事实 | 证据 |
| --- | --- |
| `youziauth-agent.exe` 进程不存在，`\\.\pipe\youziauth-agent` 不存在 | 进程/管道枚举；`\youziauth\SystemAgent` 任务存在但用户无权限查询/运行（Access denied） |
| 代理在 00:47:26 之后停止更新 | `C:\ProgramData\youziauth-system\runtime.json` 与 `logs\agent.log` |
| 界面在 00:48:10 被判「Application Hang」后关闭、00:48:19 重新启动 | Windows 事件日志 Application Hang 1002 |
| 采集日志 00:46:48/49 两次「采集结束 idle」 | `%LOCALAPPDATA%\youziauth\lptiyu\capture\capture.log` |

说明：这次现场在退出前走到了 `stop()`（代理已还原），但真正的风险是「进程被强杀时
根本走不到 stop/atexit」；另外 SYSTEM 代理消失需要管理员权限重启。

## 处理

1. **重启后台认证**：以管理员权限运行 `youziauth.exe --configure-system-startup=enable`
   （与界面「修复系统代理」同一入口）。任务重新注册、SYSTEM 代理被拉起，随后 IPers
   管道 `youziauth-agent` 恢复、`runtime.json` 继续刷新。
2. **修复代理残留**（v1.9.24）：

| 层 | 位置 | 作用 |
| --- | --- | --- |
| 独立看门狗 | 新增 `proxy_guard.py`；`CaptureSession._start_proxy_guard` | 采集开始时以自身 exe 再起一个 `--proxy-guard` 进程；父进程被强杀后按备份还原 HKCU 代理 |
| 备份必须落盘 | `SystemProxy.point_at` | 代理备份写不进去就拒绝开始采集，不再「先改代理、再留一个无法还原的现场」 |
| 启动清场 | `desktop_bridge.recover_leftover_capture_proxy`、`LptiyuController.__init__` | 界面/控制器启动时收拾上次崩溃留下的备份（活进程的备份不动） |
| 退出兜底 | `CaptureSession.start/stop` 的 atexit | 正常退出/未处理异常也还原代理 |
| 收尾不卡死 | `CaptureSession._stop_server` | `shutdown()` 最多等 5 秒，避免停采集把界面挂住 |
| 异常收尾 | `LptiyuController._capture_flow` 的 finally | 采集流程中途异常也立刻停采集并还原代理 |
| 手动修复 | `youziauth.exe --recover-proxy` | 终端一次性还原遗留代理 |

`--proxy-guard` 在 `campus_auth_gui.main`（冻结包入口）与 `campus_auth_desktop.main`
（源码运行）都最先分派；凭证提取器 `tools/lptiyu_credential_export.py` 也支持该模式，
打包 spec 已显式声明 `proxy_guard`。

## 验证

- 单测：`tests/test_proxy_guard.py`、`tests/test_proxy_entrypoints.py`、采集/面板/桥接
  相关用例全过；全量 `pytest tests` 除一个与本改动无关的既有 UAC 顺序依赖用例外通过
  （该用例单独跑稳定通过）。
- 真机 E2E（冻结包）：把代理指到死端口并写备份 → 启动 `dist\youziauth\youziauth.exe
  --proxy-guard <备份> <父 pid>` → 父进程存活时代理不动、备份保留；父进程被
  `TerminateProcess` 后约 1–2 秒代理还原、备份删除、看门狗自行退出。
- 真机 `--recover-proxy`：遗留备份存在时代理恢复、备份删除；不存在时退出码 1。
- 已在本机构建并安装 v1.9.24（静默 MSI，系统代理保持直连、`youziauth-agent` 管道与
  `runtime.json` 均正常）。

## 运维备注

- 用户仍可能看到 `127.0.0.1:<任意端口>` 的临时代理：那是采集本身，属预期；采集结束
  或程序退出后一定会被还原，即使进程被强杀也由看门狗还原。
- 若怀疑还有残留：`%LOCALAPPDATA%\youziauth\lptiyu\capture\proxy_backup.json` 存在即
  表示「有一份未收尾的代理备份」；手动修复入口
  `"C:\Program Files (x86)\youziauth\youziauth.exe" --recover-proxy`。
- v1.9.24 只在本机构建/安装，尚未发布到 GitHub；云端最新仍是 1.9.23，因此后台更新
  会显示「当前版本高于云端最新正式版，无需更新，不会降级」。发布后即恢复常规更新。