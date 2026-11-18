import unittest
from unittest.mock import MagicMock, patch

import campus_auth_desktop as desktop
from agent_ipc import UiCommand
from campus_auth_desktop import DesktopRuntime
from desktop_bridge import PreviewBridge


class DesktopRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = PreviewBridge()
        self.window = MagicMock()
        self.runtime = DesktopRuntime(self.bridge, self.window, preview=True)

    def test_launcher_is_signaled_after_the_interactive_ui_is_ready(self):
        bridge = MagicMock()
        window = MagicMock()
        runtime = DesktopRuntime(bridge, window)
        event = r'Local\youziauth-tray-ready-' + '1' * 32

        def signal(name):
            self.assertEqual(name, event)
            window.events.loaded.wait.assert_called_once()
            tray.return_value.start.assert_called_once()
            self.assertIs(runtime.tray, tray.return_value)
            runtime.closed.set()

        with patch.object(desktop.threading, 'Thread'), \
             patch.object(desktop.windows_tray, 'WindowsTrayIcon') as tray, \
             patch.object(desktop, 'signal_startup_ready', side_effect=signal, create=True) as ready:
            runtime.run(startup=True, startup_event=event)
        ready.assert_called_once_with(event)

    def test_status_reports_runtime_version_and_real_tray_readiness_without_queueing(self):
        with patch.object(desktop, 'read_current_version', return_value='1.9.42', create=True), \
             patch.object(desktop, 'current_session_id', return_value=3, create=True):
            runtime = DesktopRuntime(MagicMock(), MagicMock())
        self.assertEqual(runtime._receive_ui_command(UiCommand('status')), {
            'ok': True, 'app_version': '1.9.42', 'session_id': 3, 'tray_ready': False,
        })
        runtime.tray = MagicMock()
        self.assertTrue(runtime._receive_ui_command(UiCommand('status'))['tray_ready'])
        self.assertTrue(runtime.events.empty())
        runtime.closed.set()
        self.assertFalse(runtime._receive_ui_command(UiCommand('status'))['ok'])

    def test_duplicate_startup_only_signals_a_matching_initialized_instance(self):
        event = r'Local\youziauth-tray-ready-' + '2' * 32
        argv = ['--tray-startup', '--startup-ready-event', event]
        with patch.dict(desktop.sys.modules, {'webview': MagicMock()}), \
             patch.object(desktop.gui, 'handle_single_instance_startup', return_value=0), \
             patch.object(desktop, 'read_current_version', return_value='1.9.42', create=True), \
             patch.object(desktop, 'current_session_id', return_value=3, create=True), \
             patch.object(desktop.gui, 'send_ui_action') as send, \
             patch.object(desktop, 'signal_startup_ready', return_value=True, create=True) as ready:
            send.return_value = {'ok': True, 'app_version': '1.9.41', 'session_id': 3}
            self.assertEqual(desktop.main(argv), 1)
            ready.assert_not_called()
            send.return_value['app_version'] = '1.9.42'
            self.assertEqual(desktop.main(argv), 0)
            ready.assert_called_once_with(event)

    def test_real_startup_checks_updates_once_and_preview_does_not(self):
        for preview in (False, True):
            bridge = MagicMock()
            runtime = DesktopRuntime(bridge, MagicMock(), preview=preview)
            runtime.events.put('show')
            bridge._tick.side_effect = runtime.closed.set
            with patch('campus_auth_desktop.threading.Thread'), patch('campus_auth_desktop.windows_tray.WindowsTrayIcon') as tray:
                tray.return_value.start.return_value = False
                runtime.run()
            if preview:
                bridge._start_updates.assert_not_called()
            else:
                bridge._start_updates.assert_called_once_with()

    def test_close_hides_but_does_not_exit_or_stop_background(self):
        self.bridge._close = MagicMock()
        self.runtime.tray = MagicMock()
        self.assertFalse(self.runtime.closing())
        self.runtime._handle(self.runtime.events.get_nowait())
        self.window.hide.assert_called_once()
        self.bridge._close.assert_not_called()
        self.assertFalse(self.runtime.closed.is_set())

    def test_missing_tray_minimizes_instead_of_losing_window(self):
        self.runtime._handle('hide')
        self.window.minimize.assert_called_once()
        self.window.hide.assert_not_called()

    def test_dorm_tray_action_reuses_the_same_window(self):
        self.runtime._handle('dorm')
        self.window.show.assert_called_once()
        self.window.restore.assert_called_once()
        self.window.evaluate_js.assert_called_once_with('window.desktopNavigate("dorm")')

    def test_explicit_exit_stops_workers_and_tray_and_destroys_window(self):
        self.bridge._close = MagicMock()
        tray = self.runtime.tray = MagicMock()
        self.runtime._handle('quit')
        self.bridge._close.assert_called_once()
        tray.stop.assert_called_once()
        self.window.destroy.assert_called_once()
        self.assertTrue(self.runtime.closed.is_set())
        self.assertTrue(self.runtime.closing())


if __name__ == '__main__':
    unittest.main()
