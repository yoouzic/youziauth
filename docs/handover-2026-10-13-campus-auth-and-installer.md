# 交接：2026-10-13 校园网认证故障 + 1.8.34/1.8.35 升级事故

暂停时间：2026-10-13 09:13 (+08)，用户要求先停，等空闲时段继续。

## 0. 一句话现状

**校园网本身此刻是通的**（门户可达、当前会话有效），但 **youziauth 两个进程都没有在跑**：
1.8.35 已装上（注册表 `DisplayVersion = 1.8.35`），可安装目录里 **缺 4 个文件**，其中
`_internal\python314.dll` 缺失导致 `youziauth.exe` / `youziauth-agent.exe` **根本无法启动**。
恢复只需一条 `/fa` 修复（见 §4），但需要管理员权限。

## 1. 用户最初的问题：凌晨到早上一直认证不上

日志：`C:\ProgramData\youziauth-system\logs\agent.log`，故障窗口
**2026-10-12 23:55:59 → 2026-10-13 08:08:06**（之后自愈）。窗口内统计：

| 日志行 | 次数 |
|---|---|
| `current auth status: unauthenticated` | 110 |
| `login succeeded: success` | 110 |
| `current auth status: authenticated` | **0** |
| `uplink is up`（进外网态心跳） | 67 |
| 真实错误 | 2 |

结论（已由两个只读子智能体交叉验证）：

1. **假成功**：`campus_auth.py` 只看 ePortal 的 `result=success` 就判成功，从不复核会话是否真的建立；
   而同一窗口门户的状态查询**一次都没报过已认证**。`runtime.json` 在 08:10:06 甚至写着
   `state=online_campus, detail=success`。
2. **临时拒绝变成永久停摆**：06:42:07 门户回 `认证设备响应超时,请稍后再试!` → 代码走 `REJECTED`
   → 设置无 TTL 的 `automatic_login_blocked` → 该分支**一行日志都不写**，于是 67 分钟（4056s ÷ 60s
   = 68 个周期）完全静默。23:59:02 的 `不允许抢占` 造成另外 17 分钟静默。两次静默都是
   **用户手动点「重新认证」**（`youziauth://retry` → `retry_now`）才结束的。
3. **112 次登录 POST、0 次告警**：`notification_required` 只在 `REJECTED` 分支为真，整夜全是"成功"。
4. 网络本身也在抖（67 次 `uplink is up`），所以不是"整夜无网"，而是**会话反复掉了又建**。
5. **1.8.33 不是元凶**：`v1.8.32..v1.8.33` 只改了 UI 文案和一个把连续重复日志折叠成
   `（×N）` 的前端展示；但正是这个折叠让「记录」页看不出 8 小时重复失败。

## 2. 已经写进源码的修复（未提交）

| 文件 | 改动 |
|---|---|
| `campus_auth.py` | 新增 `CampusAuthClient.verify_login()` / `check_online_user_info_by_index()`：登录后用响应的 `userIndex` 回查 `getOnlineUserInfo`，只有门户确认会话存在才算成功；新增 `is_transient_refusal()` 按门户措辞区分「请稍后再试」与「账号密码错误」 |
| `auth_runtime.py` | 新增 `AttemptKind.LOGIN_UNVERIFIED`（既不成功也不拒绝） |
| `campus_auth_agent.py` | `LOGIN_UNVERIFIED` 分支：60/120/300 秒退避，第 5 次升级为一次 `auth_failed` 告警；登录阻塞加 300 秒 TTL 自动重试；`_log_stalled()` 让「被阻塞」和「门户不可达」两个原本静默的状态开口；新增 `--ensure-agent` |
| `startup_tasks.py` | `_run_task_command` 对 `schtasks` 瞬时拒绝重试 3 次；新增 `ensure_agent_running()` |
| `packaging/youziauth.wxs` | `RepairInstalledTasks` 由 `Return="check"` 改为 `ignore`；新增 `EnsureAgentRunning`（`--ensure-agent`，`Return="ignore"`） |
| `packaging/generate_wix_files.py` | **组件 GUID 改按 Windows Installer 实际记录的文件名推导（扩展名小写）** —— 见 §3 |
| 测试 | 新增 `tests/test_wix_component_identity.py`（8 项）等，共 22 个新测试 |

测试状态：`pytest tests/ -q` 排除 3 个环境受限文件后 **922 passed, 2 skipped**；
剩余 24 个失败全在 `test_system_storage / test_windows_update / test_installer_ui`，
在**未修改的 HEAD worktree 上同样 24 failed / 62 passed**（本沙箱无法创建子进程句柄所致），与本改动无关。

## 3. 升级事故的真正根因（这是当前阻塞项）

**同一个文件在 1.8.33 与 1.8.34 里被当成了两个不同的 MSI 组件。**

- PyInstaller 的 CArchive 在 1.8.34 里把运行库拼成 `_internal/python314.DLL`（大写扩展名），
  1.8.33 是 `.dll`；Windows Installer 实际把它落盘成 `python314.dll`（WiX 会把扩展名小写）。
- 旧代码 `generate_wix_files.py` 直接用**原始路径**算组件 GUID，于是
  `python314.DLL → {4CA84C86-…}`（新）而 `python314.dll → {4437BEA9-…}`（旧）。
- 升级时序（来自 `%TEMP%\youziauth-1.8.34-install-2.log`）：
  1. `InstallFiles` 看到已存在同版本文件 → `Won't Overwrite; Existing file is of an equal version`
     → **新组件没有写这个文件**；
  2. `RemoveExistingProducts`（`MajorUpgrade Schedule="afterInstallExecute"`）执行旧产品的
     `FileRemove(FileName=python314.dll, ComponentId={4437BEA9-…})` → **把文件删了**；
  3. 106 ms 后 `RepairInstalledTasks` 启动 agent → bootloader
     `LoadLibraryExW(...python314.DLL...)` → 126 → 弹框「Failed to load Python DLL」；该动作当时是
     `Return="check"`，于是 **整个安装以 1603 回滚**，而回滚前 `taskkill` 已经把两个进程杀掉了。
- 修复：GUID 依据改为「MSI 实际记录名（扩展名小写）」，`python314.DLL` 因此回到
  **`{4437BEA9-58C8-5190-8193-9AD5F8E0F56F}`**，与旧版一致。实测 1759 个文件里只有 4 个 GUID
  发生变化：`python314.DLL` 和 3 个 `LICENSE.APACHE/.BSD/.PSF`（它们本来就带大写扩展名，
  属于同一规则下的正确归类）。1097 个「基名含大写」的文件（时区数据、.NET 程序集）**保持原 GUID 不变**。

派生结论：**已装上的 1.8.34 本身是坏的**——它用 `{4CA84C86-…}` 认领了 python314.dll，
所以任何后续升级都会重演「跳过复制 + 旧版删除」。因此改成 1.8.35 走一次正常 major upgrade，
让修好的身份真正拿到这个文件。

## 4. 下一步（恢复机器，需管理员）

`1.8.35` 已构建、已签名（`release/youziauth.msi.ed25519` 校验通过）、已安装
（注册表 1.8.35），只差把 4 个文件补回来。**一条命令**（管理员 PowerShell）：

```powershell
msiexec /fa "D:\Users\yoouzico\Desktop\worksoace_codex\vibe_coding\xiaoyuanwang\dist\youziauth.msi" /norestart /qn
schtasks /Run /TN "\youziauth\SystemAgent"
```

现有辅助脚本（两种都能用）：
- `fix_youziauth_1.8.34.ps1`（仓库根目录，右键「使用 PowerShell 运行」）——会修复、启动代理、打印结果；
  它按需从 `dist\youziauth\_internal\` 补 `python314.dll`。

**恢复成功的判据**（缺一不可）：
1. `(Get-ChildItem 'C:\Program Files (x86)\youziauth' -Recurse -File -Force).Count` = **1759**
2. `Test-Path 'C:\Program Files (x86)\youziauth\_internal\python314.dll'` = True
3. `Get-Process youziauth-agent` 有新进程；`runtime.json` 的 `updated_at` 在 2 分钟内
4. `agent.log` 出现 `uplink is up; background check running (every 60s)`
5. 重启 `youziauth.exe`（托盘）：它同样依赖那个 DLL，装了 1.8.35 之后应能自行启动

**注意**：当前会话是已认证的，所以断网风险暂时不高；但**代理不在跑**期间若会话掉线，
机器会真的失去校园网，直到代理恢复。另外 `\youziauth\SystemAgent` 计划任务仍在，
重启不会自动修好缺失的 DLL。

## 5. 恢复之后要做的验证（防复发）

1. **离线验证升级不再丢文件**（关键）：确认 1.8.35 稳定后，再装一次 1.8.35（同版本重新安装），
   检查 `python314.dll` 仍在、文件数仍是 1759。身份修好的话这一步应当干净通过。
2. `git status` 里 `release/`、`fix_youziauth_1.8.34.ps1`、`docs/release-notes/v1.8.3{4,5}.md`、
   `packaging/*`、`tests/test_wix_component_identity.py` 都还没提交——需要决定是否提交。
3. 若要把 1.8.35 发给其他机器，需要 GitHub Release（`api.github.com/repos/yoouzic/youziauth`），
   本机无法代替发布；本机已经装上了，不依赖发布。
4. `.scratch/` 下的三个诊断脚本按需保留：
   `prove_guid_rule.py`（证明 GUID 规则）、`compare_component_guids.py`、
   `verify_frozen_fix.py`（校验冻结包真的带上了修复）。

## 6. 本次事故的时间线（便于复盘）

| 时间 | 事件 |
|---|---|
| 10-12 23:55 → 10-13 08:08 | 认证失败窗口：110 次假成功 + 2 次静默停摆 |
| 08:08 | 网络自愈，agent 恢复 ONLINE_EXTERNAL |
| 08:14 起 | 本会话取证：定位假成功、静默分支、1.8.33 无关 |
| 08:31 | 构建 1.8.34（含认证修复） |
| 08:33 | 第一次安装失败：`python314.DLL` 已被删除 → error -1 → 1603 回滚，两进程被杀 |
| 08:34–08:42 | 恢复服务（手动起计划任务），确认唯一缺失文件是 `python314.DLL` |
| 08:46–08:48 | 重建 1.8.34（`Return="ignore"` + `--ensure-agent` + 重试）并安装成功；仍缺该 DLL |
| 08:59 | `/fa` 修复成功恢复 DLL，agent 以 1.8.34 正常运行 |
| 09:05 | 子智能体查明根因：大小写导致的组件身份分裂 |
| 09:07–09:12 | 修正 GUID 推导（仅扩展名小写）、新增测试、升版 1.8.35、构建+签名+安装 |
| 09:12 | 1.8.35 安装成功但**缺 4 个文件**（同一机制），agent 无法启动 → 暂停 |
