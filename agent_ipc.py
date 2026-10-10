# Copyright (C) 2026 yoouzic
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import ctypes
import dataclasses
import json
import os
import tempfile
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Mapping


MAX_MESSAGE_BYTES = 16 * 1024
ALLOWED_COMMANDS = {
    "status",
    "retry",
    "reload-config",
    "suppress-notifications-for-boot",
    # 请求提权端现在检查一次更新。指令里**不带任何参数** —— 提权端自己决定下载和
    # 安装什么，未提权的进程无法让它安装任意文件。
    "check-update",
}
ALLOWED_UI_COMMANDS = {"show", "settings", "retry", "suppress"}
ALLOWED_STATES = {
    "online_external",
    "online_campus",
    "waiting_for_network",
    "auth_failed",
    "error",
}


class InvalidAgentCommand(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class AgentCommand:
    command: str

    def __post_init__(self) -> None:
        if self.command not in ALLOWED_COMMANDS:
            raise InvalidAgentCommand("unsupported agent command")

    @classmethod
    def parse(cls, payload: Mapping[str, object]) -> "AgentCommand":
        if set(payload) != {"command"}:
            raise InvalidAgentCommand("unexpected command fields")
        command = payload.get("command")
        if not isinstance(command, str):
            raise InvalidAgentCommand("command must be a string")
        return cls(command)

    def to_payload(self) -> dict[str, str]:
        return {"command": self.command}


@dataclasses.dataclass(frozen=True)
class UiCommand:
    command: str

    def __post_init__(self) -> None:
        if self.command not in ALLOWED_UI_COMMANDS:
            raise InvalidAgentCommand("unsupported UI command")

    @classmethod
    def parse(cls, payload: Mapping[str, object]) -> "UiCommand":
        if set(payload) != {"command"}:
            raise InvalidAgentCommand("unexpected command fields")
        command = payload.get("command")
        if not isinstance(command, str):
            raise InvalidAgentCommand("command must be a string")
        return cls(command)

    def to_payload(self) -> dict[str, str]:
        return {"command": self.command}


@dataclasses.dataclass(frozen=True)
class RuntimeSnapshot:
    boot_id: str
    state: str
    notifications_suppressed: bool = False
    incident_id: str = ""
    detail: str = ""
    updated_at: str = ""
    # 后台自动更新的状态（由 auto_update 填充）。界面读它来显示「正在后台更新 /
    # 已自动更新到 vX」，而不是自己去做检查和安装。
    update: dict = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.boot_id:
            raise ValueError("boot_id is required")
        if self.state not in ALLOWED_STATES:
            raise ValueError(f"unsupported runtime state: {self.state}")
        if not isinstance(self.update, dict):
            raise ValueError("update must be a JSON object")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_snapshot(path: Path, snapshot: RuntimeSnapshot) -> None:
    payload = json.dumps(
        dataclasses.asdict(snapshot), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    _atomic_write(path, payload)


def read_snapshot(path: Path) -> RuntimeSnapshot:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("runtime snapshot must be a JSON object")
    allowed = {field.name for field in dataclasses.fields(RuntimeSnapshot)}
    unexpected = set(value) - allowed
    if unexpected:
        raise ValueError(f"unexpected runtime snapshot fields: {sorted(unexpected)}")
    return RuntimeSnapshot(**value)


if os.name == "nt":
    PIPE_ACCESS_DUPLEX = 0x00000003
    PIPE_TYPE_MESSAGE = 0x00000004
    PIPE_READMODE_MESSAGE = 0x00000002
    PIPE_WAIT = 0x00000000
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    ERROR_PIPE_CONNECTED = 535
    ERROR_PIPE_BUSY = 231
    ERROR_FILE_NOT_FOUND = 2
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    _kernel32.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(SECURITY_ATTRIBUTES),
    ]
    _kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
    _kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    _kernel32.ConnectNamedPipe.restype = wintypes.BOOL
    _kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
    _kernel32.DisconnectNamedPipe.restype = wintypes.BOOL
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    _kernel32.WaitNamedPipeW.restype = wintypes.BOOL
    _kernel32.SetNamedPipeHandleState.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    _kernel32.SetNamedPipeHandleState.restype = wintypes.BOOL
    _kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    _kernel32.ReadFile.restype = wintypes.BOOL
    _kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    _kernel32.WriteFile.restype = wintypes.BOOL
    _kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    _kernel32.FlushFileBuffers.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL


def _pipe_path(name: str) -> str:
    return name if name.startswith("\\\\.\\pipe\\") else rf"\\.\pipe\{name}"


class NamedPipeServer:
    def __init__(
        self,
        name: str,
        handler: Callable[[AgentCommand], Mapping[str, object]],
        allowed_user_sid: str | None = None,
        command_parser: Callable[[Mapping[str, object]], object] = AgentCommand.parse,
    ):
        if os.name != "nt":
            raise OSError("named pipes are only available on Windows")
        self.path = _pipe_path(name)
        self.handler = handler
        self.allowed_user_sid = allowed_user_sid
        self.command_parser = command_parser

    def _security_attributes(self) -> tuple[SECURITY_ATTRIBUTES | None, ctypes.c_void_p | None]:
        if not self.allowed_user_sid:
            return None, None
        descriptor = ctypes.c_void_p()
        sddl = (
            "D:P"
            "(A;;GA;;;SY)"
            "(A;;GA;;;BA)"
            f"(A;;GRGW;;;{self.allowed_user_sid})"
        )
        if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = SECURITY_ATTRIBUTES(
            ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, False
        )
        return attributes, descriptor

    def serve_once(self) -> None:
        attributes, descriptor = self._security_attributes()
        handle = _kernel32.CreateNamedPipeW(
            self.path,
            PIPE_ACCESS_DUPLEX,
            PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
            1,
            MAX_MESSAGE_BYTES,
            MAX_MESSAGE_BYTES,
            0,
            ctypes.byref(attributes) if attributes is not None else None,
        )
        if handle == INVALID_HANDLE_VALUE:
            if descriptor:
                _kernel32.LocalFree(descriptor)
            raise ctypes.WinError(ctypes.get_last_error())
        if descriptor:
            _kernel32.LocalFree(descriptor)
        try:
            connected = _kernel32.ConnectNamedPipe(handle, None)
            if not connected and ctypes.get_last_error() != ERROR_PIPE_CONNECTED:
                raise ctypes.WinError(ctypes.get_last_error())
            response = self._handle_request(self._read(handle))
            self._write(handle, response)
            _kernel32.FlushFileBuffers(handle)
            _kernel32.DisconnectNamedPipe(handle)
        finally:
            _kernel32.CloseHandle(handle)

    def _handle_request(self, raw: bytes) -> bytes:
        try:
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise InvalidAgentCommand("command payload must be an object")
            command = self.command_parser(value)
            response = dict(self.handler(command))
        except Exception as exc:  # noqa: BLE001 - IPC returns bounded errors to its caller.
            response = {"ok": False, "error": str(exc)[:300]}
        return json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _read(handle) -> bytes:
        buffer = ctypes.create_string_buffer(MAX_MESSAGE_BYTES)
        read = wintypes.DWORD()
        if not _kernel32.ReadFile(handle, buffer, MAX_MESSAGE_BYTES, ctypes.byref(read), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return bytes(buffer.raw[: read.value])

    @staticmethod
    def _write(handle, value: bytes) -> None:
        if len(value) > MAX_MESSAGE_BYTES:
            raise ValueError("agent response is too large")
        written = wintypes.DWORD()
        buffer = ctypes.create_string_buffer(value)
        if not _kernel32.WriteFile(handle, buffer, len(value), ctypes.byref(written), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if written.value != len(value):
            raise OSError("incomplete named-pipe write")


def send_command(name: str, command: AgentCommand | UiCommand, timeout_ms: int = 2000) -> dict[str, object]:
    if os.name != "nt":
        raise OSError("named pipes are only available on Windows")
    path = _pipe_path(name)
    deadline = time.monotonic() + timeout_ms / 1000
    handle = None
    while time.monotonic() < deadline:
        candidate = _kernel32.CreateFileW(
            path,
            GENERIC_READ | GENERIC_WRITE,
            0,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if candidate != INVALID_HANDLE_VALUE:
            handle = candidate
            break
        error = ctypes.get_last_error()
        if error not in (ERROR_FILE_NOT_FOUND, ERROR_PIPE_BUSY):
            raise ctypes.WinError(error)
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
        _kernel32.WaitNamedPipeW(path, min(remaining_ms, 100))
        time.sleep(0.01)
    if handle is None:
        raise TimeoutError(f"timed out connecting to agent pipe {name}")
    try:
        mode = wintypes.DWORD(PIPE_READMODE_MESSAGE)
        if not _kernel32.SetNamedPipeHandleState(handle, ctypes.byref(mode), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
        request = json.dumps(command.to_payload(), separators=(",", ":")).encode("utf-8")
        NamedPipeServer._write(handle, request)
        response = NamedPipeServer._read(handle)
    finally:
        _kernel32.CloseHandle(handle)
    value = json.loads(response.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("agent response must be an object")
    return value
