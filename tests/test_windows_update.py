# Copyright (C) 2026 yoouzic
# SPDX-License-Identifier: GPL-3.0-only

"""The updater must accept only releases signed by the pinned key.

These tests drive the real verification path with a throwaway key patched in for
the duration of each test. The production pin is never used as a fixture, and no
private key ever enters the repository.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import ed25519
import windows_update as update


VERSION = "1.2.3"
UPGRADE_CODE = "{D029E636-7E7E-42EE-8B38-C2D455AD2AA1}"
PROPERTIES = {
    "ProductName": "youziauth",
    "Manufacturer": "yoouzic",
    "ProductVersion": VERSION,
    "UpgradeCode": UPGRADE_CODE,
}


def encoded(script):
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


class SignedRelease:
    """A throwaway release key plus a signed package, rebuilt on demand."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.secret = ed25519.generate_secret_key()
        self.public = ed25519.derive_public_key(self.secret)
        self.msi = root / "package.msi"
        self.msi.write_bytes(b"not an installer\x00signed fixture")
        self.sign()

    def sign(self, version: str = VERSION, contents: bytes | None = None) -> None:
        if contents is not None:
            self.msi.write_bytes(contents)
        self.version = version
        self.sha256 = hashlib.sha256(self.msi.read_bytes()).hexdigest()
        self.size = self.msi.stat().st_size
        self.payload = update.canonical_payload(version, self.sha256, self.size)
        self.signature = ed25519.sign(self.payload, self.secret)

    def request(self, **overrides) -> dict:
        body = {
            "msi": str(self.msi), "version": self.version, "bytes": self.size,
            "sha256": self.sha256, "signature": self.signature.hex(),
            "payload": self.payload.decode("ascii"), "properties": dict(PROPERTIES),
        }
        body.update(overrides)
        return body

    def pin(self, test: unittest.TestCase) -> None:
        """Make the module under test trust this fixture key instead of production."""
        patched = patch.object(update, "PUBLIC_KEY_B64",
                               base64.b64encode(self.public).decode("ascii"))
        patched.start()
        test.addCleanup(patched.stop)


class VerifierTests(unittest.TestCase):
    """The frozen-application verifier, exercised directly."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.release = SignedRelease(self.root)
        self.release.pin(self)

    def run_verifier(self, request: dict | None = None, **overrides) -> int:
        body = self.release.request(**overrides) if request is None else request
        source = self.root / "request.json"
        report = self.root / "report.json"
        report.unlink(missing_ok=True)
        source.write_text(json.dumps(body), encoding="utf-8")
        return update.verifier_main(source, report)

    def test_valid_signature_is_accepted_and_reported(self):
        source = self.root / "request.json"
        report = self.root / "report.json"
        source.write_text(json.dumps(self.release.request()), encoding="utf-8")
        self.assertEqual(update.verifier_main(source, report), 0)
        body = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(body["digest"], self.release.sha256)
        self.assertEqual(body["signature"], self.release.signature.hex())
        self.assertEqual(body["payload"], self.release.payload.decode("ascii"))
        self.assertEqual(body["properties"], PROPERTIES)

    def test_tampered_payload_is_rejected(self):
        for key, value in (("version", "9.9.9"), ("bytes", 1), ("product", "other"),
                           ("manufacturer", "other"),
                           ("upgrade", "{00000000-0000-0000-0000-000000000000}")):
            with self.subTest(key=key):
                payload = json.loads(self.release.payload)
                payload[key] = value
                self.assertEqual(self.run_verifier(payload=json.dumps(payload, separators=(",", ":"))), 12)

    def test_payload_must_agree_with_the_requested_version_and_size(self):
        other = json.loads(self.release.payload)
        other["sha256"] = "0" * 64
        self.assertEqual(self.run_verifier(payload=json.dumps(other, separators=(",", ":"))), 12)
        bad = json.loads(self.release.payload)
        bad["bytes"] = self.release.size + 1
        self.assertEqual(self.run_verifier(payload=json.dumps(bad, separators=(",", ":"))), 12)

    def test_signature_from_another_key_is_rejected(self):
        forged = ed25519.sign(self.release.payload, ed25519.generate_secret_key())
        self.assertEqual(self.run_verifier(signature=forged.hex()), 12)

    def test_signature_for_a_different_package_is_rejected(self):
        # A genuine signature that covers different bytes must not transfer.
        other = SignedRelease(self.root / "other")
        self.assertEqual(self.run_verifier(signature=other.signature.hex()), 12)

    def test_malformed_signatures_versions_and_sizes_are_rejected(self):
        for signature in ("", "zz" * 64, "ab" * 63, "ab" * 65, 123, None):
            with self.subTest(signature=signature):
                self.assertEqual(self.run_verifier(signature=signature), 11)
        for version in ("1.2", "v1.2.3", "01.2.3", "1.2.3.4", "1.2.3-rc.1", 1.2, None, "256.0.0"):
            with self.subTest(version=version):
                self.assertEqual(self.run_verifier(version=version), 21)
        for size in (0, -1, True, "12", None, 2**33):
            with self.subTest(size=size):
                self.assertEqual(self.run_verifier(bytes=size), 15)

    def test_missing_or_unreadable_package_is_rejected(self):
        self.assertEqual(self.run_verifier(msi=str(self.root / "missing.msi")), 21)
        self.assertEqual(self.run_verifier(msi=str(self.root)), 21)

    def test_request_digest_cannot_substitute_for_the_real_one(self):
        # The verifier re-hashes the file and the payload must carry that digest.
        body = self.release.request()
        body["payload"] = body["payload"].replace(self.release.sha256, "0" * 64)
        self.assertEqual(self.run_verifier(body), 12)

    def test_non_ascii_and_oversized_payloads_are_rejected(self):
        self.assertEqual(self.run_verifier(payload="\u4e2d" * 10), 12)
        self.assertEqual(self.run_verifier(payload="x" * 5000), 12)
        for payload in ("", "not json", "[]", "null"):
            with self.subTest(payload=payload):
                self.assertEqual(self.run_verifier(payload=payload), 12)

    def test_verifier_never_writes_a_report_on_failure(self):
        report = self.root / "report.json"
        source = self.root / "request.json"
        source.write_text(json.dumps(self.release.request(signature="ab" * 64)), encoding="utf-8")
        self.assertEqual(update.verifier_main(source, report), 12)
        self.assertFalse(report.exists())

    def test_unwritable_report_location_is_reported_not_raised(self):
        source = self.root / "request.json"
        source.write_text(json.dumps(self.release.request()), encoding="utf-8")
        self.assertEqual(update.verifier_main(source, self.root / "missing" / "report.json"), 21)

    def test_canonical_payload_is_stable_and_ascii(self):
        payload = update.canonical_payload(VERSION, "AB" * 32, 7)
        body = json.loads(payload)
        self.assertEqual(body["sha256"], "ab" * 32)
        self.assertEqual(body["version"], VERSION)
        self.assertEqual(body["bytes"], 7)
        self.assertEqual(body["upgrade"], UPGRADE_CODE)
        self.assertEqual(body["product"], "youziauth")
        self.assertEqual(body["manufacturer"], "yoouzic")
        self.assertEqual(payload, update.canonical_payload(VERSION, "ab" * 32, 7))
        payload.decode("ascii")  # must never contain non-ASCII characters
        for bad in (None, 1, "1.2", "v1.2.3", "1.2.3\n"):
            with self.subTest(version=bad):
                with self.assertRaises(update.UpdateVerificationError):
                    update._version(bad)

    def test_public_key_pin_is_a_32_byte_valid_point(self):
        key = update.public_key()
        self.assertEqual(len(key), ed25519.PUBLIC_KEY_BYTES)
        self.assertEqual(base64.b64encode(key).decode("ascii"), update.PUBLIC_KEY_B64)
        for broken in ("", "not base64!", base64.b64encode(b"short").decode("ascii")):
            with self.subTest(broken=broken):
                with patch.object(update, "PUBLIC_KEY_B64", broken):
                    with self.assertRaises(update.UpdateVerificationError):
                        update.public_key()

    def test_wrong_pin_rejects_a_genuinely_signed_release(self):
        # If the compiled-in key is not the release key, nothing may install.
        with patch.object(update, "PUBLIC_KEY_B64",
                          base64.b64encode(ed25519.derive_public_key(
                              ed25519.generate_secret_key())).decode("ascii")):
            self.assertEqual(self.run_verifier(), 12)


class GuardTests(unittest.TestCase):
    """Input validation must run before any process, lock, or COM boundary."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.release = SignedRelease(self.root)
        self.release.pin(self)
        self.exe = self.root / "youziauth.exe"
        self.exe.write_bytes(b"not executable; the system boundary is mocked")
        for item in (patch.object(update.sys, "platform", "win32"),
                     patch.object(update.sys, "frozen", True, create=True),
                     patch.object(update.sys, "executable", str(self.exe))):
            item.start()
            self.addCleanup(item.stop)

    def validate(self, **overrides):
        arguments = dict(path=self.release.msi, executable=self.exe, version=VERSION,
                         sha256=self.release.sha256, signature=self.release.signature)
        arguments.update(overrides)
        return update._validate_inputs(**arguments)

    def test_valid_inputs_are_accepted(self):
        msi, anchor, version, sha256, signature, size = self.validate()
        self.assertEqual(msi, self.release.msi.resolve())
        self.assertEqual(anchor, self.exe.resolve())
        self.assertEqual((version, sha256, signature.hex(), size),
                         (VERSION, self.release.sha256, self.release.signature.hex(), self.release.size))

    def test_signature_accepts_hex_and_raw_bytes(self):
        self.assertEqual(self.validate(signature=self.release.signature.hex())[4], self.release.signature)
        self.assertEqual(self.validate(signature=self.release.signature)[4], self.release.signature)
        self.assertEqual(
            self.validate(signature=self.release.signature.hex().upper())[4], self.release.signature)

    def test_bad_signatures_are_rejected_before_any_boundary(self):
        for signature in (None, "", "ab" * 63, "ab" * 65, "zz" * 64, 123, b"short", ["x"]):
            with self.subTest(signature=str(signature)[:16]):
                with self.assertRaises(update.UpdateVerificationError):
                    self.validate(signature=signature)

    def test_bad_versions_hashes_and_paths_are_rejected(self):
        for version in (None, 123, "1.2", "v1.2.3", "1.2.3.4", "01.2.3", "1.2.3-beta",
                        "1.2.3\n", "\uff11.2.3", "1.2.3'; exit 0", "256.0.0"):
            with self.subTest(version=version):
                with self.assertRaises(update.UpdateVerificationError):
                    self.validate(version=version)
        for sha256 in (None, 123, "", "f" * 63, "g" * 64, "f" * 64 + "\n"):
            with self.subTest(sha256=sha256):
                with self.assertRaises(update.UpdateVerificationError):
                    self.validate(sha256=sha256)
        for path in (None, str(self.release.msi), self.root, self.root / "missing.msi",
                     Path("bad\x00.msi")):
            with self.subTest(path=path):
                with self.assertRaises(update.UpdateVerificationError):
                    self.validate(path=path)

    def test_source_build_cannot_run_the_installer(self):
        with patch.object(update.sys, "frozen", False):
            with self.assertRaisesRegex(update.UpdateVerificationError, update.OFFICIAL_BUILD_REQUIRED[:6]):
                self.validate()

    def test_executable_must_be_the_running_packaged_executable(self):
        other = self.root / "other.exe"
        other.write_bytes(b"not the running application")
        for executable in (other, self.root, str(self.exe)):
            with self.subTest(executable=executable):
                with self.assertRaises(update.UpdateVerificationError):
                    self.validate(executable=executable)
        with patch.object(update.sys, "executable", str(other)):
            with self.assertRaisesRegex(update.UpdateVerificationError, update.OFFICIAL_BUILD_REQUIRED[:6]):
                self.validate(executable=other)

    def test_installer_must_be_an_msi(self):
        renamed = self.root / "package.exe"
        renamed.write_bytes(self.release.msi.read_bytes())
        with self.assertRaises(update.UpdateVerificationError):
            self.validate(path=renamed)

    def test_non_windows_is_explicitly_rejected(self):
        with patch.object(update.sys, "platform", "linux"):
            with self.assertRaises(update.UpdateVerificationError):
                self.validate()


class NativeWorkerTests(unittest.TestCase):
    """The detached worker must re-verify under its own exclusive file lock."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.release = SignedRelease(self.root)
        self.release.pin(self)
        self.anchor = update._system_tool("WindowsPowerShell/v1.0/powershell.exe")
        self.callback = Mock()

    def spawn_rejected_worker(self, sha256=None, signature=None, payload=None):
        """Start a real worker whose package can never satisfy verification."""
        directory = Path(tempfile.mkdtemp(prefix="youziauth-update-", dir=self.root)).resolve()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        return directory, update._start_worker(
            self.release.msi, self.anchor, VERSION, self.release.size,
            sha256 or self.release.sha256, signature or self.release.signature,
            payload or self.release.payload, directory,
        )

    def test_worker_rejects_a_tampered_package(self):
        # The request pins the original digest, so a changed file must not pass.
        digest = self.release.sha256
        self.release.msi.write_bytes(b"replaced whole package")
        directory, pid = self.spawn_rejected_worker(sha256=digest)
        with self.assertRaisesRegex(update.UpdateVerificationError, update.HASH_MISMATCH[:6]):
            update._observe_worker(pid, directory, self.callback)
        self.callback.assert_not_called()

    def test_worker_rejects_a_signature_that_does_not_cover_the_package(self):
        other = SignedRelease(self.root / "other")
        directory, pid = self.spawn_rejected_worker(signature=other.signature)
        with self.assertRaises(update.UpdateVerificationError):
            update._observe_worker(pid, directory, self.callback)
        self.callback.assert_not_called()

    def test_worker_rejects_a_payload_for_another_version(self):
        forged = update.canonical_payload("9.9.9", self.release.sha256, self.release.size)
        directory, pid = self.spawn_rejected_worker(payload=forged)
        with self.assertRaises(update.UpdateVerificationError):
            update._observe_worker(pid, directory, self.callback)
        self.callback.assert_not_called()

    def test_existing_writer_prevents_the_worker_read_lock(self):
        directory, pid = self.spawn_rejected_worker()
        with self.release.msi.open("r+b"):
            with self.assertRaises(update.UpdateVerificationError):
                update._observe_worker(pid, directory, self.callback)
        self.callback.assert_not_called()


class InstallGuardsTests(unittest.TestCase):
    """install_msi must refuse before creating a worker when a check fails."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.release = SignedRelease(self.root)
        self.release.pin(self)
        self.exe = self.root / "youziauth.exe"
        self.exe.write_bytes(b"not executable; the system boundary is mocked")
        for item in (patch.object(update.sys, "platform", "win32"),
                     patch.object(update.sys, "frozen", True, create=True),
                     patch.object(update.sys, "executable", str(self.exe))):
            item.start()
            self.addCleanup(item.stop)
        self.callback = Mock()
        self.boundary = Mock(side_effect=AssertionError("worker must not start"))
        for name in ("_start_worker", "_observe_worker", "_run_verifier"):
            patched = patch.object(update, name, self.boundary)
            patched.start()
            self.addCleanup(patched.stop)

    def install(self, **overrides):
        arguments = dict(path=self.release.msi, executable=self.exe, version=VERSION,
                         sha256=self.release.sha256, on_launch=self.callback,
                         signature=self.release.signature)
        arguments.update(overrides)
        return update.install_msi(**arguments)

    def test_hash_mismatch_stops_before_verifier_or_worker(self):
        with self.assertRaisesRegex(update.UpdateVerificationError, update.HASH_MISMATCH[:6]):
            self.install(sha256="0" * 64)
        self.boundary.assert_not_called()
        self.callback.assert_not_called()

    def test_forged_signature_stops_before_verifier_or_worker(self):
        other = SignedRelease(self.root / "other")
        with self.assertRaisesRegex(update.UpdateVerificationError, update.SIGNATURE_REJECTED[:6]):
            self.install(signature=other.signature)
        self.boundary.assert_not_called()
        self.callback.assert_not_called()

    def test_invalid_callback_prevents_every_boundary(self):
        with self.assertRaises(update.UpdateVerificationError):
            self.install(on_launch=None)
        self.boundary.assert_not_called()

    def test_verifier_failure_code_becomes_the_user_message(self):
        with patch.object(update, "_run_verifier", return_value=16):
            with self.assertRaisesRegex(update.UpdateVerificationError, update.PROPERTY_MISMATCH[:10]):
                self.install()

    def test_verifier_runs_before_the_worker_starts(self):
        events = []
        with patch.object(update, "_run_verifier", side_effect=lambda *a: events.append("verify") or 0), \
             patch.object(update, "_start_worker", side_effect=lambda *a, **k: events.append("worker") or 4321), \
             patch.object(update, "_observe_worker", side_effect=lambda *a: events.append("observe") or 3010):
            self.assertEqual(self.install(), 3010)
        self.assertEqual(events, ["verify", "worker", "observe"])

    def test_silent_install_preserves_the_workers_complete_result(self):
        result = {"code": 0, "relaunched": True, "healthy": True, "agent_restarted": True}
        with patch.object(update, "_run_verifier", return_value=0), \
             patch.object(update, "_start_worker", return_value=4321) as start, \
             patch.object(update, "_observe_worker", return_value=result) as observe:
            self.assertEqual(self.install(silent=True, log_dir=self.root), result)
        self.assertTrue(observe.call_args.kwargs.get("full_result"),
                        "the SYSTEM caller needs recovery and health fields, not only an exit code")
        self.assertEqual(start.call_args.kwargs.get("result_path"), self.root / "install-result.json")


class WorkerResultTests(unittest.TestCase):
    def observe(self, result, *, full_result=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api = Mock()
            api.OpenProcess.return_value = 0
            def statuses(path):
                return result if path.name == "final.json" else {"pid": 123}
            with patch.object(update, "_kernel32", return_value=api), \
                 patch.object(update, "_read_status", side_effect=statuses):
                if full_result:
                    return update._observe_worker(123, root, lambda: None, full_result=True)
                return update._observe_worker(123, root, lambda: None)

    def test_manual_install_accepts_relaunch_fields_but_still_returns_an_integer(self):
        self.assertEqual(self.observe({"code": 0, "relaunched": True}), 0)

    def test_silent_observer_returns_health_and_recovery_fields(self):
        result = {"code": 0, "relaunched": True, "healthy": False,
                  "agent_restarted": True, "launch_exit_code": 3221225477}
        self.assertEqual(self.observe(result, full_result=True), result)

    def test_malformed_optional_fields_and_unknown_fields_are_refused(self):
        for result in ({"code": 0, "healthy": "false"}, {"code": 0, "relaunched": 1},
                       {"code": 0, "agent_restarted": None},
                       {"code": 0, "launch_exit_code": True},
                       {"code": 0, "launch_exit_code": 0x100000000},
                       {"code": 0, "unexpected": True}, {"code": 0, "error": 20},
                       {"code": True}, {"code": 0x100000000}):
            with self.subTest(result=result):
                with self.assertRaises(update.UpdateVerificationError):
                    self.observe(result)


class DetachedRecoveryTests(unittest.TestCase):
    """Exercise completion in a real PowerShell process without an installer or tasks."""

    def run_script(self, body, *, environment=None):
        self.assertTrue(hasattr(update, "_WORKER_RECOVERY"),
                        "a detached worker must own persistence and recovery after its parent dies")
        script = ("$ErrorActionPreference = 'Stop'\n$ProgressPreference = 'SilentlyContinue'\n"
                  "[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)\n" +
                  update._WORKER_RECOVERY + "\n" + body)
        env = os.environ.copy()
        env.update(environment or {})
        completed = subprocess.run(
            [str(update._system_tool("WindowsPowerShell/v1.0/powershell.exe")),
             "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded(script)],
            env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            creationflags=0x08000000,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def completion(self, code, *, healthy=True, quiet=True):
        with tempfile.TemporaryDirectory() as temporary:
            record = Path(temporary) / "install-result.json"
            body = r'''
$script:calls = @()
$script:before = @()
function Start-UpdateTask($name) {
    $script:before += (Get-Content -LiteralPath $env:YOUZIAUTH_UPDATE_RESULT -Raw | ConvertFrom-Json)
    $script:calls += $name
    return $true
}
function Test-AgentHealthy { return ($env:TEST_HEALTHY -eq '1') }
$quiet = ($env:TEST_QUIET -eq '1')
$request = [pscustomobject]@{ properties = [pscustomobject]@{ ProductVersion = '1.2.3' } }
$final = @{ code = [int]$env:TEST_INSTALL_CODE }
Complete-Install $final
@{ result = $final; calls = @($script:calls); before = @($script:before);
   record = (Get-Content -LiteralPath $env:YOUZIAUTH_UPDATE_RESULT -Raw | ConvertFrom-Json) } |
    ConvertTo-Json -Compress -Depth 6
'''
            return self.run_script(body, environment={
                "YOUZIAUTH_UPDATE_RESULT": str(record), "TEST_INSTALL_CODE": str(code),
                "TEST_HEALTHY": "1" if healthy else "0",
                "TEST_QUIET": "1" if quiet else "0",
            })

    def test_surviving_worker_records_success_before_running_tasks(self):
        value = self.completion(0)
        self.assertEqual(value["calls"], [r"\youziauth\SystemAgent", r"\youziauth\Tray"])
        self.assertEqual(value["before"][0]["result"], {"code": 0})
        self.assertEqual(value["record"]["version"], "1.2.3")
        self.assertRegex(value["record"]["checked"], r"^\d{4}-\d{2}-\d{2}T.*(?:Z|\+00:00)$")
        self.assertEqual(value["record"]["result"],
                         {"code": 0, "relaunched": True, "healthy": True, "agent_restarted": True})

    def test_an_unresponsive_agent_is_not_reported_as_healthy(self):
        value = self.completion(0, healthy=False)
        self.assertFalse(value["record"]["result"]["healthy"])
        self.assertTrue(value["record"]["result"]["agent_restarted"])

    def test_failed_and_cancelled_silent_installs_restore_tasks_and_keep_original_exit_code(self):
        for code in (1603, 1618, 1602):
            with self.subTest(code=code):
                value = self.completion(code)
                self.assertEqual(value["calls"], [r"\youziauth\SystemAgent", r"\youziauth\Tray"])
                self.assertEqual(value["before"][0]["result"], {"code": code})
                self.assertEqual(value["record"]["result"], {
                    "code": code, "relaunched": True, "healthy": True, "agent_restarted": True})

    def test_failed_silent_install_does_not_replace_its_exit_code_when_recovery_is_unhealthy(self):
        value = self.completion(1603, healthy=False)
        self.assertEqual(value["record"]["result"]["code"], 1603)
        self.assertFalse(value["record"]["result"]["healthy"])

    def test_reboot_required_install_is_persisted_without_starting_pending_payload(self):
        value = self.completion(3010)
        self.assertEqual(value["calls"], [])
        self.assertEqual(value["record"]["result"], {"code": 3010})

    def test_failed_interactive_install_does_not_launch_the_application(self):
        value = self.completion(1603, quiet=False)
        self.assertEqual(value["calls"], [])
        self.assertEqual(value["record"]["result"], {"code": 1603})

    def test_status_probe_uses_the_real_message_pipe_protocol(self):
        import agent_ipc
        name = "youziauth-test-health-" + os.urandom(8).hex()
        seen = []
        server = agent_ipc.NamedPipeServer(name, lambda command: seen.append(command.command) or {
            "ok": True, "snapshot": {"boot_id": "test-boot", "state": "waiting_for_network"}})
        thread = threading.Thread(target=server.serve_once, daemon=True)
        thread.start()
        value = self.run_script(
            "@{ healthy = (Test-AgentHealthy -pipeName $env:TEST_PIPE -timeoutMs 1500) } | ConvertTo-Json",
            environment={"TEST_PIPE": name},
        )
        thread.join(2)
        self.assertFalse(thread.is_alive(), "the probe must consume the reply and disconnect")
        self.assertEqual(seen, ["status"])
        self.assertTrue(value["healthy"])

    def test_absent_pipe_is_not_healthy(self):
        value = self.run_script(
            "@{ healthy = (Test-AgentHealthy -pipeName $env:TEST_PIPE -timeoutMs 200) } | ConvertTo-Json",
            environment={"TEST_PIPE": "youziauth-test-missing-" + os.urandom(8).hex()},
        )
        self.assertFalse(value["healthy"])

    def test_a_reply_without_a_valid_agent_snapshot_is_not_healthy(self):
        import agent_ipc
        name = "youziauth-test-invalid-health-" + os.urandom(8).hex()
        server = agent_ipc.NamedPipeServer(name, lambda command: {
            "ok": True, "snapshot": {"boot_id": "test-boot", "state": "invented-state"}})
        thread = threading.Thread(target=server.serve_once, daemon=True)
        thread.start()
        value = self.run_script(
            "@{ healthy = (Test-AgentHealthy -pipeName $env:TEST_PIPE -timeoutMs 400) } | ConvertTo-Json",
            environment={"TEST_PIPE": name},
        )
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(value["healthy"])

    def test_an_agent_that_connects_but_never_replies_is_bounded(self):
        import agent_ipc
        name = "youziauth-test-stalled-health-" + os.urandom(8).hex()
        def slow_reply(command):
            time.sleep(1)
            return {"ok": True, "snapshot": {"boot_id": "test", "state": "waiting_for_network"}}
        server = agent_ipc.NamedPipeServer(name, slow_reply)
        def serve():
            try:
                server.serve_once()
            except OSError:
                pass  # The probe disconnects before this deliberately late reply.
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        value = self.run_script(
            "@{ healthy = (Test-AgentHealthy -pipeName $env:TEST_PIPE -timeoutMs 200) } | ConvertTo-Json",
            environment={"TEST_PIPE": name},
        )
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(value["healthy"])

    def test_verification_errors_are_persisted_without_running_tasks(self):
        with tempfile.TemporaryDirectory() as temporary:
            record = Path(temporary) / "install-result.json"
            value = self.run_script(r'''
$quiet = $true
$request = [pscustomobject]@{ properties = [pscustomobject]@{ ProductVersion = '1.2.3' } }
function Start-UpdateTask { throw 'verification-must-not-start-a-task' }
Complete-Install @{ error = 15 }
Get-Content -LiteralPath $env:YOUZIAUTH_UPDATE_RESULT -Raw
''', environment={"YOUZIAUTH_UPDATE_RESULT": str(record)})
            self.assertEqual(value["result"], {"error": 15})

    def test_all_shipped_scripts_parse_in_windows_powershell(self):
        value = self.run_script(r'''
$scripts = $env:TEST_SCRIPTS | ConvertFrom-Json
$failures = @()
foreach ($script in $scripts) {
    $tokens = $null
    $errors = $null
    $null = [Management.Automation.Language.Parser]::ParseInput($script, [ref]$tokens, [ref]$errors)
    $failures += @($errors | ForEach-Object { $_.Message })
}
@{ failures = @($failures) } | ConvertTo-Json -Compress
''', environment={"TEST_SCRIPTS": json.dumps([update._WORKER, update._LAUNCHER, update._POWERSHELL])})
        self.assertEqual(value["failures"], [])


class ProcessBoundaryTests(unittest.TestCase):
    """Script and command construction must stay fixed and path-free."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.release = SignedRelease(self.root)
        self.release.pin(self)
        self.exe = self.root / "youziauth.exe"
        self.exe.write_bytes(b"not executable; the system boundary is mocked")
        self.anchor = self.root / "System32/WindowsPowerShell/v1.0/powershell.exe"
        self.api = Mock()
        self.api.OpenProcess.return_value = 0
        self.api.GetSystemDirectoryW.side_effect = self.system_directory
        for item in (patch.object(update.sys, "platform", "win32"),
                     patch.object(update.sys, "frozen", True, create=True),
                     patch.object(update.sys, "executable", str(self.exe)),
                     patch.object(update.ctypes, "WinDLL", return_value=self.api, create=True)):
            item.start()
            self.addCleanup(item.stop)

    def system_directory(self, buffer, size):
        buffer.value = str(self.root / "System32")
        return len(buffer.value)

    def test_worker_receives_a_fixed_command_without_inlining_the_path(self):
        directory = Path(tempfile.mkdtemp(prefix="youziauth-update-", dir=self.root)).resolve()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        completed = subprocess.CompletedProcess([], 0, '{"pid":321}', "")
        with patch.object(update.subprocess, "run", return_value=completed) as run:
            update._start_worker(self.release.msi, self.anchor, VERSION, self.release.size,
                                 self.release.sha256, self.release.signature,
                                 self.release.payload, directory)
        command = run.call_args.args[0]
        options = run.call_args.kwargs
        self.assertEqual(Path(command[0]), self.anchor)
        self.assertEqual(command[1:4], ["-NoProfile", "-NonInteractive", "-EncodedCommand"])
        self.assertEqual(len(command), 5)
        self.assertIs(options["shell"], False)
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertLessEqual(options["timeout"], 20)
        self.assertEqual(options["creationflags"], 0x08000000)
        self.assertEqual(options["env"]["YOUZIAUTH_UPDATE_MSI"], str(self.release.msi))
        self.assertEqual(options["env"]["YOUZIAUTH_UPDATE_EXE"], str(self.anchor))
        self.assertEqual(options["env"]["YOUZIAUTH_UPDATE_SIGNATURE"], self.release.signature.hex())
        source = base64.b64decode(command[4]).decode("utf-16le")
        self.assertNotIn(str(self.release.msi), source)
        request = json.loads(base64.b64decode(options["env"]["YOUZIAUTH_UPDATE_REQUEST"]))
        self.assertEqual(request["sha256"], self.release.sha256)
        self.assertEqual(request["properties"], PROPERTIES)
        self.assertEqual(request["signature"], self.release.signature.hex())
        self.assertEqual(request["payload"], self.release.payload.decode("ascii"))
        self.assertTrue(Path(options["env"]["YOUZIAUTH_UPDATE_REQUEST_FILE"]).is_file())
        self.assertTrue(directory.is_absolute())
        self.assertNotEqual(directory, self.root)

    def test_msi_metadata_reader_does_not_inherit_a_windowless_process_stdin(self):
        completed = subprocess.CompletedProcess([], 0, json.dumps({"properties": PROPERTIES}), "")
        with patch.object(update.subprocess, "run", return_value=completed) as run:
            self.assertEqual(update._msi_properties(self.release.msi), PROPERTIES)
        command = run.call_args.args[0]
        options = run.call_args.kwargs
        self.assertEqual(command[1:4], ["-NoProfile", "-NonInteractive", "-EncodedCommand"])
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(options["stdout"], subprocess.PIPE)
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        self.assertEqual(options["env"]["YOUZIAUTH_UPDATE_MSI"], str(self.release.msi))
        self.assertNotIn(str(self.release.msi), base64.b64decode(command[4]).decode("utf-16le"))

    def test_worker_calls_the_verifier_by_request_file(self):
        # The MSI path and payload travel through a file, so no externally
        # controlled text can become part of a command line.
        self.assertIn("--verify-update", update._WORKER)
        self.assertIn("YOUZIAUTH_UPDATE_REQUEST_FILE", update._WORKER)
        self.assertIn("YOUZIAUTH_UPDATE_RESPONSE_FILE", update._WORKER)
        self.assertNotIn("YOUZIAUTH_UPDATE_PAYLOAD", update._WORKER)

    def test_silent_worker_receives_log_and_persistent_result_paths_as_data(self):
        directory = Path(tempfile.mkdtemp(prefix="youziauth-update-", dir=self.root)).resolve()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        log = self.root / "updates" / "msi-install.log"
        result = self.root / "updates" / "install-result.json"
        completed = subprocess.CompletedProcess([], 0, '{"pid":321}', "")
        with patch.object(update.subprocess, "run", return_value=completed) as run:
            update._start_worker(self.release.msi, self.anchor, VERSION, self.release.size,
                                 self.release.sha256, self.release.signature,
                                 self.release.payload, directory, silent=True,
                                 log_path=log, result_path=result)
        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["YOUZIAUTH_UPDATE_SILENT"], "1")
        self.assertEqual(environment["YOUZIAUTH_UPDATE_LOG"], str(log))
        self.assertEqual(environment["YOUZIAUTH_UPDATE_RESULT"], str(result))
        source = base64.b64decode(run.call_args.args[0][4]).decode("utf-16le")
        self.assertNotIn(str(log), source)
        self.assertNotIn(str(result), source)

    def test_worker_and_launcher_scripts_are_encoded_once(self):
        self.assertEqual(base64.b64decode(update._ENCODED_WORKER).decode("utf-16le"), update._WORKER)
        self.assertEqual(base64.b64decode(update._ENCODED_LAUNCHER).decode("utf-16le"), update._LAUNCHER)
        self.assertEqual(base64.b64decode(update._ENCODED_COMMAND).decode("utf-16le"), update._POWERSHELL)

    def test_windowed_worker_reports_through_files_not_a_console(self):
        # The worker runs inside a console-less build, so it must never write to
        # stdout; it publishes status files and reads the verifier's report file.
        self.assertNotIn("[Console]::Out", update._WORKER)
        self.assertIn("Publish-Status", update._WORKER)
        self.assertIn("ReadAllText", update._WORKER)

    def test_errors_are_chinese_and_free_of_os_detail(self):
        for code, message in update._ERRORS.items():
            with self.subTest(code=code):
                self.assertRegex(message, "[\u4e00-\u9fff]")
                self.assertNotIn("C:\\", message)


class VerifierTopologyTests(unittest.TestCase):
    """Who may drive an update, and what it may install.

    The privileged SYSTEM agent owns unattended updates, so *it* runs the validation --
    but it is youziauth-agent.exe, while the artifact being installed is youziauth.exe.
    Requiring the caller to be youziauth.exe made every automatic update fail at the last
    step, after the package had already been downloaded and re-verified.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.install = self.root / "install"
        self.install.mkdir()
        self.app = self.install / "youziauth.exe"
        self.agent = self.install / "youziauth-agent.exe"
        self.app.write_bytes(b"desktop build")
        self.agent.write_bytes(b"agent build")
        self.msi = self.root / "youziauth.msi"
        self.msi.write_bytes(b"package")
        self.sha256 = hashlib.sha256(b"package").hexdigest()
        for item in (
            patch.object(update.sys, "platform", "win32"),
            patch.object(update.sys, "frozen", True, create=True),
        ):
            item.start()
            self.addCleanup(item.stop)

    def validate(self, caller, executable):
        with patch.object(update.sys, "executable", str(caller)):
            return update._validate_inputs(self.msi, executable, "1.9.0", self.sha256,
                                          signature=b"x" * 64)

    def test_the_desktop_build_may_verify_and_install_itself(self):
        _msi, anchor, *_ = self.validate(self.app, None)
        self.assertEqual(anchor, self.app)

    def test_the_privileged_agent_may_drive_the_update(self):
        # 这正是自动更新走的路径：agent 提权常驻，锚点是同目录下的 youziauth.exe。
        _msi, anchor, *_ = self.validate(self.agent, self.app)
        self.assertEqual(anchor, self.app)

    def test_an_unrelated_process_may_not_drive_an_update(self):
        other = self.root / "other.exe"
        other.write_bytes(b"not ours")
        with self.assertRaises(update.UpdateVerificationError):
            self.validate(other, self.app)

    def test_the_anchor_must_be_the_desktop_build_in_the_same_directory(self):
        # 锚点换成 agent 自己、或换到别的目录，都要拒绝：更新只装 youziauth.exe 本体。
        with self.assertRaises(update.UpdateVerificationError):
            self.validate(self.agent, self.agent)
        elsewhere = self.root / "youziauth.exe"
        elsewhere.write_bytes(b"copied elsewhere")
        with self.assertRaises(update.UpdateVerificationError):
            self.validate(self.agent, elsewhere)

    def test_nothing_may_be_installed_by_a_non_frozen_process(self):
        with patch.object(update.sys, "frozen", False, create=True), \
                patch.object(update.sys, "executable", str(self.app)):
            with self.assertRaises(update.UpdateVerificationError):
                update._validate_inputs(self.msi, self.app, "1.9.0", self.sha256,
                                        signature=b"x" * 64)


class ResumeFileTests(unittest.TestCase):
    """A resumed download installs from its .msi.part file.

    Observed live: the agent downloaded all 65,962,304 bytes through the proxy in 5.7
    minutes and then refused it with "更新安装包必须为 MSI 文件" -- the partial is named
    .partial-<version>.msi.part on purpose, and the extension gate only knew .msi.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        for name in ("youziauth.exe", "youziauth-agent.exe"):
            (self.root / name).write_bytes(b"build")
        self.payload = b"package bytes"
        self.sha256 = hashlib.sha256(self.payload).hexdigest()
        for item in (
            patch.object(update.sys, "platform", "win32"),
            patch.object(update.sys, "frozen", True, create=True),
            patch.object(update.sys, "executable", str(self.root / "youziauth-agent.exe")),
        ):
            item.start()
            self.addCleanup(item.stop)

    def validate(self, candidate):
        return update._validate_inputs(candidate, self.root / "youziauth.exe", "1.9.0",
                                       self.sha256, signature=b"x" * 64)

    def test_a_resumed_partial_is_accepted(self):
        partial = self.root / ".partial-1.9.0.msi.part"
        partial.write_bytes(self.payload)
        msi, anchor, *_ = self.validate(partial)
        self.assertEqual(msi, partial)
        self.assertEqual(anchor, self.root / "youziauth.exe")

    def test_a_plain_msi_is_still_accepted(self):
        plain = self.root / "youziauth-1.9.0.msi"
        plain.write_bytes(self.payload)
        self.assertEqual(self.validate(plain)[0], plain)

    def test_anything_else_is_still_refused(self):
        for name in ("notes.txt", "youziauth.msi.part.old", "evil.exe", "x.msi.bak"):
            with self.subTest(name=name):
                candidate = self.root / name
                candidate.write_bytes(self.payload)
                with self.assertRaises(update.UpdateVerificationError):
                    self.validate(candidate)


if __name__ == "__main__":
    unittest.main()
