# youziauth 自动更新 — 遗留问题与缺陷交接

> 本文件由上一轮会话在上下文耗尽前整理。**只记录已在真实安装上验证过的事实**，
> 推测部分一律标注「未验证」。仓库：`yoouzic/youziauth`，工作目录
> `D:\Users\yoouzico\Desktop\worksoace_codex\vibe_coding\xiaoyuanwang`。

---

## 0. 当前机器状态（接手时的起点）

| 项 | 值 |
|---|---|
| 已安装版本 | **1.8.27**（`_internal\VERSION` 与 MSI `DisplayVersion` 一致） |
| 最新发布 | v1.8.27（GitHub，2026-10-11 02:09Z 发布，CI 全绿） |
| `\youziauth\Tray` 任务 | **存在** |
| `\youziauth\SystemAgent` 任务 | **不存在**（`schtasks /Query` 报 `Access is denied`，而列出 `\youziauth` 时只有 Tray） |
| 进程 | 只有 `youziauth.exe`，**`youziauth-agent.exe` 未运行** |
| 自动更新 | **停摆**（无 agent，无人执行更新） |
| 系统启动时间 | 2026-10-10 10:17（此后未重启） |

**已生成但尚未执行的修复文件**（需要管理员权限执行）：
- `%TEMP%\youziauth-agent-fix\system-agent.xml` — 任务定义（UTF-16，已核对内容）
- 创建命令：`schtasks /Create /TN "\youziauth\SystemAgent" /XML "<上述路径>" /F`
- 用户以普通权限执行时报 `ERROR: Access is denied.`（预期：需提权）

---

## 1. 【P0】升级安装会删除 `\youziauth\SystemAgent` 任务，且没有任何步骤重建它

### 现象（已实测）
1. 双击 `youziauth-1.8.27.msi` → 安装成功（版本号、exe 时间戳都更新了）
2. 结果：**窗口回来了（Tray 任务在），但 SystemAgent 任务整个消失**
3. `schtasks /Query /TN "\youziauth\SystemAgent"` → `ERROR: Access is denied.`
4. `schtasks /Query /FO LIST` 里 `\youziauth\` 下**只有 Tray**

### 根因（已核实）
`packaging/youziauth.wxs` 的 `InstallExecuteSequence` **只有一条 custom action**：

```xml
<CustomAction Id="StopYouziauthProcesses"
  ExeCommand="cmd.exe /c taskkill /f /t /im youziauth-agent.exe ..." />
<InstallExecuteSequence>
  <Custom Action="StopYouziauthProcesses" Before="InstallValidate" Condition="NOT REMOVE" />
</InstallExecuteSequence>
```

**MSI 里完全没有创建/重建计划任务的步骤。** 任务是「应用程序运行到自己注册」的路径负责的。

### 为什么自我恢复（1.8.27 已实现）救不了这个场景
1.8.27 新增的 `Updater._restart_agent()` → `startup_tasks.run_agent_task()` 会执行
`schtasks /Run /TN \youziauth\SystemAgent`。但：
- 它由**agent 自己**调用，而此场景下 **agent 的进程已经死了**（安装器杀的）
- 即使调用，`/Run` 一个**不存在的任务**也只会失败

### 建议修法（择一）
- **A（推荐）**：在 `youziauth.wxs` 增加 custom action，安装结束时用
  `schtasks /Create /XML ...` 重建两个任务。注意任务 XML 目前由 Python
  (`startup_tasks.build_system_task_xml`) 生成，需要决定由谁持有这份定义。
- **B**：`youziauth.exe`（窗口进程）启动时检测任务是否存在，缺失则重建。
  它运行在用户身份，**创建 SYSTEM 任务需要提权** → 仍需 UAC，不如 A。
- **C**：把任务定义交给 MSI 的 WiX 原生 `<util:ServiceConfig>`/任务扩展，
  但 WiX 原生不支持 SYSTEM 计划任务，仍要 custom action。

### 验收标准
装完一次 MSI 后，`schtasks /Query /TN "\youziauth\SystemAgent"` 成功，且 agent 进程存在、命名管道 `youziauth-agent` 有应答。

---

## 2. 【P0】MSI 安装无任何用户反馈（"没什么反应"是真实体验）

### 现象
用户双击 MSI → 界面没有任何窗口、进度或完成提示 → 用户以为没成功（实际成功了）。

### 根因
MSI 的 UI 级别是静默的（升级路径），且安装器会 `taskkill` 两个进程，
**窗口消失又回来**（本次因为任务缺失，窗口是旧的常驻进程）。

### 影响
用户无法区分「装成功了」与「什么都没发生」。这是**真实的可用性缺陷**，不是误会。

### 建议
- 安装完成时给一个明确信号（托盘气泡 / 更新页状态 / 完成对话框）
- 或在更新页把「已自动更新到 vX」的终态写清楚（`auto_update._status_from_install`
  已有此能力，需确认状态确实发布到了界面）

---

## 3. 【已修复，待验证】静默安装链曾完全没接上

### 历史事实（已从产物中核实）
从**已安装的 1.8.22 可执行文件**里读出 `windows_update` 模块的常量：

```
/qn                        : False
/l*v                       : False
YOUZIAUTH_UPDATE_SILENT    : False
YOUZIAUTH_UPDATE_LOG       : False
```

从**已安装的 1.8.25** 里读同一组常量：

```
/qn                        : True
/l*v                       : True
YOUZIAUTH_UPDATE_SILENT    : True
YOUZIAUTH_UPDATE_LOG       : True
```

**结论：1.8.22 及更早的「静默安装」根本不存在**，`/qn` 从未出现在产物里。

### 当时的报错
```
detail: TypeError @ auto_update.py:272 > auto_update.py:203
```
`auto_update.py:272` 调用 `self._install(...)`，
`auto_update.py:203` 转发 `installer(..., silent=True, log_dir=self.log_dir)`，
而当时 `windows_update.install_msi` 的签名是
`(path, executable, version, sha256, on_launch, signature=None)` —— **两个参数都不存在**。

### 现状
`windows_update.py` 已支持 `silent=False, log_dir=None`，`_start_worker` 透传
`YOUZIAUTH_UPDATE_SILENT` / `YOUZIAUTH_UPDATE_LOG`，worker 的 msiexec 命令行拼
`/qn /l*v <log>`。

### ✅ 已端到端验证成功一次
```
starting version : 1.8.25
t=   5s  up_to_date  v1.8.25 -> v1.8.25  0%  UI pids: 38588
final version    : 1.8.26
running          : 44824
```
证据（`C:\ProgramData\youziauth\updates\msi-install.log`，8.4 MB）：
```
Property(S): ProductVersion = 1.8.26
MSI (s): Windows Installer 已安装产品。产品名称: youziauth。产品版本: 1.8.26。
=== 记录停止: 2026/10/11 1:09:16 ===
```
另外 `%TEMP%` 下用 `/a`（administrative install）实测过该参数组合合法：
`exit 0`、日志 8065 行、未改动已装版本。

---

## 4. 【P0 新发现】装完更新后 agent 不会自己回来（1.8.27 尝试修复，未验证）

### 现象（1.8.26 上实测）
1.8.25 → 1.8.26 的**静默自动更新成功**，但结果：
- `youziauth.exe` 在新 PID 上（44824）✅
- **`youziauth-agent.exe` 消失**，命名管道超时 ❌
- **9 小时后（期间未重启）agent 仍未回来** ❌

### 根因
- 安装器 `taskkill` 杀掉两个进程
- `auto_update._relaunch_tray()` 只把**窗口**拉回来
- `startup_tasks.build_system_task_xml(trigger="BootTrigger")` —— agent 任务**只在开机时启动**
- 没有任何代码在安装后重启 agent

**后果：静默更新成功一次之后，下一次自动更新不会发生。**「无人值守」实际是一次性的。

> 注意：此问题与第 1 条**不是同一个**。第 1 条是任务被删；第 4 条是任务在、但没人启动它。

### 1.8.27 的实现（**未在真机验证**）
```python
startup_tasks.run_agent_task(runner=subprocess.run) -> bool   # schtasks /Run /TN \youziauth\SystemAgent
Updater(..., agent_relaunch=None)                             # 新参数
Updater._restart_agent()                                      # 安装成功后调用
campus_auth_agent.build_updater(..., agent_relaunch=run_agent_task)
```
状态里会写 `detail="agent restarted"`；重启失败**不会**把已完成的更新判为失败。

测试：`tests/test_auto_update.py` 的 `AgentRecoveryTests`、`AgentTaskRunnerTests`（5 条），
820 个测试全绿，CI 也绿。

### ⚠️ 未验证的原因
需要一次提权安装（1.8.27 已装上了，但**任务缺失**导致 agent 无从启动）。
**接手者应先执行第 0 节的重建命令，再验证 `_restart_agent` 是否真的生效。**

---

## 5. 【已修复】后台更新读不到用户代理（SYSTEM 账户）

### 根因（已用提权探针实测对照）
```
提权但仍用户身份:  whoami=yoouzic\yoouzico
                   HKCU proxy=127.0.0.1:7897   ← 读得到
                   proxy handlers={'https': 'http://127.0.0.1:7897'}
                   attempt 1: OK 200, first_chunk=262144B in 2.3s
```
agent 以 **SYSTEM** 运行，有独立注册表 hive，**读不到 `HKCU`**；
本机 `WinHTTP` = `Direct access (no proxy)`。所以 agent 走直连，国内线路极不稳。

### 修复
`app_update.py` 新增 `interactive_user_proxy()` / `configured_proxy()`：
- 枚举 `HKEY_USERS`，取交互用户 SID（`S-1-5-21-…`，排除 `-18/-19/-20`）
- 读该用户的 `Internet Settings` 的 `ProxyEnable` / `ProxyServer`
- `YOUZIAUTH_UPDATE_PROXY` 优先；**置空字符串 = 强制直连**（逃生口）
- 无代理时显式给 `ProxyHandler({})`，保证是「直连」而非「看环境变量」

**实测效果**：修复后 agent 第一次把 **65,962,304 字节完整下完**（344 秒），
`progress=100%` —— 此前从未下到任何字节。

### ⚠️ 未验证
无法在别人的机器上验证。只证明了本机 + 六种 `ProxyServer` 写法解析正确。

---

## 6. 【已修复】`ProxyServer` 带绕过列表时代理被整体放弃

### 根因
`ProxyServer` 常见写法是 **主机和绕过列表挤在同一个值里**：
```
127.0.0.1:7897;<local>;localhost;192.168.*
```
原实现 `if ";" in server: return None` 会**放弃整个代理 → 静默退回直连**。

**本机恰好是裸 `host:port`（绕过列表另存 `ProxyOverride`），所以本地怎么测都测不出。**
是「这个修复对别人机器适用吗」这个问题才暴露出来的。

### 修复（1.8.25）
只有「所有分段都是 `k=v`」才视为按协议分列；否则取第一段为主机、其余忽略。
实测 6 种写法（裸/带 scheme/按协议/带绕过/单绕过/空）全部正确。

---

## 7. 【已修复】续传的半成品被安装校验拒绝

### 现象
```
t= 344s  error  v1.8.20 -> v1.8.21  100%  更新安装包必须为 MSI 文件。
```
**66 MB 完整下载成功（100%）后在最后一步被丢弃。**

### 根因
断点续传的半成品命名为 `.partial-<版本>.msi.part`（刻意不是 `.msi`，免得被当成可安装的包），
而 `windows_update._validate_inputs` 的扩展名检查只认 `.msi`。

### 修复（1.8.22）
放行 `.msi` 与 `.msi.part`（更新包只能有这两个名字），其余（`notes.txt`、`x.msi.bak`、
`evil.exe`、`msi.part.old`）仍拒绝。内容仍由摘要与签名把关。

---

## 8. 【已修复】传输失败无重试 / 重试条件写错

- 最初：一次断开 = 整轮白跑，下一轮要等数小时 → 加 3 次尝试 + 20s 退避
- 第一版重试条件写错：只重试「压根没拿到版本信息」，而真实失败是**下载中途断开
  （progress=0）** → 改为由 `app_update` 控制器**显式标记** `transient=True`
  （`URLError`/`TimeoutError`/`ConnectionError`/`HTTPError`），不再靠猜错误文案
- 校验类失败（签名、安装器拒绝）**永不重试**

---

## 9. 【已修复】断点续传（下载稳定性优化）

- 半成品固定为 `.partial-<版本>.msi.part`，配 `.meta.json` 记录 **url + bytes + sha256**
- 只有三项全符才续传；任一不符即丢弃重下（否则会拼进别的版本的字节）
- 服务器忽略 Range 回 200 全量时**丢弃已有字节重写**，绝不追加
- **已实测 GitHub 支持**：`206` + `Content-Range: bytes 1000000-65945919/65945920`
- 收尾仍走 SHA-256 + Ed25519 校验

---

## 10. 【已修复】失败诊断只记异常类型名

最初真实安装上报 `detail: TypeError`，**没有行号、没有值**，无法定位。

现在 `auto_update` 的兜底处理器把**最后三帧**压进 `detail`：
```
TypeError @ auto_update.py:272 > auto_update.py:203
```
就是这个改动直接定位了第 3 节的接口漂移。

---

## 11. 环境级干扰（会浪费接手者大量时间）

### 11.1 幽灵目录 `D:\Users\yoouzico\Desktop\worksoace_codex\xiaoyuanwang`
- 与真工作区 `…\worksoace_codex\vibe_coding\xiaoyuanwang` **只差一层**
- **本会话中它被删除后至少复活过一次**，并且**反复劫持文件写入**：
  - `.scratch\patch_agent2.py`（写入后"消失"）
  - `apply_retry.py`
  - **`VERSION` 与 `docs/release-notes/v1.8.27.md` 写入失败**，导致 tag 一度对不上版本
- 已确认它**不含** `.git`，只有临时脚本；删除前做过 6 条安全断言
- **若另一个会话仍以它为工作目录，它会再次出现。** 根因是那个会话的工作目录，不是目录本身。

### 11.2 会话审批被禁用
本会话所有需要 UAC 的 `Start-Process -Verb RunAs` **一律自动被拒**
（`The operation was canceled by the user`）。涉及提权的步骤只能由人工执行。

### 11.3 网络不稳
- 66 MB 包约需 **5 分钟**（实测 ~200–260 KB/s）
- 反复出现 `Remote end closed connection without response`、TLS 超时
- **实测过一次下载后 SHA-256 不匹配**（发布包没问题：`SHA256SUMS.txt` 与
  GitHub API `digest` 一致）—— 属于传输损坏。**校验守卫必须保留。**

### 11.4 仅本机可用
无法在别人机器上验证代理修复（第 5/6 节）。

---

## 12. 已修复缺陷中「本机掩盖了问题」的共同教训

以下四个都是**本地单测 + CI 全绿**、只有真实安装才暴露：

| 缺陷 | 为什么本地看不出来 |
|---|---|
| 静默链未接（第 3 条） | 测试用 `installer=lambda` 直接替换，绕过了真实 `_install` 路径 |
| `.msi.part` 被拒（第 7 条） | 测试不经过续传路径 |
| 代理读不到（第 5 条） | 本地以用户身份跑，读得到 `HKCU` |
| 绕过列表解析失败（第 6 条） | 本机 `ProxyServer` 恰好是裸 `host:port` |

**建议补的测试类型**（目前缺失）：
- 用 `inspect.signature(...).bind(...)` 把**真实调用点**绑定到**真实签名**上
  （第 3 条的漂移可以一秒内抓到）
- 真实 `install_msi` 路径的集成测试（配置真实的 silent/log_dir 传递）
- 「任务存在性」的前置检查

---

## 13. 尚未验证 / 待决事项

1. **1.8.27 的 agent 自我恢复在真机上是否生效**（阻塞：任务缺失，见第 0 节）
2. **连续两次自动更新**能否连续成功（真正的"无人值守"验收标准）
3. 第 1、2 条尚未修复
4. 安装目录仍是 perMachine → **首次安装/手动重装仍需 UAC**（设计取舍，见 `docs/silent-auto-update.md`）
5. 仓库根有未跟踪的 `release/` 目录与 `demo_update.py`、`watch_selfupdate.py`（临时验证产物，可删）

---

## 14. 关键文件索引

| 文件 | 作用 |
|---|---|
| `app_update.py` | `UpdateController`（检查/下载/续传/校验）、`read_notes`、`interactive_user_proxy`、`configured_proxy` |
| `auto_update.py` | `Updater.run_cycle`、`_restart_agent`、`_relaunch_tray`、失败诊断、重试 |
| `windows_update.py` | `install_msi(silent, log_dir)`、`_validate_inputs`、worker PowerShell、Ed25519 校验 |
| `campus_auth_agent.py` | `Agent` 的更新线程、`build_updater`（装配 relaunch / agent_relaunch） |
| `startup_tasks.py` | 任务 XML 生成、`run_tray_task`、**`run_agent_task`（1.8.27 新增）** |
| `packaging/youziauth.wxs` | MSI 定义 —— **第 1 条缺陷就在这里缺一条 custom action** |
| `docs/silent-auto-update.md` | 设计与安全说明 |
| `docs/release-notes/v*.md` | 每版手写中文更新说明（发布门禁要求存在且标题含版本号） |

---

## 15. 接手建议的最短路径

1. **先恢复 agent**（第 0 节的 `schtasks /Create` + `/Run`，需管理员）
2. 验证 agent 应答：命名管道 `youziauth-agent` + `C:\ProgramData\youziauth\runtime.json` 的 `update` 块
3. 然后发一版（例如 1.8.28），观察**连续两版**能否自动更新成功
4. 修第 1 条（MSI 重建任务）与第 2 条（安装反馈）
5. 补第 12 节建议的接口绑定测试

---

## 附：本会话的验证命令备忘

```powershell
# 版本
Get-Content 'C:\Program Files (x86)\youziauth\_internal\VERSION'
Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*' |
  Where-Object DisplayName -like '*youziauth*' | Select-Object DisplayVersion

# 任务
schtasks /Query /TN '\youziauth\SystemAgent' /FO LIST
schtasks /Query /FO LIST | Select-String youziauth

# 静默安装日志（/l*v 的证据）
Get-Content 'C:\ProgramData\youziauth\updates\msi-install.log' | Select-String 'ProductVersion|记录停止'

# agent 应答
python -c "import agent_ipc; print(agent_ipc.send_command('youziauth-agent', agent_ipc.AgentCommand(command='status')))"

# 从真实 exe 里读模块常量（验证修复是否进了产物）
#   用 PyInstaller CArchiveReader + ZlibArchiveReader 取 PYZ，再遍历 code 常量的字符串
```
