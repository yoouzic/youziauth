import base64
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

try:
    import input_storage as storage
except ModuleNotFoundError:
    storage = None


SID = 'S-1-5-21-100-200-300-1001'

RAW_SECURITY_QUERY = r'''
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using Microsoft.Win32.SafeHandles;
public static class InputAclQueryFixture {
    [DllImport("ntdll.dll")]
    private static extern int NtQuerySecurityObject(SafeFileHandle handle, uint info,
        byte[] buffer, uint size, out uint required);
    public static string Read(SafeFileHandle handle) {
        uint required;
        int status = NtQuerySecurityObject(handle, 7, null, 0, out required);
        var data = new byte[required];
        status = NtQuerySecurityObject(handle, 7, data, required, out required);
        if (status < 0) { throw new InvalidOperationException("NtQuerySecurityObject failed " + status); }
        return new RawSecurityDescriptor(data, 0).GetSddlForm(AccessControlSections.All);
    }
}
'@
function Read-RawInputAcl([string]$path) {
    $handle = [YouziauthInputNative]::Open($path, $false)
    try { return [InputAclQueryFixture]::Read($handle) } finally { $handle.Dispose() }
}
'''


class InputStorageTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(storage, 'the initial MSI must secure an editable private input root')

    def test_invalid_user_identities_stop_before_any_process(self):
        for sid in (None, '', 'S-1-5-18', 'S-1-5-32-544', SID + '\n', SID + ';whoami',
                    'S-1-5-21-100-200-300-0', 'S-1-5-21-100-200-300-4294967296'):
            with self.subTest(sid=sid), patch.object(storage.subprocess, 'run') as run:
                with self.assertRaises(storage.InputStorageError):
                    storage.secure_input_storage(sid)
                run.assert_not_called()

    def test_installer_uses_known_folder_and_fixed_script_not_user_path_environment(self):
        root = Path('C:/TrustedData/youziauth')
        result = subprocess.CompletedProcess([], 0, json.dumps({'root': str(root)}), '')
        with patch.object(storage, 'default_input_storage_path', return_value=root), \
             patch.object(storage, '_powershell_path', return_value=Path('C:/Windows/powershell.exe')), \
             patch.object(storage.subprocess, 'run', return_value=result) as run:
            self.assertEqual(storage.secure_input_storage(SID), root)
        command = run.call_args.args[0]
        self.assertEqual(command[1:4], ['-NoProfile', '-NonInteractive', '-EncodedCommand'])
        self.assertEqual(base64.b64decode(command[4]).decode('utf-16le'), storage._SECURITY_SCRIPT)
        self.assertNotIn(SID, storage._SECURITY_SCRIPT)
        self.assertEqual(run.call_args.kwargs['env']['YOUZIAUTH_INPUT_SID'], SID)
        self.assertFalse(run.call_args.kwargs['shell'])
        self.assertEqual(run.call_args.kwargs['stdin'], subprocess.DEVNULL)

    def test_untrusted_subprocess_output_has_no_fallback(self):
        root = Path('C:/TrustedData/youziauth')
        for code, stdout in ((1, ''), (0, '{}'), (0, 'not json'),
                             (0, json.dumps({'root': 'D:/attacker'}))):
            with self.subTest(code=code, stdout=stdout), \
                 patch.object(storage, 'default_input_storage_path', return_value=root), \
                 patch.object(storage, '_powershell_path', return_value=Path('C:/Windows/powershell.exe')), \
                 patch.object(storage.subprocess, 'run', return_value=subprocess.CompletedProcess([], code, stdout, '')):
                with self.assertRaises(storage.InputStorageError):
                    storage.secure_input_storage(SID)


class InputAclTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(storage, 'editable input ACL policy must be testable without elevation')

    def run_script(self, body, raw_query=False, **environment):
        source = ("$ErrorActionPreference='Stop'\n[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)\n" +
                  storage._ACL_FUNCTIONS + '\n' + (RAW_SECURITY_QUERY if raw_query else '') + body)
        env = os.environ.copy()
        env.update(environment)
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / 'fixture.ps1'
            script.write_text(source, encoding='utf-8-sig')
            result = subprocess.run([str(storage._powershell_path()), '-NoProfile', '-NonInteractive',
                '-ExecutionPolicy', 'Bypass', '-File', str(script)], env=env,
                stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15,
                creationflags=0x08000000)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_acl_grants_only_system_admin_full_and_original_user_modify(self):
        for is_directory in ('true', 'false'):
            with self.subTest(directory=is_directory):
                value = self.run_script(r'''
$acl = New-InputSecurity $env:TEST_SID ($env:TEST_DIRECTORY -ceq 'true')
@{ owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value;
   protected = $acl.AreAccessRulesProtected;
   rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) |
        ForEach-Object { @{ sid = $_.IdentityReference.Value; rights = [int]$_.FileSystemRights;
            flags = [int]$_.InheritanceFlags } }) } | ConvertTo-Json -Depth 5 -Compress
''', TEST_SID=SID, TEST_DIRECTORY=is_directory)
                self.assertEqual(value['owner'], 'S-1-5-18')
                self.assertTrue(value['protected'])
                self.assertEqual({rule['sid']: rule['rights'] for rule in value['rules']},
                                 {'S-1-5-18': 2032127, 'S-1-5-32-544': 2032127, SID: 1245631})
                self.assertEqual({rule['flags'] for rule in value['rules']},
                                 {3 if is_directory == 'true' else 0})

    def test_unprivileged_initializer_rejects_before_creating_any_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'youziauth'
            value = self.run_script(r'''
$failed = $false
try { Initialize-InputRoot $env:TEST_ROOT $env:TEST_SID } catch { $failed = $true }
@{ failed = $failed; exists = [IO.Directory]::Exists($env:TEST_ROOT) } | ConvertTo-Json -Compress
''', TEST_ROOT=str(root), TEST_SID=SID)
            self.assertTrue(value['failed'])
            self.assertFalse(value['exists'])

    def test_real_junction_is_rejected_without_touching_target_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target = base / 'target'
            target.mkdir()
            marker = target / 'credential.dat'
            marker.write_bytes(b'keep cipher bytes')
            link = base / 'redirect'
            try:
                value = self.run_script(r'''
$null = New-Item -ItemType Junction -Path $env:TEST_LINK -Target $env:TEST_TARGET
$failed = $false
try { Assert-InputPath ([IO.Path]::Combine($env:TEST_LINK, 'youziauth')) } catch { $failed = $true }
@{ failed = $failed } | ConvertTo-Json -Compress
''', TEST_LINK=str(link), TEST_TARGET=str(target))
                self.assertTrue(value['failed'])
                self.assertEqual(marker.read_bytes(), b'keep cipher bytes')
                self.assertFalse((target / 'youziauth').exists())
            finally:
                if link.exists():
                    os.rmdir(link)

    def test_security_script_parses_without_mutations(self):
        value = self.run_script(r'''
$tokens = $null; $errors = $null
$null = [Management.Automation.Language.Parser]::ParseInput($env:TEST_SOURCE, [ref]$tokens, [ref]$errors)
@{ errors = @($errors | ForEach-Object { $_.Message }) } | ConvertTo-Json -Compress
''', TEST_SOURCE=storage._SECURITY_SCRIPT)
        self.assertEqual(value['errors'], [])

    def test_existing_input_contents_survive_native_handle_acl_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'youziauth'
            root.mkdir()
            values = {'config.ini': b'[auth]\nusername=fixture\n',
                      'credential.dat': b'fixture protected ciphertext',
                      'other.txt': b'preserve unrelated content'}
            for filename, data in values.items():
                (root / filename).write_bytes(data)
            value = self.run_script(r'''
$user = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$originalSecurity = (Get-Command New-InputSecurity).ScriptBlock
# A normal-user fixture may apply only its own owner SID. All native handle,
# path audit and DACL operations remain the production implementation.
function Get-InputIdentity { return 'S-1-5-18' }
function New-InputSecurity([string]$sid, [bool]$directory) {
    $acl = & $originalSecurity $sid $directory
    $acl.SetOwner((New-Object Security.Principal.SecurityIdentifier($user)))
    return $acl
}
Initialize-InputRoot $env:TEST_ROOT $user
$outcomes = @()
foreach ($name in @('', 'config.ini', 'credential.dat')) {
    $path = [IO.Path]::Combine($env:TEST_ROOT, $name)
    if ($name -ceq '') { $acl = [IO.Directory]::GetAccessControl($path) }
    else { $acl = [IO.File]::GetAccessControl($path) }
    $outcomes += @{ name = $name; protected = $acl.AreAccessRulesProtected;
        readers = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) |
            ForEach-Object { $_.IdentityReference.Value }) }
}
@{ user = $user; outcomes = $outcomes } | ConvertTo-Json -Depth 5 -Compress
''', TEST_ROOT=str(root))
            for item in value['outcomes']:
                self.assertTrue(item['protected'])
                self.assertEqual(set(item['readers']), {'S-1-5-18', 'S-1-5-32-544', value['user']})
            for filename, data in values.items():
                self.assertEqual((root / filename).read_bytes(), data)

    def test_hardlinked_credential_is_rejected_before_any_acl_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'youziauth'
            root.mkdir()
            original = Path(temporary) / 'outside.dat'
            original.write_bytes(b'preserve outside fixture')
            os.link(original, root / 'credential.dat')
            value = self.run_script(r'''
$user = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$before = [IO.Directory]::GetAccessControl($env:TEST_ROOT).GetSecurityDescriptorSddlForm('All')
$script:writes = 0
function Get-InputIdentity { return 'S-1-5-18' }
function Set-InputEntrySecurity { $script:writes++ }
$failed = $false
try { Initialize-InputRoot $env:TEST_ROOT $user } catch { $failed = $true }
$after = [IO.Directory]::GetAccessControl($env:TEST_ROOT).GetSecurityDescriptorSddlForm('All')
@{ failed = $failed; writes = $script:writes; sameAcl = ($before -ceq $after) } | ConvertTo-Json -Compress
''', TEST_ROOT=str(root))
            self.assertTrue(value['failed'])
            self.assertEqual(value['writes'], 0)
            self.assertTrue(value['sameAcl'])
            self.assertEqual(original.read_bytes(), b'preserve outside fixture')

    def test_open_input_handle_blocks_leaf_replacement_until_security_finishes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'config.ini'
            path.write_bytes(b'fixed fixture bytes')
            value = self.run_script(r'''
$user = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$entry = Open-InputEntry $env:TEST_FILE $false $user
$blocked = $false
try {
    try { [IO.File]::Delete($env:TEST_FILE) } catch { $blocked = $true }
} finally { $entry.Handle.Dispose() }
@{ blocked = $blocked } | ConvertTo-Json -Compress
''', TEST_FILE=str(path))
            self.assertTrue(value['blocked'])
            self.assertEqual(path.read_bytes(), b'fixed fixture bytes')

    def test_root_acl_update_preserves_existing_descendant_and_external_hardlink_acls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'youziauth'
            nested = root / 'unrelated-dir'
            nested.mkdir(parents=True)
            outside = Path(temporary) / 'outside.dat'
            outside.write_bytes(b'outside fixture remains intact')
            os.link(outside, root / 'unrelated.dat')
            self.assertEqual(outside.stat().st_ino, (root / 'unrelated.dat').stat().st_ino)
            (nested / 'other.dat').write_bytes(b'nested fixture remains intact')
            (root / 'config.ini').write_bytes(b'[auth]\nusername=fixture\n')
            (root / 'credential.dat').write_bytes(b'fixture ciphertext')
            value = self.run_script(r'''
$user = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$originalSecurity = (Get-Command New-InputSecurity).ScriptBlock
function Get-InputIdentity { return 'S-1-5-18' }
function New-InputSecurity([string]$sid, [bool]$directory) {
    $acl = & $originalSecurity $sid $directory
    $acl.SetOwner((New-Object Security.Principal.SecurityIdentifier($user)))
    return $acl
}
$paths = @($env:TEST_OUTSIDE, [IO.Path]::Combine($env:TEST_ROOT, 'unrelated.dat'),
    [IO.Path]::Combine($env:TEST_ROOT, 'unrelated-dir'),
    [IO.Path]::Combine($env:TEST_ROOT, 'unrelated-dir', 'other.dat'))
$before = @{}
foreach ($path in $paths) {
    # GetNamedSecurityInfo can compute a different inheritance view after a
    # parent changes. NtQuerySecurityObject checks this object's stored SD.
    $before[$path] = Read-RawInputAcl $path
}
$outsideNamedBefore = [IO.File]::GetAccessControl($env:TEST_OUTSIDE).GetSecurityDescriptorSddlForm('All')
Initialize-InputRoot $env:TEST_ROOT $user
$outcomes = @()
foreach ($path in $paths) {
    $after = Read-RawInputAcl $path
    $outcomes += @{ path = $path; unchanged = ($before[$path] -ceq $after);
        before = $before[$path]; after = $after }
}
$outsideAgain = [IO.File]::GetAccessControl($env:TEST_OUTSIDE).GetSecurityDescriptorSddlForm('All')
$fresh = [IO.Path]::Combine($env:TEST_ROOT, 'new-user-file.dat')
[IO.File]::WriteAllBytes($fresh, [byte[]](1, 2, 3))
$newAcl = [IO.File]::GetAccessControl($fresh)
$rootAcl = [IO.Directory]::GetAccessControl($env:TEST_ROOT)
@{ outcomes = $outcomes; outsideNamedBefore = $outsideNamedBefore; outsideAgain = $outsideAgain;
    user = $user; rootProtected = $rootAcl.AreAccessRulesProtected;
    newRules = @($newAcl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) |
        ForEach-Object { @{ sid = $_.IdentityReference.Value; rights = [int]$_.FileSystemRights;
            inherited = $_.IsInherited } }) } | ConvertTo-Json -Depth 6 -Compress
''', raw_query=True, TEST_ROOT=str(root), TEST_OUTSIDE=str(outside))
            self.assertEqual(outside.stat().st_ino, (root / 'unrelated.dat').stat().st_ino,
                             'hardlink identity must remain intact')
            self.assertTrue(all(item['unchanged'] for item in value['outcomes']), value)
            self.assertEqual(value['outsideAgain'], value['outsideNamedBefore'])
            self.assertTrue(value['rootProtected'])
            self.assertEqual({rule['sid']: rule['rights'] for rule in value['newRules']},
                             {'S-1-5-18': 2032127, 'S-1-5-32-544': 2032127, value['user']: 1245631})
            self.assertTrue(all(rule['inherited'] for rule in value['newRules']))
            self.assertEqual(outside.read_bytes(), b'outside fixture remains intact')
            self.assertEqual((nested / 'other.dat').read_bytes(), b'nested fixture remains intact')


if __name__ == '__main__':
    unittest.main()
