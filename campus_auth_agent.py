# Copyright (C) 2026 yoouzic
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import argparse
import configparser
import ctypes
import dataclasses
import datetime as dt
import logging
import logging.handlers
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

import campus_auth
from agent_ipc import AgentCommand, NamedPipeServer, RuntimeSnapshot, read_snapshot, write_snapshot
from auth_runtime import AgentState, AuthAttempt, AttemptKind, RetryPolicy
from network_probe import NetworkObservation, NetworkProbe
from windows_credentials import CredentialStore, machine_config_path, program_data_root


AGENT_PIPE_NAME = "youziauth-agent"
TRANSIENT_RETRY_DELAYS = (5, 15, 30)
# While the uplink is fine the agent has nothing to report, so it says so on entry
# to that state and then this often. Without it a quiet log is indistinguishable
# from a stopped agent.
ONLINE_HEARTBEAT_SECONDS = 1800


@dataclasses.dataclass(frozen=True)
class CycleResult:
    snapshot: RuntimeSnapshot
    next_delay: int
    notification_required: bool


def current_boot_id() -> str:
    if hasattr(ctypes, "windll"):
        kernel32 = ctypes.windll.kernel32
        kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        boot_time = time.time() - kernel32.GetTickCount64() / 1000
        return str(round(boot_time / 10) * 10)
    return str(round(time.time() / 10) * 10)


def sanitized_detail(value: str) -> str:
    detail = (value or "").replace("\r", " ").replace("\n", " ").strip()
    detail = re.sub(
        r"(?i)(password|userId|queryString)=([^&\s]+)",
        r"\1=<redacted>",
        detail,
    )
    return detail[:200]


class Agent:
    def __init__(
        self,
        config_loader: Callable[[], campus_auth.AuthConfig],
        probe: NetworkProbe,
        authenticator: Callable[..., AuthAttempt],
        snapshot_path: Path,
        logger: logging.Logger,
        boot_id: Optional[str] = None,
    ):
        self.config_loader = config_loader
        self.config = config_loader()
        self.probe = probe
        self.authenticator = authenticator
        self.snapshot_path = Path(snapshot_path)
        self.logger = logger
        self.boot_id = boot_id or current_boot_id()
        self.retry_policy = RetryPolicy(self.config.check_interval_seconds)
        self.network_attempt_index = 0
        self.transient_failures = 0
        self.automatic_login_blocked = False
        self._online_logged_at = None
        suppressed = False
        try:
            previous = read_snapshot(self.snapshot_path)
            suppressed = previous.boot_id == self.boot_id and previous.notifications_suppressed
        except (OSError, ValueError):
            pass
        self.snapshot = RuntimeSnapshot(
            boot_id=self.boot_id,
            state=AgentState.WAITING_FOR_NETWORK.value,
            notifications_suppressed=suppressed,
            updated_at=self._now(),
        )
        write_snapshot(self.snapshot_path, self.snapshot)

    @staticmethod
    def _now() -> str:
        return dt.datetime.now().astimezone().isoformat(timespec="seconds")

    def _publish(
        self,
        state: AgentState,
        detail: str,
        *,
        incident_id: Optional[str] = None,
    ) -> RuntimeSnapshot:
        if incident_id is None:
            incident_id = self.snapshot.incident_id if state is AgentState.AUTH_FAILED else ""
        self.snapshot = RuntimeSnapshot(
            boot_id=self.boot_id,
            state=state.value,
            notifications_suppressed=self.snapshot.notifications_suppressed,
            incident_id=incident_id,
            detail=sanitized_detail(detail),
            updated_at=self._now(),
        )
        write_snapshot(self.snapshot_path, self.snapshot)
        return self.snapshot

    def _failure_snapshot(self, detail: str) -> RuntimeSnapshot:
        incident_id = self.snapshot.incident_id or uuid.uuid4().hex
        return self._publish(AgentState.AUTH_FAILED, detail, incident_id=incident_id)

    def _log_online_heartbeat(self, previous_state: str) -> None:
        """Say something occasionally while the uplink is fine.

        A healthy agent publishes ONLINE_EXTERNAL and returns without touching the
        portal, so it writes no log line at all. That made "everything is fine" and
        "the process died" look identical in campus_auth.log -- which is exactly the
        question it invites. Log on entry to the online state and then every half
        hour, which is frequent enough to prove liveness and rare enough not to bury
        the lines that matter.
        """
        now = time.monotonic()
        entered = previous_state != AgentState.ONLINE_EXTERNAL.value
        if not entered and self._online_logged_at is not None:
            if now - self._online_logged_at < ONLINE_HEARTBEAT_SECONDS:
                return
        self._online_logged_at = now
        self.logger.info(
            "uplink is up; background check running (every %ss)",
            self.config.check_interval_seconds,
        )

    def run_cycle(self, force_login: bool = False) -> CycleResult:
        try:
            observation: NetworkObservation = self.probe.observe(self.config)
        except Exception as exc:  # noqa: BLE001 - probe failures are runtime state, not process failure.
            self.logger.warning("network probe failed: %s", exc)
            snapshot = self._publish(AgentState.WAITING_FOR_NETWORK, "网络检测暂时不可用")
            delay = self.retry_policy.delay(self.network_attempt_index)
            self.network_attempt_index += 1
            return CycleResult(snapshot, delay, False)

        if observation.internet_ok:
            self.network_attempt_index = 0
            self.transient_failures = 0
            self.automatic_login_blocked = False
            # The uplink works. If the browsing path does not, the campus session
            # is not the problem -- the proxy/VPN carrying ordinary traffic is --
            # and saying "互联网连接正常" there is what made this app look like it
            # was lying while nothing loaded.
            if observation.proxy_path_ok:
                detail = "互联网连接正常"
            else:
                detail = "外网可通，但代理/VPN 不通（请检查 Clash 节点）"
            previous = self.snapshot.state
            snapshot = self._publish(AgentState.ONLINE_EXTERNAL, detail)
            self._log_online_heartbeat(previous)
            return CycleResult(snapshot, self.config.check_interval_seconds, False)

        if not observation.portal_reachable:
            snapshot = self._publish(AgentState.WAITING_FOR_NETWORK, "等待网络或校园网门户")
            delay = self.retry_policy.delay(self.network_attempt_index)
            self.network_attempt_index += 1
            return CycleResult(snapshot, delay, False)

        self.network_attempt_index = 0
        if self.automatic_login_blocked and not force_login:
            snapshot = self._failure_snapshot(self.snapshot.detail)
            return CycleResult(
                snapshot,
                self.config.check_interval_seconds,
                not snapshot.notifications_suppressed,
            )

        attempt = self.authenticator(self.config, self.logger, force_login=force_login)

        if attempt.kind is AttemptKind.ALREADY_ONLINE and not observation.internet_ok:
            # The portal says this machine is authenticated, yet the uplink probe
            # failed. On a campus network the portal record is usually telling the
            # truth -- what breaks the Internet here is the proxy/VPN the machine
            # routes through, not the campus session. Measured on the live setup:
            # switching Clash off its proxy path restored the Internet without any
            # re-authentication, and logging the session out instead cost a
            # 10 minute outage while the portal kept its record anyway. So never
            # touch the session here: report what is true and let the probe clear
            # the state once the uplink is back.
            self.logger.info(
                "portal reports an authenticated session but the uplink is down; "
                "leaving the session alone"
            )
            snapshot = self._publish(
                AgentState.WAITING_FOR_NETWORK,
                "校园网已认证，但外网不通（请检查 Clash/代理）",
            )
            return CycleResult(snapshot, self.config.check_interval_seconds, False)

        if attempt.kind in (AttemptKind.ALREADY_ONLINE, AttemptKind.LOGIN_SUCCEEDED):
            self.transient_failures = 0
            self.automatic_login_blocked = False
            snapshot = self._publish(AgentState.ONLINE_CAMPUS, attempt.message or "校园网已认证")
            return CycleResult(snapshot, self.config.check_interval_seconds, False)

        if attempt.kind is AttemptKind.REJECTED:
            self.transient_failures = 0
            self.automatic_login_blocked = True
            snapshot = self._failure_snapshot(attempt.message or "校园网认证被拒绝")
            return CycleResult(
                snapshot,
                self.config.check_interval_seconds,
                not snapshot.notifications_suppressed,
            )

        self.transient_failures += 1
        if self.transient_failures >= len(TRANSIENT_RETRY_DELAYS):
            snapshot = self._failure_snapshot(attempt.message or "校园网认证暂时失败")
            return CycleResult(
                snapshot,
                self.config.check_interval_seconds,
                not snapshot.notifications_suppressed,
            )
        snapshot = self._publish(AgentState.WAITING_FOR_NETWORK, attempt.message or "认证服务暂不可用")
        return CycleResult(
            snapshot,
            TRANSIENT_RETRY_DELAYS[self.transient_failures - 1],
            False,
        )

    def retry_now(self) -> RuntimeSnapshot:
        self.automatic_login_blocked = False
        self.transient_failures = 0
        return self.run_cycle(force_login=True).snapshot

    def reload_config(self) -> RuntimeSnapshot:
        self.config = self.config_loader()
        self.retry_policy = RetryPolicy(self.config.check_interval_seconds)
        # Clear the rejection block, but do not force: reloading settings is not
        # a reason to skip the cached session status.
        self.automatic_login_blocked = False
        self.transient_failures = 0
        return self.run_cycle().snapshot

    def suppress_notifications_for_boot(self) -> RuntimeSnapshot:
        self.snapshot = dataclasses.replace(
            self.snapshot,
            notifications_suppressed=True,
            updated_at=self._now(),
        )
        write_snapshot(self.snapshot_path, self.snapshot)
        return self.snapshot

    def handle_command(self, command: AgentCommand) -> dict[str, object]:
        if command.command == "status":
            snapshot = self.snapshot
        elif command.command == "retry":
            snapshot = self.retry_now()
        elif command.command == "reload-config":
            snapshot = self.reload_config()
        else:
            snapshot = self.suppress_notifications_for_boot()
        return {"ok": True, "snapshot": dataclasses.asdict(snapshot)}

    def serve_forever(
        self,
        stop_event: threading.Event,
        allowed_user_sid: str | None = None,
    ) -> None:
        server = NamedPipeServer(
            AGENT_PIPE_NAME,
            self.handle_command,
            allowed_user_sid=allowed_user_sid,
        )

        def command_loop() -> None:
            while not stop_event.is_set():
                try:
                    server.serve_once()
                except Exception as exc:  # noqa: BLE001 - keep agent alive after one IPC failure.
                    self.logger.error("agent command channel failed: %s", exc)

        threading.Thread(target=command_loop, name="youziauth-agent-ipc", daemon=True).start()
        delay = 0
        while not stop_event.wait(delay):
            result = self.run_cycle()
            delay = result.next_delay


def load_agent_config(path: Path) -> campus_auth.AuthConfig:
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(path, encoding="utf-8"):
        raise FileNotFoundError(f"config file not found: {path}")
    store = CredentialStore(path.parent)
    if not parser.has_section("auth"):
        parser.add_section("auth")
    parser.set("auth", "password", store.load_password())
    parser.set("auth", "password_env", "")
    config = campus_auth.load_config_from_parser(parser)
    log_path = Path(config.log_file)
    if not log_path.is_absolute():
        log_path = path.parent / log_path
    return dataclasses.replace(config, log_file=str(log_path))


def configure_agent_logging(log_path: Path, verbose: bool = False) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("youziauth.agent")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=1_048_576,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the windowless youziauth system agent.")
    parser.add_argument("--config", type=Path, default=machine_config_path())
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--allowed-user-sid")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        config = load_agent_config(args.config)
    except Exception as exc:  # noqa: BLE001 - agent reports bounded configuration failures.
        print(f"agent config error: {sanitized_detail(str(exc))}")
        return 2
    logger = configure_agent_logging(Path(config.log_file), args.verbose)
    agent = Agent(
        config_loader=lambda: load_agent_config(args.config),
        probe=NetworkProbe(),
        authenticator=lambda loaded, active_logger, force_login=False: (
            campus_auth.attempt_authentication(
                campus_auth.CampusAuthClient(loaded, active_logger),
                active_logger,
                force_login=force_login,
            )
        ),
        snapshot_path=program_data_root(args.config.parent.parent) / "runtime.json"
        if args.config == machine_config_path(args.config.parent.parent)
        else args.config.parent / "runtime.json",
        logger=logger,
    )
    if args.once:
        result = agent.run_cycle()
        print(result.snapshot.state)
        return 0 if result.snapshot.state in ("online_external", "online_campus") else 1
    agent.serve_forever(threading.Event(), allowed_user_sid=args.allowed_user_sid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
