# Copyright (C) 2026 yoouzic
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import ctypes
import enum
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


class TrayStatus(enum.Enum):
    STOPPED = "stopped"
    CHECKING = "checking"
    ONLINE = "online"
    OFFLINE = "offline"
    ERROR = "error"


@dataclass(frozen=True)
class TrayMenuItem:
    label: str = ""
    command: Optional[str] = None
    enabled: bool = True
    separator: bool = False


STATUS_LABELS = {
    TrayStatus.STOPPED: "已停止",
    TrayStatus.CHECKING: "检测中",
    TrayStatus.ONLINE: "在线",
    TrayStatus.OFFLINE: "离线",
    TrayStatus.ERROR: "异常",
}

COMMAND_IDS = {
    1001: "show",
    1002: "settings",
    1003: "check",
    1004: "quit",
    1005: "dorm",
}


def is_supported() -> bool:
    return os.name == "nt"


def format_status_label(status: TrayStatus) -> str:
    return STATUS_LABELS.get(status, "未知")


def format_tray_tooltip(status: TrayStatus, detail: str = "") -> str:
    base = f"校园网认证：{format_status_label(status)}"
    if detail:
        return f"{base} - {detail}"[:127]
    return base


def build_tray_menu_items(status: TrayStatus) -> tuple[TrayMenuItem, ...]:
    return (
        TrayMenuItem(label=f"当前状态：{format_status_label(status)}", enabled=False),
        TrayMenuItem(separator=True),
        TrayMenuItem(label="显示主界面", command="show"),
        TrayMenuItem(label="进入设置", command="settings"),
        TrayMenuItem(label="立即检测", command="check"),
        TrayMenuItem(label="寝室打卡", command="dorm"),
        TrayMenuItem(separator=True),
        TrayMenuItem(label="退出", command="quit"),
    )


def default_icon_paths(asset_dir: Path) -> dict[TrayStatus, Path]:
    return {
        TrayStatus.STOPPED: asset_dir / "tray_stopped.ico",
        TrayStatus.CHECKING: asset_dir / "tray_checking.ico",
        TrayStatus.ONLINE: asset_dir / "tray_online.ico",
        TrayStatus.OFFLINE: asset_dir / "tray_offline.ico",
        TrayStatus.ERROR: asset_dir / "tray_error.ico",
    }


if is_supported():
    from ctypes import wintypes

    LRESULT = ctypes.c_ssize_t
    HICON = getattr(wintypes, "HICON", wintypes.HANDLE)
    HCURSOR = getattr(wintypes, "HCURSOR", wintypes.HANDLE)
    HBRUSH = getattr(wintypes, "HBRUSH", wintypes.HANDLE)
    HMENU = getattr(wintypes, "HMENU", wintypes.HANDLE)
    WNDPROC = ctypes.WINFUNCTYPE(
        LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", HICON),
            ("hCursor", HCURSOR),
            ("hbrBackground", HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", HICON),
            ("szTip", wintypes.WCHAR * 128),
        ]

    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32

    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    user32.LoadIconW.restype = wintypes.HICON
    user32.LoadImageW.restype = HICON
    user32.LoadImageW.argtypes = [
        wintypes.HINSTANCE,
        wintypes.LPCWSTR,
        wintypes.UINT,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.TrackPopupMenu.restype = wintypes.UINT
    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    shell32.Shell_NotifyIconW.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(NOTIFYICONDATAW),
    ]

else:
    wintypes = None
    WNDCLASSW = None
    NOTIFYICONDATAW = None
    user32 = None
    shell32 = None
    kernel32 = None


class WindowsTrayIcon:
    WM_TRAYICON = 0x0400 + 20
    WM_CLOSE = 0x0010
    WM_DESTROY = 0x0002
    WM_RBUTTONUP = 0x0205
    WM_LBUTTONDBLCLK = 0x0203
    NIM_ADD = 0x00000000
    NIM_MODIFY = 0x00000001
    NIM_DELETE = 0x00000002
    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004
    IDI_APPLICATION = 32512
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x00000010
    LR_DEFAULTSIZE = 0x00000040
    MF_STRING = 0x00000000
    MF_GRAYED = 0x00000001
    MF_SEPARATOR = 0x00000800
    TPM_RIGHTBUTTON = 0x00000002
    TPM_RETURNCMD = 0x00000100
    TPM_NONOTIFY = 0x00000080

    def __init__(
        self,
        on_command: Callable[[str], None],
        status: TrayStatus = TrayStatus.STOPPED,
        detail: str = "",
        icon_paths: Optional[dict[TrayStatus, Path]] = None,
    ):
        self.on_command = on_command
        self.status = status
        self.detail = detail
        self.icon_paths = icon_paths or {}
        self._loaded_icons: dict[TrayStatus, int] = {}
        self._class_name = f"CampusAuthTrayWindow_{os.getpid()}_{id(self)}"
        self._ready = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._hwnd = None
        self._hicon = None
        self._icon_added = False
        self._wnd_proc = None

    def start(self) -> bool:
        if not is_supported():
            return False
        if self._thread and self._thread.is_alive():
            return self._icon_added
        self._ready.clear()
        self._thread = threading.Thread(target=self._message_loop, daemon=True)
        self._thread.start()
        return self._ready.wait(3) and self._icon_added

    def update(self, status: TrayStatus, detail: str = "") -> None:
        self.status = status
        self.detail = detail
        if self._hwnd and self._icon_added:
            shell32.Shell_NotifyIconW(self.NIM_MODIFY, ctypes.byref(self._notify_data()))

    def stop(self) -> None:
        if not is_supported():
            return
        if self._hwnd and self._icon_added:
            shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(self._notify_data()))
            self._icon_added = False
        if self._hwnd:
            user32.PostMessageW(self._hwnd, self.WM_CLOSE, 0, 0)

    def _message_loop(self) -> None:
        self._wnd_proc = WNDPROC(self._handle_message)
        hinstance = kernel32.GetModuleHandleW(None)
        window_class = WNDCLASSW()
        window_class.lpfnWndProc = self._wnd_proc
        window_class.hInstance = hinstance
        window_class.lpszClassName = self._class_name
        user32.RegisterClassW(ctypes.byref(window_class))

        self._hwnd = user32.CreateWindowExW(
            0,
            self._class_name,
            self._class_name,
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            hinstance,
            None,
        )
        self._hicon = user32.LoadIconW(None, self.IDI_APPLICATION)
        self._icon_added = bool(
            shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(self._notify_data()))
        )
        self._ready.set()
        if not self._icon_added:
            if self._hwnd:
                user32.DestroyWindow(self._hwnd)
                self._hwnd = None
            return

        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

    def _notify_data(self):
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
        data.uCallbackMessage = self.WM_TRAYICON
        data.hIcon = self._icon_for_status()
        data.szTip = format_tray_tooltip(self.status, self.detail)
        return data

    def _icon_for_status(self):
        path = self.icon_paths.get(self.status)
        if path and path.exists():
            if self.status not in self._loaded_icons:
                icon = user32.LoadImageW(
                    None,
                    str(path),
                    self.IMAGE_ICON,
                    0,
                    0,
                    self.LR_LOADFROMFILE | self.LR_DEFAULTSIZE,
                )
                if icon:
                    self._loaded_icons[self.status] = icon
            if self.status in self._loaded_icons:
                return self._loaded_icons[self.status]
        return self._hicon

    def _handle_message(self, hwnd, message, wparam, lparam):
        if message == self.WM_TRAYICON:
            if lparam == self.WM_RBUTTONUP:
                self._show_context_menu(hwnd)
            elif lparam == self.WM_LBUTTONDBLCLK:
                self.on_command("show")
            return 0
        if message == self.WM_CLOSE:
            if self._icon_added:
                shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(self._notify_data()))
                self._icon_added = False
            user32.DestroyWindow(hwnd)
            return 0
        if message == self.WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _show_context_menu(self, hwnd) -> None:
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        menu = user32.CreatePopupMenu()
        command_to_id = {command: item_id for item_id, command in COMMAND_IDS.items()}

        for item in build_tray_menu_items(self.status):
            if item.separator:
                user32.AppendMenuW(menu, self.MF_SEPARATOR, 0, None)
                continue
            flags = self.MF_STRING if item.enabled else self.MF_STRING | self.MF_GRAYED
            item_id = command_to_id.get(item.command or "", 0)
            user32.AppendMenuW(menu, flags, item_id, item.label)

        user32.SetForegroundWindow(hwnd)
        selected = user32.TrackPopupMenu(
            menu,
            self.TPM_RETURNCMD | self.TPM_RIGHTBUTTON | self.TPM_NONOTIFY,
            point.x,
            point.y,
            0,
            hwnd,
            None,
        )
        user32.DestroyMenu(menu)
        command = COMMAND_IDS.get(selected)
        if command:
            self.on_command(command)
