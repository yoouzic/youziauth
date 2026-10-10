# 发布签名与自动更新密钥管理

本文件说明 youziauth 现在如何签发和校验更新，取代原先依赖 Authenticode 证书的方案。历史设计见 [trusted-release-design.md](trusted-release-design.md)，当前阻断项状态见 [signed-release-blockers.md](signed-release-blockers.md)。

## 一句话结论

自动更新**不需要代码签名证书**。客户端内置一把 Ed25519 公钥，发布时用配套私钥对安装包签名即可。证书（Authenticode）解决的是另一个问题：首次安装时的 UAC 发布者名称、SmartScreen 警告和 Smart App Control 拦截。两者互不替代，也互不阻塞。

## 信任锚

| 项 | 值 |
|---|---|
| 算法 | Ed25519（RFC 8032） |
| 公钥（hex） | `fecc4e3334a13b6f09c7887fab09dadcaacdf644eacb7c7a0ed873b323dfd4ee` |
| 公钥（base64） | `/sxOMzShO28Jx4h/qwna3KrN9kTqy3x6DthzsyPf1O4=` |
| 内置于 | `windows_update.PUBLIC_KEY_B64` |
| 私钥环境变量 | `YOUZIAUTH_RELEASE_KEY`（base64 的 32 字节种子） |
| 私钥主位置 | `%USERPROFILE%\.youziauth-release-key\ed25519-release.key` |
| 私钥备份 | `%LOCALAPPDATA%\youziauth-release-key-backup\ed25519-release.key` |
| 私钥指纹 | 密钥文件 SHA-256 `E8A62C0FD7531398B1686FAF0FC431B9DBF4782642C35E1E5CB4CCF90D9A4111` |

公钥编译进程序，私钥离线保存。**任何人拿到私钥都能冒充发布者，拿到公钥不能。** 私钥位于仓库之外，ACL 已设为仅当前用户可读写；不得进入仓库、构建产物或日志。

> 公钥一旦随版本发布就无法更改信任关系。**轮换公钥必须发布一个新版本**，旧版本只会信任旧公钥。

> 上面两个位置都在本机。真正的容灾备份需要一份**离开这台电脑**（离线介质或密码管理器），
> 否则磁盘损坏或系统重装就等于私钥丢失。

## 签名的内容

签名不是对文件本身，而是对一段规范化载荷（canonical payload）：

```json
{"v":1,"version":"1.6.6","sha256":"<64 hex>","bytes":71436084,"product":"youziauth","manufacturer":"yoouzic","upgrade":"{D029E636-7E7E-42EE-8B38-C2D455AD2AA1}"}
```

载荷由 `windows_update.canonical_payload()` 生成，签名工具和客户端调用的是同一个函数，所以「签的字节」和「验的字节」不可能漂移。

把版本、字节数和产品标识一起签进去，作用有三个：

- **不能跨版本重放**：1.6.6 的签名对 1.6.7 无效。
- **不能降级**：签名与版本绑定，云端无法把一个旧版本冒充成新版本；客户端另有 `latest <= current` 判断。
- **不能移植到别的产品**：`product`、`manufacturer`、`upgrade` 都参与了签名。

## 发布资产

一次正式 Release 必须同时包含四个资产。缺任何一个，客户端都会拒绝更新而不是降级放行（`app_update.RELEASE_ASSETS`）：

| 资产 | 作用 |
|---|---|
| `youziauth.msi` | 安装包 |
| `SHA256SUMS.txt` | `<sha256>  youziauth.msi`，供人工核对 |
| `youziauth.msi.ed25519` | 128 个十六进制字符的 Ed25519 签名 |
| `release-provenance.json` | 版本、提交、摘要与所用公钥 |

## 校验链路

客户端有两条对称的校验路径，都会完整重跑一遍：

```
用户点「检查更新」
  app_update._check()          读 Release、校验资产、下载 MSI
  windows_update.verify_msi()  预览校验：哈希 → 签名 → MSI 产品属性
用户点「安装」
  windows_update.install_msi()
    ├─ 本进程：哈希 → 签名 → 启动校验进程
    └─ PowerShell worker（脱离桌面进程树）
         ├─ 独占读锁打开 MSI，自己重算 SHA-256
         ├─ 调 MSI 产品属性读取器（只读 COM）
         ├─ 调 youziauth.exe --verify-update（持锁期间）
         │    └─ 重新哈希、构造载荷、用内置公钥验签
         └─ 全部通过后才启动 msiexec /i
```

关键设计：

- **持锁校验**。worker 在独占读锁存在期间才做验证；验证通过到安装之间的文件不可被替换。测试覆盖了「已持有写句柄时 worker 拒绝继续」。
- **验签在冻结程序内完成**。Windows PowerShell 5.1 跑在 .NET Framework 4.8 上，**没有 Ed25519 实现**，所以验签必须由 `youziauth.exe --verify-update` 完成，而不是 PowerShell。
- **冻结程序是窗口化构建（`console=False`），没有 stdout**，因此校验进程通过「退出码 + 报告文件」回报，不写控制台。
- **无 Authenticode 回退**。客户端只认这把公钥。这样即使某张证书被误签发，也无法冒充发布者；代价是首版签名版仍需手动安装一次（见下）。

## 更新说明从哪里来

发布工作流用 `gh release create --generate-notes`。GitHub 给常规提交只生成一行 compare 链接 ——
实测 `v1.8.5` 的 Release 正文只有 80 字节 —— 而且提交标题是英文。所以「这次更新改了什么」
按下面的顺序取值：

1. **手写中文说明**（首选）：发布时把 `docs/release-notes/v<版本>.md` 作为 `release-notes.md`
   资产上传。写法见 [release-notes/README.md](release-notes/README.md)。客户端会复核标题里的
   版本号，对不上就退回第 2 步 —— 宁可显示英文，也不能把上一个版本的说明讲给用户听。
2. **提交对比**：`GET /repos/yoouzic/youziauth/compare/v<已装版本>...v<最新版本>`。范围是用户
   真正跨过的那一段，不是「本次发布改了什么」；停在 1.8.4 的人看到的正是 1.8.4 → 最新版之间的
   全部变化。只列 `feat`、`fix`、`perf`、`refactor`、`revert` —— `chore: version x.y.z`、`test`、
   `ci`、`docs` 与合并提交不显示。所以**提交标题也是要给人看的**。
3. **实话实说**：前两步都取不到就在界面上写明「没能自动获取这次更新的说明」，并给出一个
   GitHub 发布记录入口。

`release-notes.md` 刻意**不放进** `RELEASE_ASSETS`：那份清单里少任何一个附件都会让客户端
拒绝更新，而「这次没写说明」不该让所有人卡在旧版本上。

其余要点：

- **安装与安装后自检**见 [silent-auto-update.md](silent-auto-update.md)：正式安装版由已提权的
  常驻代理静默安装（`msiexec /qn`），没有向导也没有人工兜底，所以失败现场只留在
  `%ProgramData%\youziauth\updates\msi-install.log`，并且装完必须通过健康检查才算成功。
- **每次成功都缓存**到 `updates/changes-v<旧>-v<新>.json`，重复检查和重装同一版本不再请求
  GitHub，也避开未登录的 60 次/小时限流。
- **纯展示，失败零代价**：无网、限流、标签不存在、响应超限、版本号对不上、字段形状不对，
  都只让这一块少内容，检查、下载、验签、安装全部照常。
- 提交对比响应上限 4 MiB。实测 36 个提交（v1.5.0 → v1.8.5）约 1.1 MiB；GitHub 自己在 250 个
  提交处拒绝比较，不会给出无界响应。中文说明上限 32 KiB。

## 签名一个版本

```powershell
# 私钥在仓库之外；不要把它复制回工作目录
$key = Join-Path $env:USERPROFILE '.youziauth-release-key\ed25519-release.key'
$env:YOUZIAUTH_RELEASE_KEY = (Get-Content $key -Raw).Trim()
$version = (Get-Content VERSION -Raw).Trim()

python tools\sign_release.py `
  --msi dist\youziauth.msi `
  --version $version `
  --output-dir release
```

（正式发布不需要手工执行这段：工作流会用 Secret 自动完成。）

`--version` 必须与 MSI 的 `ProductVersion` 以及 `VERSION` 三者一致，否则校验会在
「产品属性不匹配」这一步拒绝。工作流用 `VERSION` 同时驱动构建与签名，因此不会漂移；
手工签名时请照上面的写法取值，不要手打版本号。

工具会在写文件前做三件事：自校验签名、**确认私钥与客户端内置公钥一致**、确认载荷可解析。私钥不匹配时直接失败——否则会发出一个所有已安装副本都拒绝的版本。

只验证、不签名（发布校验用，不需要私钥）：

```powershell
python tools\sign_release.py --msi dist\youziauth.msi --version $version `
  --verify-signature-file release\youziauth.msi.ed25519
```

## CI 配置

1. 生成密钥：

   ```powershell
   python -c "import base64,ed25519; s=ed25519.generate_secret_key(); print(base64.b64encode(s).decode()); print(base64.b64encode(ed25519.derive_public_key(s)).decode())"
   ```

2. 第一行输出写入 GitHub Actions **Secret** `YOUZIAUTH_RELEASE_KEY`；第二行必须与 `windows_update.PUBLIC_KEY_B64` 一致。
3. 私钥离线备份至少两份（纸质/离线介质）。**丢失私钥 = 无法再发布可自动更新的版本**，只能靠手动安装换新公钥。
4. 打与 `VERSION` 一致的标签（例如 `VERSION` 为 `1.6.6` 就打 `v1.6.6`），工作流会自动构建、签名、验证、发布。

`.github/workflows/release.yml` 的顺序被测试固定：构建 → 签名 → 验证 → 发布，任何一步失败都不会创建 Release。

## 迁移路径

| 从 | 到 | 方式 |
|---|---|---|
| 1.6.6 及更早（未签名、无内置公钥） | 首个 Ed25519 版本 | **必须手动安装一次**。旧版只认 Authenticode 且自身未签名，无法自举 |
| 首个 Ed25519 版本之后 | 后续任意版本 | 应用内自动更新，无需证书、无需管理员之外的任何操作 |

首个 Ed25519 版本必须手动安装一次，这一点无法绕过：旧版程序里没有新公钥，也不信任未签名安装包。装好之后，同一个公钥会一直沿用，后续版本都能自动更新。

## 证书（可选，不阻塞更新）

如果以后想让首次安装也干净（去掉 UAC 的「未知发布者」和 SmartScreen 拦截），可以再拿一张代码签名证书。要点：

- **证书不参与自动更新的校验**，只影响 Windows 对首次安装的态度。
- **EV 不再有 SmartScreen 特权**：Microsoft 官方文档已说明 EV 与 OV 自 2024 年起同等对待，为 SmartScreen 单独买 EV 不再划算。
- **签名未必等于立刻没有警告**：SmartScreen 需要声誉积累（数周、数百次干净安装）。
- **不能把证书换成 `.pfx` 放进 GitHub Secret**：CA/B Forum 规定 2023-06-01 起代码签名私钥必须存放在硬件模块（FIPS 140-2 L2 / CC EAL4+ 以上），因此要么用云签名服务，要么用带令牌的自托管 runner。
- **Smart App Control（Windows 11）对未签名程序默认拦截且没有单应用豁免**，这是唯一一个「必须有 CA 签发证书」才能解决的问题。

对个人开源维护者的现实选项（已核实要点，价格与资格请以官方页面为准）：

| 方案 | 成本 | 备注 |
|---|---|---|
| SignPath Foundation | 免费 | 开源项目专用；**卡在声誉门槛**（需要外部文章、讨论、收录等第三方信号），条件达标后可重新申请；发布者显示为 SignPath Foundation，隐私最好 |
| Certum Open Source | €49/年 | 仅限个人、无需公司；但 CI 需要常驻自托管 Windows 机器 |
| SSL.com IV | $129/年 + eSigner 最低 $15/月 | 唯一确认「个人可申请 + 支持中国 + 可从 GitHub 托管 runner 免令牌签名」的组合 |

## 安全边界

- 私钥不进仓库。`.gitignore` 已忽略 `.release-key/`、`*.key`；`tests/test_release_workflow.py` 会查询 `git ls-files`，一旦有密钥被跟踪就会失败。
- **不会**为了兼容旧版而放宽校验：没有有效签名就是拒绝更新。旧版未签名安装包只作为审计留存，不作为正式分发物。
- 校验失败一律 fail-closed，错误信息是固定的中文文案，不回显系统路径或异常细节。
- 客户端只从 GitHub 官方域名下载（`app_update.validate_url`），并对重定向做白名单校验。
