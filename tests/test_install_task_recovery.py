import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock, patch

import campus_auth_agent
import startup_tasks as tasks


SID = 'S-1-5-21-1-2-3-1001'
NS = {'t': tasks.TASK_NS}


class InstallTaskRecoveryTests(unittest.TestCase):
    def run_repair(self, existing, **options):
        calls, created = [], {}

        def runner(args, **kwargs):
            calls.append(args)
            name = args[args.index('/TN') + 1]
            if args[1] == '/Query':
                return subprocess.CompletedProcess(args, 0 if name in existing else 1,
                                                   existing.get(name, ''), '')
            if args[1] == '/Create':
                created[name] = ET.fromstring(Path(args[args.index('/XML') + 1]).read_text(encoding='utf-16'))
            return subprocess.CompletedProcess(args, 0, '', '')

        restored = tasks.repair_installed_tasks(Path('C:/new install/youziauth'), runner=runner,
                                               storage_setup=lambda sid: None,
                                               input_storage_setup=lambda sid: None, **options)
        return restored, calls, created

    def test_surviving_tray_restores_system_agent_with_original_user(self):
        restored, calls, created = self.run_repair({
            tasks.TRAY_TASK_NAME: tasks.build_tray_task_xml(Path('C:/old'), SID)})
        self.assertTrue(restored)
        self.assertEqual(set(created), {tasks.SYSTEM_TASK_NAME, tasks.TRAY_TASK_NAME})
        system = created[tasks.SYSTEM_TASK_NAME]
        self.assertEqual(system.findtext('.//t:Principal/t:UserId', namespaces=NS), tasks.SYSTEM_SID)
        self.assertEqual(system.findtext('.//t:Exec/t:Arguments', namespaces=NS), '--allowed-user-sid ' + SID)
        self.assertEqual(system.findtext('.//t:Exec/t:Command', namespaces=NS), str(Path('C:/new install/youziauth/youziauth-agent.exe')))
        self.assertEqual([c[c.index('/TN') + 1] for c in calls if c[1] == '/Run'],
                         [tasks.SYSTEM_TASK_NAME, tasks.TRAY_TASK_NAME])

    def test_surviving_system_task_can_restore_missing_tray(self):
        restored, _, created = self.run_repair({
            tasks.SYSTEM_TASK_NAME: tasks.build_system_task_xml(Path('C:/old'), SID)})
        self.assertTrue(restored)
        tray = created[tasks.TRAY_TASK_NAME]
        self.assertEqual(tray.findtext('.//t:Principal/t:UserId', namespaces=NS), SID)
        self.assertEqual(tray.findtext('.//t:Principal/t:LogonType', namespaces=NS), 'InteractiveToken')

    def test_disabled_startup_is_not_reenabled_by_installation(self):
        restored, calls, _ = self.run_repair({})
        self.assertFalse(restored)
        self.assertFalse(any(c[1] in ('/Create', '/Run') for c in calls))

    def test_fresh_install_creates_both_tasks_for_original_installer_user(self):
        restored, calls, created = self.run_repair({}, first_install=True, install_user_sid=SID)
        self.assertTrue(restored)
        self.assertEqual(set(created), {tasks.SYSTEM_TASK_NAME, tasks.TRAY_TASK_NAME})
        self.assertEqual(created[tasks.TRAY_TASK_NAME].findtext('.//t:Principal/t:UserId', namespaces=NS), SID)
        self.assertEqual(created[tasks.SYSTEM_TASK_NAME].findtext('.//t:Exec/t:Arguments', namespaces=NS),
                         '--allowed-user-sid ' + SID)
        self.assertFalse(any('runas' in argument for command in calls for argument in command))

    def test_upgrade_with_installer_user_keeps_disabled_tasks_disabled(self):
        restored, calls, _ = self.run_repair({}, install_user_sid=SID)
        self.assertFalse(restored)
        self.assertFalse(any(c[1] in ('/Create', '/Run') for c in calls))

    def test_disabled_upgrade_never_rebinds_storage_to_a_different_installer_user(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 1, '', 'missing'))
        output_storage = Mock()
        input_storage = Mock()
        self.assertFalse(tasks.repair_installed_tasks(Path('C:/installed'), runner=runner,
            install_user_sid=SID, storage_setup=output_storage, input_storage_setup=input_storage))
        output_storage.assert_not_called()
        input_storage.assert_not_called()

    def test_existing_task_identity_wins_over_different_installer_user(self):
        other_sid = 'S-1-5-21-1-2-3-1002'
        restored, _, created = self.run_repair({
            tasks.TRAY_TASK_NAME: tasks.build_tray_task_xml(Path('C:/old'), SID)},
            first_install=True, install_user_sid=other_sid)
        self.assertTrue(restored)
        self.assertEqual(created[tasks.TRAY_TASK_NAME].findtext('.//t:Principal/t:UserId', namespaces=NS), SID)

    def test_fresh_install_rejects_system_identity_without_user_tasks(self):
        with self.assertRaises(ValueError):
            self.run_repair({}, first_install=True, install_user_sid=tasks.SYSTEM_SID)

    def test_input_root_is_secured_before_any_task_is_created(self):
        operations = []

        def runner(args, **kwargs):
            operations.append(args[1])
            return subprocess.CompletedProcess(args, 1 if args[1] == '/Query' else 0, '', '')

        restored = tasks.repair_installed_tasks(Path('C:/installed'), runner=runner, first_install=True,
            install_user_sid=SID, storage_setup=lambda sid: operations.append('system-storage'),
            input_storage_setup=lambda sid: operations.append('input-storage'))
        self.assertTrue(restored)
        self.assertLess(operations.index('input-storage'), operations.index('/Create'))

    def test_unsafe_input_root_stops_task_registration(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 1, '', 'missing'))
        with self.assertRaises(RuntimeError):
            tasks.repair_installed_tasks(Path('C:/installed'), runner=runner, first_install=True,
                install_user_sid=SID, storage_setup=Mock(),
                input_storage_setup=Mock(side_effect=RuntimeError('unsafe input root')))
        self.assertFalse(any(call.args[0][1] == '/Create' for call in runner.call_args_list))

    def test_system_install_with_disabled_tasks_keeps_opt_out_without_invalid_sid_failure(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 1, '', 'missing'))
        storage = Mock()
        restored = tasks.repair_installed_tasks(Path('C:/installed'), runner=runner,
                                               install_user_sid=tasks.SYSTEM_SID, storage_setup=storage)
        self.assertFalse(restored)
        storage.assert_not_called()

    def test_invalid_tray_identity_is_rejected_before_creating_system_task(self):
        xml = tasks.build_tray_task_xml(Path('C:/old'), tasks.SYSTEM_SID)
        with self.assertRaises(ValueError):
            self.run_repair({tasks.TRAY_TASK_NAME: xml})

    def test_installer_helper_dispatches_without_loading_credentials_or_network(self):
        with patch.object(tasks, 'repair_installed_tasks', return_value=True) as repair, \
             patch.object(campus_auth_agent, 'load_agent_config') as load:
            self.assertEqual(campus_auth_agent.main(['--repair-install-tasks']), 0)
        repair.assert_called_once()
        load.assert_not_called()


class MsiTaskLifecycleTests(unittest.TestCase):
    def test_chinese_installer_still_detects_previous_english_language_packages(self):
        root = ET.parse(Path(__file__).resolve().parents[1] / 'packaging/youziauth.wxs').getroot()
        ns = {'w': 'http://wixtoolset.org/schemas/v4/wxs'}
        self.assertEqual(root.find('.//w:Package', ns).get('Language'), '2052')
        self.assertEqual(root.find('.//w:MajorUpgrade', ns).get('IgnoreLanguage'), 'yes')

    def test_system_processes_are_stopped_in_install_script_before_file_copy(self):
        root = ET.parse(Path(__file__).resolve().parents[1] / 'packaging/youziauth.wxs').getroot()
        ns = {'w': 'http://wixtoolset.org/schemas/v4/wxs'}
        action = root.find('.//w:CustomAction[@Id="StopYouziauthProcessesElevated"]', ns)
        self.assertIsNotNone(action)
        self.assertEqual(action.get('Execute'), 'deferred')
        self.assertEqual(action.get('Impersonate'), 'no')
        schedule = root.find('.//w:InstallExecuteSequence/w:Custom[@Action="StopYouziauthProcessesElevated"]', ns)
        self.assertEqual(schedule.get('Before'), 'InstallFiles')
        self.assertEqual(root.find('.//w:MajorUpgrade', ns).get('Schedule'), 'afterInstallExecute')

    def test_task_repair_is_privileged_and_only_runs_for_committed_installations(self):
        root = ET.parse(Path(__file__).resolve().parents[1] / 'packaging/youziauth.wxs').getroot()
        ns = {'w': 'http://wixtoolset.org/schemas/v4/wxs'}
        action = root.find('.//w:CustomAction[@Id="RepairInstalledTasks"]', ns)
        self.assertIsNotNone(action)
        self.assertEqual(action.get('Execute'), 'commit')
        self.assertEqual(action.get('Impersonate'), 'no')
        self.assertEqual(action.get('Return'), 'check')
        self.assertIn('--repair-install-tasks', action.get('ExeCommand'))
        schedule = root.find('.//w:InstallExecuteSequence/w:Custom[@Action="RepairInstalledTasks"]', ns)
        self.assertEqual(schedule.get('Before'), 'InstallFinalize')
        self.assertIn('NOT REMOVE', schedule.get('Condition'))

    def test_first_install_uses_original_user_and_excludes_upgrade_and_repair(self):
        root = ET.parse(Path(__file__).resolve().parents[1] / 'packaging/youziauth.wxs').getroot()
        ns = {'w': 'http://wixtoolset.org/schemas/v4/wxs'}
        action = root.find('.//w:CustomAction[@Id="ConfigureFirstInstallTasks"]', ns)
        self.assertIsNotNone(action)
        self.assertEqual(action.get('Execute'), 'commit')
        self.assertEqual(action.get('Impersonate'), 'no')
        self.assertEqual(action.get('Return'), 'check')
        self.assertIn('--first-install', action.get('ExeCommand'))
        self.assertIn('--install-user-sid &quot;[UserSID]&quot;'.replace('&quot;', '"'), action.get('ExeCommand'))
        schedule = root.find('.//w:InstallExecuteSequence/w:Custom[@Action="ConfigureFirstInstallTasks"]', ns)
        self.assertEqual(schedule.get('Before'), 'InstallFinalize')
        self.assertIn('NOT Installed', schedule.get('Condition'))
        self.assertIn('NOT WIX_UPGRADE_DETECTED', schedule.get('Condition'))
        repair = root.find('.//w:InstallExecuteSequence/w:Custom[@Action="RepairInstalledTasks"]', ns)
        self.assertIn('Installed OR WIX_UPGRADE_DETECTED', repair.get('Condition'))


if __name__ == '__main__':
    unittest.main()
