import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path

import agent_ipc
import auto_update
import campus_auth
import network_probe
from agent_ipc import RuntimeSnapshot, read_snapshot, write_snapshot
from auth_runtime import AuthAttempt, AttemptKind
from campus_auth_agent import ONLINE_HEARTBEAT_SECONDS, Agent, build_arg_parser
from network_probe import NetworkObservation, NetworkProbe


class FakeProbe:
    def __init__(self, observations):
        self.observations = list(observations)

    def observe(self, config):
        if len(self.observations) > 1:
            return self.observations.pop(0)
        return self.observations[0]


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


class AgentArgumentTests(unittest.TestCase):
    def test_allowed_user_sid_is_accepted_for_pipe_acl(self):
        sid = "S-1-5-21-123-456-789-1001"

        arguments = build_arg_parser().parse_args(["--allowed-user-sid", sid])

        self.assertEqual(arguments.allowed_user_sid, sid)


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
                   update_interval_seconds=6 * 60 * 60):
        agent = Agent(
            config_loader=lambda: self.config,
            probe=probe,
            authenticator=authenticator,
            snapshot_path=self.snapshot_path,
            logger=self.logger,
            boot_id=boot_id,
            updater=updater,
            update_interval_seconds=update_interval_seconds,
        )
        self._agents.append(agent)
        return agent

    def test_an_update_request_returns_immediately_and_reports_the_result(self):
        # 检查加下载可能几分钟，而指令走在三秒超时的 named pipe 上：必须先回。
        started, finish = threading.Event(), threading.Event()

        class SlowUpdater:
            def run_cycle(inner, progress=None):
                started.set()
                finish.wait(3)
                return auto_update.UpdateStatus(state='installed', current_version='1.9.0',
                                                latest_version='1.9.0', progress=100,
                                                message='已自动更新到 v1.9.0。')

        agent = self.make_agent(FakeProbe([]), FakeAuthenticator([]), updater=SlowUpdater())
        try:
            snapshot = agent.request_update_check()
            self.assertTrue(started.wait(2), 'the update must actually start')
            # 回来的时候更新还没结束 —— 这就是「不阻塞指令通道」。
            self.assertNotEqual(snapshot.update.get('state'), 'installed')
            finish.set()
            deadline = time.monotonic() + 3
            while agent.snapshot.update.get('state') != 'installed' and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(agent.snapshot.update.get('state'), 'installed')
            # 状态经同一条运行时快照通道发布，界面照原样读。
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
                release.wait(3)
                return auto_update.UpdateStatus(state='up_to_date', message='已是最新。')

        updater = BlockingUpdater()
        agent = self.make_agent(FakeProbe([]), FakeAuthenticator([]), updater=updater)
        try:
            agent.request_update_check()
            self.assertTrue(entered.wait(2))
            agent.request_update_check()
            agent.request_update_check()
            release.set()
            deadline = time.monotonic() + 3
            while updater.calls < 1 and time.monotonic() < deadline:
                time.sleep(0.02)
            time.sleep(0.1)
            self.assertEqual(updater.calls, 1, 'concurrent checks must collapse into one')
        finally:
            release.set()

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
            self.assertLess(elapsed, 1.0, 'periodic_update_check must return at once')
            self.assertTrue(held.wait(2), 'the update must actually be running')
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

    def test_retry_command_clears_rejection_block(self):
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


if __name__ == "__main__":
    unittest.main()
