# 首次安装与连续自动升级验收

执行日期：2026-10-11（北京时间）。用户授权完成推送、发布和本机连续两次自动更新。

## 首次安装实现与发布前验证

- 全新 MSI 在自己的已提权事务中创建 SYSTEM 后台任务及原用户 Tray 任务；没有额外的 `runas` 步骤。升级及修复从已有任务恢复原用户，后台已关闭时保留关闭状态。
- 没有保存账号时，agent 提示配置账号，仍能进行后台版本检查。
- 配置及凭据仍由原用户编辑；更新包、日志和安装结果使用独立的 SYSTEM 所有目录。现存无关文件和目录外硬链接的权限不会被目录初始化递归修改。
- 发布前 Python 922 项测试：921 通过，1 跳过；前端 118 项全部通过。冻结 GUI 启动通过，MSI 的 1762 个载荷文件逐一一致，两份冻结入口及更新相关模块与源码一致。
- 原生 MSI 表确认 SYSTEM 停止动作在文件替换前，任务初始化/恢复为 commit/no-impersonation；升级移除旧包在 InstallExecute 后，且不以旧包语言过滤。

## 本机基线

本机原有安装为 1.8.27。普通用户的 status 管道可用；旧 agent 位于 Session 0，其管道 owner 是 SYSTEM，管道 server PID 与实际 agent PID 一致。普通用户无法读取该进程令牌或 SYSTEM 任务 XML，因此没有把“查询拒绝访问”记作任务缺失。

旧 Tray 任务注册为原用户 InteractiveToken，但旧界面进程实际在 Session 0。本轮连续升级需确认界面恢复到当前用户的 Session 1，并保留任务原用户身份。

## 发布与连续升级结果

验收仅发送固定的 `check-update` 指令来缩短六小时定时检查的等待时间；不向后台传入安装文件、摘要、签名或命令。下载、验签、安装及恢复由 agent 自动完成，没有手动启动 MSI。

| 升级 | 北京时间 | 安装与恢复结果 |
| --- | --- | --- |
| 存量 1.8.27 → 基线 1.8.28 | 12:54:45 触发，12:55:28 安装退出 | MSI 退出码 0；新 agent 管道正常，用户界面恢复至 Session 1 |
| 第一轮 1.8.28 → 1.8.29 | 13:03:46 触发，13:04:41 最终结果 | MSI 退出码 0；`agent_restarted`、`relaunched`、`healthy` 均为 true；无残留 Session 0 界面 |
| 第二轮 1.8.29 → 1.8.30 | 13:11:05 触发，13:12:05 最终结果 | MSI 退出码 0；`agent_restarted`、`relaunched`、`healthy` 均为 true；无残留 Session 0 界面 |

第一轮验证了安装中父 agent 被终止、MSI commit 恢复新 agent、外部 worker 写入最终结果后，新 agent 接纳晚到结果这一真实时序。状态最终为 `installed`、进度 100，显示“已自动更新到 v1.8.29”，后台继续在线。

第二轮最终状态为 `installed`、进度 100，显示“已自动更新到 v1.8.30”。实际 agent PID 从第一轮的 19600 更换为 29780，管道 server PID 与其一致，管道 owner 仍为 SYSTEM；原用户界面 PID 13696，位于 Session 1，CIM 读取到的用户 SID 与基线一致。三处受保护目录均为 SYSTEM 所有，SYSTEM/Administrators 完全控制，原用户只读，没有其他访问规则或重解析点。

两轮各通过 17 项实际验收检查。0.5 秒采样的进程观测窗口没有记录到 `consent.exe`；这与没有手动点击安装或确认 UAC 的测试过程一致。采样记录不是完整的桌面弹窗审计，因此不把它推广为所有 Windows 策略下的无弹窗保证。

基线升级时，旧 1.8.27 worker 额外直接启动了一个 Session 0 界面。第一轮由新 worker 完成后，该旧进程已退出，仅剩原用户会话内的界面；后续验收不使用这个基线例外。

## 正式发布与产物一致性

三个版本的 CI 和签名发布流程均成功。分别独立校验公开的签名、SHA256SUMS、GitHub 资产摘要、provenance 中的提交/版本/公钥和版本说明；本机实际下载的 MSI 大小及 SHA256 与发布资产一致。三个 MSI 均为 66,024,412 字节。

| 正式版本 | 源码提交 | MSI SHA256 |
| --- | --- | --- |
| [v1.8.28](https://github.com/yoouzic/youziauth/releases/tag/v1.8.28) | `3705b76` | `0a30b47462019421afb85d1ee1b1c5e0139d146680526d096b7f8e2ff15ecf04` |
| [v1.8.29](https://github.com/yoouzic/youziauth/releases/tag/v1.8.29) | `2e090dd` | `2ae0c3d6b8f7a42bad4daf1d30df9c4fc199637dbc1a7c106314f1ff87083334` |
| [v1.8.30](https://github.com/yoouzic/youziauth/releases/tag/v1.8.30) | `25a2eaa` | `f9a111c5c8a2a11a2adc9e8ee69aa3faa9f574b5c6cd4e991488e7913ba431cc` |

签名发布流程记录：[1.8.28](https://github.com/yoouzic/youziauth/actions/runs/38112744869)、[1.8.29](https://github.com/yoouzic/youziauth/actions/runs/38113184553)、[1.8.30](https://github.com/yoouzic/youziauth/actions/runs/38113674940)。

本地证据包括 `.scratch/live-upgrade-verification-1.8.29.json`、`.scratch/live-upgrade-verification-1.8.30.json`，它们保留原始生命周期/管道/发布快照与安装结果。两轮结果另分别保存为 `.scratch/install-result-after-1.8.29.json`、`.scratch/install-result-after-1.8.30.json`，避免下一轮覆盖历史证据。逐时观测为 `.scratch/auto-update-live-evidence.jsonl` 和 `.scratch/update-process-evidence.jsonl`。

## 证据范围

本机是已有安装，未卸载或清空用户配置来伪装全新安装。首次安装分支已通过源码回归、Windows 原生权限夹具及实际 MSI 表验证；尚未在干净 Windows 环境计数 UAC 弹窗。真实连续升级结果不能替代这一独立场景。

普通用户无法读取 SYSTEM 任务 XML 及 agent 进程令牌。本轮直接验证了管道对象 owner、实际 server PID/Session、健康应答和 SYSTEM 所有存储目录；没有把管道 owner 等同于进程令牌查询结果。没有重启整机来做启动恢复试验，也没有等待六小时定时器：连续两次实机升级是固定检查信号触发的自动安装验收。

发布资产使用项目现有的固定 Ed25519 公钥验签。Authenticode 状态不作为这条更新信任链的替代证据。公开资产校验及本机状态、管道、版本和安装结果记录保存在本地忽略目录 `.scratch`，不包含账号或凭据内容。
