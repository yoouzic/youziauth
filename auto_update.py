# Copyright (C) 2026 yoouzic
# SPDX-License-Identifier: GPL-3.0-only

"""Unattended self-update for the elevated system agent.

Why this lives in the agent
---------------------------
The old path asked the user to confirm an update, and Windows asked for elevation
because the MSI writes ``Program Files`` (``Scope="perMachine"`` in
``packaging/youziauth.wxs``). That is correct but noisy: every release costs two
clicks plus a UAC prompt. The fix is the one Chrome and Edge use -- a long-lived
privileged process that updates the product on its own, so the user's single
elevation at install time keeps paying off. This app already has that process: the
SYSTEM logon/boot task running ``youziauth-agent.exe``.

The security rule that makes it safe
------------------------------------
**The privileged side fetches and verifies the package itself.** It never accepts a
path, digest, or signature from the unprivileged app. If it did, any process running
as the user could hand SYSTEM an arbitrary MSI -- a local privilege escalation. So
there is deliberately no "install this file" entry point anywhere in this module:
``Updater.run_cycle`` always starts from the GitHub release API and only ever installs
an artifact whose Ed25519 signature matches the key compiled into ``windows_update``.

Compensating for the missing human
----------------------------------
Silent installation removes the human check that used to notice a broken build. Two
things replace it:

* the install worker health-checks the program it relaunches (``healthy`` in the
  worker's final record) and writes its MSI log to a persistent file, so a failure
  leaves evidence instead of a silently vanished icon;
* the installed version on disk is re-read before every attempt, so a build that
  installs but cannot start is reported once and never reinstalled in a loop.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import sys
import tempfile
import traceback
import time
from pathlib import Path

from app_update import UpdateController
from windows_update import UpdateVerificationError
import update_recovery


STATUS_FILE = "update-status.json"
STATUS_LIMIT = 64 * 1024
MSI_LOG = "msi-install.log"
INSTALL_RESULT_WAIT_SECONDS = 10 * 60

# 一次「自动更新」尝试里，对「压根没拿到版本信息」的传输失败重试几次。66 MB 的包在
# 弱网下开头就断很常见；一次断开就等于这一整轮白跑，而下一轮要等几个小时。
_CHECK_ATTEMPTS = 3
_CHECK_BACKOFF = 20
_DOWNLOAD_WAIT_SECONDS = 60 * 60
_DOWNLOAD_JOIN_SECONDS = 60
# 一次传输「完全不动」多久就判停滞。一个小时的总额度管不住这种情况：连接还在、对端
# 不发字节，于是这 66 MB 既不失败也不完成，而升级排期已经被这一轮占住了（2026-11-12
# 现场有一轮从 19:12 一直挂到 23:07 才装完）。停滞立刻收工，续传文件留着，下次接着下。
_DOWNLOAD_STALL_SECONDS = 15 * 60
# 传输类的暂时失败（断网、下载停滞）隔多久再试。原来的「等下一个间隔」（6 小时）让
# 一次抖动等于半天不检查；这不是重试风暴 —— 目标是一条稳定的线路，不是一分钟一次。
_RETRY_AFTER_TRANSPORT_SECONDS = 30 * 60

# 界面已有的状态词表，沿用它们而不是另造一套；多出来的三个是后台自动更新自己的阶段。
STATES = ("idle", "checking", "downloading", "verifying", "ready", "up_to_date",
          "installing", "installed", "error")


@dataclasses.dataclass(frozen=True)
class UpdateStatus:
    """What the agent tells the interface. Contains no paths and no URLs."""

    state: str = "idle"
    current_version: str = ""
    latest_version: str = ""
    progress: int = 0
    checked: str = ""
    message: str = ""
    detail: str = ""
    changes: dict = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"unsupported update state: {self.state}")
        if type(self.progress) is not int or not 0 <= self.progress <= 100:
            raise ValueError("progress must be an integer percentage")
        if any(not isinstance(getattr(self, field), str) for field in (
                'current_version', 'latest_version', 'checked', 'message', 'detail')):
            raise ValueError('update text must be strings')
        if not isinstance(self.changes, dict):
            raise ValueError('changes must be an object')

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def parse(cls, value: object) -> "UpdateStatus | None":
        """Read a status block back, tolerating anything shaped wrong."""
        if not isinstance(value, dict):
            return None
        allowed = {field.name for field in dataclasses.fields(cls)}
        if set(value) - allowed:
            return None
        try:
            return cls(**value)
        except (TypeError, ValueError):
            return None


def read_status(path: Path) -> UpdateStatus | None:
    try:
        with Path(path).open('r', encoding='utf-8') as stream:
            raw = stream.read(STATUS_LIMIT + 1)
        if len(raw) > STATUS_LIMIT:
            return None
        return UpdateStatus.parse(json.loads(raw))
    except (OSError, ValueError, TypeError, RecursionError):
        return None


def write_status(path: Path, status: UpdateStatus) -> None:
    """Write the status atomically; a failure here must never break the agent."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(status.to_dict(), ensure_ascii=False, separators=(",", ":"))
        descriptor, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
        pass


def read_installed_version(install_dir: Path) -> str:
    """The version actually on disk, or ``''`` when it cannot be read.

    A frozen one-folder build ships ``VERSION`` **inside** ``_internal`` (PyInstaller's
    contents directory, which is also what ``sys._MEIPASS`` points at), not next to the
    executable. Looking only at the install root reads ``''``, and an unreadable version
    makes the whole unattended update refuse to run -- so both layouts are checked, plus
    the location the running process itself would resolve.
    """
    install_dir = Path(install_dir)
    candidates = [install_dir / "VERSION", install_dir / "_internal" / "VERSION"]
    running = Path(sys.executable) if getattr(sys, "frozen", False) else None
    if running is not None and running.parent.name == Path(install_dir).name:
        candidates.insert(0, running.parent / "VERSION")
    for candidate in candidates:
        try:
            value = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        parts = value.split(".")
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            return value
    return ""


def _timestamp() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="microseconds")


def _report(report: callable, status: UpdateStatus) -> UpdateStatus:
    if report is not None:
        try:
            report(status)
        except Exception:  # noqa: BLE001 - reporting must never break the update
            pass
    return status


class Updater:
    """Drive one unattended update attempt.

    Everything the network and the filesystem touch is injectable so the whole flow can
    be tested offline. The default wiring is the real one.
    """

    def __init__(
        self,
        install_dir: Path,
        cache_dir: Path,
        executable: Path | None = None,
        *,
        controller_factory=UpdateController,
        installer=None,
        agent_relaunch=None,
        report=None,
        relaunch=None,
        log_dir: Path | None = None,
    ):
        self.install_dir = Path(install_dir)
        self.cache_dir = Path(cache_dir)
        self.executable = Path(executable) if executable is not None else None
        self.log_dir = Path(log_dir) if log_dir is not None else self.cache_dir
        self._controller_factory = controller_factory
        self._installer = installer
        self._report = report
        self._relaunch = relaunch
        self._relaunch_agent = agent_relaunch
        self._restored_status = False
        self._last_install_record = ''
        # 这次失败值不值得早一点再试。``None`` 表示「按正常间隔」，由调用方（agent）
        # 决定间隔是多少 —— 更新器只回答「这是暂时的吗」，不去猜自己的排期。
        self.retry_after_seconds: float | None = None

    def _publish(self, status: UpdateStatus, progress=None) -> UpdateStatus:
        # The parent may be killed during installation. Preserve the last stage so
        # the restarted agent can join it with the detached worker's result.
        write_status(self.log_dir / STATUS_FILE, status)
        _report(self._report, status)
        if progress is not self._report:
            _report(progress, status)
        return status

    def recover_install_result(self) -> UpdateStatus | None:
        """Recover once at startup and whenever a detached worker finishes later.

        Results are only feedback, never installation inputs. An old record cannot
        overwrite a newer check, and a successful exit is confirmed against VERSION.
        """
        saved = read_status(self.log_dir / STATUS_FILE)
        restore = not self._restored_status
        self._restored_status = True
        record = update_recovery.read_install_result(self.log_dir / update_recovery.INSTALL_RESULT_FILE)
        consumed = self.log_dir / update_recovery.CONSUMED_RESULT_FILE
        if (record is not None and record.fingerprint != self._last_install_record
                and not update_recovery.is_consumed(consumed, record)):
            saved_time = update_recovery.timestamp(saved.checked) if saved is not None else None
            result_time = update_recovery.timestamp(record.checked)
            self._last_install_record = record.fingerprint
            if saved_time is None or result_time >= saved_time:
                status = self._status_from_install(record.result, record.version)
                changes = saved.changes if saved and saved.latest_version == record.version else {}
                status = self._publish(dataclasses.replace(status, checked=record.checked, changes=changes))
                # Preserve recoverability if persisting feedback failed. Marking
                # a result seen before its terminal status lands loses it on reboot.
                if read_status(self.log_dir / STATUS_FILE) == status:
                    update_recovery.mark_consumed(consumed, record)
                return status
            update_recovery.mark_consumed(consumed, record)
        if saved is not None and saved.state == 'installing':
            started = update_recovery.timestamp(saved.checked)
            age = ((dt.datetime.now(dt.timezone.utc) - started).total_seconds()
                   if started is not None else None)
            installed = read_installed_version(self.install_dir)
            if (age is not None and 0 <= age <= INSTALL_RESULT_WAIT_SECONDS
                    and installed == saved.latest_version):
                # MSI commit may start the agent before msiexec returns to the
                # detached worker. VERSION alone cannot announce completion.
                return _report(self._report, saved) if restore else None
            return self._publish(dataclasses.replace(
                saved, state='error', current_version=installed, checked=_timestamp(),
                message='未能恢复安装结果，请查看安装日志；后台稍后会重新检查。',
                detail='installer result missing or stale'))
        if restore and saved is not None and saved.state in ('installed', 'error', 'up_to_date'):
            if saved.state == 'installed' and saved.latest_version != read_installed_version(self.install_dir):
                return None
            return _report(self._report, saved)
        return None

    def _install(self, package, version, digest, signature, on_launch):
        from windows_update import install_msi

        installer = self._installer or install_msi
        return installer(package, self.executable, version, digest, on_launch,
                         signature, silent=True, log_dir=self.log_dir)

    def _restart_agent(self) -> bool:
        """Start the privileged agent again after the installer stopped it.

        The window coming back is not enough: the agent is the half that performs the
        *next* update, and its task only fires at boot. Leaving it down turns a working
        unattended update into a one-shot.
        """
        if self._relaunch_agent is None:
            return False
        try:
            return bool(self._relaunch_agent())
        except Exception:  # noqa: BLE001 - a failed restart is reported, not raised
            return False

    def _relaunch_tray(self) -> bool:
        """Put the window back in the user's session after the installer killed it.

        The worker relaunches the executable itself, but a SYSTEM process cannot always
        draw on the interactive desktop. The tray task exists for exactly this and runs
        as the user with an interactive token, so it is the reliable path.
        """
        if self._relaunch is None:
            return False
        try:
            return bool(self._relaunch())
        except Exception:  # noqa: BLE001 - a failed relaunch is reported, not raised
            return False

    def run_cycle(self, progress=None) -> UpdateStatus:
        """Check, verify, install silently, and report. One attempt, no loops.

        ``progress`` (optional) receives a status block as soon as the release is known,
        before the download starts. The interface needs that gap filled: the package is
        tens of megabytes, and "what changed" is exactly what the user wants to read
        while they wait.
        """
        current = read_installed_version(self.install_dir)
        # 每一轮都重新判断「这次失败是不是暂时的」。留着上一轮的结论会让一次断网
        # 之后的成功轮也被当成「马上再试」。
        self.retry_after_seconds = None
        # Starting a new cycle makes any previous completion historical. Consume
        # it before publishing a new timestamp so it cannot reappear in polling.
        self._restored_status = True
        previous = update_recovery.read_install_result(self.log_dir / update_recovery.INSTALL_RESULT_FILE)
        if previous is not None:
            self._last_install_record = previous.fingerprint
            update_recovery.mark_consumed(self.log_dir / update_recovery.CONSUMED_RESULT_FILE, previous)
        if not current:
            return self._publish(UpdateStatus(
                state="error", current_version="", checked=_timestamp(),
                message="无法读取本机版本，已跳过自动更新。",
                detail="the installed VERSION file could not be read"), progress)

        self._publish(UpdateStatus(state='checking', current_version=current,
                                   checked=_timestamp(), message='正在后台检查正式版…'), progress)

        controller = self._controller_factory(current, self.cache_dir, self.executable)
        try:
            status = None
            transient = False
            for attempt in range(_CHECK_ATTEMPTS):
                controller.check()
                worker = getattr(controller, "_worker", None)
                if worker is not None:
                    # 安装包下载要几分钟。趁它在下，把「发现新版本 + 更新内容」先报出去，
                    # 而不是等下完再一次性告诉用户。
                    self._report_interim(controller, progress, current)
                    waiting = self._wait_for_download(controller, worker, progress, current)
                    if waiting is not None:
                        # This controller still has a worker. End the cycle and
                        # cancel it in finally instead of calling check() again
                        # against a busy controller. Partial files remain resumable.
                        snapshot = controller.snapshot()
                        # 超时/停滞都是传输问题：续传文件还在，半小时后再试比起等满
                        # 一个间隔能早得多地接上。原来它们被当成「等下一轮」，那半天
                        # 里这台机器不会再看一眼新版本。
                        if waiting in ('timeout', 'stalled'):
                            self.retry_after_seconds = _RETRY_AFTER_TRANSPORT_SECONDS
                        message = ('后台更新下载超时，已保留续传文件，将在下次检查时重试。'
                                   if waiting == 'timeout' else
                                   '后台更新下载停滞，已保留续传文件，将在下次检查时重试。'
                                   if waiting == 'stalled' else
                                   '后台更新已停止，已保留续传文件。')
                        detail = ('download timeout' if waiting == 'timeout' else
                                  'download stalled' if waiting == 'stalled' else 'update stopped')
                        return self._publish(UpdateStatus(
                            state='error', current_version=current,
                            latest_version=str(snapshot.get('latest_version', '')),
                            progress=int(snapshot.get('progress', 0) or 0), checked=_timestamp(),
                            message=message, detail=detail))
                snapshot = controller.snapshot()
                status = self._status_from_check(snapshot, current)
                # 只重试传输类失败：控制器为这类失败打了 transient 标记。校验、安装
                # 之类是明确结论，重试等于反复撞同一堵墙。
                transient = bool(snapshot.get("transient"))
                if not transient or attempt + 1 >= _CHECK_ATTEMPTS:
                    break
                time.sleep(_CHECK_BACKOFF * (attempt + 1))
            if status.state != "ready":
                if transient and status.state == 'error':
                    # 断网、超时：这一轮已经在本进程内重试过三次，仍然不行，说明
                    # 线路暂时不通。别让它等满一整个间隔。
                    self.retry_after_seconds = _RETRY_AFTER_TRANSPORT_SECONDS
                elif status.state == "checking":
                    # 检查线程已经结束，却还停在「正在检查」：它没有给出结论。把它当作
                    # 一次没有结果的失败报出去，而不是让界面永远显示「正在检查」。
                    status = dataclasses.replace(
                        status, state="error", detail="check did not finish",
                        message="后台检查没有返回结果，将在下次检查时重试。")
                    self.retry_after_seconds = _RETRY_AFTER_TRANSPORT_SECONDS
                return self._publish(status)

            package, version, digest, signature = controller.package()
            # 已经装上了就直接收工：这既避免重复安装，也让「装完起不来」的版本
            # 只被报一次，而不是每次检查都重装一遍。
            if version == read_installed_version(self.install_dir):
                return self._publish(dataclasses.replace(
                    status, state="installed", current_version=version,
                    message=f"已经是 v{version}，无需重复安装。"))

            self._publish(dataclasses.replace(
                status, state="installing", progress=100,
                checked=_timestamp(), message=f"正在后台安装 v{version}…"), progress)
            launched = {"seen": False}

            def on_launch():
                launched["seen"] = True

            result = self._install(package, version, digest, signature, on_launch)
            tray = False
            agent = False
            # The detached worker owns restart in production. These callbacks
            # remain a fallback for installers without recovery metadata.
            if isinstance(result, dict) and result.get('code') == 0:
                tray = bool(result.get('relaunched')) or self._relaunch_tray()
                agent = bool(result.get('agent_restarted')) or self._restart_agent()
            status = self._status_from_install(result, version, tray)
            if agent and status.state == 'installed' and status.detail != 'reboot required':
                # 两个进程都由安装器停掉：窗口回来了、agent 也必须回来，
                # 否则下一次自动更新不会发生（它的任务只在开机时触发）。
                status = dataclasses.replace(status, detail="agent restarted")
            return self._publish(status)
        except UpdateVerificationError as exc:
            return self._publish(UpdateStatus(
                state="error", current_version=current, checked=_timestamp(),
                message=str(exc), detail="verification failed"))
        except Exception as exc:  # noqa: BLE001 - the agent must survive any update failure
            # 只留异常类型名是不够的：真实安装上出过一次 TypeError，日志里只有「TypeError」
            # 三个字，既不知道在哪一行，也不知道哪个值不对。把最后一段调用栈压缩进 detail，
            # 下次再出问题就能直接读出来。detail 会进状态文件和日志，所以只取帧位置。
            where = " > ".join(
                "{0}:{1}".format(Path(frame.filename).name, frame.lineno)
                for frame in traceback.extract_tb(exc.__traceback__)[-3:])
            return self._publish(UpdateStatus(
                state="error", current_version=current, checked=_timestamp(),
                message="后台自动更新未完成，将在下次检查时重试。",
                detail=("{0} @ {1}".format(type(exc).__name__, where) if where
                        else type(exc).__name__)))
        finally:
            controller.close()

    def _wait_for_download(self, controller, worker, progress, current) -> str | None:
        """Wait for the real transfer, with a bounded total and cooperative stop.

        The updater already runs off the authentication thread. A ten-minute join
        is not completion: a slow, healthy transfer may still own the partial file.

        Two deadlines, because they answer different questions:

        * ``_DOWNLOAD_WAIT_SECONDS`` bounds the whole transfer (a genuinely slow link
          is finite);
        * ``_DOWNLOAD_STALL_SECONDS`` bounds *progress*. A connection that stays open
          and stops delivering satisfies neither "finished" nor "failed", so the total
          deadline alone would pin this cycle for an hour -- and, with it, the schedule
          of the next check. Observed 2026-11-12: one cycle started at 19:12 and did
          not install anything until 23:07.
        """
        is_alive = getattr(worker, 'is_alive', None)
        if not callable(is_alive):
            # Compatibility with existing lightweight controller test doubles.
            worker.join(_DOWNLOAD_WAIT_SECONDS)
            return None
        started = time.monotonic()
        deadline = started + _DOWNLOAD_WAIT_SECONDS
        received = -1
        moved_at = started
        while is_alive():
            closed = getattr(controller, '_closed', None)
            if closed is not None and closed.is_set():
                return 'closed'
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return 'timeout'
            worker.join(min(_DOWNLOAD_JOIN_SECONDS, remaining))
            snapshot = self._report_interim(controller, progress, current)
            downloaded = None if snapshot is None else snapshot.get('downloaded_bytes')
            if isinstance(downloaded, int) and downloaded > received:
                received = downloaded
                moved_at = time.monotonic()
            elif received >= 0 and time.monotonic() - moved_at >= _DOWNLOAD_STALL_SECONDS:
                # Only a byte count we actually observed can prove a stall. A snapshot
                # without one is "progress unknown", which is not evidence of a hang.
                return 'stalled'
        return None

    def _report_interim(self, controller, progress, current):
        """Publish the release (and its notes) while the package is still downloading.

        Only the "checking / downloading / verifying" window is published: those are the
        states a controller passes through *before* the download finishes. Guessing at
        the rest would put a status block in front of the user that the next real update
        immediately contradicts.

        Returns the snapshot it read, so the caller can watch transfer progress without
        taking the controller's lock a second time.
        """
        try:
            snapshot = controller.snapshot()
        except Exception:  # noqa: BLE001 - a status peek must never break the update
            return None
        if progress is not None and str(snapshot.get("state", "")) in (
                "checking", "downloading", "verifying"):
            status = self._status_from_check(snapshot, current)
            if status.state in ("checking", "downloading", "verifying"):
                self._publish(status, progress)
        return snapshot

    def _status_from_check(self, snapshot: dict, current: str) -> UpdateStatus:
        """Translate the controller's check result into a status block.

        ``changes`` is carried for every state that has a release on the table, not just
        ``ready``: the agent publishes this block once, when the check returns, and the
        interface has to be able to show "what changed" while the package is still
        downloading. Dropping it here would hide the notes exactly when they matter.

        ``checking`` is one of this module's own states (``STATES``) and the one the
        controller enters before it knows anything; mapping it to ``error`` made the
        documented "发现 vX，正在读取更新说明" step impossible to publish.

        An error keeps the controller's own ``detail`` (the exception name, or a stage
        the controller named). Dropping it is why every failure in the installed
        product's log ended in a bare ``latest=`` with no cause.
        """
        state = str(snapshot.get("state", "error"))
        changes = snapshot.get("changes")
        if not isinstance(changes, dict):
            changes = {}
        known = state in ("checking", "ready", "up_to_date", "error")
        interesting = state in ("downloading", "verifying", "ready", "installing")
        if not isinstance(changes.get("entries"), list):
            changes = {}
        detail = ""
        if state == "error":
            detail = str(snapshot.get("detail", "") or "")
        elif not known and state not in ("downloading", "verifying", "installing"):
            # 不认识的词不是「没问题」，但也不该整个盖掉真正的结论：报成 error 并说明。
            state, detail = "error", "check failed"
        return UpdateStatus(
            state=state,
            current_version=current, latest_version=str(snapshot.get("latest_version", "")),
            progress=int(snapshot.get("progress", 0) or 0),
            # Persistence needs timezone-bearing timestamps to distinguish a new
            # check from a late or old installer result. The controller's UI stamp
            # is a naive local datetime and is unsuitable for that ordering.
            checked=_timestamp(),
            message=str(snapshot.get("message", "")),
            detail=detail,
            changes=changes if interesting else {},
        )

    def _status_from_install(self, result: object, version: str, tray: bool = False) -> UpdateStatus:
        if isinstance(result, dict) and type(result.get('error')) is int:
            from windows_update import _ERRORS
            error = result['error']
            return UpdateStatus(
                state='error', current_version=read_installed_version(self.install_dir),
                latest_version=version, checked=_timestamp(),
                message=_ERRORS.get(error, '后台更新辅助进程未完成，请查看安装日志。'),
                detail=f'worker error {error}')
        if not isinstance(result, dict) or type(result.get("code")) is not int:
            return UpdateStatus(
                state="error", current_version=read_installed_version(self.install_dir),
                latest_version=version, checked=_timestamp(),
                message="安装进程没有返回可用的结果，将在下次检查时重试。",
                detail="malformed installer result")
        code = result["code"]
        installed = read_installed_version(self.install_dir)
        if code in (0, 3010) and installed != version:
            return UpdateStatus(
                state='error', current_version=installed, latest_version=version,
                checked=_timestamp(), message=f'安装已结束，但本机版本未更新到 v{version}，请查看安装日志。',
                detail='installed version mismatch')
        if code == 3010:
            return UpdateStatus(
                state="installed", current_version=installed or version, latest_version=version,
                progress=100, checked=_timestamp(),
                message=f"v{version} 已安装，需要重启电脑后生效。", detail="reboot required")
        if code == 1602:
            return UpdateStatus(
                state="up_to_date", current_version=installed or "", latest_version=version,
                progress=100, checked=_timestamp(),
                message="安装被取消，当前版本未更新。", detail="cancelled")
        if code != 0:
            return UpdateStatus(
                state="error", current_version=installed or "", latest_version=version,
                progress=100, checked=_timestamp(),
                message=f"后台安装未完成（返回码 {code}），已记录日志，将在下次检查时重试。",
                detail=f"msiexec exit {code}")
        # 安装返回 0 之后仍然要看程序能不能跑：静默路径没有人工兜底。
        healthy = result.get("healthy")
        if healthy is False:
            reason = result.get("launch_exit_code")
            return UpdateStatus(
                state="error", current_version=installed or version, latest_version=version,
                progress=100, checked=_timestamp(),
                message=f"v{version} 已安装但程序没能启动，已停止重试并保留日志。",
                detail=f"health check failed (exit {reason})" if reason is not None
                else "health check failed")
        return UpdateStatus(
            state="installed", current_version=installed or version, latest_version=version,
            progress=100, checked=_timestamp(),
            message=f"已自动更新到 v{version}。",
            detail="agent restarted" if result.get('agent_restarted') is True
            else "relaunched" if (result.get("relaunched") or tray)
            else "installed, relaunch not confirmed")
