import base64
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

try:
    import system_storage as storage
except ModuleNotFoundError:
    storage = None


SID = "S-1-5-21-100-200-300-1001"


class SystemStorageTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(storage, "privileged writes require a separate protected storage helper")

    def test_known_folder_lookup_ignores_user_environment_and_does_not_spawn(self):
        shell = Mock()
        def known_folder(window, folder, token, flags, buffer):
            self.assertEqual(folder, 0x23)
            self.assertEqual(flags, 0)
            buffer.value = r"C:\TrustedProgramData"
            return 0
        shell.SHGetFolderPathW.side_effect = known_folder
        with patch.object(storage.ctypes, "WinDLL", return_value=shell), \
             patch.dict(os.environ, {"PROGRAMDATA": r"D:\attacker", "SystemDrive": "D:"}), \
             patch.object(storage.subprocess, "run") as run:
            self.assertEqual(storage.default_system_storage_path(),
                             Path(r"C:\TrustedProgramData\youziauth-system"))
        run.assert_not_called()

    def test_failed_known_folder_lookup_has_no_environment_fallback(self):
        shell = Mock()
        shell.SHGetFolderPathW.return_value = -1
        with patch.object(storage.ctypes, "WinDLL", return_value=shell):
            with self.assertRaises(storage.SystemStorageError):
                storage.default_system_storage_path()

    def test_invalid_sids_stop_before_any_process_or_write(self):
        for value in (None, "", "S-1-5-18", "S-1-5-32-544", SID + "\n", SID + ";whoami",
                      "S-1-5-21-100-200-300-0", "S-1-5-21-100-200-300-4294967296",
                      "S-1-5-21-100-200-300-\u0661"):
            with self.subTest(sid=value), patch.object(storage.subprocess, "run") as run:
                with self.assertRaises(storage.SystemStorageError):
                    storage.secure_system_storage(value)
                self.assertFalse(run.called, "invalid SIDs must stop before the subprocess boundary")

    def invoke(self, mode, *, output=None, returncode=0):
        root = Path(r"C:\ProgramData\youziauth-system")
        completed = subprocess.CompletedProcess([], returncode,
            json.dumps({"root": str(root)}) if output is None else output, "")
        with patch.object(storage, "default_system_storage_path", return_value=root), \
             patch.object(storage, "_powershell_path", return_value=Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")), \
             patch.object(storage.subprocess, "run", return_value=completed) as run:
            value = (storage.secure_system_storage(SID) if mode == "secure"
                     else storage.verified_system_storage())
        return value, run.call_args

    def test_initializer_uses_fixed_script_and_sid_as_data(self):
        value, call = self.invoke("secure")
        command = call.args[0]
        self.assertEqual(command[1:4], ["-NoProfile", "-NonInteractive", "-EncodedCommand"])
        source = base64.b64decode(command[4]).decode("utf-16le")
        self.assertEqual(source, storage._SECURITY_SCRIPT)
        self.assertNotIn(SID, source)
        self.assertEqual(call.kwargs["env"]["YOUZIAUTH_STORAGE_SID"], SID)
        self.assertEqual(call.kwargs["env"]["YOUZIAUTH_STORAGE_MODE"], "secure")
        self.assertFalse(call.kwargs["shell"])
        self.assertEqual(call.kwargs["creationflags"], 0x08000000)
        self.assertEqual(value.name, "youziauth-system")

    def test_verifier_selects_read_only_mode_and_never_creates_directories(self):
        with patch.object(Path, "mkdir", side_effect=AssertionError("verification must be read-only")):
            value, call = self.invoke("verify")
        self.assertEqual(call.kwargs["env"]["YOUZIAUTH_STORAGE_MODE"], "verify")
        self.assertEqual(call.kwargs["env"]["YOUZIAUTH_STORAGE_SID"], "")
        self.assertEqual(value.name, "youziauth-system")

    def test_failed_verification_has_no_user_writable_fallback(self):
        for output, code in (("{}", 0), ("not json", 0),
                             (json.dumps({"root": r"D:\attacker"}), 0), ("", 1)):
            with self.subTest(output=output, code=code):
                with self.assertRaises(storage.SystemStorageError):
                    self.invoke("verify", output=output, returncode=code)


class NativeAclTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(storage, "protected ACL policy must be testable before privileged use")

    def run_script(self, body, **environment):
        script = ("$ErrorActionPreference='Stop'\n$ProgressPreference='SilentlyContinue'\n"
                  "[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)\n" +
                  storage._ACL_FUNCTIONS + "\n" + body)
        env = os.environ.copy()
        env.update(environment)
        result = subprocess.run([str(storage._powershell_path()), "-NoProfile", "-NonInteractive",
                                 "-EncodedCommand", base64.b64encode(script.encode("utf-16le")).decode()],
                                env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=15, creationflags=0x08000000)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_acl_template_grants_only_system_admin_full_and_user_read_execute(self):
        for sid in (SID, "S-1-12-1-100-200-300-400"):
            with self.subTest(sid=sid):
                value = self.run_script(r'''
$security = New-StorageSecurity $env:TEST_SID
$state = Convert-StorageSecurity $security ([int][IO.FileAttributes]::Directory)
@{ state = $state; trusted = (Test-TrustedStorage $state $true $env:TEST_SID) } |
    ConvertTo-Json -Depth 6 -Compress
''', TEST_SID=sid)
                self.assertTrue(value["trusted"])
                self.assertEqual(value["state"]["Owner"], "S-1-5-18")
                self.assertTrue(value["state"]["Protected"])
                rights = {rule["Sid"]: rule["Rights"] for rule in value["state"]["Rules"]}
                self.assertEqual(rights, {"S-1-5-18": 2032127, "S-1-5-32-544": 2032127, sid: 1179817})

    def test_user_owned_reparse_unprotected_and_writable_acl_are_rejected(self):
        value = self.run_script(r'''
$outcomes = @()
foreach ($kind in @('owner', 'reparse', 'inheritance', 'write', 'delete', 'change-acl', 'extra')) {
    $state = Convert-StorageSecurity (New-StorageSecurity $env:TEST_SID) ([int][IO.FileAttributes]::Directory)
    switch ($kind) {
        'owner' { $state.Owner = $env:TEST_SID }
        'reparse' { $state.Attributes = $state.Attributes -bor 1024 }
        'inheritance' { $state.Protected = $false }
        'write' { $state.Rules[2].Rights = $state.Rules[2].Rights -bor 2 }
        'delete' { $state.Rules[2].Rights = $state.Rules[2].Rights -bor 65536 }
        'change-acl' { $state.Rules[2].Rights = $state.Rules[2].Rights -bor 262144 }
        'extra' { $state.Rules += $state.Rules[2] }
    }
    $outcomes += @{ kind = $kind; trusted = (Test-TrustedStorage $state $true '') }
}
$outcomes | ConvertTo-Json -Depth 6 -Compress
''', TEST_SID=SID)
        self.assertTrue(all(not item["trusted"] for item in value))

    def test_normal_user_cannot_initialize_or_change_directory_acl(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "youziauth-system"
            value = self.run_script(r'''
$failed = $false
try { Initialize-StorageRoot $env:TEST_ROOT $env:TEST_SID } catch { $failed = $true }
@{ failed = $failed; exists = [IO.Directory]::Exists($env:TEST_ROOT) } | ConvertTo-Json -Compress
''', TEST_ROOT=str(root), TEST_SID=SID)
            self.assertTrue(value["failed"])
            self.assertFalse(value["exists"])

    def test_read_only_verification_rejects_precreated_user_owned_tree_without_acl_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "youziauth-system"
            (root / "updates").mkdir(parents=True)
            (root / "logs").mkdir()
            value = self.run_script(r'''
$before = [IO.Directory]::GetAccessControl($env:TEST_ROOT).GetSecurityDescriptorSddlForm('All')
$failed = $false
try { Verify-StorageRoot $env:TEST_ROOT } catch { $failed = $true }
$after = [IO.Directory]::GetAccessControl($env:TEST_ROOT).GetSecurityDescriptorSddlForm('All')
@{ failed = $failed; unchanged = ($before -ceq $after) } | ConvertTo-Json -Compress
''', TEST_ROOT=str(root))
            self.assertTrue(value["failed"])
            self.assertTrue(value["unchanged"])

    def test_script_parses_without_running_mutations(self):
        value = self.run_script(r'''
$tokens = $null
$errors = $null
$null = [Management.Automation.Language.Parser]::ParseInput($env:TEST_SOURCE, [ref]$tokens, [ref]$errors)
@{ errors = @($errors | ForEach-Object { $_.Message }) } | ConvertTo-Json -Compress
''', TEST_SOURCE=storage._SECURITY_SCRIPT)
        self.assertEqual(value["errors"], [])

    def test_initializer_audits_all_targets_before_creating_with_secure_descriptors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "youziauth-system"
            value = self.run_script(r'''
$script:states = @{}
$script:created = @()
$script:reads = 0
function Get-StorageIdentity { return 'S-1-5-18' }
function Assert-StoragePath { }
function Read-StorageDirectory($path) { $script:reads++; return $script:states[$path] }
function New-SecureStorageDirectory($path, $security) {
    if ($script:reads -lt 3) { throw 'mutation-before-complete-audit' }
    $state = Convert-StorageSecurity $security 16
    if (-not (Test-TrustedStorage $state $true $env:TEST_SID)) { throw 'unsafe-creation-descriptor' }
    $script:states[$path] = $state
    $script:created += $path
}
function Set-SecureStorageDirectory { throw 'unexpected-permission-repair' }
Initialize-StorageRoot $env:TEST_ROOT $env:TEST_SID
@{ created = @($script:created); onDisk = [IO.Directory]::Exists($env:TEST_ROOT) } | ConvertTo-Json -Compress
''', TEST_ROOT=str(root), TEST_SID=SID)
            self.assertEqual(value["created"], [str(root), str(root / "updates"), str(root / "logs")])
            self.assertFalse(value["onDisk"], "native mutations must remain injected in this test")

    def test_unsafe_preexisting_child_stops_before_any_write(self):
        value = self.run_script(r'''
$script:states = @{}
$root = $env:TEST_ROOT
$bad = Convert-StorageSecurity (New-StorageSecurity $env:TEST_SID) 16
$bad.Owner = $env:TEST_SID
$script:states[[IO.Path]::Combine($root, 'logs')] = $bad
$script:writes = 0
function Get-StorageIdentity { return 'S-1-5-18' }
function Assert-StoragePath { }
function Read-StorageDirectory($path) { return $script:states[$path] }
function New-SecureStorageDirectory { $script:writes++ }
function Set-SecureStorageDirectory { $script:writes++ }
$failed = $false
try { Initialize-StorageRoot $root $env:TEST_SID } catch { $failed = $true }
@{ failed = $failed; writes = $script:writes } | ConvertTo-Json -Compress
''', TEST_ROOT=r"C:\ProgramData\youziauth-system", TEST_SID=SID)
        self.assertTrue(value["failed"])
        self.assertEqual(value["writes"], 0)

    def test_real_junction_ancestor_is_rejected_without_touching_its_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target = base / "target"
            target.mkdir()
            marker = target / "preserve.txt"
            marker.write_text("untouched", encoding="utf-8")
            junction = base / "redirect"
            try:
                value = self.run_script(r'''
$null = New-Item -ItemType Junction -Path $env:TEST_LINK -Target $env:TEST_TARGET
$failed = $false
try { Assert-StoragePath ([IO.Path]::Combine($env:TEST_LINK, 'youziauth-system')) }
catch { $failed = $true }
@{ failed = $failed } | ConvertTo-Json -Compress
''', TEST_LINK=str(junction), TEST_TARGET=str(target))
                self.assertTrue(value["failed"])
                self.assertEqual(marker.read_text(encoding="utf-8"), "untouched")
                self.assertFalse((target / "youziauth-system").exists())
            finally:
                if junction.exists():
                    os.rmdir(junction)


if __name__ == "__main__":
    unittest.main()
