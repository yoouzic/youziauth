# Copyright (C) 2026 yoouzic
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import base64
import os
import subprocess
import xml.etree.ElementTree as ET
from typing import Callable

from agent_ipc import RuntimeSnapshot


APP_ID = "youziauth"


class NotificationTracker:
    """Turns agent snapshots into one-shot toast XML documents."""

    def __init__(self) -> None:
        self.boot_id = ""
        self.last_incident_id = ""
        self.retry_pending = False
        self.retry_updated_at = ""

    def mark_retry(self, snapshot: RuntimeSnapshot | None) -> None:
        self.retry_pending = True
        self.retry_updated_at = snapshot.updated_at if snapshot else ""

    def evaluate(self, snapshot: RuntimeSnapshot) -> str | None:
        if self.boot_id and snapshot.boot_id != self.boot_id:
            self.last_incident_id = ""
            self.retry_pending = False
            self.retry_updated_at = ""
        self.boot_id = snapshot.boot_id

        if self.retry_pending and snapshot.updated_at != self.retry_updated_at:
            if snapshot.state in ("online_external", "online_campus"):
                self.retry_pending = False
                return build_result_toast(True, snapshot.detail or "网络已经恢复")
            if snapshot.state in ("auth_failed", "error"):
                self.retry_pending = False
                self.last_incident_id = snapshot.incident_id
                return build_result_toast(False, snapshot.detail or "请检查设置后重试")

        if should_show_failure(snapshot, self.last_incident_id):
            self.last_incident_id = snapshot.incident_id
            return build_failure_toast(snapshot.detail)
        return None


def _build_toast(title: str, detail: str, actions: tuple[tuple[str, str], ...] = ()) -> str:
    toast = ET.Element("toast")
    visual = ET.SubElement(toast, "visual")
    binding = ET.SubElement(visual, "binding", {"template": "ToastGeneric"})
    ET.SubElement(binding, "text").text = title[:80]
    ET.SubElement(binding, "text").text = (detail or "请打开设置查看详细信息")[:240]
    if actions:
        actions_element = ET.SubElement(toast, "actions")
        for label, uri in actions:
            ET.SubElement(
                actions_element,
                "action",
                {
                    "content": label,
                    "arguments": uri,
                    "activationType": "protocol",
                },
            )
    return ET.tostring(toast, encoding="unicode", short_empty_elements=True)


def build_failure_toast(detail: str) -> str:
    return _build_toast(
        "校园网认证失败",
        detail,
        (
            ("重新认证", "youziauth://retry"),
            ("打开设置", "youziauth://settings"),
            ("本次不再提醒", "youziauth://suppress"),
        ),
    )


def build_result_toast(success: bool, detail: str) -> str:
    return _build_toast("校园网认证成功" if success else "校园网认证仍未成功", detail)


def build_dorm_toast(detail: str) -> str:
    return _build_toast("寝室打卡", detail)


def build_lptiyu_toast(detail: str) -> str:
    """乐跑自己的标题: 之前借寝室打卡的 builder, 通知抬头会写错模块。"""
    return _build_toast("乐跑", detail)


def should_show_failure(snapshot: RuntimeSnapshot, last_incident_id: str) -> bool:
    return bool(
        snapshot.state == "auth_failed"
        and snapshot.incident_id
        and not snapshot.notifications_suppressed
        and snapshot.incident_id != last_incident_id
    )


def build_powershell_command(xml: str, app_id: str = APP_ID) -> list[str]:
    xml_base64 = base64.b64encode(xml.encode("utf-8")).decode("ascii")
    safe_app_id = app_id.replace("'", "''")
    script = "\n".join(
        [
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null",
            "[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null",
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime] | Out-Null",
            f"$toastBytes = [Convert]::FromBase64String('{xml_base64}')",
            "$toastText = [Text.Encoding]::UTF8.GetString($toastBytes)",
            "$document = [Windows.Data.Xml.Dom.XmlDocument]::new()",
            "$document.LoadXml($toastText)",
            "$toast = [Windows.UI.Notifications.ToastNotification]::new($document)",
            f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{safe_app_id}').Show($toast)",
        ]
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def show_toast(
    xml: str,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> bool:
    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = runner(
        build_powershell_command(xml),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        startupinfo=startupinfo,
        creationflags=creationflags,
    )
    return result.returncode == 0
