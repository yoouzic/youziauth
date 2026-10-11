# 自动更新架构审计与修复记录

日期：2026-10-11。依据当前源码、本机只读证据及离线回归。交接文档作为问题线索；其中的发布、提权和安装命令没有被当作本轮授权执行。

## 架构结论

SYSTEM 代理自行获取并验证发布包、静默执行 MSI，再通过用户身份的 Tray 任务恢复界面，是可行的 Windows 更新架构。已启用后台代理后，更新安装不需要再次让用户点击或同意 UAC。

现有实现有生命周期与权限边界缺陷，不能仅凭一次下载安装成功就声称已经稳定无人值守。计划任务配置可以跨重启保留；进程本身不能跨重启存活，必须由任务重新启动。

原实现的新安装未直接启用 SYSTEM 任务，首次开启后台自启动可能再请求一次 UAC。1.8.28 已将真正首次安装的任务创建纳入 MSI 的 SYSTEM commit 阶段；升级及修复保留用户关闭后台的选择。未配置校园账号时，agent 等待设置但继续运行更新器。

## SYSTEM 与 Clash

原推断有事实基础，但结论过强：SYSTEM 的 HKCU 不属于登录用户；不能据此断言 SYSTEM 无法使用桌面代理。当前更新器使用 Python urllib，并不使用 WinHTTP，`netsh winhttp show proxy` 为直连不能单独证明它走直连。

本机只读核查：WinHTTP 为直连；127.0.0.1:7897 有监听；当前用户路径能发现 HTTP 代理。没有以 SYSTEM 身份验证真实 GitHub 流量或下载速度。

修复后的更新线程绑定 SYSTEM 任务中的 `--allowed-user-sid`，读取该用户的 HKU Internet Settings；不再随意取另一个用户的代理。显式代理不会被 NO_PROXY 环境变量暗中绕过；显式空代理仍表示直连。

使用 Clash 的条件是代理核心在运行、HTTP/mixed 端口正确、连接不被防火墙或进程规则阻止。用户注销或 Clash 尚未启动时，代理配置存在也不代表端口可用。PAC 与纯 SOCKS 尚未支持。为这次修复修改全局 WinHTTP 或要求打开 TUN 没有必要。

## 已修复的边界

| 缺陷 | 修复 |
|---|---|
| MSI 没有恢复任务的步骤 | 提交安装后由 SYSTEM helper 使用原 SID 重建并启动已启用任务，缺一个任务时用另一个恢复身份 |
| 立即执行的 taskkill 没有真正获得 SYSTEM 权限 | 增加事务中的提权停止步骤；在安装文件前执行，旧产品移除安排在 InstallExecute 后 |
| agent 被杀后还负责重启自己与上报终态 | 存活 worker 独立保存安装结果、恢复任务，并通过只读 status 管道确认 agent 应答 |
| 静默安装返回整数，上层却要求字典 | 静默路径返回严格校验的完整结果；原手动接口保持整数兼容 |
| worker 的成功结果被观察器拒绝 | 统一返回字段及类型约束，并覆盖真实 PowerShell 输出 |
| 安装中状态未发布、重启清空终态 | 持久状态与安装结果恢复；同一轮完成后延后下一次自动检查 |
| MSI 先启动新 agent，worker 结果后到 | 保留最近的 installing 状态等待真实结果；结果超过等待窗口缺失时给出明确错误 |
| 重启提示覆盖安装失败诊断 | 保留安装错误、健康检查失败与需重启信息 |
| 手动 MSI 没有进度与结果界面 | 原生中文进度、完成、取消和错误页；/qn 保持静默，升级检测兼容旧英文语言包 |
| SYSTEM 更新缓存、日志、快照落在用户可修改目录 | 新建独立机器目录，严格验证 SYSTEM 所有者、受保护 ACL 及路径重定向；未可信则停止写入 |
| 用户配置可选择 SYSTEM 日志写入路径 | 冻结 agent 使用固定受保护日志路径，界面读取迁移后的状态和日志 |
| 下载未完成就触发 600 秒上限并被关闭 | 分段等待真实下载线程，最长一小时；超时明确报告，保留续传文件，本轮不重复启动忙碌控制器 |

受保护目录为 Windows CommonApplicationData 已知文件夹下的 `youziauth-system`，不信任 PROGRAMDATA 环境变量选择目的地。SYSTEM/Administrators 可写，任务所属用户只读。原用户配置与凭据目录仍供界面编辑；SYSTEM 更新文件不再存放在它的子目录中。不导入旧缓存到新可信目录。

Ed25519 公钥、安装包摘要、包元数据、HTTPS 目标约束及安装时文件锁保持启用。IPC 只接受固定指令，用户端不能选择 SYSTEM 要安装的文件、摘要、公钥或命令。UI 的校园网和寝室功能仍以普通用户运行；“UI 只显示状态”仅指更新功能。

## 证据与验收范围

验证包括真实默认安装接口、真实 PowerShell 脚本、临时消息管道、磁盘版本匹配、重启与晚到结果恢复、代理身份与 NO_PROXY、安装 UI 表、权限校验拒绝不可信目录，以及 MSI 打包载荷逐文件核对。测试不使用真实学校认证或打卡。

最终结果：Python 895 项（894 通过、1 跳过）；前端 118 项通过；冻结 GUI 启动检查通过；MSI 的 1762 个载荷文件与冻结目录逐文件一致。两份 EXE 中的相关模块及入口代码与当前源码一致。实际 MSI 表确认：SYSTEM 停止步骤为 deferred/no-impersonation，位于 InstallFiles 前；任务恢复为 commit/no-impersonation；RemoveExistingProducts 位于 InstallExecute 后；升级表没有语言过滤。候选 MSI 的 Authenticode 状态为 NotSigned。

以上为最初只读修复阶段的验证记录。后续用户明确授权完成推送、发布和本机连续升级验收；新实施包括首次安装配置、未保存账号时继续更新、配置输入权限，以及失败安装后恢复代理。实际发布及本机验收记录另见追加验收报告。

最后一次只读检查时，旧版 agent 的 status 管道有应答，状态为 online_external，更新状态为 1.8.27 已是最新。这不是本轮候选升级后的验收，也不能归因为本轮修复已经在宿主生效。

真实验收目标：首次安装只有 MSI 那一次 UAC；SYSTEM /qn 升级前后原 SID/任务保留、agent 管道应答、Tray 在正确用户会话运行、安装失败后可恢复，以及连续两次自动升级。首次安装分支与尚未保存账号的更新流程还由独立回归覆盖。

## 官方参考

- [Microsoft LocalSystem Account](https://learn.microsoft.com/en-us/windows/win32/services/localsystem-account)
- [Microsoft Deferred Execution Custom Actions](https://learn.microsoft.com/en-us/windows/win32/msi/deferred-execution-custom-actions)
- [Microsoft RemoveExistingProducts Action](https://learn.microsoft.com/en-us/windows/win32/msi/removeexistingproducts-action)
- [Python urllib.request](https://docs.python.org/3.14/library/urllib.request.html)
- [mihomo HTTP/SOCKS/mixed 端口](https://wiki.metacubex.one/en/config/inbound/port/)
