# Copyright (C) 2026 yoouzic
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import dataclasses
import enum
from typing import Optional


class AgentState(enum.Enum):
    ONLINE_EXTERNAL = "online_external"
    ONLINE_CAMPUS = "online_campus"
    WAITING_FOR_NETWORK = "waiting_for_network"
    AUTH_FAILED = "auth_failed"
    ERROR = "error"


class AttemptKind(enum.Enum):
    ALREADY_ONLINE = "already_online"
    LOGIN_SUCCEEDED = "login_succeeded"
    # The login endpoint accepted the request, but no live session was confirmed.
    # This includes an unavailable status query, so it does not prove that the
    # gateway refused authorization.
    LOGIN_UNVERIFIED = "login_unverified"
    REJECTED = "rejected"
    TRANSIENT_ERROR = "transient_error"


@dataclasses.dataclass(frozen=True)
class AuthAttempt:
    kind: AttemptKind
    message: str = ""


@dataclasses.dataclass(frozen=True)
class RetryPolicy:
    regular_interval_seconds: int
    startup_delays: tuple[int, ...] = (2, 5, 10, 15)

    def delay(self, attempt_index: int) -> int:
        if attempt_index < len(self.startup_delays):
            return min(self.startup_delays[attempt_index], self.regular_interval_seconds)
        return self.regular_interval_seconds


def classify_state(
    internet_ok: bool,
    portal_reachable: bool,
    attempt: Optional[AttemptKind],
) -> AgentState:
    if internet_ok:
        return AgentState.ONLINE_EXTERNAL
    if not portal_reachable:
        return AgentState.WAITING_FOR_NETWORK
    if attempt in (AttemptKind.ALREADY_ONLINE, AttemptKind.LOGIN_SUCCEEDED):
        return AgentState.ONLINE_CAMPUS
    if attempt is AttemptKind.REJECTED:
        return AgentState.AUTH_FAILED
    return AgentState.WAITING_FOR_NETWORK
