import configparser
import datetime as dt
import queue
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import campus_auth_gui
import agent_health
import agent_ipc
import windows_tray
from windows_credentials import CredentialStore


class ReversibleProtector:
    def protect(self, value: bytes) -> bytes:
        return b"protected:" + value[::-1]

    def unprotect(self, value: bytes) -> bytes:
        return value[len(b"protected:") :][::-1]


class GuiSettingsTests(unittest.TestCase):
    def write_config(self, path: Path, values: dict[str, str]) -> None:
        parser = configparser.ConfigParser(interpolation=None)
        parser["auth"] = values
        with path.open("w", encoding="utf-8") as file:
            parser.write(file)

    def read_config(self, path: Path) -> configparser.ConfigParser:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(path, encoding="utf-8")
        return parser

    def test_loads_editable_settings_without_resolving_password_env(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.ini"
            self.write_config(
                config_path,
                {
                    "portal_url": "http://222.198.127.170/",
                    "username": "student",
                    "password": "",
                    "password_env": "MISSING_PASSWORD_ENV",
                    "check_interval_seconds": "45",
                    "log_file": "custom.log",
                },
            )

            settings = campus_auth_gui.load_gui_settings(config_path)

            self.assertEqual(settings.username, "student")
            self.assertEqual(settings.password, "")
            self.assertEqual(settings.check_interval_seconds, 45)
            self.assertEqual(settings.log_file, "custom.log")

    def test_save_updates_editable_values_and_preserves_auth_details(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.ini"
            self.write_config(
                config_path,
                {
                    "portal_url": "http://222.198.127.170/",
                    "login_url": "http://222.198.127.170/eportal/index.jsp?wlanuserip=1.2.3.4",
                    "username": "old-student",
                    "password": "",
                    "password_env": "CAMPUS_AUTH_PASSWORD",
                    "service": "%E9%BB%98%E8%AE%A4",
                    "check_interval_seconds": "60",
                    "request_timeout_seconds": "8",
                    "password_encrypt": "auto",
                    "log_file": "campus_auth.log",
                },
            )
            store = CredentialStore(Path(temp_dir) / "secure", ReversibleProtector())

            campus_auth_gui.save_gui_settings(
                config_path,
                campus_auth_gui.GuiSettings(
                    username="new-student",
                    password="new-secret",
                    check_interval_seconds=30,
                    log_file="campus_auth.log",
                ),
                credential_store=store,
            )

            auth = self.read_config(config_path)["auth"]
            self.assertEqual(auth["username"], "new-student")
            self.assertEqual(auth["password"], "")
            self.assertEqual(auth["password_env"], "")
            self.assertEqual(store.load_password(), "new-secret")
            self.assertEqual(auth["check_interval_seconds"], "30")
            self.assertEqual(
                auth["login_url"],
                "http://222.198.127.170/eportal/index.jsp?wlanuserip=1.2.3.4",
            )
            self.assertEqual(auth["service"], "%E9%BB%98%E8%AE%A4")
            self.assertEqual(auth["password_encrypt"], "auto")

    def test_save_preserves_existing_password_when_password_field_is_blank(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.ini"
            self.write_config(
                config_path,
                {
                    "portal_url": "http://222.198.127.170/",
                    "username": "student",
                    "password": "",
                    "password_env": "CAMPUS_AUTH_PASSWORD",
                    "check_interval_seconds": "60",
                },
            )
            store = CredentialStore(Path(temp_dir) / "secure", ReversibleProtector())
            store.save_password("existing-secret")

            campus_auth_gui.save_gui_settings(
                config_path,
                campus_auth_gui.GuiSettings(
                    username="student",
                    password="",
                    check_interval_seconds=120,
                    log_file="campus_auth.log",
                ),
                credential_store=store,
            )

            auth = self.read_config(config_path)["auth"]
            self.assertEqual(auth["password"], "")
            self.assertEqual(auth["password_env"], "")
            self.assertEqual(auth["check_interval_seconds"], "120")
            self.assertEqual(store.load_password(), "existing-secret")

    def test_default_config_path_uses_program_data(self):
        path = campus_auth_gui.default_config_path(
            program_data_root=Path("C:/ProgramData-Test")
        )

        self.assertEqual(
            path,
            Path("C:/ProgramData-Test") / "youziauth" / "config.ini",
        )

    def test_ensure_user_config_migrates_legacy_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            appdata = Path(temp_dir) / "Roaming"
            legacy_config = appdata / "CampusNetworkAuth" / "config.ini"
            legacy_config.parent.mkdir(parents=True)
            legacy_config.write_text(
                "[auth]\nusername = legacy-student\npassword = legacy-secret\nlog_file = campus_auth.log\n",
                encoding="utf-8",
            )
            new_config = campus_auth_gui.default_config_path(Path(temp_dir) / "ProgramData")
            store = CredentialStore(new_config.parent, ReversibleProtector())

            created = campus_auth_gui.ensure_user_config(
                new_config,
                legacy_paths=[legacy_config],
                credential_store=store,
            )

            self.assertEqual(created, new_config)
            self.assertTrue(new_config.exists())
            self.assertIn("legacy-student", new_config.read_text(encoding="utf-8"))
            self.assertTrue(legacy_config.exists())
            self.assertEqual(self.read_config(new_config)["auth"]["password"], "")
            self.assertEqual(self.read_config(legacy_config)["auth"]["password"], "")
            self.assertEqual(store.load_password(), "legacy-secret")

    def test_existing_machine_config_scrubs_matching_legacy_plaintext(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            new_config = root / "ProgramData" / "youziauth" / "config.ini"
            new_config.parent.mkdir(parents=True)
            new_config.write_text(
                "[auth]\nusername = student\npassword =\nlog_file = campus_auth.log\n",
                encoding="utf-8",
            )
            legacy_config = root / "Roaming" / "youziauth" / "config.ini"
            legacy_config.parent.mkdir(parents=True)
            legacy_config.write_text(
                "[auth]\nusername = student\npassword = legacy-secret\nlog_file = campus_auth.log\n",
                encoding="utf-8",
            )
            store = CredentialStore(new_config.parent, ReversibleProtector())
            store.save_password("legacy-secret")

            campus_auth_gui.ensure_user_config(
                new_config,
                legacy_paths=[legacy_config],
                credential_store=store,
            )

            self.assertEqual(self.read_config(legacy_config)["auth"]["password"], "")

    def test_ensure_user_config_creates_config_from_template(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            appdata = Path(temp_dir) / "Roaming"
            template = Path(temp_dir) / "config.example.ini"
            template.write_text(
                "[auth]\nusername = YOUR_STUDENT_ID\nlog_file = campus_auth.log\n",
                encoding="utf-8",
            )
            config_path = campus_auth_gui.default_config_path(Path(temp_dir) / "ProgramData")

            created = campus_auth_gui.ensure_user_config(
                config_path,
                template,
                legacy_paths=[],
                credential_store=CredentialStore(config_path.parent, ReversibleProtector()),
            )

            self.assertEqual(created, config_path)
            self.assertTrue(config_path.exists())
            self.assertIn("YOUR_STUDENT_ID", config_path.read_text(encoding="utf-8"))


class StartupOptionTests(unittest.TestCase):
    def test_startup_shortcut_path_uses_expected_name(self):
        startup_dir = Path("C:/Users/Test/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup")

        self.assertEqual(
            campus_auth_gui.startup_shortcut_path(startup_dir),
            startup_dir / "youziauth.lnk",
        )

    def test_is_startup_enabled_accepts_legacy_shortcut(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            startup_dir = Path(temp_dir)
            legacy_shortcut = startup_dir / "CampusNetworkAuth.lnk"
            legacy_shortcut.write_text("legacy", encoding="utf-8")

            self.assertTrue(
                campus_auth_gui.is_startup_enabled(startup_dir / "youziauth.lnk")
            )

    def test_startup_launch_spec_uses_packaged_executable_when_frozen(self):
        spec = campus_auth_gui.startup_launch_spec(
            executable=Path("C:/Program Files/youziauth/youziauth.exe"),
            script_path=Path("D:/source/campus_auth_gui.py"),
            frozen=True,
        )

        self.assertEqual(
            spec.target_path,
            Path("C:/Program Files/youziauth/youziauth.exe"),
        )
        self.assertEqual(spec.arguments, "--tray-startup")
        self.assertEqual(
            spec.working_directory,
            Path("C:/Program Files/youziauth"),
        )

    def test_startup_launch_spec_prefers_pythonw_for_source_runs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            python = Path(temp_dir) / "python.exe"
            pythonw = Path(temp_dir) / "pythonw.exe"
            script = Path(temp_dir) / "campus_auth_gui.py"
            python.write_text("", encoding="utf-8")
            pythonw.write_text("", encoding="utf-8")

            spec = campus_auth_gui.startup_launch_spec(
                executable=python,
                script_path=script,
                frozen=False,
            )

            self.assertEqual(spec.target_path, pythonw)
            self.assertIn("campus_auth_gui.py", spec.arguments)
            self.assertIn("--tray-startup", spec.arguments)
            self.assertEqual(spec.working_directory, Path(temp_dir).resolve())

    def test_set_startup_enabled_false_removes_shortcut(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            shortcut_path = Path(temp_dir) / "youziauth.lnk"
            shortcut_path.write_text("placeholder", encoding="utf-8")

            campus_auth_gui.set_startup_enabled(False, shortcut_path=shortcut_path)

            self.assertFalse(shortcut_path.exists())

    def test_set_startup_enabled_false_removes_legacy_shortcut(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            shortcut_path = Path(temp_dir) / "youziauth.lnk"
            legacy_shortcut_path = Path(temp_dir) / "CampusNetworkAuth.lnk"
            shortcut_path.write_text("placeholder", encoding="utf-8")
            legacy_shortcut_path.write_text("legacy", encoding="utf-8")

            campus_auth_gui.set_startup_enabled(False, shortcut_path=shortcut_path)

            self.assertFalse(shortcut_path.exists())
            self.assertFalse(legacy_shortcut_path.exists())

    def test_set_startup_enabled_waits_for_the_helper_and_verifies_the_state(self):
        with mock.patch.object(
            campus_auth_gui.startup_tasks, "launch_elevated", return_value=0
        ) as elevate, mock.patch.object(
            campus_auth_gui.startup_tasks, "is_system_startup_enabled", return_value=True
        ) as enabled:
            campus_auth_gui.set_startup_enabled(True)

        elevate.assert_called_once_with(["--configure-system-startup=enable"])
        enabled.assert_called_once_with()

    def test_a_failed_helper_exit_code_is_reported_instead_of_assumed_success(self):
        with mock.patch.object(
            campus_auth_gui.startup_tasks, "launch_elevated", return_value=1
        ):
            with self.assertRaises(RuntimeError) as raised:
                campus_auth_gui.set_startup_enabled(True)
        self.assertIn("1", str(raised.exception))

    def test_a_declined_uac_prompt_reaches_the_caller(self):
        with mock.patch.object(
            campus_auth_gui.startup_tasks,
            "launch_elevated",
            side_effect=PermissionError("administrator approval was cancelled"),
        ):
            with self.assertRaises(PermissionError):
                campus_auth_gui.set_startup_enabled(True)

    def test_an_unverified_state_change_is_reported(self):
        with mock.patch.object(
            campus_auth_gui.startup_tasks, "launch_elevated", return_value=0
        ), mock.patch.object(
            campus_auth_gui.startup_tasks, "is_system_startup_enabled", return_value=False
        ):
            with self.assertRaises(RuntimeError) as raised:
                campus_auth_gui.set_startup_enabled(True)
        self.assertIn("UAC", str(raised.exception))


class LogTailTests(unittest.TestCase):
    def test_tail_log_returns_requested_recent_lines(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "campus_auth.log"
            log_path.write_text("first\nsecond\nthird\nfourth\n", encoding="utf-8")

            text = campus_auth_gui.tail_log(log_path, max_lines=2)

            self.assertEqual(text.splitlines(), ["third", "fourth"])

    def test_tail_log_returns_empty_text_for_missing_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "missing.log"

            self.assertEqual(campus_auth_gui.tail_log(log_path, max_lines=20), "")


class TrayWindowTests(unittest.TestCase):
    def test_app_name_and_author_metadata_are_displayable(self):
        self.assertEqual(campus_auth_gui.APP_NAME, "youziauth")
        self.assertEqual(campus_auth_gui.APP_AUTHOR, "yoouzic")
        self.assertEqual(campus_auth_gui.APP_WINDOW_TITLE, "youziauth - 校园网登录设置")

    def test_desktop_window_exposes_tray_lifecycle_actions(self):
        self.assertEqual(campus_auth_gui.FULL_GEOMETRY, "1080x680")
        self.assertEqual(campus_auth_gui.MIN_WINDOW_SIZE, (980, 640))
        self.assertTrue(hasattr(campus_auth_gui.CampusAuthGui, "hide_to_tray"))
        self.assertTrue(hasattr(campus_auth_gui.CampusAuthGui, "show_main_window"))
        self.assertTrue(hasattr(campus_auth_gui.CampusAuthGui, "open_settings"))
        self.assertTrue(hasattr(campus_auth_gui.CampusAuthGui, "quit_application"))

    def test_desktop_window_uses_yuzu_background_and_app_icon_assets(self):
        asset_dir = Path("assets")

        self.assertEqual(
            campus_auth_gui.background_image_path(asset_dir),
            asset_dir / "yuzu_background.png",
        )
        self.assertEqual(
            campus_auth_gui.app_icon_path(asset_dir),
            asset_dir / "yuzu_app.ico",
        )
        self.assertEqual(
            campus_auth_gui.panel_icon_path(asset_dir),
            asset_dir / "yuzu_app.png",
        )

    def test_tray_startup_hides_before_building_ui(self):
        root = mock.Mock()

        campus_auth_gui.prepare_root_for_mode(root, tray_startup=True)

        root.withdraw.assert_called_once_with()

    def test_manual_startup_does_not_hide_root(self):
        root = mock.Mock()

        campus_auth_gui.prepare_root_for_mode(root, tray_startup=False)

        root.withdraw.assert_not_called()

    def test_retry_or_suppress_protocol_activation_starts_hidden(self):
        self.assertTrue(campus_auth_gui.should_start_hidden(False, "retry"))
        self.assertTrue(campus_auth_gui.should_start_hidden(False, "suppress"))
        self.assertFalse(campus_auth_gui.should_start_hidden(False, "settings"))

    def test_startup_toggle_text_uses_check_mark_when_enabled(self):
        self.assertEqual(
            campus_auth_gui.format_startup_toggle_text(False),
            "□ 开机自启动",
        )
        self.assertEqual(
            campus_auth_gui.format_startup_toggle_text(True),
            "✓ 开机自启动",
        )

    def test_panel_icon_is_scaled_for_title_row(self):
        self.assertEqual(campus_auth_gui.PANEL_ICON_SUBSAMPLE, 6)


class SingleInstanceTests(unittest.TestCase):
    def test_duplicate_normal_launch_forwards_show_and_exits(self):
        lock = mock.Mock()
        lock.acquire.return_value = False
        sender = mock.Mock()

        exit_code = campus_auth_gui.handle_single_instance_startup(
            startup_mode=False,
            lock=lock,
            notify_duplicate=mock.Mock(),
            secondary_action="show",
            action_sender=sender,
        )

        self.assertEqual(exit_code, 0)
        sender.assert_called_once_with("show")
        lock.release.assert_not_called()

    def test_duplicate_startup_launch_exits_silently(self):
        lock = mock.Mock()
        lock.acquire.return_value = False
        notify_duplicate = mock.Mock()

        exit_code = campus_auth_gui.handle_single_instance_startup(
            startup_mode=True,
            lock=lock,
            notify_duplicate=notify_duplicate,
        )

        self.assertEqual(exit_code, 0)
        notify_duplicate.assert_not_called()
        lock.release.assert_not_called()

    def test_first_launch_keeps_lock_for_app_lifetime(self):
        lock = mock.Mock()
        lock.acquire.return_value = True

        exit_code = campus_auth_gui.handle_single_instance_startup(
            startup_mode=False,
            lock=lock,
            notify_duplicate=mock.Mock(),
        )

        self.assertIsNone(exit_code)
        lock.release.assert_not_called()

    def test_notification_uri_maps_to_exact_secondary_action(self):
        self.assertEqual(
            campus_auth_gui.resolve_secondary_action(
                ["--notification-action", "youziauth://retry"]
            ),
            "retry",
        )
        self.assertEqual(
            campus_auth_gui.resolve_secondary_action(
                ["--notification-action", "youziauth://settings"]
            ),
            "settings",
        )
        self.assertIsNone(
            campus_auth_gui.resolve_secondary_action(
                ["--notification-action", "youziauth://unknown"]
            )
        )


class MonitorRetryTests(unittest.TestCase):
    def test_first_startup_retry_delay_is_short_after_failure(self):
        self.assertEqual(
            campus_auth_gui.next_monitor_delay(
                success=False,
                regular_interval_seconds=60,
                attempt_index=0,
                startup_mode=True,
            ),
            campus_auth_gui.STARTUP_RETRY_SECONDS,
        )

    def test_successful_or_regular_monitor_uses_configured_interval(self):
        self.assertEqual(
            campus_auth_gui.next_monitor_delay(
                success=True,
                regular_interval_seconds=60,
                attempt_index=0,
                startup_mode=True,
            ),
            60,
        )
        self.assertEqual(
            campus_auth_gui.next_monitor_delay(
                success=False,
                regular_interval_seconds=60,
                attempt_index=0,
                startup_mode=False,
            ),
            60,
        )


class LegacyDormPollingTests(unittest.TestCase):
    """旧版 Tk 界面也必须把调度跑在所有账号上，而不是只跑面板显示的那一个。"""

    def app(self, accounts):
        app = campus_auth_gui.CampusAuthGui.__new__(campus_auth_gui.CampusAuthGui)
        app.dorm_accounts = accounts
        app.dorm_controller = None
        app.dorm_panel = None
        app._dorm_notice = None
        app.root = mock.Mock()
        app._show_notification = mock.Mock()
        return app

    def test_every_account_is_polled_and_each_result_is_announced_with_its_name(self):
        from dorm_checkin import Result
        accounts = mock.Mock()
        accounts.controller_or_none.return_value = mock.Mock()
        accounts.poll.return_value = [('账号 1', Result('signed', '今日打卡已完成')),
                                      ('张三', Result('login_required', '请先登录统一身份认证'))]
        app = self.app(accounts)
        app._poll_dorm()
        accounts.poll.assert_called_once_with()
        self.assertEqual(app._show_notification.call_count, 2, '两个账号各自的结果都要提醒')
        self.assertIn('张三', app._show_notification.call_args_list[1].args[0])
        app.root.after.assert_called_once_with(1000, app._poll_dorm)

    def test_a_repeated_result_from_the_same_account_is_only_announced_once(self):
        from dorm_checkin import Result
        accounts = mock.Mock()
        accounts.controller_or_none.return_value = mock.Mock()
        accounts.poll.return_value = [('账号 1', Result('signed', '今日打卡已完成',
                                                       at='2026-10-04T21:05:00+08:00'))]
        app = self.app(accounts)
        app._poll_dorm()
        app._poll_dorm()
        self.assertEqual(app._show_notification.call_count, 1)

    def test_a_broken_account_layer_keeps_the_schedule_alive(self):
        accounts = mock.Mock()
        accounts.controller_or_none.return_value = None
        accounts.poll.return_value = []
        app = self.app(accounts)
        app._poll_dorm()
        app.root.after.assert_called_once_with(1000, app._poll_dorm)
        self.assertIsNone(app.dorm_controller)


class NotificationIntegrationTests(unittest.TestCase):
    def test_retry_action_marks_notification_tracker_before_running(self):
        app = campus_auth_gui.CampusAuthGui.__new__(campus_auth_gui.CampusAuthGui)
        app.agent_mode = True
        app.last_agent_snapshot = agent_ipc.RuntimeSnapshot(
            "boot", "auth_failed", False, "incident", "失败", "before"
        )
        app.notification_tracker = mock.Mock()
        app.run_once = mock.Mock()

        app._handle_ui_action("retry")

        app.notification_tracker.mark_retry.assert_called_once_with(
            app.last_agent_snapshot
        )
        app.run_once.assert_called_once_with()

    def test_agent_poll_sends_tracker_toast_without_showing_window(self):
        app = campus_auth_gui.CampusAuthGui.__new__(campus_auth_gui.CampusAuthGui)
        app.startup_enabled = True
        app.agent_mode = True
        app.agent_ipc_ok = True
        app.agent_startup_deadline = dt.datetime.now().astimezone()
        app.config_path = Path("C:/ProgramData/youziauth/config.ini")
        app.interval_var = mock.Mock()
        app.interval_var.get.return_value = "30"
        app.root = mock.Mock()
        app._set_status = mock.Mock()
        app._show_notification = mock.Mock()
        app._update_agent_controls = mock.Mock()
        app._start_agent_health_probe = mock.Mock()
        app.notification_tracker = mock.Mock()
        app.notification_tracker.evaluate.return_value = "<toast />"
        snapshot = agent_ipc.RuntimeSnapshot(
            "boot",
            "auth_failed",
            False,
            "incident",
            "失败",
            dt.datetime.now().astimezone().isoformat(),
        )

        with mock.patch.object(
            campus_auth_gui.agent_ipc, "read_snapshot", return_value=snapshot
        ):
            app._poll_agent_status()

        self.assertIs(app.last_agent_snapshot, snapshot)
        app.notification_tracker.evaluate.assert_called_once_with(snapshot)
        app._show_notification.assert_called_once_with("<toast />")
        app.root.deiconify.assert_not_called()


class AgentHealthIntegrationTests(unittest.TestCase):
    def make_app(self):
        app = campus_auth_gui.CampusAuthGui.__new__(campus_auth_gui.CampusAuthGui)
        app.startup_enabled = True
        app.agent_mode = True
        app.config_path = Path("C:/ProgramData/youziauth/config.ini")
        app.agent_ipc_ok = False
        app.agent_probe_in_flight = False
        app.agent_last_probe_monotonic = 0.0
        app.agent_startup_deadline = (
            dt.datetime.now().astimezone() - dt.timedelta(seconds=1)
        )
        app.interval_var = mock.Mock()
        app.interval_var.get.return_value = "30"
        app.root = mock.Mock()
        app._set_status = mock.Mock()
        app._show_notification = mock.Mock()
        app._update_agent_controls = mock.Mock()
        app._start_agent_health_probe = mock.Mock()
        app.notification_tracker = mock.Mock()
        return app

    def test_stale_snapshot_displays_degraded_instead_of_old_online_state(self):
        app = self.make_app()
        old = (dt.datetime.now().astimezone() - dt.timedelta(minutes=10)).isoformat()
        stale = agent_ipc.RuntimeSnapshot(
            "boot", "online_campus", detail="already authenticated", updated_at=old
        )
        with mock.patch.object(
            campus_auth_gui.agent_ipc, "read_snapshot", return_value=stale
        ):
            app._poll_agent_status()
        app._set_status.assert_called_with(
            "系统认证代理状态已过期", windows_tray.TrayStatus.ERROR
        )
        app._update_agent_controls.assert_called_with(
            agent_health.AgentHealthState.DEGRADED
        )

    def test_agent_probe_result_is_processed_without_blocking_tk_thread(self):
        app = self.make_app()
        app.events = queue.Queue()
        app.events.put(("agent_probe", True))
        app._process_events()
        self.assertTrue(app.agent_ipc_ok)
        self.assertFalse(app.agent_probe_in_flight)


class AgentRepairTests(unittest.TestCase):
    def test_degraded_agent_enables_repair_control(self):
        app = campus_auth_gui.CampusAuthGui.__new__(campus_auth_gui.CampusAuthGui)
        app.start_button = mock.Mock()
        app._update_agent_controls(agent_health.AgentHealthState.DEGRADED)
        app.start_button.configure.assert_called_with(
            state="normal", text="修复系统代理"
        )

    def test_repair_re_registers_tasks_even_when_tray_marker_exists(self):
        app = campus_auth_gui.CampusAuthGui.__new__(campus_auth_gui.CampusAuthGui)
        app.agent_mode = True
        app.agent_ipc_ok = False
        app.agent_probe_in_flight = False
        app._set_status = mock.Mock()
        with mock.patch.object(
            campus_auth_gui.startup_tasks, "launch_elevated", return_value=0
        ) as elevate:
            app.repair_system_agent()
        elevate.assert_called_once_with(["--configure-system-startup=enable"])
        self.assertIsNone(app.agent_ipc_ok)

    def test_repair_reports_a_declined_uac_prompt(self):
        app = campus_auth_gui.CampusAuthGui.__new__(campus_auth_gui.CampusAuthGui)
        app.agent_mode = True
        app.agent_ipc_ok = False
        app.agent_probe_in_flight = False
        app._set_status = mock.Mock()
        with mock.patch.object(
            campus_auth_gui.startup_tasks,
            "launch_elevated",
            side_effect=PermissionError("administrator approval was cancelled"),
        ):
            app.repair_system_agent()
        status = app._set_status.call_args.args[0]
        self.assertIn("修复系统代理失败", status)
        self.assertIn("cancelled", status)

    def test_healthy_agent_start_does_not_claim_repair(self):
        app = campus_auth_gui.CampusAuthGui.__new__(campus_auth_gui.CampusAuthGui)
        app.agent_mode = True
        app.agent_health_state = agent_health.AgentHealthState.HEALTHY
        app._set_status = mock.Mock()
        app._send_agent_command = mock.Mock()
        self.assertTrue(app.start_monitor())
        app._send_agent_command.assert_called_once_with("reload-config")


class TrayStatusTests(unittest.TestCase):
    def test_tooltip_includes_current_online_status(self):
        tooltip = windows_tray.format_tray_tooltip(windows_tray.TrayStatus.ONLINE)

        self.assertEqual(tooltip, "校园网认证：在线")

    def test_menu_items_include_status_and_expected_commands(self):
        items = windows_tray.build_tray_menu_items(windows_tray.TrayStatus.OFFLINE)

        labels = [item.label for item in items]
        commands = [item.command for item in items if item.command]
        self.assertEqual(labels[0], "当前状态：离线")
        self.assertIn("show", commands)
        self.assertIn("settings", commands)
        self.assertIn("check", commands)
        self.assertIn("quit", commands)

    def test_tray_icon_paths_include_status_icons(self):
        paths = windows_tray.default_icon_paths(Path("assets"))

        self.assertEqual(paths[windows_tray.TrayStatus.ONLINE], Path("assets") / "tray_online.ico")
        self.assertEqual(paths[windows_tray.TrayStatus.OFFLINE], Path("assets") / "tray_offline.ico")


if __name__ == "__main__":
    unittest.main()
