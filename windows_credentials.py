# Copyright (C) 2026 yoouzic
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import base64
import configparser
import ctypes
import os
import subprocess
import tempfile
import time
from ctypes import wintypes
from pathlib import Path
from typing import Protocol


APP_DIR_NAME = "youziauth"
CREDENTIAL_FILE_NAME = "credential.dat"
CRYPTPROTECT_LOCAL_MACHINE = 0x4


class CredentialError(RuntimeError):
    pass


class CredentialMigrationError(CredentialError):
    pass


class Protector(Protocol):
    def protect(self, value: bytes) -> bytes: ...

    def unprotect(self, value: bytes) -> bytes: ...


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _input_blob(value: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buffer = ctypes.create_string_buffer(value)
    blob = DATA_BLOB(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return blob, buffer


class DpapiProtector:
    def __init__(self, machine_scope: bool = True):
        if os.name != "nt":
            raise OSError("DPAPI is only available on Windows")
        self.flags = CRYPTPROTECT_LOCAL_MACHINE if machine_scope else 0
        self.crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(DATA_BLOB),
            wintypes.LPCWSTR,
            ctypes.POINTER(DATA_BLOB),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(DATA_BLOB),
        ]
        self.crypt32.CryptProtectData.restype = wintypes.BOOL
        self.crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(DATA_BLOB),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(DATA_BLOB),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(DATA_BLOB),
        ]
        self.crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel32.LocalFree.restype = ctypes.c_void_p

    def protect(self, value: bytes) -> bytes:
        input_value, input_buffer = _input_blob(value)
        output = DATA_BLOB()
        if not self.crypt32.CryptProtectData(
            ctypes.byref(input_value), None, None, None, None, self.flags, ctypes.byref(output)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            self.kernel32.LocalFree(output.pbData)

    def unprotect(self, value: bytes) -> bytes:
        input_value, input_buffer = _input_blob(value)
        output = DATA_BLOB()
        if not self.crypt32.CryptUnprotectData(
            ctypes.byref(input_value), None, None, None, None, 0, ctypes.byref(output)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            self.kernel32.LocalFree(output.pbData)


def atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(value)
            file.flush()
            os.fsync(file.fileno())
        # A reader can briefly block replacement on Windows.
        for delay in (0.05, 0.1, 0.2):
            try:
                os.replace(temporary, path)
                return
            except PermissionError as exc:
                if getattr(exc, "winerror", None) not in (5, 32):
                    raise
                time.sleep(delay)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_config(path: Path, parser: configparser.ConfigParser) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            parser.write(file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class CredentialStore:
    def __init__(self, root: Path, protector: Protector | None = None):
        self.root = Path(root)
        self.credential_path = self.root / CREDENTIAL_FILE_NAME
        self.protector = protector or DpapiProtector(machine_scope=True)

    def save_password(self, password: str) -> None:
        if not password:
            raise ValueError("password cannot be empty")
        protected = self.protector.protect(password.encode("utf-8"))
        atomic_write_bytes(self.credential_path, base64.b64encode(protected))

    def load_password(self) -> str:
        try:
            protected = base64.b64decode(self.credential_path.read_bytes(), validate=True)
            return self.protector.unprotect(protected).decode("utf-8")
        except (OSError, ValueError, UnicodeError) as exc:
            raise CredentialError(f"could not load protected credential: {exc}") from exc

    def clear_password(self) -> None:
        self.credential_path.unlink(missing_ok=True)


def program_data_root(program_data: Path | None = None) -> Path:
    if program_data is not None:
        return Path(program_data) / APP_DIR_NAME
    root = os.environ.get("PROGRAMDATA")
    if not root:
        root = str(Path(os.environ.get("SystemDrive", "C:")) / "ProgramData")
    return Path(root) / APP_DIR_NAME


def machine_config_path(program_data: Path | None = None) -> Path:
    return program_data_root(program_data) / "config.ini"


def secure_program_data_acl(path: Path, user_sid: str) -> None:
    subprocess.run(
        [
            "icacls.exe",
            str(path),
            "/inheritance:r",
            "/grant:r",
            "*S-1-5-18:(OI)(CI)F",
            "*S-1-5-32-544:(OI)(CI)F",
            f"*{user_sid}:(OI)(CI)M",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def migrate_plaintext_password(user_config: Path, store: CredentialStore) -> bool:
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(user_config, encoding="utf-8") or not parser.has_section("auth"):
        return False
    password = parser.get("auth", "password", fallback="")
    if not password:
        return False
    store.save_password(password)
    if store.load_password() != password:
        raise CredentialMigrationError("protected credential verification failed")
    parser.set("auth", "password", "")
    atomic_write_config(user_config, parser)
    return True


def clear_matching_plaintext_password(user_config: Path, store: CredentialStore) -> bool:
    """Clear a legacy password only when the protected copy is identical."""
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(user_config, encoding="utf-8") or not parser.has_section("auth"):
        return False
    password = parser.get("auth", "password", fallback="")
    if not password:
        return False
    try:
        protected_password = store.load_password()
    except CredentialError:
        return False
    if protected_password != password:
        return False
    parser.set("auth", "password", "")
    atomic_write_config(user_config, parser)
    return True
