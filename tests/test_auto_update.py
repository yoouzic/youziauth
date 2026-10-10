"""The unattended update path: what the privileged agent does and refuses to do."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import auto_update
from auto_update import Updater, UpdateStatus
from windows_update import UpdateVerificationError


class FakeController:
    """Stands in for app_update.UpdateController: records what the updater asked for."""

    def __init__(self, snapshot, package=(Path('pkg.msi'), '1.9.0', 'a' * 64, 'b' * 128),
                 error=None):
        self._snapshot = snapshot
        self._package = package
        self._error = error
        self.checked = 0
        self.closed = 0
        self._worker = None

    def check(self):
        self.checked += 1
        if self._error is not None:
            raise self._error

    def snapshot(self):
        return dict(self._snapshot)

    def package(self):
        return self._package

    def close(self):
        self.closed += 1


READY = {'state': 'ready', 'latest_version': '1.9.0', 'progress': 100,
         'checked': '2026-10-10 18:00', 'message': '安装包已下载并校验通过。',
         'changes': {'entries': [{'subject': '一条说明', 'kind': '', 'date': ''}],
                     'total': 1, 'more': False, 'note': ''}}
UP_TO_DATE = {'state': 'up_to_date', 'latest_version': '1.8.4', 'progress': 0,
              'checked': '2026-10-10 18:00', 'message': '当前已是最新正式版。',
              'changes': {'entries': [], 'total': 0, 'more': False, 'note': 'x'}}


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.install = self.root / 'install'
        self.install.mkdir()
        self.cache = self.root / 'cache'
        self.cache.mkdir()
        self.statuses = []
        self.installed = []

    def write_version(self, version):
        (self.install / 'VERSION').write_text(version, encoding='utf-8')

    def install_stub(self, result):
        # 参数顺序必须与 windows_update.install_msi 一致：signature 在 silent 之前。
        def installer(package, executable, version, digest, on_launch, signature=None,
                      silent=False, log_dir=None):
            self.installed.append(
                {'package': package, 'version': version, 'digest': digest,
                 'signature': signature, 'silent': silent, 'log_dir': log_dir})
            return result
        return installer

    def updater(self, snapshot, result=None, **kwargs):
        controller = FakeController(snapshot, error=kwargs.pop('error', None))
        updater = Updater(
            install_dir=self.install,
            cache_dir=self.cache,
            executable=self.install / 'youziauth.exe',
            controller_factory=lambda *a, **k: controller,
            installer=self.install_stub(result if result is not None else {'code': 0, 'healthy': True, 'relaunched': True}),
            report=self.statuses.append,
            log_dir=self.cache,
            **kwargs,
        )
        return updater, controller

    def test_an_available_update_is_installed_silently_and_reported(self):
        self.write_version('1.8.4')
        updater, controller = self.updater(READY)
        status = updater.run_cycle()
        self.assertEqual(controller.checked, 1)
        self.assertEqual(controller.closed, 1, 'the controller must be closed even on success')
        self.assertEqual(status.state, 'installed')
        self.assertEqual(status.latest_version, '1.9.0')
        self.assertIn('1.9.0', status.message)
        # 静默是关键：这条路径上没有向导，也没有任何确认。
        self.assertEqual([item['silent'] for item in self.installed], [True])
        self.assertEqual(self.installed[0]['version'], '1.9.0')
        self.assertEqual(self.installed[0]['log_dir'], self.cache)
        # 中间的「正在安装」也要报给界面，否则用户看不到任何进展。
        self.assertEqual([status.state for status in self.statuses], ['installing', 'installed'])

    def test_nothing_happens_when_the_release_is_not_newer(self):
        self.write_version('1.8.4')
        updater, _ = self.updater(UP_TO_DATE)
        status = updater.run_cycle()
        self.assertEqual(status.state, 'up_to_date')
        self.assertEqual(self.installed, [], 'an up-to-date check must not install anything')
        self.assertEqual([item.state for item in self.statuses], ['up_to_date'])

    def test_a_missing_installed_version_stops_before_touching_the_network(self):
        updater, controller = self.updater(READY)
        status = updater.run_cycle()
        self.assertEqual(status.state, 'error')
        self.assertEqual(controller.checked, 0, 'no VERSION means nothing to update')
        self.assertEqual(self.installed, [])

    def test_the_version_actually_on_disk_decides_whether_to_install(self):
        # 装完起不来的版本只报一次，不会每次检查都重装一遍。
        self.write_version('1.9.0')          # 目标版本已经装上了
        updater, _ = self.updater(READY)
        status = updater.run_cycle()
        self.assertEqual(status.state, 'installed')
        self.assertEqual(self.installed, [], 'already installed means no reinstall')

    def test_a_failed_health_check_is_reported_and_never_retried_in_a_loop(self):
        self.write_version('1.8.4')
        updater, _ = self.updater(READY, result={'code': 0, 'healthy': False, 'launch_exit_code': 3221225477})
        status = updater.run_cycle()
        self.assertEqual(status.state, 'error')
        self.assertIn('没能启动', status.message)
        self.assertIn('3221225477', status.detail)
        # 版本文件现在写成了新版本，所以下一次检查会直接认为已装好，不再重装。
        (self.install / 'VERSION').write_text('1.9.0', encoding='utf-8')
        again, _ = self.updater(READY)
        self.assertEqual(again.run_cycle().state, 'installed')
        self.assertEqual(self.installed, [self.installed[0]], 'no second install attempt')

    def test_msiexec_failures_are_mapped_to_something_a_user_can_understand(self):
        self.write_version('1.8.4')
        for code, expected in ((1603, '后台安装未完成'), (1618, '后台安装未完成'),
                               (3010, '需要重启'), (1602, '被取消')):
            with self.subTest(code=code):
                self.statuses.clear()
                self.installed.clear()
                updater, _ = self.updater(READY, result={'code': code})
                status = updater.run_cycle()
                self.assertIn(expected, status.message)
                self.assertEqual(status.state, 'installed' if code == 3010 else
                                 'up_to_date' if code == 1602 else 'error')
                if code == 3010:
                    self.assertEqual(status.detail, 'reboot required')

    def test_verification_failures_are_surfaced_verbatim_and_stop_the_update(self):
        self.write_version('1.8.4')
        updater, _ = self.updater(READY, error=UpdateVerificationError('MSI 安装包签名未通过校验，已拒绝更新。'))
        status = updater.run_cycle()
        # 检查阶段的验签失败由控制器表达，不是异常穿出来。
        self.assertEqual(status.state, 'error')

        def refusing(*args, **kwargs):
            raise UpdateVerificationError('MSI 安装包签名未通过校验，已拒绝更新。')

        self.statuses.clear()
        updater, _ = self.updater(READY)
        updater._installer = refusing
        status = updater.run_cycle()
        self.assertEqual(status.state, 'error')
        self.assertIn('签名未通过', status.message)
        self.assertEqual(status.detail, 'verification failed')

    def test_an_unexpected_failure_leaves_the_agent_alive(self):
        self.write_version('1.8.4')

        def exploding(*args, **kwargs):
            raise KeyError('boom')

        updater, _ = self.updater(READY)
        updater._installer = exploding
        status = updater.run_cycle()
        self.assertEqual(status.state, 'error')
        self.assertEqual(status.detail, 'KeyError')
        self.assertIn('重试', status.message)

    def test_the_relaunch_path_reaches_the_users_session_and_is_reported(self):
        self.write_version('1.8.4')
        calls = []
        updater, _ = self.updater(READY, result={'code': 0, 'healthy': True},
                                  relaunch=lambda: calls.append(True) or True)
        status = updater.run_cycle()
        self.assertEqual(calls, [True])
        self.assertEqual(status.state, 'installed')
        self.assertEqual(status.detail, 'relaunched')

    def test_a_failed_relaunch_is_recorded_rather_than_hidden(self):
        self.write_version('1.8.4')
        updater, _ = self.updater(READY, result={'code': 0, 'healthy': True},
                                  relaunch=lambda: False)
        status = updater.run_cycle()
        self.assertEqual(status.state, 'installed')
        self.assertEqual(status.detail, 'installed, relaunch not confirmed')

    def test_a_raising_relaunch_never_escapes(self):
        self.write_version('1.8.4')

        def exploding():
            raise OSError('no task')

        updater, _ = self.updater(READY, result={'code': 0, 'healthy': True}, relaunch=exploding)
        status = updater.run_cycle()
        self.assertEqual(status.state, 'installed')


class StatusFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / auto_update.STATUS_FILE

    def test_a_status_round_trips_through_the_file(self):
        status = UpdateStatus(state='installed', current_version='1.9.0', latest_version='1.9.0',
                              progress=100, checked='2026-10-10 18:00',
                              message='已自动更新到 v1.9.0。', detail='relaunched')
        auto_update.write_status(self.path, status)
        self.assertEqual(auto_update.read_status(self.path), status)

    def test_a_missing_or_broken_status_file_reads_as_unknown(self):
        self.assertIsNone(auto_update.read_status(self.path))
        self.path.write_text('{ not json', encoding='utf-8')
        self.assertIsNone(auto_update.read_status(self.path))
        self.path.write_text(json.dumps({'state': 'give-me-admin'}), encoding='utf-8')
        self.assertIsNone(auto_update.read_status(self.path))
        self.path.write_text(json.dumps({'state': 'idle', 'evil': 1}), encoding='utf-8')
        self.assertIsNone(auto_update.read_status(self.path))
        self.path.write_text(json.dumps({'state': 'idle', 'progress': 900}), encoding='utf-8')
        self.assertIsNone(auto_update.read_status(self.path))

    def test_the_status_block_carries_no_paths_or_urls(self):
        status = UpdateStatus(state='error', current_version='1.8.4', latest_version='1.9.0',
                              message='后台安装未完成（返回码 1603），已记录日志，将在下次检查时重试。',
                              detail='msiexec exit 1603')
        auto_update.write_status(self.path, status)
        raw = self.path.read_text(encoding='utf-8')
        self.assertNotIn('github', raw.lower())
        self.assertNotIn(str(self.path.parent), raw)
        self.assertNotIn('C:\\', raw)

    def test_an_unwritable_status_location_does_not_raise(self):
        # 报告失败绝不能让 agent 崩掉。父路径是一个**文件**时目录建不出来。
        blocker = Path(self.temporary.name) / 'blocked'
        blocker.write_text('not a directory', encoding='utf-8')
        auto_update.write_status(blocker / 'update-status.json', UpdateStatus(state='idle'))
        self.assertEqual(blocker.read_text(encoding='utf-8'), 'not a directory')

    def test_a_status_directory_is_created_on_demand(self):
        target = Path(self.temporary.name) / 'nested' / auto_update.STATUS_FILE
        auto_update.write_status(target, UpdateStatus(state='up_to_date', current_version='1.9.0'))
        self.assertTrue(target.is_file())
        self.assertEqual(auto_update.read_status(target).state, 'up_to_date')

    def test_the_installed_version_is_read_or_reported_as_unknown(self):
        install = Path(self.temporary.name) / 'install'
        install.mkdir()
        self.assertEqual(auto_update.read_installed_version(install), '')
        (install / 'VERSION').write_text('1.9.0\n', encoding='utf-8')
        self.assertEqual(auto_update.read_installed_version(install), '1.9.0')
        for junk in ('nonsense', '1.9', '1.9.0.1', 'v1.9.0', ''):
            with self.subTest(junk=junk):
                (install / 'VERSION').write_text(junk, encoding='utf-8')
                self.assertEqual(auto_update.read_installed_version(install), '')


if __name__ == '__main__':
    unittest.main()
