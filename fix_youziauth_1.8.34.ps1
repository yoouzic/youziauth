# 修复 youziauth 1.8.34:恢复丢失的 python314.dll 并启动系统代理
# 用法:右键本文件 →「使用 PowerShell 运行」,或在管理员 PowerShell 里执行本文件。
# 只做三件事:用安装包修复文件、启动 SYSTEM 计划任务、报告结果。

$ErrorActionPreference = 'Continue'
$output = Join-Path $env:TEMP 'youziauth-fix-result.txt'
$msi = 'D:\Users\yoouzico\Desktop\worksoace_codex\vibe_coding\xiaoyuanwang\dist\youziauth.msi'
$install = 'C:\Program Files (x86)\youziauth'
$dll = Join-Path $install '_internal\python314.dll'
$log = Join-Path $env:TEMP 'youziauth-repair.log'

function Say($text) {
    Write-Host $text
    $text | Out-File -LiteralPath $output -Append -Encoding utf8
}

Remove-Item -LiteralPath $output -ErrorAction SilentlyContinue
Say "=== youziauth repair $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Say "ERROR: 需要管理员权限。请右键本文件选择「使用 PowerShell 运行」,或在管理员 PowerShell 中执行。"
    Read-Host "按回车退出"
    exit 1
}

Say "[1/5] 修复前 python314.dll 是否存在: $(Test-Path -LiteralPath $dll)"

if (-not (Test-Path -LiteralPath $msi)) {
    Say "ERROR: 找不到安装包 $msi"
    Read-Host "按回车退出"
    exit 1
}

Say "[2/5] 用安装包修复文件(msiexec /fa)..."
$p = Start-Process -FilePath "$env:SystemRoot\System32\msiexec.exe" `
    -ArgumentList @('/fa', "`"$msi`"", '/norestart', '/qn', '/l*v', "`"$log`"") `
    -PassThru -Wait
Say "[2/5] msiexec 退出码 = $($p.ExitCode)  (0 = 成功)"

Say "[3/5] 修复后 python314.dll 是否存在: $(Test-Path -LiteralPath $dll)"
if (-not (Test-Path -LiteralPath $dll)) {
    $source = 'D:\Users\yoouzico\Desktop\worksoace_codex\vibe_coding\xiaoyuanwang\dist\youziauth\_internal\python314.dll'
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination $dll -Force
        Say "[3/5] 已从构建产物直接复制:$(Test-Path -LiteralPath $dll)"
    } else {
        Say "[3/5] 仍然缺失,且找不到可复制的来源"
    }
}

Say "[4/5] 启动 SYSTEM 计划任务..."
$query = schtasks /Query /TN '\youziauth\SystemAgent' 2>&1 | Out-String
Say ($query.Trim())
schtasks /Run /TN '\youziauth\SystemAgent' 2>&1 | ForEach-Object { Say $_ }

Start-Sleep -Seconds 12
$agent = Get-Process youziauth-agent -ErrorAction SilentlyContinue
if ($agent) {
    Say "[5/5] 代理已运行,PID = $($agent.Id)"
} else {
    Say "[5/5] 代理仍未运行。请把本窗口内容发给助手。"
}

Say ""
Say "也可以打开这个文件查看日志:$output"
Say "安装日志:$log"
Read-Host "按回车关闭"
