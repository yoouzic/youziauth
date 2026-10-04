# SignPath Foundation 申请与仓库配置（已搁置）

> **状态：已搁置。** SignPath Foundation 申请未获批准，理由是项目还缺少足够的第三方声誉信号
> （外部文章、讨论、目录收录等）。本仓库已改为自持 Ed25519 发布密钥，**自动更新不再依赖任何证书**，
> 见 [release-signing.md](release-signing.md)。
> 本文件保留作为将来重新申请时的参考：申请条件、控制台配置项与验收流程没有变化，
> 但 `.signpath/` 目录已从仓库移除，重新申请需要按下面的说明重建。
> Microsoft 官方文档也推荐该项目，条件积累后可以重新申请。

## 申请资料

在 [SignPath Foundation 申请页面](https://signpath.org/apply.html) 提交以下项目信息：

| 项目 | 值 |
|---|---|
| Project | `youziauth` |
| Repository | `https://github.com/yoouzic/youziauth` |
| License | `GPL-3.0-only` |
| Maintainer / publisher display name | `yoouzic` |
| Release artifact | `youziauth.msi` |
| Signing scope | MSI 内的 `youziauth.exe`、`youziauth-agent.exe`，以及外层 MSI |

申请前确认仓库公开、已有可下载版本、README 准确说明程序行为，并符合 [SignPath Foundation 开源项目条件](https://signpath.org/terms.html)。免费证书的 Authenticode 发布者通常显示为 SignPath Foundation，而项目身份由签名请求的仓库来源证明关联。

## SignPath 项目配置

1. 创建或获批项目 `youziauth`，仓库 URL 填写 `https://github.com/yoouzic/youziauth`。
2. 创建 artifact configuration，slug 使用 `windows-msi-deep-signing`，内容以仓库的 `.signpath/artifact-configuration.xml` 为准。
3. 创建 signing policy，slug 使用 `release-signing`，开启 trusted build system verification 与 origin verification。
4. 将预定义的 GitHub.com Trusted Build System 链接到项目。
5. 按 [SignPath GitHub 集成说明](https://docs.signpath.io/trusted-build-systems/github)安装 SignPath GitHub App，并只授权本仓库。
6. 确认仓库默认分支包含 `.signpath/policies/youziauth/release-signing.yml`；该策略仅允许 GitHub 托管 runner，且禁止重跑旧构建后签名。

## GitHub 仓库配置

在 GitHub 仓库设置中配置：

- Repository variable：`SIGNPATH_ORGANIZATION_ID`
- Repository secret：`SIGNPATH_API_TOKEN`
- Environment：`signing`，条件允许时启用人工审批

令牌只能保存在 GitHub Secret 中，不得写入源文件、文档、构建产物或日志。首次发布前先确认 `.github/workflows/release.yml` 中的 project、policy 和 artifact configuration slug 与 SignPath 控制台一致。

## 首次签名验收

合并经审查的代码后再创建与 `VERSION` 完全一致的标签。工作流必须先构建并上传 GitHub artifact，由 SignPath 返回深度签名的 MSI，再运行 `packaging/verify_release.ps1`。只有外层 MSI、两个内嵌 EXE 的签名、时间戳和版本都通过时，工作流才会创建 Release。

在 SignPath 审批、仓库变量、Secret 和 GitHub App 都配置完成之前，不要创建生产发布标签，也不要把 CI 生成的 unsigned artifact 当作正式安装包分发。

## v1.5.0 发布阻断记录（2026-09-24）

`v1.5.0` 标签的发布工作流已通过测试、构建和未签名 MSI 上传，但 `Submit SignPath request` 报 `Input required and not supplied: organization-id`。当时仓库没有 `SIGNPATH_ORGANIZATION_ID` Actions 变量，也没有 `SIGNPATH_API_TOKEN` Actions Secret；签名请求、签名验证和 Release 发布均未执行。GitHub 最新正式 Release 仍是 `v1.1.3`。

先确认 SignPath Foundation 申请及项目审批状态。获批后，在 SignPath 控制台核对组织 ID、项目 `youziauth`、签名策略 `release-signing`、工件配置 `windows-msi-deep-signing`、GitHub.com Trusted Build System 链接，以及具有提交权限的 API Token。只把组织 ID 写入 GitHub Actions 变量，把 Token 写入 GitHub Actions Secret；不要把 Token 发到聊天、提交到仓库或打印到日志。

本仓库策略使用 `disallow_reruns: true`。SignPath 官方说明该选项禁止对旧构建重跑后签名，因此不要把 `gh run rerun 35894237390` 当成可靠的恢复办法。配置齐全、修复提交合入默认分支后，递增 `VERSION` 并创建对应的新标签，触发一次新的发布工作流。`v1.5.0` 标签保留原样，不移动或覆盖。新工作流会在构建前检查两个配置项，并在签名验证通过后才发布 Release。

桌面自动更新还要求当前运行的 `youziauth.exe` 本身有有效签名，且与新 MSI 的签名发布者一致。因此首个签名版发布后，使用旧版未签名安装包的电脑需要从正式 Release 手动安装一次；此后的签名版之间才能走应用内自动更新。不要为迁移旧版而绕过发布者签名检查。
