import base64
import configparser
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from windows_credentials import (
    CredentialMigrationError,
    CredentialStore,
    DpapiProtector,
    machine_config_path,
    migrate_plaintext_password,
)


class ReversibleProtector:
    def protect(self, value: bytes) -> bytes:
        return b"protected:" + value[::-1]

    def unprotect(self, value: bytes) -> bytes:
        if not value.startswith(b"protected:"):
            raise ValueError("invalid protected value")
        return value[len(b"protected:") :][::-1]


class FailedVerificationProtector(ReversibleProtector):
    def unprotect(self, value: bytes) -> bytes:
        return b"different-password"


class CredentialStoreTests(unittest.TestCase):
    def test_machine_config_path_uses_program_data(self):
        path = machine_config_path(Path(r"C:\ProgramData-Test"))

        self.assertEqual(path, Path(r"C:\ProgramData-Test") / "youziauth" / "config.ini")

    def test_store_round_trip_never_writes_plaintext(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CredentialStore(Path(temporary), protector=ReversibleProtector())

            store.save_password("secret-value")

            self.assertEqual(store.load_password(), "secret-value")
            self.assertNotIn(b"secret-value", store.credential_path.read_bytes())
            base64.b64decode(store.credential_path.read_bytes(), validate=True)

    def test_clear_password_removes_blob(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CredentialStore(Path(temporary), protector=ReversibleProtector())
            store.save_password("secret-value")

            store.clear_password()

            self.assertFalse(store.credential_path.exists())

    @unittest.skipUnless(os.name == "nt", "Windows readers can block replacement")
    def test_save_retries_until_a_reader_releases_the_previous_credential(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            protector = DpapiProtector(machine_scope=False)
            store = CredentialStore(root, protector)
            store.save_password("fixture-before")
            replace_file = os.replace

            with store.credential_path.open("rb") as reader:
                def replace_after_reader_releases(source, destination):
                    try:
                        replace_file(source, destination)
                    except PermissionError:
                        self.assertEqual(store.load_password(), "fixture-before")
                        reader.close()
                        raise

                with patch("windows_credentials.os.replace", side_effect=replace_after_reader_releases) as replace:
                    store.save_password("fixture-after")

            self.assertEqual(replace.call_count, 2)
            self.assertEqual(CredentialStore(root, protector).load_password(), "fixture-after")

    @unittest.skipUnless(os.name == "nt", "Windows readers can block replacement")
    def test_save_preserves_previous_credential_when_reader_keeps_file_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CredentialStore(root, ReversibleProtector())
            store.save_password("fixture-before")

            with store.credential_path.open("rb"), self.assertRaises(PermissionError):
                store.save_password("fixture-after")

            self.assertEqual(store.load_password(), "fixture-before")
            self.assertEqual(list(root.iterdir()), [store.credential_path])

    def test_migration_clears_plaintext_only_after_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.ini"
            config_path.write_text("[auth]\nusername = student\npassword = old-secret\n", encoding="utf-8")
            store = CredentialStore(root / "machine", protector=ReversibleProtector())

            changed = migrate_plaintext_password(config_path, store)

            self.assertTrue(changed)
            self.assertEqual(store.load_password(), "old-secret")
            parser = configparser.ConfigParser(interpolation=None)
            parser.read(config_path, encoding="utf-8")
            self.assertEqual(parser.get("auth", "password"), "")

    def test_failed_migration_verification_preserves_old_plaintext(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.ini"
            config_path.write_text("[auth]\npassword = old-secret\n", encoding="utf-8")
            store = CredentialStore(root / "machine", protector=FailedVerificationProtector())

            with self.assertRaises(CredentialMigrationError):
                migrate_plaintext_password(config_path, store)

            self.assertIn("password = old-secret", config_path.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "nt", "DPAPI is Windows-only")
    def test_real_dpapi_machine_scope_round_trip(self):
        protector = DpapiProtector(machine_scope=True)

        encrypted = protector.protect("校园网密码".encode("utf-8"))

        self.assertNotIn("校园网密码".encode("utf-8"), encrypted)
        self.assertEqual(protector.unprotect(encrypted).decode("utf-8"), "校园网密码")


if __name__ == "__main__":
    unittest.main()
