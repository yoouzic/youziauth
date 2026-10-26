import logging
import tempfile
import sys
import threading
import time
import unittest
from pathlib import Path

import agent_ipc

# 这些等待只在「测试自己放行之后」才需要走完，所以给得足够宽：
# 在 CI 上线程调度比开发机慢得多，卡着 3 秒会让状态发布落在轮询窗口之外 —— 
# 测试失败的原因就成了机器快慢，而不是被测行为。
UPDATE_RELEASE_TIMEOUT = 30
UPDATE_SETTLE_TIMEOUT = 30
from unittest.mock import patch

import auto_update
import campus_auth
import campus_auth_agent
import network_probe
from agent_ipc import RuntimeSnapshot, read_snapshot, write_snapshot
from auth_runtime import AgentState, AuthAttempt, AttemptKind
from campus_auth_agent import ONLINE_HEARTBEAT_SECONDS, Agent, build_arg_parser
from network_probe import NetworkObservation, NetworkProbe


class FakeProbe:
    def __init__(self, observations):
        self.observations = list(observations)
        self.calls = 0

    def observe(self, config):
        self.calls += 1
        if len(self.observations) > 1:
            return self.observations.pop(0)
        return self.observations[0]


class BlockingProbe:
    """一直卡在探测里：模拟 Wi-Fi 切换时 Windows 的 DNS 解析挂住、没有超时。"""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def observe(self, config):
        self.calls += 1
        self.entered.set()
        # 由测试放行，避免卡住的线程永远占着探测名额。
        self.release.wait(30)
        return NetworkObservation(True, False)


def wait_until(predicate, timeout=10.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class FakeAuthenticator:
    """Returns ``attempts`` for normal calls and ``forced_attempts`` for repairs."""

    def __init__(self, attempts, forced_attempts=None):
        self.attempts = list(attempts)
        self.forced_attempts = (
            None if forced_attempts is None else list(forced_attempts)
        )
        self.calls = 0
        self.force_flags = []

    def __call__(self, config, logger, force_login=False):
        self.calls += 1
        self.force_flags.append(force_login)
        source = self.attempts
        if force_login and self.forced_attempts is not None:
            source = self.forced_attempts
        if len(source) > 1:
            return source.pop(0)
        return source[0]


class FakeSessionVerifier:
    """Answers "does the portal hold a session?" without touching the network."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0

    def __call__(self, config, logger):
        self.calls += 1
        if len(self.statuses) > 1:
            return self.statuses.pop(0)
        return self.statuses[0]


class NetworkProbeTests(unittest.TestCase):
    def test_external_internet_short_circuits_portal_probe(self):
        calls = []
        probe = NetworkProbe(
            internet_check=lambda timeout: True,
            portal_check=lambda url, timeout: calls.append(url) or False,
            proxy_path_check=lambda timeout: True,
        )

        observation = probe.observe(campus_auth.AuthConfig(request_timeout_seconds=4))

        self.assertEqual(observation, NetworkObservation(True, False, True))
        self.assertEqual(calls, [])

    def test_failed_internet_check_probes_campus_portal(self):
        probe = NetworkProbe(
            internet_check=lambda timeout: False,
            portal_check=lambda url, timeout: url.startswith("http://222.198.127.170"),
            proxy_path_check=lambda timeout: True,
        )

        observation = probe.observe(campus_auth.AuthConfig())

        # No uplink means nothing can carry browsing either; the proxy path is
        # not claimed to work.
        self.assertEqual(observation, NetworkObservation(False, True, False))

    def test_a_working_uplink_reports_a_broken_proxy_path_separately(self):
        # The whole point: campus session fine, uplink fine, proxy node dead.
        probe = NetworkProbe(
            internet_check=lambda timeout: True,
            portal_check=lambda url, timeout: False,
            proxy_path_check=lambda timeout: False,
        )

        observation = probe.observe(campus_auth.AuthConfig())

        self.assertTrue(observation.internet_ok)
        self.assertFalse(observation.proxy_path_ok)

    def test_the_proxy_path_check_is_not_asked_without_an_uplink(self):
        asked = []
        probe = NetworkProbe(
            internet_check=lambda timeout: False,
            portal_check=lambda url, timeout: True,
            proxy_path_check=lambda timeout: asked.append(timeout) or True,
        )

        probe.observe(campus_auth.AuthConfig())

        self.assertEqual(asked, [])

    def test_probe_is_plain_http_like_windows_ncsi(self):
        # https://www.msftconnecttest.com/connecttest.txt is served by an Akamai
        # edge whose certificate does not cover the hostname, so TLS verification
        # always fails and internet_ok stayed False on a healthy network.
        for url, body in network_probe.CONNECTIVITY_PROBES:
            self.assertTrue(url.startswith("http://"), url)
            self.assertFalse(url.startswith("https://"), url)
            self.assertTrue(body)

        self.assertEqual(
            network_probe.CONNECTIVITY_URL,
            "http://www.msftconnecttest.com/connecttest.txt",
        )
        self.assertEqual(network_probe.CONNECTIVITY_BODY, "Microsoft Connect Test")

    def test_the_proxy_path_probes_are_not_ncsi_hosts(self):
        # If they were, a proxy that sends the NCSI hosts DIRECT would make both
        # questions identical and the distinction would be worthless.
        ncsi = {url for url, _ in network_probe.CONNECTIVITY_PROBES}
        for url in network_probe.PROXY_PATH_PROBES:
            self.assertNotIn(url, ncsi)
            self.assertTrue(url.startswith("http://"), url)

    def test_captive_portal_page_does_not_count_as_internet(self):
        class FakeResponse:
            status = 200
            headers = {}

            def read(self, size):
                return b"<html>Please sign in to the campus network</html>"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        original = network_probe.DIRECT_OPENER.open
        network_probe.DIRECT_OPENER.open = lambda request, timeout: FakeResponse()
        try:
            self.assertFalse(network_probe.check_external_internet(2))
        finally:
            network_probe.DIRECT_OPENER.open = original

    def test_uplink_probe_ignores_the_system_proxy(self):
        class FakeResponse:
            status = 200
            headers = {}

            def read(self, size):
                return b"Microsoft Connect Test"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        import urllib.request

        # What Clash/v2ray leave behind on this machine: a loopback proxy that
        # urllib would honour for every request.
        original_getproxies = urllib.request.getproxies
        original_open = network_probe.DIRECT_OPENER.open
        urllib.request.getproxies = lambda: {"http": "http://127.0.0.1:1"}
        network_probe.DIRECT_OPENER.open = lambda request, timeout: FakeResponse()
        try:
            self.assertTrue(network_probe.check_external_internet(2))
        finally:
            urllib.request.getproxies = original_getproxies
            network_probe.DIRECT_OPENER.open = original_open

        self.assertEqual(
            [
                handler
                for handler in network_probe.DIRECT_OPENER.handlers
                if isinstance(handler, urllib.request.ProxyHandler)
            ],
            [],
        )

    def test_the_uplink_is_also_asked_through_mainland_204_responders(self):
        # On a TUN setup "no proxy handler" does not mean "skips the proxy
        # rules", so the NCSI hosts alone cannot answer whether the uplink works.
        ncsi = {url for url, _ in network_probe.CONNECTIVITY_PROBES}
        self.assertTrue(network_probe.DIRECT_204_PROBES)
        for url in network_probe.DIRECT_204_PROBES:
            self.assertTrue(url.startswith("http://"), url)
            self.assertNotIn(url, ncsi)
            self.assertNotIn(url, network_probe.PROXY_PATH_PROBES)

    def test_a_dead_proxy_node_cannot_hide_a_working_uplink(self):
        # 2026-10-22 18:26: Clash's node was down, so both NCSI hosts failed,
        # and the agent reported the campus uplink as down for 13 minutes while
        # the session and China-direct traffic were healthy. The mainland 204
        # responders still go DIRECT and must keep that from happening.
        class No204Response:
            status = 204
            headers = {}

            def read(self, size):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        ncsi_urls = {url for url, _ in network_probe.CONNECTIVITY_PROBES}
        direct_urls = set(network_probe.DIRECT_204_PROBES)

        def fake_open(request, timeout):
            if request.full_url in ncsi_urls:
                raise OSError("proxy node closed the connection")
            if request.full_url in direct_urls:
                return No204Response()
            raise AssertionError(f"unexpected probe target {request.full_url}")

        original = network_probe.DIRECT_OPENER.open
        network_probe.DIRECT_OPENER.open = fake_open
        try:
            self.assertTrue(network_probe.check_external_internet(2))
        finally:
            network_probe.DIRECT_OPENER.open = original

    def test_a_captive_portal_page_still_fails_every_uplink_probe(self):
        # The extra endpoints must not turn a sign-in interception into "online".
        class CaptiveResponse:
            status = 200
            headers = {}

            def read(self, size):
                return b"<html>Please sign in</html>"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        original = network_probe.DIRECT_OPENER.open
        network_probe.DIRECT_OPENER.open = lambda request, timeout: CaptiveResponse()
        try:
            self.assertFalse(network_probe.check_external_internet(2))
        finally:
            network_probe.DIRECT_OPENER.open = original


class AgentArgumentTests(unittest.TestCase):
    def test_allowed_user_sid_is_accepted_for_pipe_acl(self):
        sid = "S-1-5-21-123-456-789-1001"

        arguments = build_arg_parser().parse_args(["--allowed-user-sid", sid])

        self.assertEqual(arguments.allowed_user_sid, sid)


class AgentStartupHelperTests(unittest.TestCase):
    def test_an_unhealthy_ensure_agent_result_has_a_failing_exit_status(self):
        with patch("startup_tasks.ensure_agent_running", return_value=False), \
             patch.object(campus_auth_agent, "load_agent_config") as load:
            self.assertEqual(campus_auth_agent.main(["--ensure-agent"]), 1)

        load.assert_not_called()


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self._agents = []
        self.snapshot_path = Path(self.temporary.name) / "runtime.json"
        self.config = campus_auth.AuthConfig(
            username="student",
            password="secret",
            check_interval_seconds=60,
        )
        self.logger = logging.getLogger(f"agent-test-{id(self)}")
        self.logger.addHandler(logging.NullHandler())

    def tearDown(self):
        # 更新检查在后台线程里跑，清理临时目录前要等它收工，否则它会往正在删的目录里写快照。
        for agent in self._agents:
            agent.wait_for_update(5)
        self.temporary.cleanup()

    def make_agent(self, probe, authenticator, boot_id="boot-1", updater=None,
                   update_interval_seconds=6 * 60 * 60, update_reason="",
                   config_loader=None, **agent_options):
        agent = Agent(
            config_loader=config_loader or (lambda: self.config),
            probe=probe,
            authenticator=authenticator,
            snapshot_path=self.snapshot_path,
            logger=self.logger,
            boot_id=boot_id,
            updater=updater,
            update_interval_seconds=update_interval_seconds,
            update_reason=update_reason,
            **agent_options,
        )
        self._agents.append(agent)
        return agent

    def test_an_update_request_returns_immediately_and_reports_the_result(self):
        # 检查加下载可能几分钟，而指令走在三秒超时的 named pipe 上：必须先回。
        started, finish = threading.Event(), threading.Event()

        class SlowUpdater:
            def run_cycle(inner, progress=None):
                started.set()
                finish.wait(UPDATE_RELEASE_TIMEOUT)
                return auto_update.UpdateStatus(state='installed', current_version='1.9.0',
                                                latest_version='1.9.0', progress=100,
                                                message='已自动更新到 v1.9.0。')

        agent = self.make_agent(FakeProbe([]), FakeAuthenticator([]), updater=SlowUpdater())
        try:
            snapshot = agent.request_update_check()
            self.assertTrue(started.wait(10), 'the update must actually start')
            # 回来的时候更新还没结束 —— 这就是「不阻塞指令通道」。
            self.assertNotEqual(snapshot.update.get('state'), 'installed')
            finish.set()
            # 内存状态先发布，文件随后原子替换。先等 worker 完成，再读磁盘；
            # Windows 下并发轮询文件会持有读句柄，可能阻止 os.replace。
            self.assertTrue(agent.wait_for_update(UPDATE_SETTLE_TIMEOUT),
                            'the update worker must finish publishing its result')
            self.assertEqual(agent.snapshot.update.get('state'), 'installed')
            written = agent_ipc.read_snapshot(self.snapshot_path)
            self.assertEqual(written.update.get('state'), 'installed')
        finally:
            finish.set()

    def test_a_second_request_while_one_is_running_is_ignored(self):
        entered, release = threading.Event(), threading.Event()

        class BlockingUpdater:
            def __init__(inner):
                inner.calls = 0

            def run_cycle(inner, progress=None):
                inner.calls += 1
                entered.set()
                release.wait(UPDATE_RELEASE_TIMEOUT)
                return auto_update.UpdateStatus(state='up_to_date', message='已是最新。')

        updater = BlockingUpdater()
        agent = self.make_agent(FakeProbe([]), FakeAuthenticator([]), updater=updater)
        try:
            agent.request_update_check()
            self.assertTrue(entered.wait(10))
            agent.request_update_check()
            agent.request_update_check()
            release.set()
            deadline = time.monotonic() + UPDATE_SETTLE_TIMEOUT
            while updater.calls < 1 and time.monotonic() < deadline:
                time.sleep(0.02)
            time.sleep(0.1)
            self.assertEqual(updater.calls, 1, 'concurrent checks must collapse into one')
        finally:
            release.set()

    def test_an_update_line_does_not_trail_an_empty_detail_field(self):
        # 2026-10-12 现场：detail 为空时行尾留着光秃秃的「detail=」，用户读起来像日志被截断了。
        class QuietUpdater:
            def run_cycle(inner, progress=None):
                return auto_update.UpdateStatus(state='up_to_date', current_version='1.8.31',
                                                latest_version='1.8.31', message='当前已是最新正式版。')

        agent = self.make_agent(FakeProbe([]), FakeAuthenticator([]), updater=QuietUpdater())
        with agent._update_lock:
            with self.assertLogs(agent.logger, level='INFO') as captured:
                agent._run_update_check('periodic')
        line = [entry for entry in captured.output if 'automatic update' in entry][-1]
        self.assertIn('state=up_to_date current=1.8.31 latest=1.8.31', line)
        self.assertNotIn('detail=', line, line)

    def test_a_failed_update_still_names_its_detail(self):
        class FailingUpdater:
            def run_cycle(inner, progress=None):
                return auto_update.UpdateStatus(state='error', current_version='1.8.31',
                                                latest_version='1.8.32', message='更新未完成',
                                                detail='签名校验失败')

        agent = self.make_agent(FakeProbe([]), FakeAuthenticator([]), updater=FailingUpdater())
        with agent._update_lock:
            with self.assertLogs(agent.logger, level='INFO') as captured:
                agent._run_update_check('periodic')
        line = [entry for entry in captured.output if 'automatic update' in entry][-1]
        self.assertIn('detail=签名校验失败', line)

    def test_the_update_command_is_accepted_and_carries_no_arguments(self):
        # 指令里不能有参数：提权端自己决定下载和安装什么。
        command = agent_ipc.AgentCommand.parse({'command': 'check-update'})
        self.assertEqual(command.command, 'check-update')
        self.assertEqual(command.to_payload(), {'command': 'check-update'})
        for payload in ({'command': 'check-update', 'path': 'C:\\evil.msi'},
                        {'command': 'check-update', 'version': '9.9.9'}):
            with self.subTest(payload=payload), self.assertRaises(agent_ipc.InvalidAgentCommand):
                agent_ipc.AgentCommand.parse(payload)

    def test_an_agent_without_an_updater_never_checks(self):
        # 源码运行时没有可安装的包，agent 照常做认证，只是不碰更新。
        agent = self.make_agent(FakeProbe([]), FakeAuthenticator([]))
        self.assertFalse(agent.update_due())
        before = agent.snapshot.update
        self.assertEqual(agent.request_update_check().update, before)
        self.assertEqual(agent.periodic_update_check().update, before)

    def test_the_periodic_schedule_is_set_before_the_work_so_a_restart_cannot_loop(self):
        class CountingUpdater:
            def __init__(inner):
                inner.calls = 0

            def run_cycle(inner, progress=None):
                inner.calls += 1
                return auto_update.UpdateStatus(state='up_to_date', message='已是最新。')

        updater = CountingUpdater()
        agent = self.make_agent(FakeProbe([]), FakeAuthenticator([]), updater=updater,
                                update_interval_seconds=300)
        self.assertTrue(agent.update_due(), 'the first check is due immediately')
        agent.periodic_update_check()
        # 排期是立刻生效的（安装会杀掉进程，重启后不该马上又装一遍）；
        # 实际检查在后台线程里跑，所以这里等它结束再数次数。
        self.assertFalse(agent.update_due())
        self.assertTrue(agent.wait_for_update(5), 'the background check must finish')
        self.assertEqual(updater.calls, 1)

    def test_a_slow_update_never_delays_the_network_heartbeat(self):
        # 更新要下载几十 MB，而它是插在网络循环间隙跑的。同步跑会把校园网认证推迟
        # 几分钟 —— 那是这个进程的本职工作，不能给更新让路。
        held, release = threading.Event(), threading.Event()

        class HangingUpdater:
            def run_cycle(inner, progress=None):
                held.set()
                release.wait(5)
                return auto_update.UpdateStatus(state='up_to_date', message='已是最新。')

        agent = self.make_agent(FakeProbe([NetworkObservation(True, False)]),
                                FakeAuthenticator([]), updater=HangingUpdater())
        try:
            started = time.monotonic()
            agent.periodic_update_check()
            elapsed = time.monotonic() - started
            # 主判据用锁而不是墙钟：更新还卡在 release.wait 上，所以检查一旦返回，
            # 锁必须仍然被后台线程持有 —— 这就是「没有等待」。墙钟只做一个很松的兜底
            # （CI runner 比开发机慢，1 秒的界会间歇性失败）。
            self.assertTrue(held.wait(5), 'the update must actually be running')
            self.assertFalse(
                agent._update_lock.acquire(blocking=False),
                'the work must still be in flight, so it cannot have been awaited')
            self.assertLess(elapsed, 5.0, 'periodic_update_check must not wait for the download')
            # 更新还卡着，但网络循环照常跑完一轮。
            result = agent.run_cycle()
            self.assertEqual(result.snapshot.state, 'online_external')
        finally:
            release.set()
            agent.wait_for_update(5)

    def test_waiting_for_an_update_returns_once_it_settles(self):
        agent = self.make_agent(FakeProbe([]), FakeAuthenticator([]))
        self.assertTrue(agent.wait_for_update(1), 'a free agent settles immediately')

    def test_hotspot_connection_skips_portal_login(self):
        authenticator = FakeAuthenticator([AuthAttempt(AttemptKind.REJECTED, "no")])
        agent = self.make_agent(FakeProbe([NetworkObservation(True, False)]), authenticator)

        result = agent.run_cycle()

        self.assertEqual(result.snapshot.state, "online_external")
        self.assertEqual(authenticator.calls, 0)
        self.assertFalse(result.notification_required)

    def test_a_healthy_agent_says_so_without_flooding_the_log(self):
        # A quiet log must not look like a stopped agent: entering the online state
        # logs at once, then it repeats on the heartbeat interval only.
        agent = self.make_agent(
            FakeProbe([NetworkObservation(True, False)]),
            FakeAuthenticator([AuthAttempt(AttemptKind.REJECTED, "no")]),
        )
        with self.assertLogs(agent.logger, level="INFO") as captured:
            agent.run_cycle()
            agent.run_cycle()
            self.assertEqual(sum("uplink is up" in line for line in captured.output), 1)

            agent._online_logged_at -= ONLINE_HEARTBEAT_SECONDS + 1
            agent.run_cycle()
            self.assertEqual(sum("uplink is up" in line for line in captured.output), 2)

    def test_returning_online_after_an_outage_reports_immediately(self):
        agent = self.make_agent(
            FakeProbe(
                [
                    NetworkObservation(True, False),   # online
                    NetworkObservation(False, False),  # portal gone
                    NetworkObservation(True, False),   # online again
                ]
            ),
            FakeAuthenticator([AuthAttempt(AttemptKind.REJECTED, "no")]),
        )
        with self.assertLogs(agent.logger, level="INFO") as captured:
            agent.run_cycle()
            agent.run_cycle()
            agent.run_cycle()

        # Two entries into the online state, and the second one must not have waited
        # for the interval: the outage in between is worth reporting.
        self.assertEqual(sum("uplink is up" in line for line in captured.output), 2)

    def test_a_broken_proxy_path_is_named_instead_of_claiming_all_is_well(self):
        authenticator = FakeAuthenticator([AuthAttempt(AttemptKind.REJECTED, "no")])
        agent = self.make_agent(
            FakeProbe([NetworkObservation(True, False, proxy_path_ok=False)]),
            authenticator,
        )

        result = agent.run_cycle()

        self.assertEqual(result.snapshot.state, "online_external")
        self.assertIn("代理", result.snapshot.detail)
        self.assertEqual(authenticator.calls, 0)

    def test_network_not_ready_uses_fast_retry_without_notification(self):
        authenticator = FakeAuthenticator([AuthAttempt(AttemptKind.REJECTED, "no")])
        agent = self.make_agent(FakeProbe([NetworkObservation(False, False)]), authenticator)

        first = agent.run_cycle()
        second = agent.run_cycle()

        self.assertEqual(first.next_delay, 2)
        self.assertEqual(second.next_delay, 5)
        self.assertEqual(first.snapshot.state, "waiting_for_network")
        self.assertEqual(authenticator.calls, 0)

    def test_explicit_rejection_blocks_automatic_retries_and_notifies_once(self):
        authenticator = FakeAuthenticator([AuthAttempt(AttemptKind.REJECTED, "账号或密码错误")])
        agent = self.make_agent(FakeProbe([NetworkObservation(False, True)]), authenticator)

        first = agent.run_cycle()
        second = agent.run_cycle()

        self.assertEqual(first.snapshot.state, "auth_failed")
        self.assertTrue(first.notification_required)
        self.assertEqual(first.snapshot.incident_id, second.snapshot.incident_id)
        self.assertEqual(authenticator.calls, 1)

    def test_retry_now_stays_synchronous_for_direct_callers(self):
        # retry_now 是给直接调用方/测试用的同步入口；走 named pipe 的 retry 指令
        # 不能走它（见 AgentLivenessTests），否则会卡住整条指令通道。
        authenticator = FakeAuthenticator(
            [
                AuthAttempt(AttemptKind.REJECTED, "账号或密码错误"),
                AuthAttempt(AttemptKind.LOGIN_SUCCEEDED, "ok"),
            ]
        )
        agent = self.make_agent(FakeProbe([NetworkObservation(False, True)]), authenticator)
        agent.run_cycle()

        snapshot = agent.retry_now()

        self.assertEqual(snapshot.state, "online_campus")
        self.assertEqual(authenticator.calls, 2)
        # The user asking for a retry must actually re-authenticate instead of
        # being short-circuited by the portal's "already authenticated" answer.
        self.assertEqual(authenticator.force_flags, [False, True])

    def test_unverified_session_is_never_reported_online(self):
        # The portal holds a session while the machine has no Internet. Nothing
        # can force a login out of this state, so the agent must publish the
        # truth and keep polling until the portal releases the record itself.
        authenticator = FakeAuthenticator(
            [AuthAttempt(AttemptKind.ALREADY_ONLINE, "already authenticated")]
        )
        agent = self.make_agent(FakeProbe([NetworkObservation(False, True)]), authenticator)

        results = [agent.run_cycle() for _ in range(5)]

        self.assertTrue(
            all(item.snapshot.state == "waiting_for_network" for item in results)
        )
        self.assertTrue(all(not item.notification_required for item in results))
        # It must not blame the campus session: the measured cause of "portal says
        # online but nothing loads" on this setup is the proxy/VPN path.
        self.assertIn("Clash", results[0].snapshot.detail)
        # Never logs out, never forces a login: every call is a plain status check.
        self.assertEqual(authenticator.force_flags, [False] * 5)

    def test_a_verified_session_still_says_what_the_session_is(self):
        # 2026-10-22: the user clicked 重新认证 because the tray said 外网不通, and
        # the answer was an English toast telling them to release a healthy
        # session. A forced attempt now verifies the session, and that sentence
        # must reach the status line instead of being flattened into the generic
        # "check Clash" line.
        held_detail = "门户仍持有有效会话，无需重新登录（会话 IP 10.65.104.219）"
        authenticator = FakeAuthenticator(
            [AuthAttempt(AttemptKind.ALREADY_ONLINE, held_detail)],
            forced_attempts=[AuthAttempt(AttemptKind.ALREADY_ONLINE, held_detail)],
        )
        agent = self.make_agent(FakeProbe([NetworkObservation(False, True)]), authenticator)

        snapshot = agent.retry_now()

        self.assertEqual(snapshot.state, "waiting_for_network")
        self.assertIn("门户仍持有有效会话", snapshot.detail)
        self.assertIn("10.65.104.219", snapshot.detail)
        self.assertIn("Clash", snapshot.detail)

    def test_unverified_session_recovers_when_the_portal_releases_it(self):
        # Measured on the live portal: the stale record clears by itself and the
        # normal login path then takes over, all without touching the session.
        authenticator = FakeAuthenticator(
            [
                AuthAttempt(AttemptKind.ALREADY_ONLINE, "already authenticated"),
                AuthAttempt(AttemptKind.LOGIN_SUCCEEDED, "success"),
            ]
        )
        agent = self.make_agent(FakeProbe([NetworkObservation(False, True)]), authenticator)

        waiting = agent.run_cycle()
        recovered = agent.run_cycle()

        self.assertEqual(waiting.snapshot.state, "waiting_for_network")
        self.assertEqual(recovered.snapshot.state, "online_campus")
        self.assertEqual(authenticator.force_flags, [False, False])

    def test_verified_already_online_is_still_accepted_when_internet_works(self):
        authenticator = FakeAuthenticator([AuthAttempt(AttemptKind.ALREADY_ONLINE, "already authenticated")])
        agent = self.make_agent(FakeProbe([NetworkObservation(True, False)]), authenticator)

        result = agent.run_cycle()

        # internet_ok short-circuits before the portal is ever consulted.
        self.assertEqual(result.snapshot.state, "online_external")
        self.assertEqual(authenticator.calls, 0)

    def test_three_transient_failures_become_one_failure_incident(self):
        authenticator = FakeAuthenticator(
            [AuthAttempt(AttemptKind.TRANSIENT_ERROR, "timeout")] * 3
        )
        agent = self.make_agent(FakeProbe([NetworkObservation(False, True)]), authenticator)

        results = [agent.run_cycle() for _ in range(3)]

        self.assertEqual([item.next_delay for item in results[:2]], [5, 15])
        self.assertEqual(results[0].snapshot.state, "waiting_for_network")
        self.assertEqual(results[2].snapshot.state, "auth_failed")
        self.assertTrue(results[2].notification_required)

    def test_suppression_survives_agent_restart_in_same_boot(self):
        agent = self.make_agent(
            FakeProbe([NetworkObservation(False, True)]),
            FakeAuthenticator([AuthAttempt(AttemptKind.REJECTED, "no")]),
        )
        agent.run_cycle()
        agent.suppress_notifications_for_boot()

        restarted = self.make_agent(
            FakeProbe([NetworkObservation(False, True)]),
            FakeAuthenticator([AuthAttempt(AttemptKind.REJECTED, "no")]),
        )

        self.assertTrue(restarted.snapshot.notifications_suppressed)

    def test_suppression_resets_for_new_boot(self):
        write_snapshot(
            self.snapshot_path,
            RuntimeSnapshot("old-boot", "auth_failed", True, "incident", "no"),
        )

        agent = self.make_agent(
            FakeProbe([NetworkObservation(False, False)]),
            FakeAuthenticator([AuthAttempt(AttemptKind.REJECTED, "no")]),
            boot_id="new-boot",
        )

        self.assertFalse(agent.snapshot.notifications_suppressed)
        self.assertEqual(read_snapshot(self.snapshot_path).boot_id, "new-boot")


class IneffectiveLoginTests(AgentLoopTests):
    """What the agent does when the portal keeps accepting logins that never stick.

    2026-10-13, 23:55 -> 08:08: 110 accepted logins and 110 "login succeeded" lines,
    the portal never once reporting a live session, and no alert at all. The status
    check and the login answer are two different questions; before this change only
    the second one was asked.
    """

    def test_a_login_that_never_takes_effect_is_not_reported_as_success(self):
        authenticator = FakeAuthenticator(
            [AuthAttempt(AttemptKind.LOGIN_UNVERIFIED, "门户已受理（success），但会话未生效")]
        )
        agent = self.make_agent(FakeProbe([NetworkObservation(False, True)]), authenticator)

        result = agent.run_cycle()

        # "login succeeded" is exactly the line that made eight hours of this look
        # like a working agent.
        self.assertNotEqual(result.snapshot.state, "online_campus")
        self.assertEqual(result.snapshot.state, "waiting_for_network")
        self.assertIn("未生效", result.snapshot.detail)
        self.assertFalse(result.notification_required, "a single retry is not an alert")

    def test_repeated_ineffective_logins_back_off_and_raise_one_incident(self):
        authenticator = FakeAuthenticator(
            [AuthAttempt(AttemptKind.LOGIN_UNVERIFIED, "门户已受理，但会话未生效")] * 8
        )
        agent = self.make_agent(FakeProbe([NetworkObservation(False, True)]), authenticator)

        results = [agent.run_cycle() for _ in range(8)]

        # Retrying once a minute for hours is what kept resetting the session the
        # portal had just filed; the cadence has to decay after the first retry.
        self.assertEqual(
            [item.next_delay for item in results[:6]], [60, 120, 300, 300, 300, 300]
        )
        # It becomes a real, notifiable failure instead of endless silent success.
        self.assertEqual(results[4].snapshot.state, "auth_failed")
        self.assertTrue(results[4].notification_required)
        # ...once. The same incident must not toast again on every cycle.
        self.assertFalse(any(item.notification_required for item in results[5:]))
        self.assertEqual(results[4].snapshot.incident_id, results[7].snapshot.incident_id)

    def test_unverified_login_snapshots_reach_the_real_notification_tracker_once(self):
        from windows_notifications import NotificationTracker

        authenticator = FakeAuthenticator([
            AuthAttempt(AttemptKind.LOGIN_UNVERIFIED, "门户已受理，但无法确认会话状态")
        ])
        agent = self.make_agent(FakeProbe([NetworkObservation(False, True)]), authenticator)
        tracker = NotificationTracker()

        snapshots = [agent.run_cycle().snapshot for _ in range(8)]
        toasts = [tracker.evaluate(snapshot) for snapshot in snapshots]

        self.assertTrue(all(toast is None for toast in toasts[:4]))
        self.assertIn("无法确认", toasts[4])
        self.assertTrue(all(toast is None for toast in toasts[5:]))

    def test_a_briefly_unreachable_portal_does_not_repeat_the_same_failure_toast(self):
        from windows_notifications import NotificationTracker

        authenticator = FakeAuthenticator([
            AuthAttempt(AttemptKind.LOGIN_UNVERIFIED, "门户已受理，但无法确认会话状态")
        ])
        agent = self.make_agent(FakeProbe(
            [NetworkObservation(False, True)] * 5 + [
                NetworkObservation(False, False), NetworkObservation(False, True),
            ]
        ), authenticator)
        tracker = NotificationTracker()

        snapshots = [agent.run_cycle().snapshot for _ in range(7)]
        toasts = [tracker.evaluate(snapshot) for snapshot in snapshots]

        self.assertEqual([index for index, toast in enumerate(toasts) if toast], [4])
        self.assertEqual(snapshots[5].state, "waiting_for_network")
        self.assertEqual(snapshots[4].incident_id, snapshots[6].incident_id)

    def test_a_verified_login_allows_a_later_unverified_incident_to_notify(self):
        from windows_notifications import NotificationTracker

        unverified = AuthAttempt(AttemptKind.LOGIN_UNVERIFIED, "门户已受理，但无法确认会话状态")
        authenticator = FakeAuthenticator(
            [unverified] * 5 + [AuthAttempt(AttemptKind.LOGIN_SUCCEEDED, "会话已确认")] + [unverified] * 5
        )
        agent = self.make_agent(FakeProbe([NetworkObservation(False, True)]), authenticator)
        tracker = NotificationTracker()

        snapshots = [agent.run_cycle().snapshot for _ in range(11)]
        toasts = [tracker.evaluate(snapshot) for snapshot in snapshots]

        self.assertEqual([index for index, toast in enumerate(toasts) if toast], [4, 10])
        self.assertNotEqual(snapshots[4].incident_id, snapshots[10].incident_id)

    def test_the_ineffective_login_cadence_is_bounded(self):
        self.assertEqual(campus_auth_agent.unverified_login_delay(1), 60)
        self.assertEqual(campus_auth_agent.unverified_login_delay(2), 120)
        self.assertEqual(campus_auth_agent.unverified_login_delay(3), 300)
        # It must not run away: every later attempt keeps the longest delay.
        for attempts in (4, 5, 50, 10_000):
            self.assertEqual(campus_auth_agent.unverified_login_delay(attempts), 300)

    def test_a_verified_login_clears_the_ineffective_login_counter(self):
        authenticator = FakeAuthenticator(
            [
                AuthAttempt(AttemptKind.LOGIN_UNVERIFIED, "未生效"),
                AuthAttempt(AttemptKind.LOGIN_UNVERIFIED, "未生效"),
                AuthAttempt(AttemptKind.LOGIN_SUCCEEDED, "success"),
                AuthAttempt(AttemptKind.LOGIN_UNVERIFIED, "未生效"),
            ]
        )
        agent = self.make_agent(FakeProbe([NetworkObservation(False, True)]), authenticator)

        results = [agent.run_cycle() for _ in range(4)]

        self.assertEqual(results[2].snapshot.state, "online_campus")
        # The counter restarted, so the fourth attempt is a first attempt again.
        self.assertEqual(results[3].next_delay, 60)
        self.assertEqual(results[3].snapshot.state, "waiting_for_network")

    def test_a_user_retry_resets_the_escalation(self):
        authenticator = FakeAuthenticator(
            [AuthAttempt(AttemptKind.LOGIN_UNVERIFIED, "未生效")] * 6,
            forced_attempts=[AuthAttempt(AttemptKind.LOGIN_UNVERIFIED, "未生效")],
        )
        agent = self.make_agent(FakeProbe([NetworkObservation(False, True)]), authenticator)
        for _ in range(5):
            agent.run_cycle()
        self.assertEqual(agent.unverified_logins, 5)

        agent.retry_now()

        self.assertEqual(agent.unverified_logins, 1)

    def test_reaching_the_uplink_clears_the_ineffective_login_counter(self):
        authenticator = FakeAuthenticator(
            [AuthAttempt(AttemptKind.LOGIN_UNVERIFIED, "未生效")] * 3
        )
        agent = self.make_agent(
            FakeProbe(
                [
                    NetworkObservation(False, True),
                    NetworkObservation(False, True),
                    NetworkObservation(True, False),
                ]
            ),
            authenticator,
        )
        for _ in range(3):
            agent.run_cycle()

        self.assertEqual(agent.unverified_logins, 0)


class LoginBlockTests(AgentLoopTests):
    """A refusal slows retries down; it must not stop them without a way back.

    2026-10-13 06:42: the portal answered 认证设备响应超时,请稍后再试!, and the agent
    stopped authenticating for 67 minutes while logging nothing whatsoever.
    """

    def test_a_blocked_agent_says_so_instead_of_logging_nothing(self):
        authenticator = FakeAuthenticator([AuthAttempt(AttemptKind.REJECTED, "账号或密码错误")])
        agent = self.make_agent(FakeProbe([NetworkObservation(False, True)]), authenticator)
        agent.run_cycle()

        with self.assertLogs(agent.logger, level="INFO") as captured:
            agent.run_cycle()
            agent.run_cycle()

        # Entry plus the heartbeat, not one line per silent cycle.
        self.assertEqual(
            sum("login is blocked" in line for line in captured.output), 1
        )
        self.assertEqual(authenticator.calls, 1)

    def test_a_password_refusal_stays_blocked_after_the_retry_deadline(self):
        authenticator = FakeAuthenticator([
            AuthAttempt(AttemptKind.REJECTED, "账号或密码错误"),
        ])
        agent = self.make_agent(FakeProbe([NetworkObservation(False, True)]), authenticator)
        agent.run_cycle()

        with patch.object(campus_auth_agent, "LOGIN_BLOCK_RETRY_SECONDS", 0):
            result = agent.run_cycle()
            agent.run_cycle()

        self.assertEqual(authenticator.calls, 1, "invalid credentials require a user/config change")
        self.assertEqual(result.snapshot.state, "auth_failed")

    def test_a_working_uplink_does_not_clear_a_bad_credentials_block(self):
        authenticator = FakeAuthenticator([
            AuthAttempt(AttemptKind.REJECTED, "账号或密码错误"),
        ])
        agent = self.make_agent(FakeProbe([
            NetworkObservation(False, True),
            NetworkObservation(True, False),
            NetworkObservation(False, True),
        ]), authenticator)

        results = [agent.run_cycle() for _ in range(3)]

        self.assertEqual([r.snapshot.state for r in results],
                         ["auth_failed", "online_external", "auth_failed"])
        self.assertEqual(authenticator.calls, 1, "connectivity is not a credentials change")
        self.assertEqual(results[-1].snapshot.detail, "账号或密码错误")

    def test_an_unreachable_portal_does_not_replace_the_stored_refusal_reason(self):
        authenticator = FakeAuthenticator([
            AuthAttempt(AttemptKind.REJECTED, "账号或密码错误"),
        ])
        agent = self.make_agent(FakeProbe([
            NetworkObservation(False, True),
            NetworkObservation(False, False),
            NetworkObservation(False, True),
        ]), authenticator)

        results = [agent.run_cycle() for _ in range(3)]

        self.assertEqual(authenticator.calls, 1)
        self.assertEqual(results[-1].snapshot.detail, "账号或密码错误")

    def test_a_working_uplink_does_clear_an_unclassified_temporary_block(self):
        authenticator = FakeAuthenticator([
            AuthAttempt(AttemptKind.REJECTED, "authentication service refused"),
        ])
        agent = self.make_agent(FakeProbe([
            NetworkObservation(False, True),
            NetworkObservation(True, False),
            NetworkObservation(False, True),
        ]), authenticator)

        for _ in range(3):
            agent.run_cycle()

        self.assertEqual(authenticator.calls, 2)

    def test_unknown_session_queries_do_not_blame_the_gateway(self):
        authenticator = FakeAuthenticator([
            AuthAttempt(AttemptKind.LOGIN_UNVERIFIED, "门户已受理，但无法确认会话状态"),
        ])
        agent = self.make_agent(FakeProbe([NetworkObservation(False, True)]), authenticator)
        results = [agent.run_cycle() for _ in range(5)]

        for result in results:
            self.assertIn("无法确认", result.snapshot.detail)
            self.assertNotIn("未放行", result.snapshot.detail)
            self.assertNotIn("未生效", result.snapshot.detail)
        self.assertEqual(results[-1].snapshot.state, "auth_failed")

    def test_a_block_expires_so_a_transient_refusal_cannot_stall_the_agent(self):
        authenticator = FakeAuthenticator(
            [
                AuthAttempt(AttemptKind.REJECTED, "认证设备响应超时,请稍后再试!"),
                AuthAttempt(AttemptKind.LOGIN_SUCCEEDED, "success"),
            ]
        )
        agent = self.make_agent(FakeProbe([NetworkObservation(False, True)]), authenticator)
        agent.run_cycle()
        self.assertEqual(authenticator.calls, 1)

        with patch.object(campus_auth_agent, "LOGIN_BLOCK_RETRY_SECONDS", 0):
            result = agent.run_cycle()

        self.assertEqual(authenticator.calls, 2, "the block must not be permanent")
        self.assertEqual(result.snapshot.state, "online_campus")

    def test_an_unreachable_portal_says_so_instead_of_logging_nothing(self):
        agent = self.make_agent(
            FakeProbe([NetworkObservation(False, False)]),
            FakeAuthenticator([AuthAttempt(AttemptKind.REJECTED, "no")]),
        )

        with self.assertLogs(agent.logger, level="INFO") as captured:
            agent.run_cycle()
            agent.run_cycle()

        self.assertEqual(
            sum("portal is not reachable" in line for line in captured.output), 1
        )


class AgentLivenessTests(AgentLoopTests):
    """手动指令不准在 pipe 线程做网络 I/O；探测有墙钟死线；卡住时由巡检说话。

    2026-10-24 14:08：用户刚在 Wi-Fi 之间切换过，handle_command("retry") 在 pipe
    handler 里同步跑了一整轮 run_cycle(force_login=True)，网络探测卡死（Windows 的
    getaddrinfo/DNS 解析没有超时），pipe 一直 busy，runtime.json 和 agent.log 一起
    静默，界面那条没有超时上界的读取就把过期的「连接正常」一直留在屏幕上。
    这一组测试锁住「再也不会那样静默地冻住」。
    """

    def test_the_retry_command_returns_without_doing_network_work(self):
        probe = BlockingProbe()
        authenticator = FakeAuthenticator([AuthAttempt(AttemptKind.LOGIN_SUCCEEDED, "ok")])
        agent = self.make_agent(probe, authenticator)

        started = time.monotonic()
        with self.assertLogs(agent.logger, level="INFO") as captured:
            response = agent.handle_command(agent_ipc.AgentCommand("retry"))
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.0, "the retry command must not wait for the network")
        self.assertTrue(response["ok"])
        self.assertEqual(probe.calls, 0, "no network I/O on the pipe thread")
        self.assertEqual(authenticator.calls, 0)
        self.assertIn("manual retry requested", " ".join(captured.output))
        self.assertTrue(agent._wake_event.is_set(), "the main loop must be woken")
        self.assertTrue(agent._take_pending_force_login(), "the next cycle must be forced")
        self.assertFalse(agent._take_pending_force_login(), "the flag is consumed once")

    def test_the_retry_command_clears_the_block_without_authenticating(self):
        authenticator = FakeAuthenticator(
            [
                AuthAttempt(AttemptKind.REJECTED, "账号或密码错误"),
                AuthAttempt(AttemptKind.LOGIN_SUCCEEDED, "ok"),
            ]
        )
        agent = self.make_agent(FakeProbe([NetworkObservation(False, True)]), authenticator)
        agent.run_cycle()
        self.assertEqual(authenticator.calls, 1)
        self.assertTrue(agent.login_block_requires_action)

        response = agent.handle_command(agent_ipc.AgentCommand("retry"))

        # 指令只清状态、叫醒主循环；真正的强制登录还没发生。
        self.assertEqual(authenticator.calls, 1, "the pipe thread must not authenticate")
        # 但快照必须马上变一下：命令立即返回，而一整轮探测可能要几十秒，中间界面若
        # 一个字都不变，点按钮的人会以为没点到（2026-10-26：122 次点击无可见变化）。
        self.assertEqual(response["snapshot"]["state"], "waiting_for_network")
        self.assertIn("正在重新检测", response["snapshot"]["detail"])
        self.assertEqual(read_snapshot(agent.snapshot_path).state, "waiting_for_network")
        self.assertFalse(agent.automatic_login_blocked)
        self.assertFalse(agent.login_block_requires_action)
        self.assertEqual(agent.transient_failures, 0)
        self.assertEqual(agent.unverified_logins, 0)

    def test_the_main_loop_runs_the_forced_cycle_the_retry_asked_for(self):
        authenticator = FakeAuthenticator(
            [AuthAttempt(AttemptKind.REJECTED, "账号或密码错误")],
            forced_attempts=[AuthAttempt(AttemptKind.LOGIN_SUCCEEDED, "ok")],
        )
        agent = self.make_agent(FakeProbe([NetworkObservation(False, True)]), authenticator)
        stop_event = threading.Event()

        class FakePipeServer:
            """代替真实 named pipe：只模拟「等连接」，不碰网络也不绑端口。"""

            def __init__(inner, name, handler, allowed_user_sid=None):
                inner.handler = handler

            def serve_once(inner):
                time.sleep(0.02)

        with patch.object(campus_auth_agent, "NamedPipeServer", FakePipeServer):
            loop = threading.Thread(target=agent.serve_forever, args=(stop_event,), daemon=True)
            loop.start()
            try:
                self.assertTrue(
                    wait_until(lambda: len(authenticator.force_flags) >= 1),
                    "the loop must run its periodic cycle",
                )
                self.assertEqual(authenticator.force_flags, [False])

                agent.handle_command(agent_ipc.AgentCommand("retry"))

                self.assertTrue(
                    wait_until(lambda: True in authenticator.force_flags),
                    "the retry must reach the loop as a forced cycle",
                )
                # 点击会先发布「正在重新检测」的临时快照，再等主循环跑完这一轮。
                self.assertTrue(
                    wait_until(lambda: agent.snapshot.state == "online_campus"),
                    "the forced cycle must publish its result",
                )
            finally:
                stop_event.set()
                loop.join(5)
        self.assertFalse(loop.is_alive(), "the main loop must stop promptly")

    def test_the_reload_config_command_does_not_run_a_cycle(self):
        configs = [
            self.config,
            campus_auth.AuthConfig(username="student", password="secret",
                                   check_interval_seconds=30),
        ]
        probe = FakeProbe([NetworkObservation(True, False)])
        authenticator = FakeAuthenticator([AuthAttempt(AttemptKind.REJECTED, "no")])
        agent = self.make_agent(probe, authenticator, config_loader=lambda: configs.pop(0))

        with self.assertLogs(agent.logger, level="INFO") as captured:
            response = agent.handle_command(agent_ipc.AgentCommand("reload-config"))

        self.assertTrue(response["ok"])
        self.assertEqual(probe.calls, 0, "reload-config must not run a cycle")
        self.assertEqual(authenticator.calls, 0)
        self.assertEqual(agent.config.check_interval_seconds, 30)
        self.assertEqual(agent.retry_policy.regular_interval_seconds, 30)
        self.assertTrue(agent._wake_event.is_set(), "the main loop must be woken")
        self.assertFalse(agent._take_pending_force_login(), "a reload must not force a login")
        self.assertIn("configuration reloaded", " ".join(captured.output))

    def test_the_probe_deadline_default_bounds_a_request_timeout(self):
        # 默认死线 = max(30s, 5 * request_timeout_seconds)；request_timeout 默认 8s。
        agent = self.make_agent(FakeProbe([]), FakeAuthenticator([]))
        self.assertEqual(agent.probe_deadline_seconds, 40.0)
        # 巡检阈值 = max(120s, 2 * 探测死线 + 30s)。
        self.assertEqual(agent.watchdog_stall_seconds, 120.0)
        self.assertEqual(agent.watchdog_interval_seconds, 15.0)

    def test_a_blocked_probe_times_out_instead_of_hanging_the_cycle(self):
        probe = BlockingProbe()
        agent = self.make_agent(probe, FakeAuthenticator([]), probe_deadline_seconds=0.2)
        try:
            started = time.monotonic()
            with self.assertLogs(agent.logger, level="WARNING") as captured:
                result = agent.run_cycle()
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 5.0, "the cycle must return at the deadline")
            self.assertGreaterEqual(elapsed, 0.15, "it must actually wait for the deadline")
            self.assertTrue(probe.entered.wait(2), "the probe really was started")
            self.assertEqual(result.snapshot.state, "waiting_for_network")
            self.assertIn("网络检测长时间没有返回", result.snapshot.detail)
            self.assertIn("仍在重试", result.snapshot.detail)
            # 「正常重试节奏」：第一次失败还是 2 秒后再来，不是把循环拖死。
            self.assertEqual(result.next_delay, 2)
            self.assertTrue(any("did not return" in line for line in captured.output))
            # 快照文件也是新的：界面不会读到一条过期状态。
            self.assertEqual(read_snapshot(self.snapshot_path).state, "waiting_for_network")
            self.assertEqual(len(agent._probe_threads), 1, "the hung worker is accounted for")
        finally:
            probe.release.set()
            for thread in list(agent._probe_threads):
                thread.join(5)

    def test_abandoned_probe_threads_are_capped(self):
        # 挂住的探测线程收不回来（DNS 卡在系统调用里），所以名额有上限：
        # 到顶就不再新开线程，直接按超时处理，绝不允许无界增长。
        probe = BlockingProbe()
        agent = self.make_agent(probe, FakeAuthenticator([]), probe_deadline_seconds=0.05)
        try:
            with patch.object(campus_auth_agent, "MAX_ABANDONED_PROBE_THREADS", 2):
                for _ in range(3):
                    result = agent.run_cycle()
                    self.assertEqual(result.snapshot.state, "waiting_for_network")
                    self.assertIn("网络检测长时间没有返回", result.snapshot.detail)
            self.assertEqual(len(agent._probe_threads), 2, "the cap must stop new threads")
            self.assertEqual(probe.calls, 2, "at the cap no new probe is launched")
        finally:
            probe.release.set()
            for thread in list(agent._probe_threads):
                thread.join(5)

    def test_the_capped_probe_recovers_after_the_retry_cooldown(self):
        # 到顶不等于永久停止探测：系统 DNS 可能只是暂时卡住，恢复后 agent 必须还能
        # 自己回来。冷却期一过就放行一次新探测；只有硬上限才是真的放弃。
        probe = BlockingProbe()
        agent = self.make_agent(probe, FakeAuthenticator([]), probe_deadline_seconds=0.05)
        try:
            with patch.object(campus_auth_agent, "MAX_ABANDONED_PROBE_THREADS", 1):
                agent.run_cycle()  # 第一个挂住的探测线程
                agent.run_cycle()  # 到顶且还在冷却期：不再新开线程
                self.assertEqual(probe.calls, 1)

                with patch.object(campus_auth_agent, "PROBE_AT_CAP_RETRY_SECONDS", 0.0):
                    agent.run_cycle()
                    self.assertEqual(
                        probe.calls, 2, "a capped agent must probe again after the cooldown"
                    )

                    with patch.object(campus_auth_agent, "PROBE_ABANDONED_HARD_CAP", 2):
                        agent.run_cycle()
                    self.assertEqual(
                        probe.calls, 2, "the hard cap must stop unbounded thread growth"
                    )
        finally:
            probe.release.set()
            for thread in list(agent._probe_threads):
                thread.join(5)

    def test_the_watchdog_publishes_a_fresh_stuck_snapshot(self):
        agent = self.make_agent(
            FakeProbe([NetworkObservation(True, False)]),
            FakeAuthenticator([AuthAttempt(AttemptKind.REJECTED, "no")]),
        )
        agent.run_cycle()
        self.assertEqual(agent.snapshot.state, "online_external")

        # 模拟一轮卡住的循环：卡住的循环不会再发布任何快照。
        agent._cycle_started_at = time.monotonic() - (agent.watchdog_stall_seconds + 5)
        with self.assertLogs(agent.logger, level="ERROR") as captured:
            self.assertTrue(agent.check_stall())

        self.assertEqual(agent.snapshot.state, "waiting_for_network")
        self.assertIn("后台检测已超过约", agent.snapshot.detail)
        self.assertIn("仍在重试", agent.snapshot.detail)
        self.assertEqual(read_snapshot(self.snapshot_path).state, "waiting_for_network")
        self.assertTrue(any("without returning" in line for line in captured.output))

        # ERROR 日志按 300 秒节流，但快照每次巡检都必须刷新：界面不能把过期的
        # online 当成当前状态。这里先把快照「弄旧」，再确认巡检重新发布。
        agent._publish(AgentState.ONLINE_EXTERNAL, "互联网连接正常")
        with self.assertNoLogs(agent.logger, level="ERROR"):
            self.assertTrue(agent.check_stall())
        self.assertEqual(agent.snapshot.state, "waiting_for_network")

    def test_the_watchdog_stays_quiet_when_nothing_is_stuck(self):
        agent = self.make_agent(
            FakeProbe([NetworkObservation(True, False)]),
            FakeAuthenticator([AuthAttempt(AttemptKind.REJECTED, "no")]),
        )
        with self.assertNoLogs(agent.logger, level="ERROR"):
            # 没有周期在跑
            self.assertFalse(agent.check_stall())
            # 周期刚开始，没到阈值
            agent._cycle_started_at = time.monotonic()
            self.assertFalse(agent.check_stall())
        agent._cycle_started_at = None

    def test_the_watchdog_thread_notices_a_stalled_loop(self):
        agent = self.make_agent(
            FakeProbe([NetworkObservation(True, False)]),
            FakeAuthenticator([AuthAttempt(AttemptKind.REJECTED, "no")]),
            watchdog_interval_seconds=0.02,
            watchdog_stall_seconds=0.05,
        )
        agent.run_cycle()
        self.assertEqual(agent.snapshot.state, "online_external")
        stop_event = threading.Event()
        agent._cycle_started_at = time.monotonic() - 1.0

        watchdog = agent._start_watchdog(stop_event)
        try:
            with self.assertLogs(agent.logger, level="ERROR") as captured:
                self.assertTrue(
                    wait_until(lambda: "后台检测已超过约" in agent.snapshot.detail),
                    "the watchdog must publish a stuck snapshot",
                )
            self.assertTrue(any("without returning" in line for line in captured.output))
        finally:
            stop_event.set()
            agent._cycle_started_at = None
            # 巡检线程必须在临时目录被删掉之前收工，否则它会往正在删除的目录里写快照。
            watchdog.join(5)
        self.assertFalse(watchdog.is_alive(), "the watchdog must stop with the agent")


class BuildUpdaterTests(unittest.TestCase):
    """build_updater is the switch for the whole unattended update path.

    It used to fail silently on every real install: it gated on VERSION sitting in the
    install root, which a frozen one-folder build does not do (VERSION lives in
    _internal). Nothing reported that, so the feature shipped permanently off.
    """

    def setUp(self):
        # resolve() 是必须的：Windows 上 TemporaryDirectory 给出的是 8.3 短路径
        # （C:/Users/RUNNER~1/...），而 install_dir_for_current_process 会把它 resolve 成
        # 长路径。CI runner 开着 8.3 名字，于是两个路径字符串不相等 —— 本地机器通常不是。
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.install = self.root / "install"
        (self.install / "_internal").mkdir(parents=True)
        (self.install / "_internal" / "VERSION").write_text("1.9.0\n", encoding="utf-8")
        (self.install / "youziauth.exe").write_bytes(b"stub")
        self.app_dir = self.root / "appdata"

    def build(self):
        import campus_auth_agent

        real_frozen, real_exe = getattr(sys, "frozen", None), sys.executable
        sys.frozen = True
        sys.executable = str(self.install / "youziauth-agent.exe")
        try:
            return campus_auth_agent.build_updater(self.app_dir)
        finally:
            sys.executable = real_exe
            if real_frozen is None:
                del sys.frozen
            else:
                sys.frozen = real_frozen

    def test_the_frozen_one_folder_layout_enables_the_updater(self):
        updater, reason = self.build()
        self.assertEqual(reason, "")
        self.assertIsNotNone(updater, "VERSION inside _internal must still enable updates")
        self.assertEqual(updater.install_dir, self.install)
        self.assertEqual(updater.executable, self.install / "youziauth.exe")

    def test_the_update_cache_and_logs_stay_out_of_the_install_directory(self):
        # 缓存写进 Program Files 有个实际毛病：MSI 修复/卸载会连它一起动，而往安装目录
        # 写日志还会把安装包弄成「需要修复」。
        updater, _ = self.build()
        self.assertEqual(updater.cache_dir, self.app_dir / "updates")
        self.assertEqual(updater.log_dir, self.app_dir / "updates")
        self.assertNotIn(str(self.install), str(updater.cache_dir))
        self.assertNotIn(str(self.install), str(updater.log_dir))

    def test_source_runs_get_no_updater_and_say_why(self):
        import campus_auth_agent

        updater, reason = campus_auth_agent.build_updater(self.app_dir)
        self.assertIsNone(updater)
        self.assertIn("安装版", reason)

    def test_a_missing_version_reports_a_reason_instead_of_failing_silently(self):
        (self.install / "_internal" / "VERSION").unlink()
        updater, reason = self.build()
        self.assertIsNone(updater)
        self.assertIn("读不到已安装版本", reason)


class AgentUpdateStatusTests(unittest.TestCase):
    """The interface must be able to tell "updater off" from "nothing to update"."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.snapshot_path = Path(self.temporary.name) / "runtime.json"
        config = campus_auth.AuthConfig(username="u", password="p", check_interval_seconds=60)
        logger = logging.getLogger(f"agent-status-{id(self)}")
        logger.addHandler(logging.NullHandler())
        self.make = lambda **kw: Agent(
            config_loader=lambda: config,
            probe=FakeProbe([]),
            authenticator=FakeAuthenticator([]),
            snapshot_path=self.snapshot_path,
            logger=logger,
            boot_id="boot-1",
            **kw,
        )

    def test_an_agent_without_an_updater_publishes_the_reason(self):
        agent = self.make(update_reason="读不到已安装版本，后台自动更新未启用")
        self.assertEqual(agent.snapshot.update, {})
        agent._publish_disabled_reason()
        published = agent.snapshot.update
        self.assertEqual(published["state"], "error")
        self.assertIn("读不到已安装版本", published["message"])
        self.assertEqual(read_snapshot(self.snapshot_path).update["state"], "error")

    def test_a_healthy_updater_publishes_no_disabled_reason(self):
        class Quiet:
            def run_cycle(inner, progress=None):
                return auto_update.UpdateStatus(state="up_to_date", message="已是最新。")

        agent = self.make(updater=Quiet(), update_reason="")
        agent._publish_disabled_reason()
        self.assertEqual(agent.snapshot.update, {})

    def test_the_reason_can_be_absent(self):
        agent = self.make()
        agent._publish_disabled_reason()
        self.assertEqual(agent.snapshot.update, {})


class UplinkProbeFalsePositiveTests(unittest.TestCase):
    """A captive-portal whitelist must not turn「认证」into a no-op.

    2026-10-26 现场：校园网会话已经过期，但门户放行了几个连通性探测域名，于是 uplink
    探测报「外网可通」。界面显示「外网可通，但代理/VPN 不通」，用户点了 122 次「重新
    认证」，每一次都在 internet_ok 分支直接返回 —— 既没问门户，也没尝试登录。与此同时
    Windows 自己弹着「需要执行的操作」。这一组测试锁住：门户的回答优先于 uplink 探测，
    而且点击必须有可见动作。
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.snapshot_path = Path(self.temporary.name) / "runtime.json"
        self.config = campus_auth.AuthConfig(
            username="student", password="secret", check_interval_seconds=60
        )
        self.logger = logging.getLogger(f"agent-false-positive-{id(self)}")
        self.logger.addHandler(logging.NullHandler())

    def make_agent(self, observation, authenticator, verifier=None):
        return Agent(
            config_loader=lambda: self.config,
            probe=FakeProbe([observation]),
            authenticator=authenticator,
            session_verifier=verifier,
            snapshot_path=self.snapshot_path,
            logger=self.logger,
            boot_id="boot-1",
        )

    def test_a_manual_retry_authenticates_even_when_the_uplink_probe_passed(self):
        verifier = FakeSessionVerifier([campus_auth.AuthStatus.UNAUTHENTICATED])
        authenticator = FakeAuthenticator(
            [AuthAttempt(AttemptKind.LOGIN_SUCCEEDED, "认证成功")]
        )
        agent = self.make_agent(
            NetworkObservation(True, False, proxy_path_ok=False), authenticator, verifier
        )

        pending = agent.request_retry()
        self.assertEqual(pending.state, "waiting_for_network")
        self.assertIn("正在重新检测", pending.detail)
        result = agent.run_cycle(force_login=agent._take_pending_force_login())

        self.assertEqual(verifier.calls, 1, "the portal, not the uplink probe, decides")
        self.assertEqual(authenticator.calls, 1, "the click must really authenticate")
        self.assertEqual(result.snapshot.state, "online_campus")
        self.assertIn("代理", result.snapshot.detail)

    def test_a_manual_retry_while_the_browsing_path_works_needs_no_relogin(self):
        # gstatic 204 都通了，说明普通 HTTP 真的被带出去了：此时校园网会话不可能还停在
        # 未认证，所以不必打门户，只要让点击有一个看得见的答案。
        verifier = FakeSessionVerifier([campus_auth.AuthStatus.UNAUTHENTICATED])
        authenticator = FakeAuthenticator([])
        agent = self.make_agent(NetworkObservation(True, False), authenticator, verifier)

        agent.request_retry()
        result = agent.run_cycle(force_login=agent._take_pending_force_login())

        self.assertEqual(verifier.calls, 0)
        self.assertEqual(authenticator.calls, 0)
        self.assertEqual(result.snapshot.state, "online_external")
        self.assertIn("无需重新认证", result.snapshot.detail)

    def test_a_broken_browsing_path_with_an_expired_session_authenticates_automatically(self):
        verifier = FakeSessionVerifier([campus_auth.AuthStatus.UNAUTHENTICATED])
        authenticator = FakeAuthenticator(
            [AuthAttempt(AttemptKind.LOGIN_SUCCEEDED, "认证成功")]
        )
        agent = self.make_agent(
            NetworkObservation(True, False, proxy_path_ok=False), authenticator, verifier
        )

        result = agent.run_cycle()

        self.assertEqual(verifier.calls, 1)
        self.assertEqual(authenticator.calls, 1)
        self.assertEqual(result.snapshot.state, "online_campus")

    def test_a_broken_browsing_path_with_a_live_session_still_blames_the_proxy(self):
        verifier = FakeSessionVerifier([campus_auth.AuthStatus.AUTHENTICATED])
        authenticator = FakeAuthenticator([])
        agent = self.make_agent(
            NetworkObservation(True, False, proxy_path_ok=False), authenticator, verifier
        )

        result = agent.run_cycle()

        self.assertEqual(result.snapshot.state, "online_external")
        self.assertIn("会话有效", result.snapshot.detail)
        self.assertIn("代理", result.snapshot.detail)
        self.assertEqual(authenticator.calls, 0)

    def test_a_broken_browsing_path_without_a_portal_answer_keeps_the_proxy_warning(self):
        verifier = FakeSessionVerifier([campus_auth.AuthStatus.UNKNOWN])
        authenticator = FakeAuthenticator([])
        agent = self.make_agent(
            NetworkObservation(True, False, proxy_path_ok=False), authenticator, verifier
        )

        result = agent.run_cycle()

        self.assertEqual(result.snapshot.state, "online_external")
        self.assertIn("代理/VPN", result.snapshot.detail)
        self.assertEqual(authenticator.calls, 0)

    def test_a_manual_retry_that_cannot_get_a_portal_answer_says_so(self):
        verifier = FakeSessionVerifier([campus_auth.AuthStatus.UNKNOWN])
        authenticator = FakeAuthenticator([])
        agent = self.make_agent(
            NetworkObservation(True, False, proxy_path_ok=False), authenticator, verifier
        )

        agent.request_retry()
        result = agent.run_cycle(force_login=agent._take_pending_force_login())

        self.assertEqual(result.snapshot.state, "online_external")
        self.assertIn("代理/VPN", result.snapshot.detail)
        self.assertIn("未能确认校园网会话", result.snapshot.detail)
        self.assertEqual(authenticator.calls, 0)

    def test_the_plain_online_path_does_not_ask_the_portal(self):
        verifier = FakeSessionVerifier([campus_auth.AuthStatus.UNAUTHENTICATED])
        authenticator = FakeAuthenticator([])
        agent = self.make_agent(NetworkObservation(True, False), authenticator, verifier)

        result = agent.run_cycle()

        self.assertEqual(result.snapshot.state, "online_external")
        self.assertEqual(result.snapshot.detail, "互联网连接正常")
        self.assertEqual(verifier.calls, 0, "a healthy uplink and browsing path needs no portal query")


if __name__ == "__main__":
    unittest.main()
