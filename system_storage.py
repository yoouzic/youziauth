# Copyright (C) 2026 yoouzic
# SPDX-License-Identifier: GPL-3.0-only

"""Fixed, protected storage for SYSTEM updater, runtime state, and agent logs.

The editable user configuration remains in ``youziauth``. Privileged processes
must obtain this sibling root through ``verified_system_storage`` before writing.
Only SYSTEM may initialize it; verification never repairs permissions. The installer
or a registered SYSTEM agent can initialize a previously absent trusted root.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path


STORAGE_NAME = "youziauth-system"
_ACCOUNT_SID = re.compile(r"S-1-(?:5-21|12-1)-([0-9]{1,10})-([0-9]{1,10})-([0-9]{1,10})-([0-9]{1,10})")


class SystemStorageError(RuntimeError):
    """The privileged storage boundary could not be established or verified."""


def default_system_storage_path() -> Path:
    """Read Windows' CommonApplicationData known folder without creating anything."""
    if sys.platform != "win32":
        raise SystemStorageError("SYSTEM storage is only available on Windows")
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.SHGetFolderPathW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.HANDLE,
                                      wintypes.DWORD, wintypes.LPWSTR]
    shell.SHGetFolderPathW.restype = ctypes.c_long
    buffer = ctypes.create_unicode_buffer(260)
    # CSIDL_COMMON_APPDATA, SHGFP_TYPE_CURRENT; deliberately omit FLAG_CREATE.
    if shell.SHGetFolderPathW(None, 0x23, None, 0, buffer) != 0:
        raise SystemStorageError("Windows CommonApplicationData could not be located")
    root = Path(buffer.value)
    if not root.is_absolute():
        raise SystemStorageError("Windows returned an invalid CommonApplicationData path")
    return root / STORAGE_NAME


def _powershell_path() -> Path:
    if sys.platform != "win32":
        raise SystemStorageError("SYSTEM storage is only available on Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel.GetSystemDirectoryW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise SystemStorageError("Windows system tools could not be located")
    return Path(buffer.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe"


_ACL_FUNCTIONS = r"""
function Convert-StorageSecurity($security, [int]$attributes) {
    $rules = @($security.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) |
        ForEach-Object { @{ Sid = $_.IdentityReference.Value; Rights = [int]$_.FileSystemRights;
            Flags = [int]$_.InheritanceFlags; Propagation = [int]$_.PropagationFlags;
            Type = [int]$_.AccessControlType; Inherited = $_.IsInherited } })
    return @{ Owner = $security.GetOwner([Security.Principal.SecurityIdentifier]).Value;
        Protected = $security.AreAccessRulesProtected; Rules = $rules; Attributes = $attributes }
}
function Read-StorageDirectory([string]$path) {
    try { $attributes = [IO.File]::GetAttributes($path) }
    catch [IO.FileNotFoundException] { return $null }
    catch [IO.DirectoryNotFoundException] { return $null }
    if (($attributes -band [IO.FileAttributes]::Directory) -eq 0 -or
        ($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'unsafe-directory' }
    $security = [IO.Directory]::GetAccessControl($path,
        ([Security.AccessControl.AccessControlSections]::Owner -bor
         [Security.AccessControl.AccessControlSections]::Access))
    return (Convert-StorageSecurity $security ([int]$attributes))
}
function Assert-StoragePath([string]$path) {
    $current = New-Object IO.DirectoryInfo([IO.Path]::GetFullPath($path))
    while ($null -ne $current) {
        try {
            $attributes = [IO.File]::GetAttributes($current.FullName)
            if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
                ($attributes -band [IO.FileAttributes]::Directory) -eq 0) { throw 'redirected-path' }
        } catch [IO.FileNotFoundException] { }
          catch [IO.DirectoryNotFoundException] { }
        $current = $current.Parent
    }
}
function Test-TrustedStorage($state, [bool]$systemOwnerOnly = $true, [string]$readerSid = '') {
    if ($null -eq $state -or -not $state.Protected -or
        ($state.Attributes -band 1024) -ne 0 -or ($state.Attributes -band 16) -eq 0 -or
        $state.Rules.Count -ne 3) { return $false }
    if ($systemOwnerOnly) {
        if ($state.Owner -cne 'S-1-5-18') { return $false }
    } elseif (@('S-1-5-18', 'S-1-5-32-544') -cnotcontains $state.Owner) { return $false }
    $system = 0; $admin = 0; $reader = 0
    foreach ($rule in $state.Rules) {
        if ($rule.Type -ne 0 -or $rule.Inherited -or $rule.Flags -ne 3 -or
            $rule.Propagation -ne 0) { return $false }
        if ($rule.Sid -ceq 'S-1-5-18') {
            if ($rule.Rights -ne 2032127) { return $false }; $system++
        } elseif ($rule.Sid -ceq 'S-1-5-32-544') {
            if ($rule.Rights -ne 2032127) { return $false }; $admin++
        } else {
            if ($rule.Sid -cnotmatch '^S-1-(?:5-21|12-1)-[0-9]{1,10}-[0-9]{1,10}-[0-9]{1,10}-[0-9]{1,10}\z' -or
                $rule.Rights -ne 1179817 -or ($readerSid -and $rule.Sid -cne $readerSid)) { return $false }
            $reader++
        }
    }
    return ($system -eq 1 -and $admin -eq 1 -and $reader -eq 1)
}
function New-StorageSecurity([string]$userSid) {
    $security = New-Object Security.AccessControl.DirectorySecurity
    $security.SetAccessRuleProtection($true, $false)
    $security.SetOwner((New-Object Security.Principal.SecurityIdentifier('S-1-5-18')))
    foreach ($sid in @('S-1-5-18', 'S-1-5-32-544', $userSid)) {
        $rights = [Security.AccessControl.FileSystemRights]::FullControl
        if ($sid -ceq $userSid) { $rights = [Security.AccessControl.FileSystemRights]::ReadAndExecute }
        $rule = New-Object Security.AccessControl.FileSystemAccessRule(
            (New-Object Security.Principal.SecurityIdentifier($sid)), $rights,
            ([Security.AccessControl.InheritanceFlags]::ObjectInherit -bor
             [Security.AccessControl.InheritanceFlags]::ContainerInherit),
            [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow)
        $security.AddAccessRule($rule)
    }
    return $security
}
function Get-StorageIdentity { return [Security.Principal.WindowsIdentity]::GetCurrent().User.Value }
function New-SecureStorageDirectory([string]$path, $security) {
    if (-not [IO.Directory]::Exists([IO.Path]::GetDirectoryName($path))) { throw 'missing-parent' }
    $null = [IO.Directory]::CreateDirectory($path, $security)
}
function Set-SecureStorageDirectory([string]$path, $security) {
    [IO.Directory]::SetAccessControl($path, $security)
}
function Verify-StorageRoot([string]$root, [string]$readerSid = '') {
    foreach ($path in @($root, [IO.Path]::Combine($root, 'updates'), [IO.Path]::Combine($root, 'logs'))) {
        Assert-StoragePath $path
        if (-not (Test-TrustedStorage (Read-StorageDirectory $path) $true $readerSid)) { throw 'untrusted-storage' }
    }
}
function Initialize-StorageRoot([string]$root, [string]$userSid) {
    if ((Get-StorageIdentity) -cne 'S-1-5-18') { throw 'system-required' }
    $paths = @($root, [IO.Path]::Combine($root, 'updates'), [IO.Path]::Combine($root, 'logs'))
    # Audit all existing targets before the first mutation. Never take over a
    # user-owned or user-writable tree, nor recursively repair unrelated contents.
    foreach ($path in $paths) {
        Assert-StoragePath $path
        $state = Read-StorageDirectory $path
        if ($null -ne $state -and -not (Test-TrustedStorage $state $false '')) { throw 'unsafe-preexisting-storage' }
    }
    $security = New-StorageSecurity $userSid
    foreach ($path in $paths) {
        Assert-StoragePath $path
        $state = Read-StorageDirectory $path
        if ($null -eq $state) { New-SecureStorageDirectory $path $security }
        elseif (-not (Test-TrustedStorage $state $false '')) { throw 'storage-changed' }
        elseif (-not (Test-TrustedStorage $state $true $userSid)) { Set-SecureStorageDirectory $path $security }
        Assert-StoragePath $path
        if (-not (Test-TrustedStorage (Read-StorageDirectory $path) $true $userSid)) { throw 'storage-not-secured' }
    }
    Verify-StorageRoot $root $userSid
}
"""

_SECURITY_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
""" + _ACL_FUNCTIONS + r"""
try {
    $common = [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
    if (-not $common -or -not [IO.Path]::IsPathRooted($common)) { throw 'known-folder' }
    $root = [IO.Path]::Combine($common, 'youziauth-system')
    if ($env:YOUZIAUTH_STORAGE_MODE -ceq 'secure') {
        $sid = New-Object Security.Principal.SecurityIdentifier($env:YOUZIAUTH_STORAGE_SID)
        if ($sid.Value -cne $env:YOUZIAUTH_STORAGE_SID -or
            $sid.Value -cnotmatch '^S-1-(?:5-21|12-1)-[0-9]{1,10}-[0-9]{1,10}-[0-9]{1,10}-[0-9]{1,10}\z') { throw 'account-sid' }
        Initialize-StorageRoot $root $sid.Value
    } elseif ($env:YOUZIAUTH_STORAGE_MODE -ceq 'verify') {
        Verify-StorageRoot $root
    } else { throw 'invalid-mode' }
    [Console]::Out.Write((@{ root = $root } | ConvertTo-Json -Compress))
} catch { exit 1 }
"""
_ENCODED_SECURITY_SCRIPT = base64.b64encode(_SECURITY_SCRIPT.encode("utf-16le")).decode("ascii")


def _run_security_mode(mode: str, user_sid: str = "") -> Path:
    try:
        root = default_system_storage_path()
        powershell = _powershell_path()
        environment = os.environ.copy()
        environment.update({"YOUZIAUTH_STORAGE_MODE": mode, "YOUZIAUTH_STORAGE_SID": user_sid,
                            "PSModulePath": str(powershell.parent / "Modules")})
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", _ENCODED_SECURITY_SCRIPT],
            env=environment, shell=False, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, creationflags=0x08000000,
        )
        if result.returncode != 0 or len(result.stdout) > 4096:
            raise ValueError
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or set(value) != {"root"} or value["root"] != str(root):
            raise ValueError
        return root
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, RecursionError) as exc:
        raise SystemStorageError("SYSTEM storage is missing, redirected, or has unsafe permissions") from exc


def secure_system_storage(user_sid: str) -> Path:
    """Initialize only the fixed trusted directories, while running as SYSTEM."""
    match = _ACCOUNT_SID.fullmatch(user_sid) if isinstance(user_sid, str) else None
    if (match is None or any(int(part) > 0xFFFFFFFF for part in match.groups()) or
            (user_sid.startswith("S-1-5-21-") and int(match.group(4)) == 0)):
        raise SystemStorageError("A valid original task user SID is required")
    return _run_security_mode("secure", user_sid)


def verified_system_storage() -> Path:
    """Read-only check of SYSTEM owner, protected ACLs, and unredirected paths."""
    return _run_security_mode("verify")
