import unittest

from auth_runtime import AgentState, AttemptKind, RetryPolicy, classify_state


class RuntimeStateTests(unittest.TestCase):
    def test_working_external_internet_skips_campus_auth(self):
        self.assertEqual(
            classify_state(internet_ok=True, portal_reachable=False, attempt=None),
            AgentState.ONLINE_EXTERNAL,
        )

    def test_unreachable_portal_waits_without_auth_failure(self):
        self.assertEqual(
            classify_state(internet_ok=False, portal_reachable=False, attempt=None),
            AgentState.WAITING_FOR_NETWORK,
        )

    def test_campus_online_maps_to_online_campus(self):
        self.assertEqual(
            classify_state(False, True, AttemptKind.ALREADY_ONLINE),
            AgentState.ONLINE_CAMPUS,
        )

    def test_explicit_rejection_is_immediate_failure(self):
        self.assertEqual(
            classify_state(False, True, AttemptKind.REJECTED),
            AgentState.AUTH_FAILED,
        )

    def test_transient_error_is_not_yet_a_final_failure(self):
        self.assertEqual(
            classify_state(False, True, AttemptKind.TRANSIENT_ERROR),
            AgentState.WAITING_FOR_NETWORK,
        )

    def test_an_unverified_login_is_not_online_campus(self):
        # Accepted login requests alone do not confirm a working session.
        self.assertEqual(
            classify_state(False, True, AttemptKind.LOGIN_UNVERIFIED),
            AgentState.WAITING_FOR_NETWORK,
        )

    def test_startup_delays_then_use_regular_interval(self):
        policy = RetryPolicy(regular_interval_seconds=60)

        self.assertEqual([policy.delay(index) for index in range(6)], [2, 5, 10, 15, 60, 60])

    def test_regular_interval_caps_short_startup_delays(self):
        policy = RetryPolicy(regular_interval_seconds=3)

        self.assertEqual([policy.delay(index) for index in range(4)], [2, 3, 3, 3])


if __name__ == "__main__":
    unittest.main()
