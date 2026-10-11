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
        # detail 现在带上出错位置：只有类型名的话，真实安装上根本定位不了。
        self.assertTrue(status.detail.startswith('KeyError'), status.detail)
        self.assertIn('.py:', status.detail)
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

    def test_the_installed_version_is_read_from_either_layout(self):
        # 冻结的 one-folder 构建把 VERSION 放在 _internal 里（PyInstaller 的内容目录，
        # 也就是 sys._MEIPASS 指向的地方），**不在**可执行文件旁边。只看安装根目录会
        # 读到空字符串，而读不到版本会让整条自动更新直接拒绝运行 —— 实测线上 1.8.7 的
        # 安装目录就是这种布局。
        install = Path(self.temporary.name) / 'install'
        (install / '_internal').mkdir(parents=True)
        self.assertEqual(auto_update.read_installed_version(install), '')
        (install / '_internal' / 'VERSION').write_text('1.9.0\n', encoding='utf-8')
        self.assertEqual(auto_update.read_installed_version(install), '1.9.0')
        # 根目录那份优先（源码布局、或将来改了打包方式）。
        (install / 'VERSION').write_text('2.0.0\n', encoding='utf-8')
        self.assertEqual(auto_update.read_installed_version(install), '2.0.0')
        # 根目录那份坏了就退回 _internal 里的好副本。
        (install / 'VERSION').write_text('nonsense', encoding='utf-8')
        self.assertEqual(auto_update.read_installed_version(install), '1.9.0')

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


class RetryTests(unittest.TestCase):
    """A flaky link must not cost a whole update cycle.

    The controller reports transport failures as an "error" state with no version at all.
    Without a retry, one dropped connection meant waiting for the next scheduled check,
    which is hours away.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.install = self.root / "install"
        self.install.mkdir()
        (self.install / "VERSION").write_text("1.8.11\n", encoding="utf-8")
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.statuses = []

    TRANSPORT_ERROR = {
        "state": "error", "latest_version": "", "progress": 0, "transient": True,
        "checked": "", "message": "无法连接 GitHub 或下载超时，请检查网络后重新检查。",
        "changes": {},
    }
    READY = {
        "state": "ready", "latest_version": "1.8.14", "progress": 100,
        "checked": "2026-10-10 20:30", "message": "已下载并校验通过。", "changes": {},
    }

    def make(self, snapshots, install_result=None):
        outcomes = list(snapshots)

        class Flaky:
            calls = 0

            def __init__(inner, *args, **kwargs):
                pass

            def check(inner):
                inner.calls += 1

            def snapshot(inner):
                index = min(inner.calls - 1, len(outcomes) - 1)
                return dict(outcomes[index], busy=False)

            def package(inner):
                return Path("pkg.msi"), "1.8.14", "a" * 64, "b" * 128

            def close(inner):
                pass

        holder = {}

        def factory(*args, **kwargs):
            holder["controller"] = Flaky(*args, **kwargs)
            return holder["controller"]

        installer = lambda *a, **k: (install_result or {"code": 0, "healthy": True, "relaunched": True})
        updater = Updater(install_dir=self.install, cache_dir=self.cache,
                          executable=self.install / "youziauth.exe",
                          controller_factory=factory, installer=installer,
                          report=self.statuses.append)
        return updater, holder

    def test_a_transport_failure_is_retried_and_can_still_succeed(self):
        updater, holder = self.make([self.TRANSPORT_ERROR, self.READY])
        with patch.object(auto_update, "_CHECK_BACKOFF", 0):
            status = updater.run_cycle()
        self.assertEqual(holder["controller"].calls, 2, "the check must be retried once")
        self.assertEqual(status.state, "installed")

    def test_retries_stop_at_the_attempt_limit(self):
        updater, holder = self.make([self.TRANSPORT_ERROR])
        with patch.object(auto_update, "_CHECK_BACKOFF", 0):
            status = updater.run_cycle()
        self.assertEqual(status.state, "error")
        self.assertEqual(holder["controller"].calls, auto_update._CHECK_ATTEMPTS)

    def test_a_transport_failure_during_the_download_is_retried_too(self):
        # 下载中途断开同样是传输问题：包没下完，重试才有意义。
        dropped = dict(self.TRANSPORT_ERROR)
        dropped.update(latest_version="1.8.14", progress=0)
        updater, holder = self.make([dropped, self.READY])
        with patch.object(auto_update, "_CHECK_BACKOFF", 0):
            status = updater.run_cycle()
        self.assertEqual(holder["controller"].calls, 2)
        self.assertEqual(status.state, "installed")

    def test_a_real_verification_failure_is_not_retried(self):
        # 校验失败是明确结论：重试等于反复撞同一堵墙。
        failure = dict(self.READY, state="error", latest_version="1.8.14", transient=False,
                       progress=100, message="MSI 安装包签名未通过校验，已拒绝更新。")
        updater, holder = self.make([failure])
        with patch.object(auto_update, "_CHECK_BACKOFF", 0):
            status = updater.run_cycle()
        self.assertEqual(holder["controller"].calls, 1, "a decided failure must be reported at once")
        self.assertEqual(status.state, "error")
        self.assertIn("签名", status.message)


class AgentRecoveryTests(unittest.TestCase):
    """A successful update must leave the agent running.

    The installer stops both processes. Relaunching only the window turns unattended
    updating into a one-shot: the agent is what performs the next update, and its task
    only fires at boot. Observed live - after 1.8.25 -> 1.8.26 the window was back but
    the agent was gone, and nine hours later (no reboot) it still had not returned.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.install = self.root / "install"
        self.install.mkdir()
        (self.install / "VERSION").write_text("1.8.25\n", encoding="utf-8")
        self.cache = self.root / "cache"
        self.cache.mkdir()

    READY = {
        "state": "ready", "latest_version": "1.8.26", "progress": 100,
        "checked": "2026-10-11 01:00", "message": "ok", "changes": {},
    }

    def build(self, agent_relaunch):
        class Controller:
            _worker = None
            def __init__(inner, *a, **k): pass
            def check(inner): pass
            def snapshot(inner): return dict(self.READY)
            def package(inner): return Path("p.msi"), "1.8.26", "a" * 64, "b" * 128
            def close(inner): pass

        return Updater(install_dir=self.install, cache_dir=self.cache,
                       executable=self.install / "youziauth.exe",
                       controller_factory=lambda *a, **k: Controller(),
                       installer=lambda *a, **k: {"code": 0, "healthy": True, "relaunched": True},
                       relaunch=lambda: True,
                       agent_relaunch=agent_relaunch)

    def test_the_agent_is_started_again_after_a_successful_install(self):
        calls = []
        status = self.build(lambda: calls.append("agent") or True).run_cycle()
        self.assertEqual(calls, ["agent"], "the agent must be started again")
        self.assertEqual(status.state, "installed")
        self.assertEqual(status.detail, "agent restarted")

    def test_a_failed_restart_is_reported_but_does_not_fail_the_update(self):
        # 更新已经装好了，agent 没拉起来不该让整件事变成失败。
        def boom():
            raise OSError("schtasks is unavailable")

        status = self.build(boom).run_cycle()
        self.assertEqual(status.state, "installed")
        self.assertNotEqual(status.detail, "agent restarted")

    def test_no_runner_configured_is_not_an_error(self):
        self.assertFalse(self.build(None)._restart_agent())


class AgentTaskRunnerTests(unittest.TestCase):
    """run_agent_task asks Task Scheduler for the agent's own task."""

    def test_it_runs_the_system_agent_task(self):
        import startup_tasks
        seen = {}

        class Result:
            returncode = 0

        def runner(command, **kwargs):
            seen["command"] = command
            seen["kwargs"] = kwargs
            return Result()

        self.assertTrue(startup_tasks.run_agent_task(runner=runner))
        self.assertEqual(seen["command"][1:], ["/Run", "/TN", startup_tasks.SYSTEM_TASK_NAME])
        self.assertTrue(seen["command"][0].lower().endswith("schtasks.exe"))
        self.assertFalse(seen["kwargs"].get("check", True), "a failure is returned, not raised")

    def test_a_failing_runner_returns_false(self):
        import startup_tasks

        class Result:
            returncode = 1

        self.assertFalse(startup_tasks.run_agent_task(runner=lambda *a, **k: Result()))

    def test_an_unavailable_schtasks_returns_false(self):
        import startup_tasks

        def runner(*args, **kwargs):
            raise OSError("not found")

        self.assertFalse(startup_tasks.run_agent_task(runner=runner))


if __name__ == '__main__':
    unittest.main()
