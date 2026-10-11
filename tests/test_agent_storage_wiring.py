import logging
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import campus_auth
import campus_auth_agent as agent
import campus_auth_gui as gui


class ProtectedAgentWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.storage = types.ModuleType('system_storage')
        self.storage.verified_system_storage = Mock(return_value=self.root)
        self.storage.default_system_storage_path = Mock(return_value=self.root)
        self.storage.secure_system_storage = Mock(return_value=self.root)

    def test_frozen_agent_never_uses_user_configured_log_destination(self):
        config = campus_auth.AuthConfig(username='fixture', password='fixture',
                                        log_file='C:/Windows/arbitrary-user-path.log')
        instance = Mock()
        instance.run_cycle.return_value.snapshot.state = 'online_external'
        with patch.dict('sys.modules', {'system_storage': self.storage}), \
             patch.object(agent.sys, 'frozen', True, create=True), \
             patch.object(agent, 'load_agent_config', return_value=config), \
             patch.object(agent, 'configure_agent_logging', return_value=logging.getLogger('fixture')) as log, \
             patch.object(agent, 'build_updater', return_value=(None, '')) as updater, \
             patch.object(agent, 'Agent', return_value=instance) as factory:
            self.assertEqual(agent.main(['--once', '--allowed-user-sid', 'S-1-5-21-1-2-3-1001']), 0)
        self.storage.verified_system_storage.assert_called_once()
        self.assertEqual(log.call_args.args[0], self.root / 'logs' / 'agent.log')
        self.assertEqual(factory.call_args.kwargs['snapshot_path'], self.root / 'runtime.json')
        self.assertEqual(updater.call_args.args, (self.root, 'S-1-5-21-1-2-3-1001'))

    def test_untrusted_storage_stops_before_any_privileged_file_write(self):
        self.storage.verified_system_storage.side_effect = PermissionError('untrusted owner')
        config = campus_auth.AuthConfig(username='fixture', password='fixture')
        with patch.dict('sys.modules', {'system_storage': self.storage}), \
             patch.object(agent.sys, 'frozen', True, create=True), \
             patch.object(agent, 'load_agent_config', return_value=config), \
             patch.object(agent, 'configure_agent_logging') as log:
            self.assertEqual(agent.main(['--once']), 3)
        log.assert_not_called()

    def test_registered_system_agent_can_initialize_storage_after_interactive_enable(self):
        self.storage.verified_system_storage.side_effect = RuntimeError('storage missing')
        config = campus_auth.AuthConfig(username='fixture', password='fixture')
        instance = Mock()
        instance.run_cycle.return_value.snapshot.state = 'online_external'
        sid = 'S-1-5-21-1-2-3-1001'
        with patch.dict('sys.modules', {'system_storage': self.storage}), \
             patch.object(agent.sys, 'frozen', True, create=True), \
             patch.object(agent, 'load_agent_config', return_value=config), \
             patch.object(agent, 'configure_agent_logging', return_value=logging.getLogger('fixture')), \
             patch.object(agent, 'build_updater', return_value=(None, '')), \
             patch.object(agent, 'Agent', return_value=instance):
            self.assertEqual(agent.main(['--once', '--allowed-user-sid', sid]), 0)
        self.storage.secure_system_storage.assert_called_once_with(sid)

    def test_desktop_reads_migrated_runtime_and_log_but_keeps_legacy_fallback(self):
        config = self.root / 'user' / 'config.ini'
        legacy = config.parent / 'runtime.json'
        with patch.dict('sys.modules', {'system_storage': self.storage}), \
             patch.object(gui, 'DEFAULT_CONFIG_PATH', config):
            self.assertEqual(gui.agent_runtime_path(config), legacy)
            (self.root / 'runtime.json').write_text('{}')
            (self.root / 'logs').mkdir()
            (self.root / 'logs' / 'agent.log').write_text('fixture')
            self.assertEqual(gui.agent_runtime_path(config), self.root / 'runtime.json')
            self.assertEqual(gui.resolve_log_path(config, 'campus_auth.log'), self.root / 'logs' / 'agent.log')


if __name__ == '__main__':
    unittest.main()
