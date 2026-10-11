# 后台静默自动更新

## 一句话结论

更新由**已经提权的常驻代理**（SYSTEM 计划任务 `\youziauth\SystemAgent`，跑
`youziauth-agent.exe`）自己完成：检查 → 下载 → 用内置 Ed25519 公钥验签 → `msiexec /qn`
静默安装 → 把界面拉回用户会话 → 自检新进程是否真的活着。用户不需要确认，也不会再弹 UAC。

## 为什么要改

旧流程里，每次更新都要：界面点「安装更新」→ 弹确认框 → 点「确认」→ Windows 弹 UAC →
（可选）走安装向导。根因是 MSI 的 `Scope="perMachine"`（`packaging/youziauth.wxs`），装在
`C:\Program Files (x86)\youziauth`、写 `HKLM`，所以**写安装目录必须提权** —— 这是 Windows
的设计，不是代码能绕开的。

但「perMachine」和「每次更新都要提权」并不是绑定的。Chrome 和 Edge 同样装在 Program Files，
却从不因为更新弹 UAC：它们在**首次安装那一次** UAC 里装了一个常驻的提权更新服务
（`gupdate` / `edgeupdate`），之后的每次更新都由那个服务代劳。

本项目已经有那个常驻提权进程了（agent 的 SYSTEM 计划任务），所以只需要让它顺便负责更新 ——
不必像 Chrome 那样额外引入一个服务。

## 安全边界（最重要的一节）

**提权端必须自己下载、自己验签。绝不接受未提权进程递过来的文件路径、摘要或签名。**

如果提权端接受「把包给我，我装」，那么任何以用户身份运行的进程都能让 SYSTEM 安装任意 MSI ——
这是一个**本地提权漏洞**，危害远大于「每次点一下 UAC」。

因此：

- `auto_update.Updater.run_cycle()` 永远从 GitHub Release API 开始，只安装签名与内置公钥匹配的包；
- `app_update.UpdateController` 在正式安装版里以 `manual=False` 构造，`check()` / `install()`
  直接抛错，桌面进程不会再自己发起下载，也不会出现两个互相打架的更新流程；
- 界面的 `update_install` 动作**已被删除**（`desktop_bridge._dispatch` 里没有这个分支），
  所以不存在「界面把包交给提权端」的入口；
- 「立即检查」走 agent 的 named pipe 指令通道，指令是 `check-update`，**不带任何参数**
  （`agent_ipc.ALLOWED_COMMANDS`）。提权端收到信号后仍然自己决定下载和安装什么。

## 静默安装没有人工兜底，所以补了自检

旧流程里，「人点了安装、装完看着程序起来」本身就是一道检查。静默安装去掉了它，所以补上：

1. **安装后健康检查**：worker 在 `msiexec /qn` 返回 0 之后启动新版本，观察 8 秒；只有进程还活着
   才写 `healthy = true`。这个结果一路上报成界面上的状态。
2. **持久 MSI 日志**：静默安装写 `/l*v` 到受保护的 `%ProgramData%\youziauth-system\updates\msi-install.log`。
   没有向导，失败现场就在日志中。1.8.28 的 SYSTEM 写入移至 Windows 已知目录
   `%ProgramData%\youziauth-system\updates`；旧的用户可修改目录不再存放更新缓存或日志。
3. **不会反复重装**：每次尝试前都重新读安装目录里的 `VERSION`；目标版本已经装上了就直接收工。
   于是一个「装得上但起不来」的版本只被报一次，不会变成每次检查都重装一遍的死循环。
4. **失败保留痕迹**：健康检查失败时状态写明「已安装但程序没能启动」，并带上子进程退出码，
   而不是让用户面对一个无声消失的图标。

## 状态怎么到界面

```
auto_update.Updater.run_cycle() → UpdateStatus
        │  写进 agent 的运行时快照（agent_ipc.RuntimeSnapshot.update）
        ▼
%ProgramData%\youziauth-system\runtime.json   （受保护的机器状态目录）
        ▼
desktop_bridge._tick() 读 agent_ipc.read_snapshot()
        ▼
UpdateController.adopt(block)  ← 校验字段与状态词，多一个字段都不收
        ▼
app.js renderUpdates() 只读显示
```

仍使用运行时快照通道。独立 worker 将安装结果写到受保护更新目录的 `install-result.json`，
新 agent 恢复到快照，避免安装终止旧 agent 后丢失终态。界面兼容读取旧快照供迁移显示。

1.8.28 的安装器在提交时恢复已启用的任务；存活 worker 也会恢复任务并通过只读 status
命名管道核查 agent。不能依赖旧 agent 在 MSI 返回后执行收尾，因为它已经被安装器终止。

## 界面上的变化

- 没有「安装更新」按钮，也没有安装确认框。
- 状态词表新增 `installing`（正在后台安装）与 `installed`（已自动更新）；
  `ready` 现在表示「已下载校验完成，代理准备安装」，此时检查按钮是禁用的。
- 只剩「立即检查」一个按钮：它请求代理现在去看一眼，不携带任何参数。
- 全局提示只在**成功**和**失败**时出现：成功是「已自动更新到 vX」，失败是「自动更新未完成 · 原因」。
  中间过程不再打扰用户。

## 配置与启用条件

- 只对**冻结的正式安装版**启用：`campus_auth_agent.build_updater()` 在源码运行时返回 `None`，
  开发机上跑 agent 不会误装什么东西。
- 需要 agent 在运行，也就是「校园网」页开启了后台自启动（SYSTEM 计划任务）。
  代理读不到时会明确写「后台自动更新未在运行」，而不是留一个永远装不上的「可以安装」状态。
- 1.8.28 首次 MSI 在已有 SYSTEM 安装授权中默认创建两项任务，不再要求先进入界面再次提权。
  未保存账号时仍运行更新检查，认证等待用户设置。后续升级和修复不会重新启用已关闭的后台。
- 周期是 6 小时（`Agent.update_interval_seconds`，下限 5 分钟）。安装会终止 agent 进程本身，
  所以下一次检查的时间戳在动手**之前**就排好，重启后不会立刻又跑一遍。

## 已知取舍

- 静默安装会**终止正在运行的界面和代理**（MSI 的 `StopYouziauthProcesses` 必须在
  `InstallValidate` 之前杀掉它们，否则文件被占用会要求重启）。所以更新瞬间窗口会消失再回来，
  校园网认证也会中断十几秒。没有做「用户空闲时才装」的时间窗 —— 需要的话可以加。
- 安装目录仍然是 perMachine，所以**首次安装**和「手动重装」仍需提权。要彻底去掉 UAC 需要改成
  perUser 安装（另一个方向的选择，见 `docs/release-signing.md` 的讨论）。
- 用户主动关闭后台后重新启用仍需授权；首次安装的默认配置已由 MSI 完成。
  代理核心未运行、纯 SOCKS/PAC 配置及真实连续升级验收有明确边界，详见
  `docs/auto-update-architecture-audit.md`。
- 更新下载每 60 秒检查一次线程与进度，整次下载最长等待一小时。超时明确失败并保留半成品，
  后续检查可续传；不会在旧下载还忙时发起第二个下载。

## 测试覆盖

`tests/test_auto_update.py` 覆盖：正常静默安装、已是最新不装、读不到版本不联网、目标版本已装不重装、
健康检查失败只报一次不循环、`msiexec` 各返回码映射、验签失败原样上报、意外异常不杀 agent、
重定向到用户会话成功/失败/抛异常、状态文件往返与畸形拒绝、状态里不含路径或 URL。
`tests/test_windows_update.py` 覆盖 worker 的静默开关、`/l*v` 日志位置与 `final` 记录的形状校验。
`tests/test_desktop_bridge.py` 覆盖「界面没有安装动作」「检查只是发信号」「只读控制器拒绝自查」。
`tests/test_desktop_ui.cjs` 覆盖新状态词、没有安装按钮、以及成功/失败提示。
