# Run the updater's own network path under an elevated token -- the same privilege level
# the SYSTEM agent has -- and record exactly what happens. Elevated runs cannot write to
# the user's profile, so the result goes somewhere both can read.
$ErrorActionPreference = 'Continue'
$report = 'C:\ProgramData\youziauth-agent-probe.txt'
$lines = New-Object System.Collections.Generic.List[string]

function Say($text) { $lines.Add($text); Write-Host $text }

Say ("time          : " + (Get-Date).ToString('s'))
Say ("whoami        : " + (whoami))
Say ("elevated      : " + ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))

# What the resolution chain would hand a urllib request in THIS token.
Say ""
Say "--- proxy as seen by this token ---"
try {
    $rk = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction Stop
    Say ("HKCU ProxyEnable : " + $rk.ProxyEnable)
    Say ("HKCU ProxyServer : " + $rk.ProxyServer)
    Say ("HKCU AutoConfig  : " + $rk.AutoConfigURL)
} catch { Say ("HKCU read failed : " + $_.Exception.Message) }
$env:HTTP_PROXY = $null; $env:HTTPS_PROXY = $null
Say ("env HTTP_PROXY   : " + [string]$env:HTTP_PROXY + " / " + [string]$env:HTTPS_PROXY)
$winhttp = (netsh winhttp show proxy) -join ' '
Say ("winhttp          : " + ($winhttp -replace '\s+', ' ').Trim())

# The updater itself: same module, same URL handling, real release asset.
Say ""
Say "--- the updater's own code path ---"
$py = 'C:\Program Files (x86)\youziauth\_internal'
$env:PYTHONPATH = $py
$script = @'
import sys, traceback, time, json
sys.path.insert(0, r"C:\Program Files (x86)\youziauth\_internal")
import urllib.request
data = json.loads(urllib.request.urlopen(
    urllib.request.Request("https://api.github.com/repos/yoouzic/youziauth/releases/latest",
        headers={"User-Agent": "probe", "Accept": "application/vnd.github+json"}), timeout=30).read())
url = None
for asset in data["assets"]:
    if asset["name"] == "youziauth.msi":
        url = asset["browser_download_url"]
print("resolved msi url:", url[:70] + "...")
print("proxy handlers  :", urllib.request.getproxies())
for attempt in range(1, 4):
    started = time.monotonic()
    try:
        opener = urllib.request.build_opener()
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "probe"}), timeout=20) as r:
            first = r.read(256 * 1024)
            print("attempt {0}: OK status={1} first_chunk={2}B in {3:.1f}s".format(
                attempt, r.status, len(first), time.monotonic() - started))
            break
    except Exception as exc:
        print("attempt {0}: {1} after {2:.1f}s -> {3}".format(
            attempt, type(exc).__name__, time.monotonic() - started, exc))
'@
$tmp = Join-Path $env:TEMP 'agent-network-probe.py'
Set-Content -Path $tmp -Value $script -Encoding utf8
try {
    $output = & python $tmp 2>&1
    foreach ($line in $output) { Say ("  " + $line) }
} catch { Say ("  python failed: " + $_.Exception.Message) }

Say ""
Say "--- conclusions ---"
Say ("report written : " + $report)
[IO.File]::WriteAllLines($report, $lines)
