param(
    [string]$PythonPath = "",
    [switch]$InstallDependencies,
    [switch]$VerifyPayload,
    # Stop after the frozen executables pass the boot check. Iterating on the
    # PyInstaller/Tcl/Tk layer then costs seconds instead of a full MSI build.
    [switch]$FrozenOnly
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PackagingDir = Join-Path $Root "packaging"
$BuildDir = Join-Path $Root "build"
$WixBuildDir = Join-Path $BuildDir "wix"
$DistDir = Join-Path $Root "dist"
$MsiPath = Join-Path $DistDir "youziauth.msi"
$Version = (Get-Content -LiteralPath (Join-Path $Root "VERSION") -Raw).Trim()
if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "VERSION must use MAJOR.MINOR.PATCH"
}

function Resolve-Python {
    param([string]$RequestedPath)
    if (-not [string]::IsNullOrWhiteSpace($RequestedPath)) {
        return $RequestedPath
    }
    foreach ($candidate in @("python.exe", "python")) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($null -ne $command) {
            return $command.Source
        }
    }
    throw "Python was not found. Install Python 3.10+ or pass -PythonPath C:\Path\To\python.exe"
}

function Ensure-PythonBuildDependencies {
    param([string]$Python)
    $Requirements = Join-Path $Root "requirements-build.txt"
    if ($InstallDependencies) {
        & $Python -m pip install --requirement $Requirements
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to install pinned Python build dependencies"
        }
    }
    & $Python -c "import PIL, PyInstaller; assert PIL.__version__ == '12.2.0'; assert PyInstaller.__version__ == '6.16.0'"
    if ($LASTEXITCODE -ne 0) {
        throw "Pinned Python build dependencies are unavailable"
    }
    # 验证码识别（IDM 静默登录）需要 numpy 与模型文件。
    # 缺任何一项都会让静默登录在**运行时**才失败，因此在构建阶段提前拦住。
    $ModelPath = Join-Path $Root "data\captcha\model.npz"
    if (-not (Test-Path $ModelPath)) {
        throw "Captcha model is missing: $ModelPath"
    }
    Push-Location $Root
    try {
        & $Python -c "from captcha_ocr import CaptchaSolver; CaptchaSolver(r'$ModelPath')"
        if ($LASTEXITCODE -ne 0) {
            throw "Captcha recognition dependencies are unavailable (numpy or model load failed)"
        }
    } finally {
        Pop-Location
    }
}

function Assert-WixManifestIsComplete {
    param([string]$ManifestPath, [string]$AppDir)
    # The generated manifest is the whole payload contract. A bundle file that never
    # reaches ApplicationFiles.wxs ships an interpreter without its standard library,
    # which fails at startup with "Failed to import encodings module".
    $expected = New-Object System.Collections.Generic.List[string]
    foreach ($item in Get-ChildItem -LiteralPath $AppDir -Recurse -File -Force) {
        $expected.Add($item.FullName)
    }
    $listed = @{}
    foreach ($match in [regex]::Matches((Get-Content -LiteralPath $ManifestPath -Raw), 'Source="([^"]+)"')) {
        $listed[$match.Groups[1].Value] = $true
    }
    $missing = @($expected | Where-Object { -not $listed.ContainsKey($_) })
    if ($missing.Count -gt 0) {
        $missing | Select-Object -First 20 | ForEach-Object { Write-Host "  NOT IN MANIFEST  $_" }
        throw "$($missing.Count) bundle files are missing from the WiX file manifest"
    }
    Write-Host "WiX manifest covers all $($expected.Count) bundle files"
}

function Resolve-Wix {
    $toolPath = Join-Path $Root ".tools"
    $localWix = Join-Path $toolPath "wix.exe"
    if (Test-Path -LiteralPath $localWix) {
        return $localWix
    }

    $command = Get-Command wix.exe -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }

    if (-not $InstallDependencies) {
        throw "WiX is not installed. Re-run with -InstallDependencies or install it with: dotnet tool install wix --tool-path .tools"
    }

    New-Item -ItemType Directory -Force -Path $toolPath | Out-Null
    dotnet tool install wix --tool-path $toolPath --version 7.0.0 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install WiX"
    }
    if (-not (Test-Path -LiteralPath $localWix)) {
        throw "WiX installation completed, but wix.exe was not found at $localWix"
    }
    return $localWix
}

function Ensure-DormDependencies {
    param([string]$Python)
    if ($InstallDependencies) {
        & $Python -m pip install -r (Join-Path $Root "requirements-dorm.txt")
        if ($LASTEXITCODE -ne 0) { throw "Failed to install dorm login dependencies" }
    }
    & $Python -c "from playwright.sync_api import sync_playwright; from winrt.windows.devices.geolocation import Geolocator"
    if ($LASTEXITCODE -ne 0) {
        throw "Dorm login/location dependencies are required. Re-run with -InstallDependencies."
    }
}

$Python = Resolve-Python $PythonPath
Ensure-PythonBuildDependencies $Python
Ensure-DormDependencies $Python
if ($InstallDependencies) {
    & $Python -m pip install -r (Join-Path $Root "requirements-desktop.txt")
    if ($LASTEXITCODE -ne 0) { throw "Failed to install desktop UI dependencies" }
}
& $Python -c "import webview; import clr"
if ($LASTEXITCODE -ne 0) { throw "Desktop dependencies missing. Re-run with -InstallDependencies." }
$Wix = Resolve-Wix

& $Python (Join-Path $PackagingDir "make_icons.py")
if ($LASTEXITCODE -ne 0) {
    throw "Icon generation failed"
}

& $Python (Join-Path $PackagingDir "generate_version_info.py") --output (Join-Path $BuildDir "version")
if ($LASTEXITCODE -ne 0) {
    throw "Version resource generation failed"
}

# PyInstaller 在**成功**时也会往 stderr 写 INFO/WARNING 日志；
# 由于脚本顶部设了 $ErrorActionPreference = "Stop"，PowerShell 会把这类
# 正常输出当成致命错误中断构建（实测踩到）。这里临时放开，
# 只依据退出码判断成败，构建完立即恢复原设置。
$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    & $Python -m PyInstaller --noconfirm --clean (Join-Path $PackagingDir "youziauth.spec")
    $pyInstallerExit = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $previousErrorAction
}
if ($pyInstallerExit -ne 0) {
    throw "PyInstaller build failed"
}
$AppDir = Join-Path $DistDir "youziauth"
if (-not (Test-Path -LiteralPath (Join-Path $AppDir "youziauth.exe"))) {
    throw "PyInstaller did not produce youziauth.exe in $AppDir"
}
if (-not (Test-Path -LiteralPath (Join-Path $AppDir "youziauth-agent.exe"))) {
    throw "PyInstaller did not produce youziauth-agent.exe in $AppDir"
}

# A PyInstaller bundle can be produced successfully and still fail at startup:
# its Tkinter runtime hook raises when the Tcl/Tk data directories are missing,
# which a windowed build only shows as a modal dialog. Run the frozen executable's
# console-free verifier path so a non-booting binary fails the build, not the user.
function Assert-FrozenGuiBoots {
    param([string]$AppDir, [string]$DistDir)
    $exe = Join-Path $AppDir "youziauth.exe"
    $probeDir = Join-Path $DistDir "boot-probe"
    New-Item -ItemType Directory -Force -Path $probeDir | Out-Null
    $request = Join-Path $probeDir "request.json"
    # A path that cannot exist, so the verifier exits promptly instead of verifying.
    '{"msi":"C:\\youziauth-boot-probe\\missing.msi"}' |
        Set-Content -LiteralPath $request -Encoding utf8

    # The frozen Tkinter hook reads these; their absence is the failure we catch.
    foreach ($required in @("_tcl_data", "_tk_data")) {
        if (-not (Test-Path -LiteralPath (Join-Path $AppDir "_internal\$required"))) {
            throw "Frozen bundle is missing _internal\$required; the Tkinter runtime hook will abort at startup. This usually means the build Python ships Tcl/Tk 9, which PyInstaller 6.16 does not support."
        }
    }

    $process = Start-Process -FilePath $exe -ArgumentList @("--verify-update", $request) `
        -PassThru -WindowStyle Hidden
    if (-not $process.WaitForExit(90000)) {
        $process.Kill()
        throw "Frozen youziauth.exe did not exit within 90s for --verify-update; it is not a usable build."
    }
    # 21 is the stage code for "could not verify" and proves main() ran to the
    # verifier. Any other value means startup failed before dispatch.
    if ($process.ExitCode -ne 21) {
        throw "Frozen youziauth.exe returned $($process.ExitCode) for a bogus --verify-update request; expected 21. The bundle does not start correctly."
    }
    Write-Host "Frozen GUI boot check passed (Tcl/Tk data present, verifier dispatch reached)"
}
Assert-FrozenGuiBoots -AppDir $AppDir -DistDir $DistDir

if ($FrozenOnly) {
    Write-Host "Frozen-only build requested; stopping before the MSI step."
    exit 0
}

New-Item -ItemType Directory -Force -Path $WixBuildDir | Out-Null
$GeneratedWxs = Join-Path $WixBuildDir "ApplicationFiles.wxs"
& $Python (Join-Path $PackagingDir "generate_wix_files.py") --app-dir $AppDir --output $GeneratedWxs
if ($LASTEXITCODE -ne 0) {
    throw "WiX file manifest generation failed"
}
Assert-WixManifestIsComplete -ManifestPath $GeneratedWxs -AppDir $AppDir

& $Wix --acceptEula wix7 build `
    (Join-Path $PackagingDir "youziauth.wxs") `
    (Join-Path $PackagingDir "installer-ui.wxs") `
    $GeneratedWxs `
    -d ProductVersion=$Version `
    -out $MsiPath
if ($LASTEXITCODE -ne 0) {
    throw "WiX MSI build failed"
}
if (-not (Test-Path -LiteralPath $MsiPath)) {
    throw "WiX completed without producing $MsiPath"
}

if ($VerifyPayload) {
    & (Join-Path $PackagingDir "verify_msi_payload.ps1") -MsiPath $MsiPath -AppDir $AppDir
    if ($LASTEXITCODE -ne 0) {
        throw "MSI payload verification failed"
    }
}

Write-Host "MSI created: $MsiPath"
