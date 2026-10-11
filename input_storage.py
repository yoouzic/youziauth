# Copyright (C) 2026 yoouzic
# SPDX-License-Identifier: GPL-3.0-only

"""Initialize the original user's private, editable input root during MSI commit.

SYSTEM output belongs in system_storage instead. This helper only establishes
directory/config/credential security; it never creates or rewrites user data.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from pathlib import Path

from system_storage import _powershell_path, default_system_storage_path


_ACCOUNT_SID = re.compile(r"S-1-(?:5-21|12-1)-([0-9]{1,10})-([0-9]{1,10})-([0-9]{1,10})-([0-9]{1,10})")


class InputStorageError(RuntimeError):
    """The original user's editable input boundary could not be secured."""


def default_input_storage_path() -> Path:
    return default_system_storage_path().parent / "youziauth"


_ACL_FUNCTIONS = r'''
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class YouziauthInputNative {
    [StructLayout(LayoutKind.Sequential)]
    public struct FileInfo {
        public uint Attributes; public System.Runtime.InteropServices.ComTypes.FILETIME Created;
        public System.Runtime.InteropServices.ComTypes.FILETIME Accessed;
        public System.Runtime.InteropServices.ComTypes.FILETIME Written;
        public uint Volume; public uint SizeHigh; public uint SizeLow; public uint Links;
        public uint IndexHigh; public uint IndexLow;
    }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    private static extern SafeFileHandle CreateFileW(string path, uint access, uint share,
        IntPtr security, uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError=true)]
    private static extern bool GetFileInformationByHandle(SafeFileHandle handle, out FileInfo info);
    [DllImport("advapi32.dll", SetLastError=true)]
    private static extern bool GetSecurityDescriptorOwner(IntPtr descriptor, out IntPtr owner, out bool defaulted);
    [DllImport("advapi32.dll", SetLastError=true)]
    private static extern bool GetSecurityDescriptorDacl(IntPtr descriptor, out bool present, out IntPtr dacl, out bool defaulted);
    [DllImport("advapi32.dll", SetLastError=true)]
    private static extern uint SetSecurityInfo(SafeFileHandle handle, int type, uint info,
        IntPtr owner, IntPtr group, IntPtr dacl, IntPtr sacl);
    public static SafeFileHandle Open(string path, bool writable, bool directory = false) {
        // OPEN_REPARSE_POINT prevents following a last-moment leaf substitution.
        // No FILE_SHARE_DELETE keeps this exact object from being replaced.
        // READ_DATA/LIST_DIRECTORY is necessary for the share checks: handles
        // requesting metadata rights alone do not stop DeleteFile replacement.
        // SetSecurityInfo explicitly skips child propagation for a handle
        // opened with exact MAXIMUM_ALLOWED. Only the root uses this documented
        // mode; fixed input files keep their precise required access rights.
        uint access = writable ? (directory ? 0x02000000u : 0x000E0081u) : 0x00020081u;
        var handle = CreateFileW(path, access, 3, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero);
        if (handle.IsInvalid) { handle.Dispose(); throw new Win32Exception(Marshal.GetLastWin32Error()); }
        return handle;
    }
    public static FileInfo Inspect(SafeFileHandle handle) {
        FileInfo value;
        if (!GetFileInformationByHandle(handle, out value)) { throw new Win32Exception(Marshal.GetLastWin32Error()); }
        return value;
    }
    public static void SetSecurity(SafeFileHandle handle, byte[] descriptor) {
        // The directory handle's MAXIMUM_ALLOWED mode prevents this Win32 API
        // from rewriting any existing descendant (including unrelated links).
        // Future children still inherit the root's restricted three ACEs.
        var pin = GCHandle.Alloc(descriptor, GCHandleType.Pinned);
        try {
            IntPtr owner, dacl; bool ignored, present;
            if (!GetSecurityDescriptorOwner(pin.AddrOfPinnedObject(), out owner, out ignored) ||
                !GetSecurityDescriptorDacl(pin.AddrOfPinnedObject(), out present, out dacl, out ignored) || !present) {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            uint result = SetSecurityInfo(handle, 1, 0x80000005u, owner, IntPtr.Zero, dacl, IntPtr.Zero);
            if (result != 0) { throw new Win32Exception((int)result); }
        } finally { pin.Free(); }
    }
}
'@
function Get-InputIdentity { return [Security.Principal.WindowsIdentity]::GetCurrent().User.Value }
function Assert-InputPath([string]$path) {
    $current = New-Object IO.DirectoryInfo([IO.Path]::GetFullPath($path))
    while ($null -ne $current) {
        try {
            $attributes = [IO.File]::GetAttributes($current.FullName)
            if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
                ($attributes -band [IO.FileAttributes]::Directory) -eq 0) { throw 'redirected-input-path' }
        } catch [IO.FileNotFoundException] { }
          catch [IO.DirectoryNotFoundException] { }
        $current = $current.Parent
    }
}
function New-InputSecurity([string]$userSid, [bool]$directory) {
    if ($directory) { $security = New-Object Security.AccessControl.DirectorySecurity }
    else { $security = New-Object Security.AccessControl.FileSecurity }
    $security.SetAccessRuleProtection($true, $false)
    $security.SetOwner((New-Object Security.Principal.SecurityIdentifier('S-1-5-18')))
    foreach ($sid in @('S-1-5-18', 'S-1-5-32-544', $userSid)) {
        $rights = [Security.AccessControl.FileSystemRights]::FullControl
        if ($sid -ceq $userSid) { $rights = [Security.AccessControl.FileSystemRights]::Modify }
        $inheritance = [Security.AccessControl.InheritanceFlags]::None
        if ($directory) { $inheritance = [Security.AccessControl.InheritanceFlags]::ObjectInherit -bor
                                       [Security.AccessControl.InheritanceFlags]::ContainerInherit }
        $security.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
            (New-Object Security.Principal.SecurityIdentifier($sid)), $rights, $inheritance,
            [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow)))
    }
    return $security
}
function Open-InputEntry([string]$path, [bool]$directory, [string]$userSid) {
    $handle = [YouziauthInputNative]::Open($path, $true, $directory)
    try {
        $info = [YouziauthInputNative]::Inspect($handle)
        if (($info.Attributes -band 1024) -ne 0 -or
            ((($info.Attributes -band 16) -ne 0) -ne $directory) -or
            (-not $directory -and $info.Links -ne 1)) { throw 'unsafe-input-entry' }
        if ($directory) { $acl = [IO.Directory]::GetAccessControl($path) }
        else { $acl = [IO.File]::GetAccessControl($path) }
        $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
        if (@('S-1-5-18', 'S-1-5-32-544', $userSid) -cnotcontains $owner) { throw 'unexpected-input-owner' }
        return @{ Path = $path; Directory = $directory; Handle = $handle }
    } catch { $handle.Dispose(); throw }
}
function Set-InputEntrySecurity($entry, [string]$userSid) {
    $security = New-InputSecurity $userSid $entry.Directory
    [YouziauthInputNative]::SetSecurity($entry.Handle, $security.GetSecurityDescriptorBinaryForm())
}
function Assert-InputTree([string]$root) {
    $pending = New-Object 'Collections.Generic.Stack[string]'
    $pending.Push($root); $count = 0
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        foreach ($path in [IO.Directory]::EnumerateFileSystemEntries($directory)) {
            $count++; if ($count -gt 10000) { throw 'input-tree-too-large' }
            $attributes = [IO.File]::GetAttributes($path)
            if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'redirected-input-entry' }
            if (($attributes -band [IO.FileAttributes]::Directory) -ne 0) { $pending.Push($path) }
        }
    }
}
function Initialize-InputRoot([string]$root, [string]$userSid) {
    if ((Get-InputIdentity) -cne 'S-1-5-18') { throw 'system-required' }
    Assert-InputPath $root
    $entries = New-Object 'Collections.Generic.List[object]'
    try {
        if ([IO.Directory]::Exists($root)) {
            $entries.Add((Open-InputEntry $root $true $userSid))
            Assert-InputTree $root
            foreach ($filename in @('config.ini', 'credential.dat')) {
                $path = [IO.Path]::Combine($root, $filename)
                try { $attributes = [IO.File]::GetAttributes($path) }
                catch [IO.FileNotFoundException] { continue }
                catch [IO.DirectoryNotFoundException] { continue }
                $entries.Add((Open-InputEntry $path $false $userSid))
            }
        } else {
            if (-not [IO.Directory]::Exists([IO.Path]::GetDirectoryName($root))) { throw 'missing-input-parent' }
            # An atomic secure descriptor also prevents a newly created root
            # inheriting ProgramData's generic-user read access.
            $null = [IO.Directory]::CreateDirectory($root, (New-InputSecurity $userSid $true))
            Assert-InputPath $root
            $entries.Add((Open-InputEntry $root $true $userSid))
        }
        # All existing fixed entries are audited and held before the first ACL
        # change. No user file is opened for content writes or copied elsewhere.
        foreach ($entry in $entries) { Set-InputEntrySecurity $entry $userSid }
    } finally {
        foreach ($entry in $entries) { $entry.Handle.Dispose() }
    }
}
'''

_SECURITY_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
''' + _ACL_FUNCTIONS + r'''
try {
    $common = [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
    if (-not $common -or -not [IO.Path]::IsPathRooted($common)) { throw 'known-folder' }
    $sid = New-Object Security.Principal.SecurityIdentifier($env:YOUZIAUTH_INPUT_SID)
    if ($sid.Value -cne $env:YOUZIAUTH_INPUT_SID -or
        $sid.Value -cnotmatch '^S-1-(?:5-21|12-1)-[0-9]{1,10}-[0-9]{1,10}-[0-9]{1,10}-[0-9]{1,10}\z') { throw 'account-sid' }
    $root = [IO.Path]::Combine($common, 'youziauth')
    Initialize-InputRoot $root $sid.Value
    [Console]::Out.Write((@{ root = $root } | ConvertTo-Json -Compress))
} catch { exit 1 }
'''
_ENCODED_SECURITY_SCRIPT = base64.b64encode(_SECURITY_SCRIPT.encode('utf-16le')).decode('ascii')


def secure_input_storage(user_sid: str) -> Path:
    """MSI SYSTEM helper: grant the original user Modify, preserving all contents."""
    match = _ACCOUNT_SID.fullmatch(user_sid) if isinstance(user_sid, str) else None
    if (match is None or any(int(part) > 0xFFFFFFFF for part in match.groups()) or
            (user_sid.startswith('S-1-5-21-') and int(match.group(4)) == 0)):
        raise InputStorageError('A valid original task user SID is required')
    try:
        root = default_input_storage_path()
        powershell = _powershell_path()
        environment = os.environ.copy()
        environment.update({'YOUZIAUTH_INPUT_SID': user_sid,
                            'PSModulePath': str(powershell.parent / 'Modules')})
        result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-EncodedCommand',
            _ENCODED_SECURITY_SCRIPT], env=environment, shell=False, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=30, creationflags=0x08000000)
        if result.returncode != 0 or len(result.stdout) > 4096:
            raise ValueError
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or set(value) != {'root'} or value['root'] != str(root):
            raise ValueError
        return root
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, RecursionError, RuntimeError) as exc:
        raise InputStorageError('User input storage is redirected, shared, or could not be secured') from exc
