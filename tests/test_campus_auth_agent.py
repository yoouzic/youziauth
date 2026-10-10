import logging
import tempfile
import unittest
from pathlib import Path

import campus_auth
import network_probe
from agent_ipc import RuntimeSnapshot, read_snapshot, write_snapshot
from auth_runtime import AuthAttempt, AttemptKind
from campus_auth_agent import Agent, build_arg_parser
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
        )

        observation = probe.observe(campus_auth.AuthConfig(request_timeout_seconds=4))

        self.assertEqual(observation, NetworkObservation(True, False))
        self.assertEqual(calls, [])

    def test_failed_internet_check_probes_campus_portal(self):
        probe = NetworkProbe(
            internet_check=lambda timeout: False,
            portal_check=lambda url, timeout: url.startswith("http://222.198.127.170"),
        )

        observation = probe.observe(campus_auth.AuthConfig())

        self.assertEqual(observation, NetworkObservation(False, True))

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
        self.snapshot_path = Path(self.temporary.name) / "runtime.json"
        self.config = campus_auth.AuthConfig(
            username="student",
            password="secret",
            check_interval_seconds=60,
        )
        self.logger = logging.getLogger(f"agent-test-{id(self)}")
        self.logger.addHandler(logging.NullHandler())

    def tearDown(self):
        self.temporary.cleanup()

    def make_agent(self, probe, authenticator, boot_id="boot-1"):
        return Agent(
            config_loader=lambda: self.config,
            probe=probe,
            authenticator=authenticator,
            snapshot_path=self.snapshot_path,
            logger=self.logger,
            boot_id=boot_id,
        )

    def test_hotspot_connection_skips_portal_login(self):
        authenticator = FakeAuthenticator([AuthAttempt(AttemptKind.REJECTED, "no")])
        agent = self.make_agent(FakeProbe([NetworkObservation(True, False)]), authenticator)

        result = agent.run_cycle()

        self.assertEqual(result.snapshot.state, "online_external")
        self.assertEqual(authenticator.calls, 0)
        self.assertFalse(result.notification_required)

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
