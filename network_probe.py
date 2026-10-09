# Copyright (C) 2026 yoouzic
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import dataclasses
import urllib.error
import urllib.request
from typing import Callable


# Plain HTTP on port 80, exactly what Windows NCSI probes (see
# HKLM\SYSTEM\CurrentControlSet\Services\NlaSvc\Parameters\Internet). Both
# entries are checked against their exact body, so a captive portal that
# intercepts the request with its own page fails the comparison instead of
# being mistaken for a working uplink.
#
# Deliberately NOT https://www.msftconnecttest.com/connecttest.txt: that
# hostname is served by an Akamai edge whose TLS certificate is the shared
# "a248.e.akamai.net" default and does not cover www.msftconnecttest.com, so
# TLS verification always fails -- including from a healthy network. Using it
# made internet_ok permanently False, which is what let the agent report a
# campus session as healthy while the machine had no Internet at all.
CONNECTIVITY_PROBES: tuple[tuple[str, str], ...] = (
    ("http://www.msftconnecttest.com/connecttest.txt", "Microsoft Connect Test"),
    ("http://www.msftncsi.com/ncsi.txt", "Microsoft NCSI"),
)
CONNECTIVITY_URL, CONNECTIVITY_BODY = CONNECTIVITY_PROBES[0]


@dataclasses.dataclass(frozen=True)
class NetworkObservation:
    internet_ok: bool
    portal_reachable: bool


def probe_internet_once(url: str, expected_body: str, timeout: int) -> bool:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "youziauth-connectivity-check/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read(256).decode("utf-8", errors="replace").strip()
            return response.status == 200 and text == expected_body
    except (OSError, urllib.error.URLError):
        return False


def check_external_internet(timeout: int) -> bool:
    return any(
        probe_internet_once(url, expected_body, timeout)
        for url, expected_body in CONNECTIVITY_PROBES
    )


def check_portal_reachable(url: str, timeout: int) -> bool:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "youziauth-portal-probe/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(1)
            return True
    except urllib.error.HTTPError:
        return True
    except (OSError, urllib.error.URLError):
        return False


class NetworkProbe:
    def __init__(
        self,
        internet_check: Callable[[int], bool] = check_external_internet,
        portal_check: Callable[[str, int], bool] = check_portal_reachable,
    ):
        self.internet_check = internet_check
        self.portal_check = portal_check

    def observe(self, config) -> NetworkObservation:
        timeout = max(1, min(int(config.request_timeout_seconds), 8))
        if self.internet_check(timeout):
            return NetworkObservation(True, False)
        return NetworkObservation(
            False,
            self.portal_check(config.portal_url, timeout),
        )
