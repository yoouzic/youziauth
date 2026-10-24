import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from agent_ipc import (
    AgentCommand,
    InvalidAgentCommand,
    NamedPipeServer,
    RuntimeSnapshot,
    UiCommand,
    read_snapshot,
    send_command,
    write_snapshot,
)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_round_trip_is_non_sensitive(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime.json"
            expected = RuntimeSnapshot(
                boot_id="boot-1",
                state="online_campus",
                notifications_suppressed=False,
                incident_id="",
                detail="已认证",
            )

            write_snapshot(path, expected)

            self.assertEqual(read_snapshot(path), expected)
            serialized = path.read_text(encoding="utf-8").lower()
            self.assertNotIn("password", serialized)
            self.assertNotIn("querystring", serialized)

    def test_invalid_snapshot_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime.json"
            path.write_text('{"boot_id":"x","state":"made_up"}', encoding="utf-8")

            with self.assertRaises(ValueError):
                read_snapshot(path)


class CommandTests(unittest.TestCase):
    def test_command_vocabulary_accepts_retry(self):
        self.assertEqual(AgentCommand.parse({"command": "retry"}), AgentCommand("retry"))

    def test_command_vocabulary_rejects_unknown_actions(self):
        with self.assertRaises(InvalidAgentCommand):
            AgentCommand.parse({"command": "run-powershell"})

    def test_command_rejects_unexpected_fields(self):
        with self.assertRaises(InvalidAgentCommand):
            AgentCommand.parse({"command": "retry", "arguments": "whoami"})

    def test_ui_command_has_separate_fixed_vocabulary(self):
        self.assertEqual(UiCommand.parse({"command": "show"}), UiCommand("show"))
        with self.assertRaises(InvalidAgentCommand):
            UiCommand.parse({"command": "run-powershell"})

    @unittest.skipUnless(os.name == "nt", "named pipes are Windows-only")
    def test_real_named_pipe_round_trip(self):
        name = f"youziauth-test-{uuid.uuid4().hex}"
        server = NamedPipeServer(name, lambda command: {"ok": True, "command": command.command})
        thread = threading.Thread(target=server.serve_once, daemon=True)
        thread.start()

        response = send_command(name, AgentCommand("status"), timeout_ms=3000)

        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(response, {"ok": True, "command": "status"})


class _ClosingPipeServer(NamedPipeServer):
    """Accept and read one request, then close the pipe without replying."""

    def _write(self, handle, value):
        # Simulates the agent dying between reading the request and replying;
        # serve_once still closes the server handle in its finally block.
        raise OSError("test server closed the pipe instead of replying")


@unittest.skipUnless(os.name == "nt", "named pipes are Windows-only")
class BoundedPipeReadTests(unittest.TestCase):
    def start_server(self, handler, server_type=NamedPipeServer):
        name = f"youziauth-test-{uuid.uuid4().hex}"
        server = server_type(name, handler)
        errors = []

        def serve():
            try:
                server.serve_once()
            except OSError as exc:
                errors.append(exc)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        return name, thread, errors

    def test_an_absent_pipe_still_reports_the_connect_timeout(self):
        name = f"youziauth-test-missing-{uuid.uuid4().hex}"
        started = time.monotonic()

        with self.assertRaises(TimeoutError) as caught:
            send_command(name, AgentCommand("status"), timeout_ms=200)

        self.assertEqual(
            str(caught.exception), f"timed out connecting to agent pipe {name}"
        )
        self.assertLess(time.monotonic() - started, 2)

    def test_a_wedged_agent_times_out_without_hanging_the_caller(self):
        request_seen = threading.Event()
        release_reply = threading.Event()
        received = []

        def wedged_handler(command):
            received.append(command.command)
            request_seen.set()
            release_reply.wait(10)
            return {"ok": True, "command": command.command}

        name, thread, errors = self.start_server(wedged_handler)
        started = time.monotonic()
        try:
            with self.assertRaises(TimeoutError) as caught:
                send_command(name, AgentCommand("status"), timeout_ms=500)
            elapsed = time.monotonic() - started
            self.assertEqual(
                str(caught.exception),
                f"timed out waiting for a reply from agent pipe {name}",
            )
            self.assertTrue(
                request_seen.is_set(),
                "the deadline must bound a connected read, not just connect retries",
            )
            self.assertGreaterEqual(elapsed, 0.3)
            self.assertLess(elapsed, 3)
            self.assertEqual(
                received, ["status"], "a timed-out command must not be replayed"
            )
        finally:
            release_reply.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors, "the late reply must fail on the caller's closed pipe")

    def test_a_closed_agent_pipe_raises_a_bounded_oserror(self):
        name, thread, errors = self.start_server(
            lambda command: {"ok": True}, server_type=_ClosingPipeServer
        )
        try:
            with self.assertRaises(OSError) as caught:
                send_command(name, AgentCommand("status"), timeout_ms=1000)
            self.assertNotIsInstance(
                caught.exception,
                TimeoutError,
                "a closed pipe must not be reported as a reply timeout",
            )
            self.assertIn("winerror", str(caught.exception))
            self.assertLess(len(str(caught.exception)), 300)
        finally:
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
