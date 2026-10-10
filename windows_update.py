# Copyright (C) 2026 yoouzic
# SPDX-License-Identifier: GPL-3.0-only

"""Authenticate updates against a pinned release key before interactive installation.

The trust anchor is an Ed25519 public key compiled into this module. Releases are
signed offline with the matching private key, so authenticity does not depend on
an Authenticode certificate and an unsigned build can still ship verifiable
updates. `docs/release-signing.md` documents the signing procedure and key
handling.

Why a helper process rather than one check
------------------------------------------
A compromised desktop process must not be able to swap the installer between
verification and installation. Three processes enforce that:

1. the launcher starts the worker detached from the desktop process tree;
2. the worker takes an exclusive read lock on the MSI, re-hashes it, and runs the
   verifier while holding that lock;
3. the verifier -- the frozen application re-entered with ``--verify-update`` --
   re-hashes the locked file, checks the Ed25519 signature over the canonical
   payload, and reads the MSI product properties.

Only after every check passes does the worker start ``msiexec /i``.

The verifier cannot be PowerShell: Windows PowerShell 5.1 runs on .NET Framework
4.8, which exposes no Ed25519 implementation. The frozen executable already
bundles this module, so it is the natural host for the check.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path

import ed25519


class UpdateVerificationError(RuntimeError):
    """An update could not be safely verified, launched, or observed."""


# Stable identifiers for the messages callers and tests assert on, so wording can
# change in one place without silently changing behaviour.
OFFICIAL_BUILD_REQUIRED = "无法确认当前程序的更新公钥，请使用官方安装版。"
HASH_MISMATCH = "MSI 安装包 SHA256 哈希不匹配或无法读取，已拒绝更新。"
PROPERTY_MISMATCH = "MSI 产品名称、制造商、版本或升级标识不匹配，已拒绝更新。"
SIGNATURE_REJECTED = "MSI 安装包签名未通过校验，已拒绝更新。"

_ERRORS = {
    10: OFFICIAL_BUILD_REQUIRED,
    11: "MSI 安装包缺少有效的发布签名，已拒绝更新。",
    12: SIGNATURE_REJECTED,
    13: "更新签名与内置发布公钥不一致，已拒绝更新。",
    14: "无法只读检查 MSI 产品信息，已拒绝更新。",
    15: HASH_MISMATCH,
    16: PROPERTY_MISMATCH,
    17: "无法锁定 MSI 安装包，已取消更新。",
    18: "更新辅助进程未能安全独立启动或启动超时，已取消更新。",
    19: "无法启动 Windows MSI 安装向导，请稍后重试。",
    20: "无法获取安装向导结果，请检查安装向导状态。",
    21: "无法完成更新安全验证，请稍后重试。",
    22: "更新安全验证超时，请稍后重试。",
}
_UPGRADE_CODE = "{D029E636-7E7E-42EE-8B38-C2D455AD2AA1}"

# Production release key. The matching private key never enters this repository;
# see docs/release-signing.md. Rotating this value requires shipping a new build.
PUBLIC_KEY_B64 = "/sxOMzShO28Jx4h/qwna3KrN9kTqy3x6DthzsyPf1O4="

_PRODUCT_NAME = "youziauth"
_MANUFACTURER = "yoouzic"

# The canonical payload binds product identity to the exact artifact digest, so a
# signature cannot be replayed onto another version, size, or product.
_PAYLOAD_TEMPLATE = ('{{"v":1,"version":"{version}","sha256":"{sha256}","bytes":{size},'
                     '"product":"{product}","manufacturer":"{manufacturer}","upgrade":"{upgrade}"}}')

_VERIFY_ARGUMENT = "--verify-update"

# The two frozen processes allowed to drive an update, and the one file an update may
# install. They sit side by side in the install directory.
_UPDATE_EXECUTABLES = frozenset({"youziauth.exe", "youziauth-agent.exe"})
_UPDATE_ANCHOR = "youziauth.exe"

# Signature gates precede Windows Installer COM access; paths never become
# script source. The worker holds an exclusive read lock throughout, and the
# verifier re-hashes the locked file instead of trusting a prior digest.
_POWERSHELL = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$stage = 14
try {
    $installer = $null
    $database = $null
    $view = $null
    $record = $null
    $properties = @{}
    $names = @('ProductName', 'Manufacturer', 'ProductVersion', 'UpgradeCode')
    $method = [System.Reflection.BindingFlags]::InvokeMethod
    $property = [System.Reflection.BindingFlags]::GetProperty
    try {
        $installer = New-Object -ComObject WindowsInstaller.Installer
        # 0 = msiOpenDatabaseModeReadOnly. No install, extraction, or actions.
        $database = $installer.GetType().InvokeMember('OpenDatabase', $method, $null,
                                                       $installer, @($env:YOUZIAUTH_UPDATE_MSI, 0))
        $view = $database.GetType().InvokeMember('OpenView', $method, $null, $database,
                                                @('SELECT `Property`, `Value` FROM `Property`'))
        $null = $view.GetType().InvokeMember('Execute', $method, $null, $view, $null)
        while ($true) {
            $record = $view.GetType().InvokeMember('Fetch', $method, $null, $view, $null)
            if ($null -eq $record) { break }
            try {
                $name = [string]$record.GetType().InvokeMember('StringData', $property, $null, $record, @(1))
                if ($names -ccontains $name) {
                    if ($properties.ContainsKey($name)) { throw 'duplicate-property' }
                    $properties[$name] = [string]$record.GetType().InvokeMember('StringData', $property,
                                                                                $null, $record, @(2))
                }
            } finally {
                $null = [System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($record)
                $record = $null
            }
        }
    } finally {
        if ($null -ne $view) {
            try { $null = $view.GetType().InvokeMember('Close', $method, $null, $view, $null) }
            finally { $null = [System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($view) }
        }
        if ($null -ne $database) { $null = [System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($database) }
        if ($null -ne $installer) { $null = [System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($installer) }
    }
    [Console]::Out.Write((@{ properties = $properties } | ConvertTo-Json -Depth 4 -Compress))
    exit 0
} catch {
    # Never emit exception details, OS paths, or the original script.
    exit $stage
}
"""
_ENCODED_COMMAND = base64.b64encode(_POWERSHELL.encode("utf-16le")).decode("ascii")

_WORKER = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$directory = $null
$lock = $null
$installer = $null
$stage = 18
$final = @{ error = 18 }
function Publish-Status($name, $value) {
    $target = [IO.Path]::Combine($directory, $name + '.json')
    [IO.File]::WriteAllText($target + '.tmp', ($value | ConvertTo-Json -Compress),
                           (New-Object Text.UTF8Encoding($false)))
    [IO.File]::Move($target + '.tmp', $target)
}
try {
    $request = [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String($env:YOUZIAUTH_UPDATE_REQUEST)) | ConvertFrom-Json
    $directory = $request.directory
    $launcher = $null
    try { $launcher = [Diagnostics.Process]::GetProcessById([int]$env:YOUZIAUTH_UPDATE_LAUNCHER) }
    catch [ArgumentException] { }
    if ($null -ne $launcher) {
        try {
            if (-not $launcher.WaitForExit(15000)) { throw 'launcher-alive' }
        } finally { $launcher.Dispose() }
    }

    $stage = 17
    $lock = [IO.File]::Open($env:YOUZIAUTH_UPDATE_MSI, [IO.FileMode]::Open,
                           [IO.FileAccess]::Read, [IO.FileShare]::Read)
    $stage = 15
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { $digest = [BitConverter]::ToString($hasher.ComputeHash($lock)).Replace('-', '').ToLowerInvariant() }
    finally { $hasher.Dispose() }
    if ($digest -cne $request.sha256) { throw 'hash' }

    $stage = 21
    $powershell = [IO.Path]::Combine([Environment]::SystemDirectory, 'WindowsPowerShell\v1.0\powershell.exe')
    $check = New-Object Diagnostics.ProcessStartInfo
    $check.FileName = $powershell
    $check.Arguments = '-NoProfile -NonInteractive -EncodedCommand ' + $env:YOUZIAUTH_UPDATE_VERIFY
    $check.UseShellExecute = $false
    $check.CreateNoWindow = $true
    $check.RedirectStandardOutput = $true
    $check.RedirectStandardError = $true
    $check.StandardOutputEncoding = New-Object Text.UTF8Encoding($false)
    $reader = [Diagnostics.Process]::Start($check)
    try {
        $output = $reader.StandardOutput.ReadToEndAsync()
        $errors = $reader.StandardError.ReadToEndAsync()
        $stage = 22
        if (-not $reader.WaitForExit(60000)) { throw 'metadata-timeout' }
        $stage = 21
        if ($reader.ExitCode -ne 0) { throw 'metadata' }
        $metadata = $output.Result | ConvertFrom-Json
    } finally { $reader.Dispose() }
    $stage = 14
    if ($metadata.properties -isnot [Management.Automation.PSCustomObject]) { throw 'properties' }

    $stage = 11
    $verify = New-Object Diagnostics.ProcessStartInfo
    $verify.FileName = $env:YOUZIAUTH_UPDATE_EXE
    $verify.Arguments = '--verify-update "' + $env:YOUZIAUTH_UPDATE_REQUEST_FILE + '"'
    $verify.UseShellExecute = $false
    $verify.CreateNoWindow = $true
    $verifier = [Diagnostics.Process]::Start($verify)
    if ($null -eq $verifier) { throw 'verifier-start' }
    try {
        $stage = 22
        if (-not $verifier.WaitForExit(60000)) { throw 'verification-timeout' }
        $stage = 11
        if ($verifier.ExitCode -ne 0) { $stage = $verifier.ExitCode }
        if ($verifier.ExitCode -ne 0) { throw 'signature' }
    } finally { $verifier.Dispose() }

    $stage = 15
    $report = [IO.File]::ReadAllText($env:YOUZIAUTH_UPDATE_RESPONSE_FILE, [Text.Encoding]::UTF8) | ConvertFrom-Json
    if ($report.digest -isnot [string] -or $report.digest -cne $request.sha256) { throw 'digest' }
    $stage = 12
    if ($report.signature -isnot [string] -or $report.signature -cne $request.signature) { throw 'signature-report' }
    if ($report.payload -isnot [string] -or $report.payload -cne $request.payload) { throw 'payload-report' }

    $stage = 16
    $result = @{ properties = $metadata.properties }
    foreach ($property in $request.properties.PSObject.Properties) {
        $actual = $result.properties.PSObject.Properties[$property.Name]
        if ($null -eq $actual -or $actual.Value -isnot [string] -or
            $actual.Value -cne $property.Value) { throw 'property' }
    }

    $stage = 18
    if ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -ge $request.deadline) { throw 'startup-timeout' }
    $stage = 19
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = [IO.Path]::Combine([Environment]::SystemDirectory, 'msiexec.exe')
    $start.Arguments = '/i "' + $env:YOUZIAUTH_UPDATE_MSI + '" /norestart'
    $start.UseShellExecute = $false
    $installer = [Diagnostics.Process]::Start($start)
    if ($null -eq $installer) { throw 'installer-start' }
    $stage = 20
    try { Publish-Status 'launch' @{ pid = $installer.Id } }
    finally { $installer.WaitForExit() }
    $final = @{ code = $installer.ExitCode }
    # 装完把程序重新拉起来。MSI 在 InstallValidate 之前就 taskkill 掉了原来的进程（见
    # packaging/youziauth.wxs），不重启的话用户装完只看到一片空白，还得自己去找快捷方式。
    # 只在 0 时重启：3010 表示有文件要等重启才能落盘，这时启动会因缺文件直接挂掉；
    # 1602 是用户取消了安装，当然也不该启动。
    # 这个工作进程是以当前用户身份运行的（提权发生在 msiexec 自己弹的那个 UAC 上），
    # 所以这里拉起的是普通权限的进程，不会把程序变成以管理员运行。
    # 不带参数启动 = 不隐藏窗口（campus_auth_gui.should_start_hidden(False, "show") 为假）。
    if ($installer.ExitCode -eq 0) {
        try {
            $target = $env:YOUZIAUTH_UPDATE_EXE
            Start-Process -FilePath $target -WorkingDirectory (Split-Path -Parent $target) | Out-Null
            $final.relaunched = $true
        } catch {
            $final.relaunched = $false
        }
    }
} catch {
    $final = @{ error = $stage }
} finally {
    if ($null -ne $installer) { $installer.Dispose() }
    if ($null -ne $lock) { $lock.Dispose() }
}
try { Publish-Status 'final' $final }
catch { exit 1 }
"""
_ENCODED_WORKER = base64.b64encode(_WORKER.encode("utf-16le")).decode("ascii")

_LAUNCHER = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
try {
    $env:YOUZIAUTH_UPDATE_LAUNCHER = [string]$PID
    $powershell = [IO.Path]::Combine([Environment]::SystemDirectory, 'WindowsPowerShell\v1.0\powershell.exe')
    $worker = Start-Process -FilePath $powershell -WindowStyle Hidden -PassThru -ArgumentList @(
        '-NoProfile', '-NonInteractive', '-EncodedCommand', $env:YOUZIAUTH_UPDATE_WORKER)
    [Console]::Out.Write((@{ pid = $worker.Id } | ConvertTo-Json -Compress))
    $worker.Dispose()
} catch { exit 1 }
"""
_ENCODED_LAUNCHER = base64.b64encode(_LAUNCHER.encode("utf-16le")).decode("ascii")


def _kernel32():
    if sys.platform != "win32":
        raise UpdateVerificationError("更新安装仅支持 Windows。")
    try:
        # kernel32 is a Windows KnownDLL, not a PATH-resolved executable.
        api = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        api.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
        api.GetSystemDirectoryW.restype = wintypes.UINT
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        return api
    except (OSError, AttributeError, TypeError, ValueError):
        raise UpdateVerificationError("无法使用 Windows 更新安全接口。") from None


def _system_tool(relative_path: str) -> Path:
    api = _kernel32()
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = api.GetSystemDirectoryW(buffer, len(buffer))
        if not 0 < length < len(buffer) or not Path(buffer.value).is_absolute():
            raise ValueError
        return Path(buffer.value) / relative_path
    except (OSError, ValueError):
        raise UpdateVerificationError("无法定位 Windows 系统更新工具。") from None


def _file_path(value: Path, message: str) -> Path:
    try:
        if not isinstance(value, Path):
            raise ValueError
        raw = str(value)
        # Refuse control characters, network/device namespaces and ADS paths.
        if (any(ord(char) < 32 for char in raw) or raw.startswith(("\\\\", "//"))
                or ":" in os.path.splitdrive(raw)[1]):
            raise ValueError
        resolved = value.resolve(strict=True)
        if not resolved.is_file() or str(resolved).startswith(("\\\\", "//")):
            raise ValueError
        return resolved
    except (OSError, ValueError, RuntimeError):
        raise UpdateVerificationError(message) from None


def public_key() -> bytes:
    """Return the pinned Ed25519 release key this build trusts."""
    try:
        key = base64.b64decode(PUBLIC_KEY_B64, validate=True)
    except (ValueError, TypeError):
        raise UpdateVerificationError(_ERRORS[10]) from None
    if len(key) != ed25519.PUBLIC_KEY_BYTES:
        raise UpdateVerificationError(_ERRORS[10])
    return key


def canonical_payload(version: str, sha256: str, size: int) -> bytes:
    """Build the exact ASCII bytes a release signature covers.

    Every field is shape-checked before it reaches this template, so the result
    can never contain characters whose encoding is ambiguous.
    """
    return _PAYLOAD_TEMPLATE.format(
        version=version, sha256=sha256.lower(), size=size,
        product=_PRODUCT_NAME, manufacturer=_MANUFACTURER, upgrade=_UPGRADE_CODE,
    ).encode("ascii")


def _expected_properties(version: str) -> dict[str, str]:
    return {
        "ProductName": _PRODUCT_NAME, "Manufacturer": _MANUFACTURER,
        "ProductVersion": version, "UpgradeCode": _UPGRADE_CODE,
    }


def _version(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(
        r"(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,4})", value
    ) is None:
        raise UpdateVerificationError("更新版本必须为严格的 MAJOR.MINOR.PATCH 格式。")
    if any(part > limit for part, limit in zip(map(int, value.split(".")), (255, 255, 65535))):
        raise UpdateVerificationError("更新版本超出 Windows MSI 支持的范围。")
    return value


def _sha256(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise UpdateVerificationError("更新 SHA256 哈希格式无效。")
    return value.lower()


def _signature(value: object) -> bytes:
    """Normalize a detached signature supplied as hex or raw bytes."""
    if isinstance(value, str):
        if re.fullmatch(r"[0-9a-fA-F]{128}", value) is None:
            raise UpdateVerificationError(_ERRORS[11])
        return bytes.fromhex(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if len(raw) != ed25519.SIGNATURE_BYTES:
            raise UpdateVerificationError(_ERRORS[11])
        return raw
    raise UpdateVerificationError(_ERRORS[11])


def _payload_fields(payload: object, version: str, size: int, digest: str) -> bytes:
    """Validate the canonical payload and return it as ASCII bytes."""
    if not isinstance(payload, str) or not payload.isascii():
        raise UpdateVerificationError(_ERRORS[12])
    raw = payload.encode("ascii")
    if len(raw) > 4096:
        raise UpdateVerificationError(_ERRORS[12])
    try:
        data = json.loads(raw)
    except ValueError:
        raise UpdateVerificationError(_ERRORS[12]) from None
    if not isinstance(data, dict):
        raise UpdateVerificationError(_ERRORS[12])
    expected = json.loads(canonical_payload(_version(version), "0" * 64, size).decode("ascii"))
    expected["sha256"] = digest.lower()
    if set(data) != set(expected):
        raise UpdateVerificationError(_ERRORS[12])
    for key, value in expected.items():
        if type(data.get(key)) is not type(value) or data[key] != value:
            raise UpdateVerificationError(_ERRORS[12])
    return raw


def _read_digest(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        raise UpdateVerificationError("无法读取 MSI 安装包以校验哈希。") from None


def _validate_inputs(path, executable, version, sha256, signature):
    """Validate every caller-supplied value before any process or COM boundary."""
    if sys.platform != "win32":
        raise UpdateVerificationError("更新安装仅支持 Windows。")
    version = _version(version)
    sha256 = _sha256(sha256)
    signature = _signature(signature)
    if not getattr(sys, "frozen", False):
        raise UpdateVerificationError(_ERRORS[10])
    current = _file_path(Path(sys.executable), _ERRORS[10])
    anchor = current if executable is None else _file_path(executable, _ERRORS[10])
    # Two processes legitimately run this check, and the anchor must be the desktop build
    # in both cases:
    #  * youziauth.exe verifying and installing itself;
    #  * youziauth-agent.exe -- the privileged SYSTEM agent that owns unattended updates --
    #    which spawns youziauth.exe with --verify-update and installs that anchor.
    # Requiring the caller to *be* youziauth.exe made every automatic update fail with
    # "无法确认当前程序的更新公钥" after it had already downloaded and re-checked the
    # package: the check described the old topology, not this one.
    if current.name.casefold() not in _UPDATE_EXECUTABLES:
        raise UpdateVerificationError(_ERRORS[10])
    if (anchor.name.casefold() != _UPDATE_ANCHOR
            or anchor.parent != current.parent):
        raise UpdateVerificationError(_ERRORS[10])
    msi = _file_path(path, "MSI 安装包路径无效或文件不可读取。")
    if msi.suffix.lower() != ".msi":
        raise UpdateVerificationError("更新安装包必须为 MSI 文件。")
    size = msi.stat().st_size
    if not 0 < size <= 0xFFFFFFFF:
        raise UpdateVerificationError(_ERRORS[15])
    return msi, anchor, version, sha256, signature, size


def verify_locked_msi(request: object, reader=_read_digest) -> dict:
    """Verify a release request and return the report the worker cross-checks.

    Runs inside the frozen application, which holds the MSI open with an exclusive
    read lock, so the digest read here is the digest that will be installed.

    ``reader`` is an injectable seam for tests only; production callers never pass
    it, and the pinned key is never taken from the request.
    """
    if not isinstance(request, dict):
        raise UpdateVerificationError(_ERRORS[21])
    msi = _file_path(Path(str(request.get("msi"))), "MSI 安装包路径无效或文件不可读取。")
    version = _version(request.get("version"))
    size = request.get("bytes")
    if type(size) is not int or not 0 < size <= 0xFFFFFFFF:
        raise UpdateVerificationError(_ERRORS[15])
    signature = _signature(request.get("signature"))
    digest = reader(msi)
    if type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise UpdateVerificationError(_ERRORS[15])
    if msi.stat().st_size != size:
        raise UpdateVerificationError(_ERRORS[15])
    payload = _payload_fields(request.get("payload"), version, size, digest)
    if not ed25519.verify(signature, payload, public_key()):
        raise UpdateVerificationError(_ERRORS[12])
    return {"digest": digest, "signature": signature.hex(), "payload": payload.decode("ascii"),
            "properties": _expected_properties(version)}


def verifier_main(request_path: Path, report_path: Path) -> int:
    """Entry point for ``youziauth.exe --verify-update``.

    The exit code selects the user-facing message, so every failure maps to a
    fixed stage number instead of leaking exception text to the caller.
    """
    def failure(exc: BaseException) -> int:
        return next((code for code, text in _ERRORS.items() if text == str(exc)), 21)

    try:
        request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return 21
    try:
        report = verify_locked_msi(request)
    except UpdateVerificationError as exc:
        return failure(exc)
    except (OSError, ValueError, TypeError, RecursionError):
        return 21
    try:
        Path(report_path).write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    except (OSError, ValueError, TypeError):
        return 21
    return 0


def _run_verifier(anchor: Path, msi: Path, version: str, size: int, digest: str,
                  signature: bytes, payload: bytes) -> int:
    """Re-enter the frozen application to verify; return an ``_ERRORS`` stage code.

    The frozen executable is windowed and therefore has no console, so the
    verifier reports through its exit code plus a JSON file rather than stdout.
    """
    request_path, report_path = _write_request(msi, version, size, digest, signature, payload)
    try:
        try:
            result = subprocess.run(
                [str(anchor), _VERIFY_ARGUMENT, str(request_path)], shell=False,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=120, creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
        except subprocess.TimeoutExpired:
            return 22
        except (OSError, subprocess.SubprocessError, ValueError):
            return 21
        if result.returncode != 0:
            return result.returncode if result.returncode in _ERRORS else 21
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            return 21
        # The verifier must have reached the same digest and payload we computed.
        if not isinstance(report, dict) or report.get("digest") != digest:
            return 15
        if report.get("payload") != payload.decode("ascii"):
            return 12
        return 0
    finally:
        shutil.rmtree(request_path.parent, ignore_errors=True)


def _write_request(msi: Path, version: str, size: int, digest: str,
                   signature: bytes, payload: bytes) -> tuple[Path, Path]:
    """Create the private request/report files the verifier process exchanges.

    The request lives in a directory only this process can enumerate, so a
    concurrent process cannot substitute the input the verifier reads.
    """
    request = {
        "msi": str(msi), "version": version, "bytes": size, "sha256": digest,
        "signature": signature.hex(), "payload": payload.decode("ascii"),
        "properties": _expected_properties(version),
    }
    try:
        directory = Path(tempfile.mkdtemp(prefix="youziauth-verify-")).resolve()
    except OSError:
        raise UpdateVerificationError(_ERRORS[21]) from None
    request_path = directory / "request.json"
    report_path = directory / "report.json"
    try:
        request_path.write_text(json.dumps(request), encoding="utf-8")
    except OSError:
        shutil.rmtree(directory, ignore_errors=True)
        raise UpdateVerificationError(_ERRORS[21]) from None
    return request_path, report_path


def _msi_properties(msi: Path) -> dict:
    powershell = _system_tool("WindowsPowerShell/v1.0/powershell.exe")
    environment = os.environ.copy()
    environment["YOUZIAUTH_UPDATE_MSI"] = str(msi)
    environment["PSModulePath"] = str(powershell.parent / "Modules")
    try:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", _ENCODED_COMMAND],
            env=environment, shell=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding="utf-8", timeout=60, creationflags=0x08000000,
        )
    except subprocess.TimeoutExpired:
        raise UpdateVerificationError(_ERRORS[22]) from None
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        raise UpdateVerificationError(_ERRORS[21]) from None
    if result.returncode != 0:
        raise UpdateVerificationError(_ERRORS.get(result.returncode, _ERRORS[21]))
    try:
        return json.loads(result.stdout)["properties"]
    except (ValueError, TypeError, KeyError, RecursionError):
        raise UpdateVerificationError(_ERRORS[21]) from None


def verify_msi(path, executable, version, sha256, signature=None) -> None:
    """Verify an already-downloaded MSI without launching the installer.

    Installation repeats every check while holding the MSI file lock, so this is
    a preview for the user rather than the enforcement point.
    """
    msi, _anchor, version, sha256, signature, size = _validate_inputs(
        path, executable, version, sha256, signature
    )
    digest = _read_digest(msi)
    if not hmac.compare_digest(digest, sha256):
        raise UpdateVerificationError(_ERRORS[15])
    payload = _payload_fields(canonical_payload(version, digest, size).decode("ascii"),
                              version, size, digest)
    if not ed25519.verify(signature, payload, public_key()):
        raise UpdateVerificationError(_ERRORS[12])
    properties = _msi_properties(msi)
    expected = _expected_properties(version)
    if not isinstance(properties, dict) or any(properties.get(key) != value for key, value in expected.items()):
        raise UpdateVerificationError(_ERRORS[16])


def _start_worker(msi: Path, anchor: Path, version: str, size: int, digest: str,
                  signature: bytes, payload: bytes, directory: Path) -> int:
    powershell = _system_tool("WindowsPowerShell/v1.0/powershell.exe")
    request_path, report_path = _write_request(msi, version, size, digest, signature, payload)
    request = {
        "directory": str(directory), "sha256": digest, "signature": signature.hex(),
        "payload": payload.decode("ascii"), "properties": _expected_properties(version),
        "deadline": int(time.time()) + 90,
    }
    environment = os.environ.copy()
    environment.update({
        "YOUZIAUTH_UPDATE_MSI": str(msi), "YOUZIAUTH_UPDATE_EXE": str(anchor),
        "YOUZIAUTH_UPDATE_SIGNATURE": signature.hex(), "YOUZIAUTH_UPDATE_PAYLOAD": payload.decode("ascii"),
        "YOUZIAUTH_UPDATE_REQUEST_FILE": str(request_path), "YOUZIAUTH_UPDATE_RESPONSE_FILE": str(report_path),
        "YOUZIAUTH_UPDATE_REQUEST": base64.b64encode(json.dumps(request).encode("utf-8")).decode("ascii"),
        "YOUZIAUTH_UPDATE_VERIFY": _ENCODED_COMMAND, "YOUZIAUTH_UPDATE_WORKER": _ENCODED_WORKER,
        "PSModulePath": str(powershell.parent / "Modules"),
    })
    try:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", _ENCODED_LAUNCHER],
            env=environment, shell=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding="utf-8", timeout=20, creationflags=0x08000000,
        )
        status = json.loads(result.stdout)
        if (result.returncode != 0 or not isinstance(status, dict) or set(status) != {"pid"}
                or type(status["pid"]) is not int or not 0 < status["pid"] <= 0xFFFFFFFF):
            raise ValueError
        return status["pid"]
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, RecursionError):
        raise UpdateVerificationError(_ERRORS[18]) from None


def _read_status(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8") as stream:
            data = stream.read(4097)
        if len(data) > 4096:
            raise ValueError
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError):
        raise UpdateVerificationError("更新辅助进程状态无效，请检查安装向导状态。") from None


def _observe_worker(pid: int, directory: Path, on_launch: Callable[[], None]) -> int:
    api = _kernel32()
    handle = None
    finished = False
    notified = False
    notification_error = None
    deadline = time.monotonic() + 120
    try:
        handle = api.OpenProcess(0x00100000, False, pid)
        exited = not handle
        while True:
            final = _read_status(directory / "final.json")
            launch = _read_status(directory / "launch.json")
            if launch is not None:
                if (set(launch) != {"pid"} or type(launch["pid"]) is not int
                        or not 0 < launch["pid"] <= 0xFFFFFFFF):
                    raise UpdateVerificationError(_ERRORS[20])
                if not notified:
                    notified = True
                    try:
                        on_launch()
                    except BaseException as exc:
                        notification_error = exc
            if final is not None:
                if (set(final) not in ({"code"}, {"error"}) or
                        type(next(iter(final.values()))) is not int):
                    raise UpdateVerificationError(_ERRORS[20])
                finished = True
                if "error" in final:
                    raise UpdateVerificationError(_ERRORS.get(final["error"], _ERRORS[21]))
                if not notified or not -0x80000000 <= final["code"] <= 0xFFFFFFFF:
                    raise UpdateVerificationError(_ERRORS[20])
                if notification_error is not None:
                    if not isinstance(notification_error, Exception):
                        raise notification_error
                    raise UpdateVerificationError("安装向导已启动，但启动通知失败；请检查安装结果。") from None
                return final["code"]
            if exited:
                finished = bool(handle)
                raise UpdateVerificationError("更新辅助进程意外退出或无法观察，请检查安装向导状态。")
            if not notified and time.monotonic() >= deadline:
                raise UpdateVerificationError(_ERRORS[18])
            wait = api.WaitForSingleObject(handle, 100)
            if wait not in (0, 258):
                raise UpdateVerificationError(_ERRORS[20])
            exited = wait == 0
    except (OSError, ValueError):
        raise UpdateVerificationError(_ERRORS[20]) from None
    finally:
        if handle:
            api.CloseHandle(handle)
        if finished:
            shutil.rmtree(directory, ignore_errors=True)


def install_msi(path, executable, version, sha256, on_launch, signature=None) -> int:
    if not callable(on_launch):
        raise UpdateVerificationError("安装启动通知回调无效。")
    msi, anchor, version, sha256, signature, size = _validate_inputs(
        path, executable, version, sha256, signature
    )
    digest = _read_digest(msi)
    if not hmac.compare_digest(digest, sha256):
        raise UpdateVerificationError(_ERRORS[15])
    payload = _payload_fields(canonical_payload(version, digest, size).decode("ascii"),
                              version, size, digest)
    if not ed25519.verify(signature, payload, public_key()):
        raise UpdateVerificationError(_ERRORS[12])
    # Preview in this process; the worker enforces the same checks under its lock.
    stage = _run_verifier(anchor, msi, version, size, digest, signature, payload)
    if stage != 0:
        raise UpdateVerificationError(_ERRORS.get(stage, _ERRORS[21]))
    try:
        directory = Path(tempfile.mkdtemp(prefix="youziauth-update-")).resolve()
    except OSError:
        raise UpdateVerificationError("无法创建更新私有状态目录，已取消更新。") from None
    pid = _start_worker(msi, anchor, version, size, digest, signature, payload, directory)
    return _observe_worker(pid, directory, on_launch)
