import base64
import unittest

from agent_ipc import RuntimeSnapshot
from windows_notifications import (
    NotificationTracker,
    build_failure_toast,
    build_lptiyu_toast,
    build_result_toast,
    build_powershell_command,
    should_show_failure,
)


class NotificationXmlTests(unittest.TestCase):
    def test_failure_toast_has_exact_three_protocol_actions(self):
        xml = build_failure_toast("账号或密码错误")

        self.assertIn('content="重新认证"', xml)
        self.assertIn('arguments="youziauth://retry"', xml)
        self.assertIn('content="打开设置"', xml)
        self.assertIn('arguments="youziauth://settings"', xml)
        self.assertIn('content="本次不再提醒"', xml)
        self.assertIn('arguments="youziauth://suppress"', xml)
        self.assertEqual(xml.count('activationType="protocol"'), 3)
        self.assertNotIn("password", xml.lower())

    def test_detail_is_xml_escaped_and_bounded(self):
        xml = build_failure_toast("错误 <tag> & " + "x" * 500)

        self.assertIn("错误 &lt;tag&gt; &amp;", xml)
        self.assertNotIn("<tag>", xml)
        self.assertLess(len(xml), 2000)

    def test_lptiyu_toast_carries_its_own_title(self):
        xml = build_lptiyu_toast("还没有可用的跑步凭证")
        self.assertIn("乐跑", xml)
        self.assertIn("还没有可用的跑步凭证", xml)
        self.assertNotIn("寝室打卡", xml, "乐跑的通知不该顶着寝室打卡的标题")

    def test_success_result_has_no_action_buttons(self):
        xml = build_result_toast(True, "认证成功")

        self.assertIn("校园网认证成功", xml)
        self.assertNotIn("<actions>", xml)

    def test_powershell_command_uses_encoded_command(self):
        command = build_powershell_command(build_failure_toast("失败"))

        self.assertEqual(command[:4], ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand"])
        decoded = base64.b64decode(command[4]).decode("utf-16le")
        self.assertIn("ToastNotificationManager", decoded)
        self.assertNotIn("失败", decoded)


class NotificationDecisionTests(unittest.TestCase):
    def test_new_unsuppressed_failure_incident_notifies(self):
        snapshot = RuntimeSnapshot("boot", "auth_failed", False, "incident-1", "失败")

        self.assertTrue(should_show_failure(snapshot, last_incident_id=""))

    def test_same_or_suppressed_incident_does_not_notify(self):
        same = RuntimeSnapshot("boot", "auth_failed", False, "incident-1", "失败")
        suppressed = RuntimeSnapshot("boot", "auth_failed", True, "incident-2", "失败")

        self.assertFalse(should_show_failure(same, last_incident_id="incident-1"))
        self.assertFalse(should_show_failure(suppressed, last_incident_id=""))

    def test_tracker_emits_one_failure_toast_per_incident(self):
        tracker = NotificationTracker()
        failed = RuntimeSnapshot(
            "boot", "auth_failed", False, "incident-1", "账号或密码错误"
        )

        first = tracker.evaluate(failed)
        second = tracker.evaluate(failed)

        self.assertIn("重新认证", first or "")
        self.assertIsNone(second)

    def test_tracker_waits_for_new_snapshot_before_retry_result(self):
        tracker = NotificationTracker()
        failed = RuntimeSnapshot(
            "boot",
            "auth_failed",
            False,
            "incident-1",
            "账号或密码错误",
            "before-retry",
        )
        tracker.evaluate(failed)
        tracker.mark_retry(failed)

        self.assertIsNone(tracker.evaluate(failed))

        online = RuntimeSnapshot(
            "boot", "online_campus", False, "", "认证成功", "after-retry"
        )
        result = tracker.evaluate(online)
        self.assertIn("校园网认证成功", result or "")
        self.assertNotIn("重新认证", result or "")

    def test_tracker_reports_failed_retry_once(self):
        tracker = NotificationTracker()
        before = RuntimeSnapshot(
            "boot", "auth_failed", False, "incident-1", "失败", "before-retry"
        )
        tracker.evaluate(before)
        tracker.mark_retry(before)
        after = RuntimeSnapshot(
            "boot", "auth_failed", False, "incident-2", "仍然失败", "after-retry"
        )

        result = tracker.evaluate(after)

        self.assertIn("仍未成功", result or "")
        self.assertIsNone(tracker.evaluate(after))


if __name__ == "__main__":
    unittest.main()
