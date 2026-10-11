import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import auto_update
import campus_auth
import campus_auth_agent as module
import startup_tasks
from windows_credentials import CredentialError


class FirstInstallAgentTests(unittest.TestCase):
    def test_missing_credentials_keep_the_updater_alive_without_authentication(self):
        with tempfile.TemporaryDirectory() as temporary:
            updater, probe, authenticate = Mock(), Mock(), Mock()
            updater.recover_install_result.return_value = None
            updater.run_cycle.return_value = auto_update.UpdateStatus(state='up_to_date')
            agent = module.Agent(lambda: campus_auth.AuthConfig(), probe, authenticate,
                                 Path(temporary) / 'runtime.json', logging.getLogger('empty-config'),
                                 boot_id='fixture', updater=updater)
            network = agent.run_cycle()
            self.assertEqual(network.snapshot.state, 'waiting_for_network')
            self.assertIn('保存', network.snapshot.detail)
            probe.observe.assert_not_called()
            authenticate.assert_not_called()
            agent._run_update_check('first-install')
            self.assertEqual(agent.snapshot.update['state'], 'up_to_date')
            updater.run_cycle.assert_called_once()

    def test_runtime_config_missing_or_invalid_enters_setup_mode(self):
        loader = getattr(module, 'load_runtime_config', None)
        self.assertTrue(callable(loader))
        for error in (FileNotFoundError('missing'), CredentialError('no password'), ValueError('invalid settings')):
            with self.subTest(type=type(error).__name__), patch.object(module, 'load_agent_config', side_effect=error):
                config = loader(Path('missing-config.ini'))
                self.assertEqual(config.username, '')
                self.assertEqual(config.password, '')

    def test_saved_credentials_are_used_after_reload_without_another_uac(self):
        with tempfile.TemporaryDirectory() as temporary:
            configs = [campus_auth.AuthConfig(), campus_auth.AuthConfig(username='fixture', password='fixture')]
            probe, authenticate = Mock(), Mock()
            probe.observe.return_value.internet_ok = True
            probe.observe.return_value.proxy_path_ok = True
            agent = module.Agent(lambda: configs.pop(0), probe, authenticate,
                                 Path(temporary) / 'runtime.json', logging.getLogger('reload-config'), boot_id='fixture')
            self.assertEqual(agent.reload_config().state, 'online_external')
            self.assertEqual(agent.config.username, 'fixture')
            probe.observe.assert_called_once()
            authenticate.assert_not_called()

    def test_first_install_cli_dispatches_to_privileged_task_setup_before_loading_config(self):
        args = ['--repair-install-tasks', '--first-install', '--install-user-sid', 'S-1-5-21-1-2-3-1001']
        with patch.object(startup_tasks, 'repair_installed_tasks') as repair, \
             patch.object(module, 'load_agent_config') as config:
            self.assertEqual(module.main(args), 0)
        self.assertTrue(repair.call_args.kwargs['first_install'])
        config.assert_not_called()


if __name__ == '__main__':
    unittest.main()
