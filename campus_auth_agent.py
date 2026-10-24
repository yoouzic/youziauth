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
import subprocess
import sys
import threading
import time
import uuid
from functools import partial
from pathlib import Path
from typing import Callable, Optional

import auto_update
import campus_auth
from agent_ipc import AgentCommand, NamedPipeServer, RuntimeSnapshot, read_snapshot, write_snapshot
from auth_runtime import AgentState, AuthAttempt, AttemptKind, RetryPolicy
from network_probe import NetworkObservation, NetworkProbe
from windows_credentials import CredentialError, CredentialStore, machine_config_path, program_data_root


AGENT_PIPE_NAME = "youziauth-agent"
TRANSIENT_RETRY_DELAYS = (5, 15, 30)
# While the uplink is fine the agent has nothing to report, so it says so on entry
# to that state and then this often. Without it a quiet log is indistinguishable
# from a stopped agent.
ONLINE_HEARTBEAT_SECONDS = 1800
# An accepted login with no confirmed session backs off to avoid repeatedly
# submitting credentials while the portal is settling or its queries are failing.
LOGIN_UNVERIFIED_DELAYS = (60, 120, 300)
# Five unverified attempts raise an incident after at least 13 minutes
# (60 + 120 + 300 + 300 seconds, plus request time).
LOGIN_UNVERIFIED_ESCALATE_AFTER = 5
# Unclassified refusals have a bounded block. Explicit invalid credentials or
# account policy errors remain blocked until the user retries or changes config.
LOGIN_BLOCK_RETRY_SECONDS = 300
# How often the two states that used to log nothing at all say something.
STALLED_HEARTBEAT_SECONDS = 300
# 手动指令（retry / reload-config）走在 named pipe 上，界面那边三秒就超时。整条指令
# 通道只有一个 handler，所以这里**绝不能**发网络请求：2026-10-24 14:08，用户的 Wi-Fi
# 刚在 campus / hotspot / 门户之间切换过，handler 里同步跑的 run_cycle(force_login=True)
# 卡在网络探测上，pipe 一直 busy，界面那条没有超时上界的读取永远等不到回应，屏幕上
# 于是留着一个过期的「连接正常」，日志和 runtime.json 也一起静默。指令现在只记账 +
# 叫醒主循环，网络 I/O 只允许发生在主循环线程里。
# 探测死线：一次探测内部要串行问几个端点，逐个 request_timeout 累加的正常上限就在
# 这个量级；Windows 的 getaddrinfo/DNS 解析没有超时参数，才是真正会挂住的地方，所以
# 光靠 urllib 的 socket timeout 不够，必须有墙钟死线兜底。
PROBE_DEADLINE_FLOOR_SECONDS = 30.0
PROBE_DEADLINE_TIMEOUT_MULTIPLIER = 5.0
# 被放弃的探测线程收不回来（DNS 卡在系统调用里），所以最多同时挂这么多个；到顶就
# 不再新开线程，直接按超时处理，绝不能让它无界增长。
MAX_ABANDONED_PROBE_THREADS = 8
# 到顶不等于「永远不再探测」：系统 DNS 可能只是暂时卡住，网络恢复后 agent 必须还能
# 自己回来。所以到顶后每隔这么久仍放行一次新的探测（线程增长很慢：约 4 个/小时）；
# 而真正的长时间故障另有硬上限，避免一整天下来线程无限增长。
PROBE_AT_CAP_RETRY_SECONDS = 900.0
PROBE_ABANDONED_HARD_CAP = 32
# 巡检：卡住的循环不会再发布任何东西，界面会把最后一条状态（很可能是「连接正常」）
# 当成当前状态一直显示。巡检线程定期替它说一句「还在检查」，让快照保持新鲜。
WATCHDOG_INTERVAL_SECONDS = 15.0
WATCHDOG_STALL_FLOOR_SECONDS = 120.0
WATCHDOG_LOG_THROTTLE_SECONDS = 300.0


class ProbeDeadlineExceeded(RuntimeError):
    """探测线程在墙钟死线内没有返回 —— 结果是「不知道」，不是「网络不通」。"""

    def __init__(self, waited_seconds: float):
        super().__init__(f"network probe did not return within {waited_seconds:.0f}s")
        self.waited_seconds = waited_seconds


@dataclasses.dataclass(frozen=True)
class CycleResult:
    snapshot: RuntimeSnapshot
    next_delay: int
    notification_required: bool


def unverified_login_delay(attempts: int) -> int:
    """How long to wait before the next login attempt after ``attempts`` of them.

    The first retry stays at the normal cadence so a single blip recovers at once;
    after that the pace decays to one attempt every few minutes. ``attempts`` is
    1-based.
    """
    if attempts < 1:
        return LOGIN_UNVERIFIED_DELAYS[0]
    return LOGIN_UNVERIFIED_DELAYS[min(attempts, len(LOGIN_UNVERIFIED_DELAYS)) - 1]


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
        updater=None,
        update_interval_seconds: int = 6 * 60 * 60,
        update_reason: str = "",
        probe_deadline_seconds: Optional[float] = None,
        watchdog_interval_seconds: float = WATCHDOG_INTERVAL_SECONDS,
        watchdog_stall_seconds: Optional[float] = None,
    ):
        self.config_loader = config_loader
        self.config = config_loader()
        self.probe = probe
        self.authenticator = authenticator
        self.snapshot_path = Path(snapshot_path)
        self.logger = logger
        self.boot_id = boot_id or current_boot_id()
        # 快照是所有读者（界面 / 巡检 / 指令）共用的一份状态，写它必须串行；
        # RLock 是因为 _publish 之间偶尔会互相嵌套（例如更新进度回调）。
        self._snapshot_lock = threading.RLock()
        self._pending_lock = threading.Lock()
        # 主循环等下一轮时用的事件：手动指令把它 set 起来就能立刻插一轮。
        self._wake_event = threading.Event()
        self._pending_force_login = False
        # 探测线程与巡检共用的运行状态：_cycle_started_at 只由主循环写，
        # 巡检线程只读；None 表示当前没有周期在跑。
        self._cycle_started_at = None
        self._watchdog_logged_at = None
        self._probe_timeout_logged_at = None
        self._probe_threads: list[threading.Thread] = []
        self._probe_lock = threading.Lock()
        # 上一次真正发起探测的时刻：到顶冷启动用，保证卡死的旧线程不会把探测永久停掉。
        self._last_probe_started_at = None
        request_timeout = max(1, int(getattr(self.config, "request_timeout_seconds", 8) or 8))
        if probe_deadline_seconds is None:
            probe_deadline_seconds = max(
                PROBE_DEADLINE_FLOOR_SECONDS,
                PROBE_DEADLINE_TIMEOUT_MULTIPLIER * request_timeout,
            )
        self.probe_deadline_seconds = max(1.0, float(probe_deadline_seconds))
        if watchdog_stall_seconds is None:
            watchdog_stall_seconds = max(
                WATCHDOG_STALL_FLOOR_SECONDS,
                2 * self.probe_deadline_seconds + 30,
            )
        self.watchdog_stall_seconds = max(1.0, float(watchdog_stall_seconds))
        self.watchdog_interval_seconds = max(0.01, float(watchdog_interval_seconds))
        self.retry_policy = RetryPolicy(self.config.check_interval_seconds)
        self.network_attempt_index = 0
        self.transient_failures = 0
        self.automatic_login_blocked = False
        self.login_block_requires_action = False
        self.login_block_detail = ""
        self.login_blocked_since = 0.0
        self.unverified_logins = 0
        self.unverified_incident_id = ""
        self._stalled_logged_at = None
        # 后台自动更新：这是 SYSTEM 常驻进程存在的意义之一 —— 它已经提权，所以
        # 检查、下载、验签、静默安装都不需要再弹一次 UAC。
        self.updater = updater
        # 更新器没启用时说明原因：界面要能区分「后台没在跑」和「已经是最新」。
        self.update_reason = str(update_reason or "")
        self.update_interval_seconds = max(300, int(update_interval_seconds))
        self._next_update_at = 0.0
        self._update_lock = threading.Lock()
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

        self._recover_update()

    @staticmethod
    def _now() -> str:
        return dt.datetime.now().astimezone().isoformat(timespec="seconds")

    def _current_snapshot(self) -> RuntimeSnapshot:
        """给指令线程读一份一致的快照：发布方可能正在替换它。"""
        with self._snapshot_lock:
            return self.snapshot

    def _publish(
        self,
        state: AgentState,
        detail: str,
        *,
        incident_id: Optional[str] = None,
    ) -> RuntimeSnapshot:
        with self._snapshot_lock:
            if incident_id is None:
                incident_id = self.snapshot.incident_id if state is AgentState.AUTH_FAILED else ""
            self.snapshot = RuntimeSnapshot(
                boot_id=self.boot_id,
                state=state.value,
                notifications_suppressed=self.snapshot.notifications_suppressed,
                incident_id=incident_id,
                detail=sanitized_detail(detail),
                updated_at=self._now(),
                update=self.snapshot.update,
            )
            write_snapshot(self.snapshot_path, self.snapshot)
            return self.snapshot

    def _failure_snapshot(self, detail: str, *, incident_id: Optional[str] = None) -> RuntimeSnapshot:
        incident_id = incident_id or self.snapshot.incident_id or uuid.uuid4().hex
        return self._publish(AgentState.AUTH_FAILED, detail, incident_id=incident_id)

    def _log_stalled(self, message: str, *args) -> None:
        """Say something about a state that used to be completely silent.

        The blocked branch and the no-portal branch both published a snapshot and
        returned without touching the log, so 67 minutes of "the agent gave up"
        looked exactly like "the agent is dead". Report on entry and then no more
        often than the heartbeat.
        """
        now = time.monotonic()
        if self._stalled_logged_at is not None:
            if now - self._stalled_logged_at < STALLED_HEARTBEAT_SECONDS:
                return
        self._stalled_logged_at = now
        self.logger.info(message, *args)

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

    def _prune_probe_threads(self) -> int:
        """丢掉已经收工的探测线程，返回还在挂着的数量。"""
        with self._probe_lock:
            self._probe_threads = [
                thread for thread in self._probe_threads if thread.is_alive()
            ]
            return len(self._probe_threads)

    def _log_probe_timeout(self, deadline: float, *, at_cap: bool) -> None:
        """探测死线到了要留一条记录 —— 这个状态以前是完全静默的（所以像进程死了）。"""
        now = time.monotonic()
        if self._probe_timeout_logged_at is not None:
            if now - self._probe_timeout_logged_at < STALLED_HEARTBEAT_SECONDS:
                return
        self._probe_timeout_logged_at = now
        if at_cap:
            # 到上限只说明以前那几次确实没回来；再开线程只是继续堆垃圾。
            self.logger.warning(
                "network probe is still stuck in %s earlier cycles; skipping a new "
                "probe and reporting the deadline as exceeded",
                MAX_ABANDONED_PROBE_THREADS,
            )
            return
        self.logger.warning(
            "network probe did not return within %ss; it may be stuck in DNS "
            "resolution (Windows has no timeout for that) -- continuing to retry",
            int(deadline),
        )

    def _observe_with_deadline(self) -> NetworkObservation:
        """在墙钟死线内拿一次网络观测，超时抛 ``ProbeDeadlineExceeded``。

        urllib 的 ``timeout`` 只管 socket 读写，管不了 ``getaddrinfo``/DNS 解析：
        在 Wi-Fi 刚切换（campus <-> hotspot <-> 门户）时它可能挂任意久。所以探测
        放进 daemon 线程，这里的 ``join`` 是唯一的上界 —— 挂住的线程收不回来，
        只能数着数量放弃它。
        """
        outcome: dict[str, object] = {}

        def work() -> None:
            try:
                outcome["observation"] = self.probe.observe(self.config)
            except BaseException as exc:  # noqa: BLE001 - 交给调用线程重抛，行为与同步探测一致
                outcome["error"] = exc

        deadline = self.probe_deadline_seconds
        abandoned = self._prune_probe_threads()
        if abandoned >= MAX_ABANDONED_PROBE_THREADS:
            cooling_down = (
                self._last_probe_started_at is not None
                and time.monotonic() - self._last_probe_started_at < PROBE_AT_CAP_RETRY_SECONDS
            )
            if cooling_down or abandoned >= PROBE_ABANDONED_HARD_CAP:
                self._log_probe_timeout(deadline, at_cap=True)
                raise ProbeDeadlineExceeded(deadline)
            # 冷却期已过且未到硬上限：放行一次新探测。系统 DNS 若已经恢复，这一下就能
            # 把 agent 从「永久超时」里救回来；没恢复也只是很慢地多一个挂住的线程。
        self._last_probe_started_at = time.monotonic()
        worker = threading.Thread(
            target=work, name="youziauth-agent-probe", daemon=True
        )
        worker.start()
        worker.join(deadline)
        if worker.is_alive():
            with self._probe_lock:
                self._probe_threads.append(worker)
            self._log_probe_timeout(deadline, at_cap=False)
            raise ProbeDeadlineExceeded(deadline)
        if "error" in outcome:
            raise outcome["error"]  # type: ignore[misc]
        if "observation" not in outcome:
            raise RuntimeError("network probe returned no observation")
        return outcome["observation"]  # type: ignore[return-value]

    def run_cycle(self, force_login: bool = False) -> CycleResult:
        # 巡检靠这个时刻判断「这一轮是不是卡住了」。卡住的循环不会再发布任何快照，
        # 只能由巡检线程替它说话，否则界面一直显示卡住前的最后一条状态。
        self._cycle_started_at = time.monotonic()
        try:
            return self._run_cycle(force_login)
        finally:
            self._cycle_started_at = None

    def _run_cycle(self, force_login: bool) -> CycleResult:
        if not self.config.username or not self.config.password:
            snapshot = self._publish(AgentState.WAITING_FOR_NETWORK,
                                     "请先在校园网页面保存账号和密码。")
            return CycleResult(snapshot, self.config.check_interval_seconds, False)
        try:
            observation: NetworkObservation = self._observe_with_deadline()
        except ProbeDeadlineExceeded as timeout:
            # 「不知道」也是一种必须说出来的状态：仍然报等待网络，但把「还在等」写进
            # 详情，界面就不会把一条旧状态当成当前状态。
            snapshot = self._publish(
                AgentState.WAITING_FOR_NETWORK,
                f"网络检测长时间没有返回（已等待约 {timeout.waited_seconds:.0f}s），仍在重试",
            )
            delay = self.retry_policy.delay(self.network_attempt_index)
            self.network_attempt_index += 1
            return CycleResult(snapshot, delay, False)
        except Exception as exc:  # noqa: BLE001 - probe failures are runtime state, not process failure.
            self.logger.warning("network probe failed: %s", exc)
            snapshot = self._publish(AgentState.WAITING_FOR_NETWORK, "网络检测暂时不可用")
            delay = self.retry_policy.delay(self.network_attempt_index)
            self.network_attempt_index += 1
            return CycleResult(snapshot, delay, False)

        if observation.internet_ok:
            self.network_attempt_index = 0
            self.transient_failures = 0
            # Working connectivity does not fix rejected saved credentials.
            # Keep that block until retry/config reload, while reporting the
            # current usable uplink normally. Temporary blocks can recover here.
            if not self.login_block_requires_action:
                self.automatic_login_blocked = False
                self.login_block_detail = ""
            self.unverified_logins = 0
            self.unverified_incident_id = ""
            self._stalled_logged_at = None
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
            self._log_stalled(
                "portal is not reachable; waiting for the campus network "
                "(next check in %ss)", delay,
            )
            return CycleResult(snapshot, delay, False)

        self.network_attempt_index = 0
        if self.automatic_login_blocked and not force_login:
            if self.login_block_requires_action:
                snapshot = self._failure_snapshot(self.login_block_detail or self.snapshot.detail)
                self._log_stalled(
                    "login is blocked after a credential/account refusal (%s); "
                    "waiting for updated configuration or a user retry",
                    self.snapshot.detail,
                )
                return CycleResult(snapshot, self.config.check_interval_seconds,
                                   not snapshot.notifications_suppressed)
            waited = time.monotonic() - self.login_blocked_since
            if waited < LOGIN_BLOCK_RETRY_SECONDS:
                snapshot = self._failure_snapshot(self.login_block_detail or self.snapshot.detail)
                self._log_stalled(
                    "login is blocked after a refusal (%s); retrying automatically "
                    "in %ss",
                    self.snapshot.detail, int(LOGIN_BLOCK_RETRY_SECONDS - waited),
                )
                return CycleResult(
                    snapshot,
                    self.config.check_interval_seconds,
                    not snapshot.notifications_suppressed,
                )
            # A refusal is not necessarily permanent. Letting the block stand until
            # someone clicks 重新认证 is what turned the portal's 认证设备响应超时
            # ("device timed out, try again later") into a 67-minute silent outage.
            self.logger.info(
                "login block expired after %ss; trying again", LOGIN_BLOCK_RETRY_SECONDS
            )
            self.automatic_login_blocked = False
            self.login_block_requires_action = False
            # A new block is a new stall and must be reported on entry, not held
            # back by the previous one's heartbeat.
            self._stalled_logged_at = None

        attempt = self.authenticator(self.config, self.logger, force_login=force_login)

        if attempt.kind is AttemptKind.ALREADY_ONLINE and not observation.internet_ok:
            self.unverified_logins = 0
            self.unverified_incident_id = ""
            # The portal says this machine is authenticated, yet the uplink probe
            # failed. The portal record is usually telling the truth -- measured
            # on the live setup, the cause of "portal says online but nothing
            # loads" is the proxy/VPN the machine routes through, not the campus
            # session. So never touch the session here: report what is true and
            # let the probe clear the state once the uplink is back.
            #
            # The one thing this can be is a stale ePortal record that no longer
            # authorises traffic. Since 2026-10-22 the authenticator tells those
            # apart for a forced attempt (verified session vs refused session),
            # so its sentence wins when it is more specific; the release path for
            # a genuinely stale record is the portal's own self-service, never a
            # client-side logout.
            session_detail = (attempt.message or "").strip()
            if not session_detail or session_detail == campus_auth.ALREADY_AUTHENTICATED_DETAIL:
                detail = (
                    "校园网会话仍在，但外网不通（检查 Clash/代理；代理正常仍不通"
                    "请在门户自助服务厅退出登录设备）"
                )
            else:
                detail = f"{session_detail}，但外网不通（检查 Clash/代理）"
            self.logger.info(
                "portal reports an authenticated session but the uplink is down; "
                "leaving the session alone"
            )
            snapshot = self._publish(AgentState.WAITING_FOR_NETWORK, detail)
            return CycleResult(snapshot, self.config.check_interval_seconds, False)

        if attempt.kind in (AttemptKind.ALREADY_ONLINE, AttemptKind.LOGIN_SUCCEEDED):
            self.transient_failures = 0
            self.automatic_login_blocked = False
            self.unverified_logins = 0
            self.unverified_incident_id = ""
            snapshot = self._publish(AgentState.ONLINE_CAMPUS, attempt.message or "校园网已认证")
            return CycleResult(snapshot, self.config.check_interval_seconds, False)

        if attempt.kind is AttemptKind.REJECTED:
            self.transient_failures = 0
            self.automatic_login_blocked = True
            self.login_block_requires_action = campus_auth.is_permanent_refusal(attempt.message)
            self.login_blocked_since = time.monotonic()
            self._stalled_logged_at = None
            snapshot = self._failure_snapshot(attempt.message or "校园网认证被拒绝")
            self.login_block_detail = snapshot.detail
            if self.login_block_requires_action:
                self.logger.error(
                    "login refused: %s; automatic login waits for a user/config change",
                    attempt.message or "unknown",
                )
            else:
                self.logger.error(
                    "login refused: %s; automatic retries slow to every %ss",
                    attempt.message or "unknown", LOGIN_BLOCK_RETRY_SECONDS,
                )
            return CycleResult(
                snapshot,
                self.config.check_interval_seconds,
                not snapshot.notifications_suppressed,
            )

        if attempt.kind is AttemptKind.LOGIN_UNVERIFIED:
            # Preserve whether the session was explicitly absent or simply could
            # not be queried; both need retry, but only the former is a verdict.
            self.transient_failures = 0
            self.unverified_logins += 1
            delay = unverified_login_delay(self.unverified_logins)
            if self.unverified_logins >= LOGIN_UNVERIFIED_ESCALATE_AFTER:
                snapshot = self._failure_snapshot(
                    attempt.message or "认证请求已被受理，但会话尚未确认",
                    incident_id=self.unverified_incident_id,
                )
                # WAITING_FOR_NETWORK snapshots intentionally have no incident
                # ID, so keep this unresolved episode across portal outages.
                self.unverified_incident_id = snapshot.incident_id
                return CycleResult(
                    snapshot,
                    delay,
                    self.unverified_logins == LOGIN_UNVERIFIED_ESCALATE_AFTER
                    and not snapshot.notifications_suppressed,
                )
            snapshot = self._publish(
                AgentState.WAITING_FOR_NETWORK,
                f"{attempt.message or '认证请求已被受理，但会话尚未确认'}（重试中）",
            )
            return CycleResult(snapshot, delay, False)

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

    def _reset_login_blocks(self) -> None:
        """用户要求重试 / 换了配置时要清掉的短期状态。"""
        self.automatic_login_blocked = False
        self.login_block_requires_action = False
        self.login_block_detail = ""
        self.transient_failures = 0
        # The user asking for a retry is also the signal that "the portal keeps
        # accepting logins that never take effect" may have cleared, so the
        # circuit breaker starts over rather than staying escalated.
        self.unverified_logins = 0
        self.unverified_incident_id = ""

    def _set_pending_force_login(self) -> None:
        with self._pending_lock:
            self._pending_force_login = True

    def _take_pending_force_login(self) -> bool:
        with self._pending_lock:
            forced = self._pending_force_login
            self._pending_force_login = False
        return forced

    def retry_now(self) -> RuntimeSnapshot:
        """**同步地**立刻强制认证一次。只给直接调用方/测试用。

        走 named pipe 的指令不能调它：一次网络请求就可能挂住整条指令通道，
        那是 2026-10-24 14:08 现场的直接原因。
        """
        self._reset_login_blocks()
        return self.run_cycle(force_login=True).snapshot

    def request_retry(self) -> RuntimeSnapshot:
        """界面点了「重新认证」：**立刻返回**，强制登录交给后台主循环。

        这里只做记账 + 叫醒：清掉短期阻塞状态、记下「下一轮要强制登录」、设置唤醒
        事件，然后返回当前快照。pipe handler 线程里绝不允许出现网络 I/O。
        """
        self._reset_login_blocks()
        self._set_pending_force_login()
        self._wake_event.set()
        self.logger.info("manual retry requested; running a forced check now")
        return self._current_snapshot()

    def _reload_config_state(self) -> None:
        """重读配置并清掉短期阻塞状态（本地文件 + DPAPI，不发网络请求）。"""
        self.config = self.config_loader()
        self.retry_policy = RetryPolicy(self.config.check_interval_seconds)
        # Clear the rejection block, but do not force: reloading settings is not
        # a reason to skip the cached session status.
        self._reset_login_blocks()

    def reload_config(self) -> RuntimeSnapshot:
        """**同步地**重读配置并立刻检测一轮。只给直接调用方/测试用。"""
        self._reload_config_state()
        return self.run_cycle().snapshot

    def request_reload_config(self) -> RuntimeSnapshot:
        """pipe 上的 reload-config：重读配置后**立刻返回**，检测交给主循环。"""
        self._reload_config_state()
        self._wake_event.set()
        self.logger.info("configuration reloaded; running a check now")
        return self._current_snapshot()

    def suppress_notifications_for_boot(self) -> RuntimeSnapshot:
        with self._snapshot_lock:
            self.snapshot = dataclasses.replace(
                self.snapshot,
                notifications_suppressed=True,
                updated_at=self._now(),
            )
            write_snapshot(self.snapshot_path, self.snapshot)
            return self.snapshot

    def handle_command(self, command: AgentCommand) -> dict[str, object]:
        self._recover_update()
        if command.command == "status":
            snapshot = self._current_snapshot()
        elif command.command == "retry":
            # 立刻返回；强制登录由主循环执行。这个 handler 是单线程的，
            # 在这里做网络 I/O 会让整条指令通道跟着一起卡住。
            snapshot = self.request_retry()
        elif command.command == "reload-config":
            # 只重读配置（本地操作），一轮检测同样交给主循环。
            snapshot = self.request_reload_config()
        elif command.command == "check-update":
            snapshot = self.request_update_check()
        else:
            snapshot = self.suppress_notifications_for_boot()
        return {"ok": True, "snapshot": dataclasses.asdict(snapshot)}

    def _recover_update(self) -> None:
        """Adopt the detached install worker's result, including late writes."""
        recover = getattr(self.updater, "recover_install_result", None)
        if not callable(recover) or not self._update_lock.acquire(blocking=False):
            return
        try:
            status = recover()
            if status is not None:
                self._publish_update(status)
                # monotonic deadlines do not survive a process restart. Schedule
                # from the recovered terminal state, so its feedback stays visible.
                self._next_update_at = time.monotonic() + self.update_interval_seconds
        except Exception as exc:  # noqa: BLE001 - recovery must never stop authentication
            self.logger.warning("update result recovery failed: %s", type(exc).__name__)
        finally:
            self._update_lock.release()

    def _publish_update(self, status) -> RuntimeSnapshot:
        """把自动更新状态并进运行时快照，界面经由同一条通道读到它。"""
        with self._snapshot_lock:
            self.snapshot = dataclasses.replace(
                self.snapshot,
                update=status.to_dict(),
                updated_at=self._now(),
            )
            try:
                write_snapshot(self.snapshot_path, self.snapshot)
            except OSError:
                pass
            return self.snapshot

    def _publish_disabled_reason(self) -> RuntimeSnapshot:
        """把「为什么没有后台更新」写进快照，让界面说出来而不是什么都不显示。"""
        if self.updater is not None or not self.update_reason:
            return self.snapshot
        return self._publish_update(auto_update.UpdateStatus(
            state="error", message=self.update_reason, detail="updater unavailable"))

    def _run_update_check(self, reason: str) -> RuntimeSnapshot:
        """跑一次后台更新。任何异常都只降级为错误状态，绝不能让 agent 退出。

        调用方必须已经持有 ``self._update_lock``。

        中途也会发布状态：安装包有几十 MB，用户不该在整个下载期间都看不到「这次改了什么」。
        """
        if self.updater is None:
            return self.snapshot
        try:
            status = self.updater.run_cycle(progress=self._publish_update)
        except Exception as exc:  # noqa: BLE001 - a failed update must not stop the agent
            self.logger.warning("automatic update failed: %s", type(exc).__name__)
            return self.snapshot
        # detail 为空时不写这个字段：行尾挂一个光秃秃的「detail=」看上去就像日志被截断了
        # （2026-10-12 现场），而它本来就不带任何信息。有内容才写。
        self.logger.info(
            "automatic update (%s): state=%s current=%s latest=%s%s",
            reason, status.state, status.current_version, status.latest_version,
            f" detail={status.detail}" if status.detail else "",
        )
        return self._publish_update(status)

    def request_update_check(self) -> RuntimeSnapshot:
        """界面请求「现在检查一次更新」—— 只是一个信号，没有任何参数。

        **立刻返回**：检查加下载可能好几分钟，而这条指令走在 named pipe 上，界面那边
        三秒就超时。真正的更新在后台线程里跑，进度由运行时快照回报。
        """
        self._start_update_thread("requested")
        return self._current_snapshot()

    def update_due(self) -> bool:
        return self.updater is not None and time.monotonic() >= self._next_update_at

    def periodic_update_check(self) -> RuntimeSnapshot:
        """到点检查一次。**在后台线程里跑**，立刻返回。

        它是在网络循环的间隙被调用的，而一次更新要下载几十 MB。同步跑会把校园网认证
        推迟几分钟 —— 那是这个进程的本职工作，不能给更新让路。
        """
        # 先排下一次，再动手：安装会把这个进程杀掉，重启后不该立刻再跑一遍。
        self._next_update_at = time.monotonic() + self.update_interval_seconds
        self._start_update_thread("periodic")
        return self.snapshot

    def _start_update_thread(self, reason: str) -> None:
        """拿不到锁就说明已经有一次在跑，直接返回（不排队、不重复发起）。"""
        if self.updater is None or not self._update_lock.acquire(blocking=False):
            return

        def work():
            try:
                self._run_update_check(reason)
            finally:
                self._update_lock.release()

        threading.Thread(target=work, name="youziauth-agent-update", daemon=True).start()

    def wait_for_update(self, timeout: float = 5.0) -> bool:
        """等正在跑的那次更新结束。给测试和退出流程用，不参与正常调度。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._update_lock.acquire(blocking=False):
                time.sleep(0.01)
                continue
            self._update_lock.release()
            return True
        return False

    def check_stall(self) -> bool:
        """一轮循环超过 ``watchdog_stall_seconds`` 还没返回就说出来。

        卡住的循环不会再发布任何快照，界面于是把最后一条状态（很可能是「连接正常」）
        当成当前状态一直显示 —— 2026-10-24 14:08 现场。这里每次巡检都发布一条**新鲜**的
        「仍在重试」，让界面不可能再显示过期状态；ERROR 日志按 300 秒节流，避免刷屏。
        返回是否判定为卡住。
        """
        started = self._cycle_started_at
        if started is None:
            return False
        elapsed = time.monotonic() - started
        if elapsed < self.watchdog_stall_seconds:
            return False
        now = time.monotonic()
        if (
            self._watchdog_logged_at is None
            or now - self._watchdog_logged_at >= WATCHDOG_LOG_THROTTLE_SECONDS
        ):
            self._watchdog_logged_at = now
            self.logger.error(
                "background check has been running for about %ss without returning; "
                "publishing a fresh stuck snapshot so the interface cannot keep "
                "showing a stale state",
                int(elapsed),
            )
        minutes = max(1, int(elapsed // 60))
        self._publish(
            AgentState.WAITING_FOR_NETWORK,
            f"后台检测已超过约 {minutes} 分钟没有返回；仍在重试",
        )
        return True

    def _start_watchdog(self, stop_event: threading.Event) -> threading.Thread:
        """起巡检线程：循环卡住时替它说话，进程其余部分照常工作。"""

        def watch() -> None:
            while not stop_event.wait(self.watchdog_interval_seconds):
                try:
                    self.check_stall()
                except Exception as exc:  # noqa: BLE001 - 巡检自己不能让 agent 退出
                    self.logger.error("agent watchdog failed: %s", type(exc).__name__)

        thread = threading.Thread(
            target=watch, name="youziauth-agent-watchdog", daemon=True
        )
        thread.start()
        return thread

    def _wait_for_next_cycle(self, stop_event: threading.Event, delay: float) -> bool:
        """等到下一轮、或者被手动指令/退出信号提前叫醒。返回 True 表示应当退出。

        两个 Event 不能一起 wait，所以 ``serve_forever`` 起了一个把 stop 转成唤醒信号
        的桥接线程；这样手动指令不必等满一个轮询周期，退出也不必等。
        """
        self._wake_event.wait(max(0.0, delay))
        self._wake_event.clear()
        return stop_event.is_set()

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

        def wake_on_stop() -> None:
            # stop 和「手动指令」都是「别睡了」，但 Event 不能一起 wait；
            # 把 stop 桥接成同一个唤醒信号，退出就能立刻生效。
            stop_event.wait()
            self._wake_event.set()

        threading.Thread(
            target=wake_on_stop, name="youziauth-agent-stop", daemon=True
        ).start()
        self._start_watchdog(stop_event)
        # 更新器没启用就先把原因写进快照：不必等到「有更新要检查」才让用户知道。
        self._publish_disabled_reason()
        delay = 0.0
        while not stop_event.is_set():
            self._recover_update()
            if self.update_due():
                # 只在网络循环的间隙跑一次；安装会终止本进程，重启后由 next_update_at
                # 拦住，不会变成「装一次、重启、马上又装」的循环。
                try:
                    self.periodic_update_check()
                except Exception as exc:  # noqa: BLE001 - never let an update stop the agent
                    self.logger.error("periodic update check failed: %s", type(exc).__name__)
            # 手动指令只留下「下一轮要强制登录」这个标记，真正的一轮在这里跑。
            force_login = self._take_pending_force_login()
            result = self.run_cycle(force_login=force_login)
            delay = result.next_delay
            if self._wait_for_next_cycle(stop_event, delay):
                break


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


def load_runtime_config(path: Path) -> campus_auth.AuthConfig:
    """Keep installed updates running while the user finishes account setup."""
    try:
        return load_agent_config(path)
    except (OSError, CredentialError, ValueError, configparser.Error):
        return campus_auth.AuthConfig()


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
    parser.add_argument("--repair-install-tasks", action="store_true")
    parser.add_argument("--ensure-agent", action="store_true")
    parser.add_argument("--install-user-sid")
    parser.add_argument("--first-install", action="store_true")
    return parser


def build_updater(app_dir: Path, allowed_user_sid: str | None = None) -> tuple:
    """后台自动更新的实装。返回 ``(updater, reason)``：拿到更新器时 reason 为空。

    只在冻结的正式安装版里启用：源码运行时没有可安装的 MSI，也没有稳定的安装目录，
    所以返回 ``None`` —— agent 照常做校园网认证，只是不碰更新。

    **为什么要把原因带出来**：这里原本只在失败时返回 ``None``。真实安装上它就一直返回
    ``None``（判断 VERSION 时只看安装根目录），而自动更新「没在跑」和「没什么可更新」
    在界面上长得一模一样 —— 这个功能整整一个版本都没生效却没人发现。原因必须能被说出来。
    """
    if not getattr(sys, "frozen", False):
        return None, "源码运行：后台自动更新只在安装版里启用"
    try:
        import auto_update
        from app_update import UpdateController
        from startup_tasks import (install_dir_for_current_process, run_agent_task,
                               run_tray_task)

        install_dir = install_dir_for_current_process()
        if install_dir is None:
            return None, "找不到安装目录，后台自动更新未启用"
        # 版本文件的位置交给 auto_update 判断：冻结的 one-folder 构建把 VERSION 放在
        # _internal 里，**不在**安装根目录。
        if not auto_update.read_installed_version(install_dir):
            return None, "读不到已安装版本，后台自动更新未启用"
        updater = auto_update.Updater(
            install_dir=install_dir,
            # app_dir 是 **app 数据目录**（%ProgramData%\youziauth），不是它的父目录。
            cache_dir=Path(app_dir) / "updates",
            controller_factory=partial(UpdateController, proxy_user_sid=allowed_user_sid),
            executable=install_dir / "youziauth.exe",
            relaunch=run_tray_task,
            agent_relaunch=run_agent_task,
            log_dir=Path(app_dir) / "updates",
        )
        return updater, ""
    except Exception as exc:  # noqa: BLE001 - an unavailable updater must not stop the agent
        return None, f"后台自动更新不可用（{type(exc).__name__}）"


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.ensure_agent:
        # Report observed recovery to the caller. MSI's Return="ignore" keeps
        # this commit action best effort; exit 0 alone must not mask an unhealthy
        # agent. A registered boot trigger can attempt startup later, but cannot
        # repair missing runtime files.
        import startup_tasks
        running = startup_tasks.ensure_agent_running()
        print("agent running" if running else "agent not responding; inspect installed payload and task")
        return 0 if running else 1
    if args.repair_install_tasks:
        # MSI invokes this as SYSTEM after its payload has committed. Dispatch
        # before config/network setup: the helper only maintains startup tasks.
        import startup_tasks
        try:
            startup_tasks.repair_installed_tasks(Path(sys.executable).resolve().parent,
                                                install_user_sid=args.install_user_sid,
                                                first_install=args.first_install)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            return 1
        return 0
    load_config = load_runtime_config if getattr(sys, "frozen", False) else load_agent_config
    try:
        config = load_config(args.config)
    except Exception as exc:  # noqa: BLE001 - agent reports bounded configuration failures.
        print(f"agent config error: {sanitized_detail(str(exc))}")
        return 2
    if getattr(sys, "frozen", False):
        # This is the privileged path. User-writable config may select credentials
        # and cadence, but it must never select a SYSTEM file-write destination.
        from system_storage import secure_system_storage, verified_system_storage
        try:
            app_dir = verified_system_storage()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            # An elevated user's startup helper runs as that administrator, not
            # SYSTEM. The registered SYSTEM task may initialize the fixed root
            # on first launch; the helper rejects pre-existing unsafe trees.
            if not args.allowed_user_sid:
                return 3
            try:
                app_dir = secure_system_storage(args.allowed_user_sid)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                return 3
        config = dataclasses.replace(config, log_file=str(app_dir / "logs" / "agent.log"))
        snapshot_path = app_dir / "runtime.json"
    else:
        snapshot_path = (program_data_root(args.config.parent.parent) / "runtime.json"
                     if args.config == machine_config_path(args.config.parent.parent)
                     else args.config.parent / "runtime.json")
    logger = configure_agent_logging(Path(config.log_file), args.verbose)
    # snapshot_path.parent 就是 app 数据目录（%ProgramData%\youziauth）；更新缓存与
    # MSI 日志都放在它下面，不能放到安装目录（Program Files）里去。
    updater, update_reason = build_updater(snapshot_path.parent, args.allowed_user_sid)
    if update_reason:
        logger.warning("automatic update disabled: %s", update_reason)
    agent = Agent(
        config_loader=lambda: dataclasses.replace(load_config(args.config), log_file=config.log_file),
        probe=NetworkProbe(),
        authenticator=lambda loaded, active_logger, force_login=False: (
            campus_auth.attempt_authentication(
                campus_auth.CampusAuthClient(loaded, active_logger),
                active_logger,
                force_login=force_login,
            )
        ),
        snapshot_path=snapshot_path,
        logger=logger,
        updater=updater,
        update_reason=update_reason,
    )
    if args.once:
        result = agent.run_cycle()
        print(result.snapshot.state)
        return 0 if result.snapshot.state in ("online_external", "online_campus") else 1
    agent.serve_forever(threading.Event(), allowed_user_sid=args.allowed_user_sid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
