"""When the background update check is allowed to be skipped, and for how long.

The installed product checks for a new release on a fixed interval. Everything in this
file is about the gap between two of those checks, because that gap is what the owner of
a machine actually experiences as "自动更新有时候不跑":

* a check that could not start used to consume its slot silently (six hours);
* a failure used to consume the full interval even when it was just a dropped
  connection;
* a recovered install result pushed the next check six hours out, so a machine that
  updates more often than the interval never reached its next scheduled check at all.

Observed in the shipped product's ``agent.log``: 81 periodic checks in 33 days of
near-continuous uptime (a 6-hour interval over that span allows ~130), a 48-hour stretch
with no periodic check while the agent kept cycling, and 125 manual "check now" requests
from the interface in between.
"""

import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import auto_update
import campus_auth
from campus_auth_agent import Agent

INTERVAL = 6 * 60 * 60
# Captured before any test replaces ``time.monotonic``: waiting for a worker thread has
# to use the real clock, or a frozen one makes ``wait_for_update`` spin forever.
_REAL_MONOTONIC = time.monotonic


class StubUpdater:
    """A background updater that reports one canned outcome."""

    def __init__(self, status=None, recovered=None):
        self._status = status or auto_update.UpdateStatus(state='up_to_date', message='已是最新。')
        self._recovered = recovered
        self.calls = 0
        self.retry_after_seconds = None

    def run_cycle(self, progress=None):
        self.calls += 1
        return self._status

    def recover_install_result(self):
        status, self._recovered = self._recovered, None
        return status


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = campus_auth.AuthConfig(username='fixture', password='fixture')
        self.logger = logging.getLogger(f'update-scheduling-{id(self)}')
        self.logger.addHandler(logging.NullHandler())
        self.clock = 1000.0
        clock = patch.object(time, 'monotonic', side_effect=lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)

    def make_agent(self, updater, **options):
        agent = Agent(
            lambda: self.config, None, None, self.root / 'runtime.json', self.logger,
            boot_id='fixture-boot', updater=updater,
            update_interval_seconds=INTERVAL, **options)
        self.addCleanup(self.settle, agent)
        return agent

    @staticmethod
    def settle(agent, timeout=5):
        """Wait for the update worker with the real clock, not the frozen test clock."""
        with patch.object(time, 'monotonic', _REAL_MONOTONIC):
            return agent.wait_for_update(timeout)

    # --- a slot that could not be used is not a slot that was used -------------

    def test_a_blocked_start_retries_in_a_minute_instead_of_skipping_the_interval(self):
        # 界面每秒一次的 status 轮询会短暂持有同一把锁：撞上就该一分钟后再试，
        # 而不是把整整一个间隔悄悄吃掉（日志里一行都没有，看起来就像没这回事）。
        updater = StubUpdater()
        agent = self.make_agent(updater)
        self.assertTrue(agent._update_lock.acquire(blocking=False))
        try:
            with self.assertLogs(self.logger, level='WARNING') as captured:
                agent.periodic_update_check()
        finally:
            agent._update_lock.release()
        self.assertIn('still running', ' '.join(captured.output))
        self.assertEqual(updater.calls, 0, 'the blocked slot must not have run anything')
        self.assertEqual(agent._next_update_at, self.clock + 60.0,
                         'a blocked slot retries in a minute, not in an interval')
        self.assertFalse(agent.update_due(), 'not due immediately')
        self.clock += 61
        self.assertTrue(agent.update_due(), 'the retry must be minutes away, not an interval')

    def test_a_successful_start_still_schedules_the_whole_interval(self):
        # 安装会杀掉进程：排期必须在开工之前就往后推，重启后不该立刻再装一遍。
        updater = StubUpdater()
        agent = self.make_agent(updater)
        agent.periodic_update_check()
        self.assertTrue(self.settle(agent))
        self.assertEqual(updater.calls, 1)
        self.assertEqual(agent._next_update_at, self.clock + INTERVAL)
        self.assertFalse(agent.update_due())
        self.clock += INTERVAL + 1
        self.assertTrue(agent.update_due())

    def test_a_thread_that_cannot_start_does_not_leave_the_lock_held(self):
        # 线程起不来却留着锁，等于这个进程活着的每一天都不会再有自动更新。
        updater = StubUpdater()
        agent = self.make_agent(updater)
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError('no threads')):
            self.assertFalse(agent._start_update_thread('requested'))
        self.assertTrue(agent._update_lock.acquire(blocking=False),
                        'the lock must be released when the worker cannot start')
        agent._update_lock.release()
        # And the next scheduled check still works, instead of being dead forever.
        agent.periodic_update_check()
        self.assertTrue(self.settle(agent))
        self.assertEqual(updater.calls, 1)

    def test_an_agent_without_an_updater_never_reschedules_anything(self):
        agent = self.make_agent(None)
        before, published = agent._next_update_at, agent.snapshot.update
        self.assertEqual(agent.periodic_update_check().update, published)
        self.assertEqual(agent._next_update_at, before)
        self.assertFalse(agent.update_due())

    # --- how long after a failure or an install the next attempt happens ------

    def test_a_transport_failure_is_retried_sooner_than_a_full_interval(self):
        class Failing:
            retry_after_seconds = 30 * 60

            def run_cycle(inner, progress=None):
                return auto_update.UpdateStatus(state='error', message='无法连接 GitHub。',
                                                detail='URLError')

        agent = self.make_agent(Failing())
        agent._next_update_at = self.clock + INTERVAL
        with agent._update_lock:
            agent._run_update_check('periodic')
        self.assertEqual(agent._next_update_at, self.clock + 30 * 60)
        self.assertFalse(agent.update_due())
        self.clock += 30 * 60 + 1
        self.assertTrue(agent.update_due())

    def test_a_decided_failure_keeps_the_normal_interval(self):
        # 安装失败会 taskkill 掉窗口和 agent：按 30 分钟一轮反复重试等于反复关掉
        # 用户的程序，所以只有「暂时的」失败才提前重试。
        class Refusing:
            retry_after_seconds = None

            def run_cycle(inner, progress=None):
                return auto_update.UpdateStatus(state='error', message='签名未通过',
                                                detail='verification failed')

        agent = self.make_agent(Refusing())
        agent._next_update_at = self.clock + INTERVAL
        with agent._update_lock:
            agent._run_update_check('periodic')
        self.assertEqual(agent._next_update_at, self.clock + INTERVAL)

    def test_a_recovered_install_defers_a_settle_window_not_the_whole_interval(self):
        # 恢复出来的是「刚刚装完」。它要挡的只是装完立刻再装一遍；按 6 小时去挡，
        # 等于把「装完之后发布的下一个版本」也一起挡住。
        recovered = auto_update.UpdateStatus(state='installed', current_version='1.9.14',
                                             latest_version='1.9.14')
        agent = self.make_agent(StubUpdater(recovered=recovered))
        agent._recover_update()
        self.assertEqual(agent.snapshot.update.get('state'), 'installed')
        self.assertFalse(agent.update_due(), '刚装完不能重启后立刻再跑一遍')
        self.assertEqual(agent._next_update_at, self.clock + 15 * 60)
        self.clock += 15 * 60 + 1
        self.assertTrue(agent.update_due(),
                        '装完之后发布的新版本最多等一个静默期，而不是一整个间隔')

    def test_a_recovered_install_never_pulls_an_already_later_check_forward(self):
        recovered = auto_update.UpdateStatus(state='installed', current_version='1.9.14',
                                             latest_version='1.9.14')
        agent = self.make_agent(StubUpdater(recovered=recovered))
        agent._next_update_at = self.clock + INTERVAL
        agent._recover_update()
        self.assertEqual(agent._next_update_at, self.clock + INTERVAL)


class StatusCarriesTheCauseTests(unittest.TestCase):
    """The agent's log line is the only post-mortem for an unattended update.

    Every one of the eight failures in the shipped log ended in ``latest=`` with no
    ``detail=`` at all: the controller knew the cause (exception type or named stage)
    and ``_status_from_check`` threw it away.
    """

    def test_the_controllers_failure_detail_reaches_the_status(self):
        status = auto_update.Updater(Path('.'), Path('.'))._status_from_check(
            {'state': 'error', 'message': '无法连接 GitHub 或下载超时…', 'detail': 'URLError'},
            '1.9.15')
        self.assertEqual(status.state, 'error')
        self.assertEqual(status.detail, 'URLError')

    def test_an_unknown_state_is_still_an_error(self):
        status = auto_update.Updater(Path('.'), Path('.'))._status_from_check(
            {'state': 'launched', 'message': '向导已打开'}, '1.9.15')
        self.assertEqual(status.state, 'error')
        self.assertEqual(status.detail, 'check failed')

    def test_the_checking_state_is_not_rewritten_as_a_failure(self):
        # 'checking' 是本模块自己的状态词，也是「正在读取更新说明」这一步：
        # 把它映射成 error 会让那段提示永远发不出去。
        status = auto_update.Updater(Path('.'), Path('.'))._status_from_check(
            {'state': 'checking', 'message': '发现 v1.9.16，正在读取更新说明…',
             'latest_version': '1.9.16'}, '1.9.15')
        self.assertEqual(status.state, 'checking')
        self.assertEqual(status.detail, '')


if __name__ == '__main__':
    unittest.main()
