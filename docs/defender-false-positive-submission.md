# Microsoft Defender 误报提交说明

使用微软官方的 [文件分析提交入口](https://www.microsoft.com/en-us/wdsi/filesubmission)，选择 **Software developer**。对同一个正式 Release 的以下文件分别提交，不要把它们混成一个压缩包：

1. `youziauth-agent.exe`
2. `youziauth.exe`
3. `youziauth.msi`

提交前先确认文件来自已经通过签名审计的 Release。建议说明：

```text
Repository: https://github.com/yoouzic/youziauth
License: GPL-3.0-only
Detection: Trojan:Win32/Wacatac.C!ml
Product purpose: Windows campus-network authentication helper
Build source: exact Git tag and commit from release-provenance.json
File identity: SHA-256 from SHA256SUMS.txt or Get-FileHash
Expected behavior: SYSTEM boot task, local named pipe, campus portal HTTP requests, DPAPI machine-scope credential read
```

选择“incorrectly detected”或页面上等价的误报选项，并保存每个文件的 submission ID、提交时间和最终 verdict。只有微软实际返回结果后，才能在 `docs/release_audit.md` 中记录“已清除误报”或类似结论。

不得上传以下本地数据：

- `config.ini`
- `credential.dat`
- 校园网认证日志
- 含账号、MAC、IP 或其他个人数据的截图
- 完整校园门户 query string

不要为了提交或测试而关闭 Defender，也不要添加整个安装目录的排除项。若需恢复当前机器，只允许路径和哈希均已核对的那一条检测。
