import datetime as dt
import json
import logging
import tempfile
import unittest
from pathlib import Path

import auto_update
import campus_auth
from agent_ipc import AgentCommand, read_snapshot
from campus_auth_agent import Agent


class AgentUpdateRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.install = self.root / 'install'
        self.install.mkdir()
        (self.install / 'VERSION').write_text('1.8.27')
        self.updates = self.root / 'updates'
        self.updates.mkdir()
        self.updater = auto_update.Updater(self.install, self.updates)
        self.config = campus_auth.AuthConfig(username='fixture', password='fixture')

    def write_result(self):
        (self.updates / 'install-result.json').write_text(json.dumps({
            'version': '1.8.27', 'result': {'code': 0, 'healthy': True, 'agent_restarted': True},
            'checked': dt.datetime.now(dt.timezone.utc).isoformat()}), encoding='utf-8')

    def make_agent(self):
        return Agent(lambda: self.config, None, None, self.root / 'runtime.json',
                     logging.getLogger('recovery-test'), boot_id='fixture-boot', updater=self.updater)

    def test_restart_recovers_success_without_network_and_delays_next_check(self):
        self.write_result()
        agent = self.make_agent()
        self.assertEqual(agent.snapshot.update.get('state'), 'installed')
        self.assertFalse(agent.update_due())
        self.assertEqual(read_snapshot(self.root / 'runtime.json').update.get('state'), 'installed')

    def test_late_worker_result_is_recovered_by_status_request(self):
        agent = self.make_agent()
        self.write_result()
        response = agent.handle_command(AgentCommand('status'))
        self.assertEqual(response['snapshot']['update'].get('state'), 'installed')
        self.assertFalse(agent.update_due())


if __name__ == '__main__':
    unittest.main()
