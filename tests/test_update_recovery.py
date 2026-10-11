"""Recover a detached installer after the MSI has killed its parent agent."""

import importlib
import importlib.util
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import auto_update
from auto_update import Updater, UpdateStatus


class UpdateRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.install = self.root / 'install'
        self.install.mkdir()
        self.cache = self.root / 'updates'
        self.cache.mkdir()
        self.path = self.cache / 'install-result.json'
        self.version('1.9.0')
        self.statuses = []
        self.updater = Updater(self.install, self.cache, report=self.statuses.append)

    def version(self, value):
        (self.install / 'VERSION').write_text(value, encoding='utf-8')

    def record(self, result=None, version='1.9.0', checked='2026-10-11T04:00:00+00:00'):
        data = {'version': version, 'checked': checked,
                'result': result or {'code': 0, 'healthy': True, 'relaunched': True,
                                     'agent_restarted': True}}
        self.path.write_text(json.dumps(data), encoding='utf-8')
        return data

    def recover(self):
        recover = getattr(self.updater, 'recover_install_result', None)
        self.assertTrue(callable(recover), 'the updater must recover its detached worker result')
        return recover()

    def parser(self):
        self.assertIsNotNone(importlib.util.find_spec('update_recovery'),
                             'bounded worker-result parser must exist')
        return importlib.import_module('update_recovery')

    def test_a_dead_parent_is_recovered_to_the_existing_success_feedback(self):
        self.record()
        status = self.recover()
        self.assertEqual(status.state, 'installed')
        self.assertEqual(status.current_version, '1.9.0')
        self.assertEqual(status.checked, '2026-10-11T04:00:00+00:00')
        self.assertIn('已自动更新', status.message)
        self.assertEqual(self.statuses, [status])
        self.assertEqual(auto_update.read_status(self.cache / auto_update.STATUS_FILE), status)
        self.assertIsNone(self.recover(), 'polling must consume each record once')

    def test_a_restarted_agent_can_restore_the_consumed_completion(self):
        self.record()
        status = self.recover()
        restarted = Updater(self.install, self.cache)
        self.assertEqual(restarted.recover_install_result(), status)
        self.assertIsNone(restarted.recover_install_result())

    def test_an_install_result_written_after_agent_startup_is_still_recovered(self):
        self.assertIsNone(self.recover())
        self.record()
        self.assertEqual(self.recover().state, 'installed')

    def test_restart_during_msi_commit_preserves_installing_until_the_worker_finishes(self):
        checked = dt.datetime.now(dt.timezone.utc).isoformat()
        installing = UpdateStatus(state='installing', current_version='1.8.27',
                                  latest_version='1.9.0', progress=100,
                                  checked=checked, message='installing')
        auto_update.write_status(self.cache / auto_update.STATUS_FILE, installing)
        self.assertEqual(self.recover(), installing, 'new VERSION is not proof of installer completion')
        self.assertIsNone(self.recover(), 'waiting must not keep republishing the same stage')
        self.record(checked=dt.datetime.now(dt.timezone.utc).isoformat())
        self.assertEqual(self.recover().state, 'installed')

    def test_a_missing_worker_result_expires_instead_of_leaving_installing_forever(self):
        checked = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=11)).isoformat()
        auto_update.write_status(self.cache / auto_update.STATUS_FILE,
                                 UpdateStatus(state='installing', latest_version='1.9.0',
                                              checked=checked, message='installing'))
        status = self.recover()
        self.assertIsNotNone(status)
        self.assertEqual(status.state, 'error')
        self.assertIn('安装结果', status.message)
        self.assertIsNone(self.recover())

    def test_installing_restored_earlier_is_also_expired_during_polling(self):
        checked = dt.datetime.now(dt.timezone.utc).isoformat()
        installing = UpdateStatus(state='installing', latest_version='1.9.0', checked=checked)
        auto_update.write_status(self.cache / auto_update.STATUS_FILE, installing)
        self.assertEqual(self.recover(), installing)
        stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=11)
        with patch.object(auto_update.update_recovery, 'timestamp', return_value=stale):
            self.assertEqual(self.recover().state, 'error')

    def test_success_requires_the_target_version_on_disk(self):
        self.version('1.8.27')
        for index, code in enumerate((0, 3010)):
            with self.subTest(code=code):
                self.updater = Updater(self.install, self.cache)
                self.record({'code': code}, checked=f'2026-10-11T04:00:0{index}+00:00')
                status = self.recover()
                self.assertEqual(status.state, 'error')
                self.assertIn('版本', status.message)
                self.assertEqual(status.current_version, '1.8.27')

    def test_recovery_preserves_reboot_health_and_installer_failure_details(self):
        outcomes = (({'code': 3010}, 'installed', 'reboot required'),
                    ({'code': 0, 'healthy': False, 'launch_exit_code': 42}, 'error', '42'),
                    ({'code': 1603, 'agent_restarted': True}, 'error', 'msiexec exit 1603'),
                    ({'error': 17}, 'error', 'worker error 17'))
        for index, (result, state, detail) in enumerate(outcomes):
            with self.subTest(result=result):
                self.record(result, checked=f'2026-10-11T04:00:0{index}+00:00')
                status = self.recover()
                self.assertEqual(status.state, state)
                self.assertIn(detail, status.detail)

    def test_an_old_result_cannot_replace_a_new_check_or_its_failure(self):
        self.record()
        recent = UpdateStatus(state='error', current_version='1.9.0',
                              checked='2026-10-11T05:00:00+00:00', message='new failure')
        auto_update.write_status(self.cache / auto_update.STATUS_FILE, recent)
        self.assertEqual(self.recover(), recent, 'startup should restore the newer terminal status')
        self.assertEqual(auto_update.read_status(self.cache / auto_update.STATUS_FILE), recent)
        self.assertIsNone(self.recover())

    def test_new_health_fields_with_the_same_timestamp_are_not_deduplicated(self):
        self.record({'code': 0})
        self.assertEqual(self.recover().state, 'installed')
        self.record({'code': 0, 'healthy': False, 'launch_exit_code': 42})
        self.assertEqual(self.recover().state, 'error')

    def test_a_new_check_makes_the_previous_result_historical(self):
        self.record()
        self.assertEqual(self.recover().state, 'installed')

        class Current:
            _worker = None

            def __init__(self, *args): pass
            def check(self): pass
            def close(self): pass
            def snapshot(self):
                return {'state': 'up_to_date', 'latest_version': '1.9.0',
                        'message': 'current', 'changes': {}}

        self.updater._controller_factory = Current
        with patch.object(auto_update, '_timestamp', return_value='2026-10-11T05:00:00+00:00'):
            status = self.updater.run_cycle()
        self.assertEqual(status.state, 'up_to_date')
        # Even a late health revision of the earlier attempt must not replace
        # the user's newer check result.
        self.record({'code': 0, 'healthy': False})
        self.assertIsNone(self.recover())
        restarted = Updater(self.install, self.cache)
        self.assertEqual(restarted.recover_install_result(), status)

    def test_persistence_failure_leaves_the_worker_result_recoverable_after_restart(self):
        parser = self.parser()
        self.record()
        record = parser.read_install_result(self.path)
        with patch.object(auto_update, 'write_status'):
            self.assertEqual(self.recover().state, 'installed')
        self.assertFalse(parser.is_consumed(self.cache / parser.CONSUMED_RESULT_FILE, record))
        restarted = Updater(self.install, self.cache)
        self.assertEqual(restarted.recover_install_result().state, 'installed')

    def test_the_parser_rejects_unknown_fields_bad_types_and_unbounded_input(self):
        parser = self.parser()
        valid = self.record()
        malformed = [dict(valid, unexpected='x'), dict(valid, version='1.9'),
                     dict(valid, checked='not a timestamp'), dict(valid, checked='2026-10-11T04:00:00'),
                     dict(valid, result={'code': True}), dict(valid, result={'code': 0, 'error': 17}),
                     dict(valid, result={'code': 0, 'healthy': 'yes'}),
                     dict(valid, result={'code': 0, 'unknown': True}),
                     dict(valid, result={'code': 0, 'launch_exit_code': 2 ** 33})]
        for item in malformed:
            with self.subTest(item=item):
                self.path.write_text(json.dumps(item), encoding='utf-8')
                self.assertIsNone(parser.read_install_result(self.path))
        self.path.write_text('x' * 100_000, encoding='utf-8')
        self.assertIsNone(parser.read_install_result(self.path))
        self.path.write_text('{"version":"1.9.0","version":"2.0.0",'
                             '"result":{"code":0},"checked":"2026-10-11T04:00:00+00:00"}', encoding='utf-8')
        self.assertIsNone(parser.read_install_result(self.path), 'duplicate keys must be rejected')

    def test_reading_or_consuming_missing_or_unwritable_files_never_breaks_the_agent(self):
        parser = self.parser()
        self.assertIsNone(parser.read_install_result(self.path))
        self.record()
        record = parser.read_install_result(self.path)
        blocker = self.root / 'blocker'
        blocker.write_text('file', encoding='utf-8')
        self.assertFalse(parser.mark_consumed(blocker / 'seen.json', record))
        self.assertFalse(parser.is_consumed(blocker / 'seen.json', record))


if __name__ == '__main__':
    unittest.main()
