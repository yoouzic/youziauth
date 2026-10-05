"""账号档案层：清单、目录布局、老数据迁移，以及「同时只有一个活动账号」的交接。

这一层最贵的 bug 不是功能性错误，而是**悄悄换了一个账号**：
A 的凭据用到 B 上、B 的打卡点画到 A 的地图上、A 的当日已打卡让 B 不再打卡。
所以下面的测试重点盯三件事：隔离、切换守卫、以及档案读不出来时绝不静默降级。
"""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import dorm_accounts
import dorm_login
import dorm_points
from dorm_accounts import AccountError, DormAccounts, MachineLoginBudget, Registry
from dorm_checkin import Result, Settings, Store


class FakeController:
    """控制器替身：只记下自己被哪个档案目录、用什么错峰偏移构造，以及是否正忙。"""

    def __init__(self, root, stagger_seconds=0):
        self.root = Path(root)
        self.store = Store(self.root)
        self.stagger_seconds = stagger_seconds
        self.busy = False
        self.closed = False
        self.cancelled = False
        self.polled = 0
        self.results = []
        self.latest = Result('idle', '尚未查询今日任务')

    def poll(self):
        self.polled += 1
        return True

    def drain(self):
        values, self.results = self.results, []
        if values:
            self.latest = values[-1]
        return values

    def cancel(self):
        self.cancelled = True

    def close(self):
        self.closed = True


def build_accounts(directory, **kwargs):
    return DormAccounts(Path(directory) / 'accounts', controller_factory=FakeController, **kwargs)


def _at(value):
    import datetime as dt
    return dt.datetime.fromisoformat(value)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'accounts'

    def test_first_run_creates_exactly_one_account_with_a_directory(self):
        registry = Registry(self.root)
        accounts = registry.ensure()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].name, '账号 1')
        self.assertEqual(registry.active_id(), accounts[0].id)
        self.assertTrue(registry.exists(accounts[0].id))
        self.assertEqual(json.loads((self.root / 'accounts.json').read_text(encoding='utf-8'))['v'], 1)

    def test_adding_activates_and_caps_the_count(self):
        registry = Registry(self.root)
        registry.ensure()
        for index in range(2, dorm_accounts.MAX_ACCOUNTS + 1):
            record = registry.add()
            self.assertEqual(record.name, f'账号 {index}')
        self.assertEqual(len(registry.accounts()), dorm_accounts.MAX_ACCOUNTS)
        with self.assertRaisesRegex(AccountError, '最多'):
            registry.add()

    def test_names_are_cleaned_and_must_be_unique(self):
        registry = Registry(self.root)
        first = registry.ensure()[0]
        renamed = registry.rename(first.id, '  张三\u0007  ')
        self.assertEqual(renamed, '张三')
        second = registry.add('李四')
        with self.assertRaisesRegex(AccountError, '同名'):
            registry.rename(second.id, '张三')
        with self.assertRaisesRegex(AccountError, '不能为空'):
            registry.rename(second.id, '   ')
        self.assertEqual(registry.find(second.id).name, '李四')

    def test_switching_to_a_missing_profile_is_refused(self):
        registry = Registry(self.root)
        first = registry.ensure()[0]
        second = registry.add()
        registry.activate(first.id)                 # add() 会把新账号设为活动账号
        import shutil
        shutil.rmtree(registry.profile(second.id))
        with self.assertRaisesRegex(AccountError, '档案目录不存在'):
            registry.activate(second.id)
        self.assertEqual(registry.active_id(), first.id)
        self.assertFalse(registry.exists(second.id))

    def test_removing_the_active_account_moves_the_flag_and_keeps_one(self):
        registry = Registry(self.root)
        first = registry.ensure()[0]
        second = registry.add()
        removed = registry.remove(second.id)
        self.assertEqual(removed.id, second.id)
        self.assertEqual(registry.active_id(), first.id)
        with self.assertRaisesRegex(AccountError, '至少保留一个账号'):
            registry.remove(first.id)

    def test_corrupt_registry_fails_closed_instead_of_rebuilding(self):
        self.root.mkdir(parents=True)
        (self.root / 'accounts.json').write_text('{', encoding='utf-8')
        with self.assertRaisesRegex(AccountError, '损坏'):
            Registry(self.root).ensure()

    def test_registry_never_returns_an_empty_account_list(self):
        self.root.mkdir(parents=True)
        (self.root / 'accounts.json').write_text(
            json.dumps({'v': 1, 'active': '', 'accounts': []}), encoding='utf-8')
        with self.assertRaisesRegex(AccountError, '没有任何账号'):
            Registry(self.root).ensure()

    def test_a_stale_active_id_falls_back_instead_of_failing(self):
        self.root.mkdir(parents=True)
        (self.root / 'accounts.json').write_text(json.dumps({
            'v': 1, 'active': 'ffffffffffff',
            'accounts': [{'id': 'aaaaaaaaaaaa', 'name': '甲', 'created_at': ''}]}), encoding='utf-8')
        registry = Registry(self.root)
        self.assertEqual(registry.active_id(), 'aaaaaaaaaaaa')

    def test_an_account_id_may_not_escape_the_accounts_directory(self):
        self.root.mkdir(parents=True)
        (self.root / 'accounts.json').write_text(json.dumps({
            'v': 1, 'active': '', 'accounts': [{'id': '../..', 'name': '坏', 'created_at': ''}]}),
            encoding='utf-8')
        with self.assertRaisesRegex(AccountError, '标识无效'):
            Registry(self.root).ensure()

    def test_a_registry_deleted_while_running_says_so_instead_of_confusing_the_caller(self):
        registry = Registry(self.root)
        registry.ensure()
        (self.root / 'accounts.json').unlink()
        with self.assertRaisesRegex(AccountError, '账号列表文件不存在'):
            registry.accounts()

    def test_a_directory_that_cannot_be_created_is_reported_not_raised_as_oserror(self):
        blocker = Path(self.tmp.name) / 'blocked'
        blocker.write_text('not a directory', encoding='utf-8')
        with self.assertRaisesRegex(AccountError, '无法创建或读取'):
            Registry(blocker / 'accounts').ensure()
        broken = DormAccounts(blocker / 'accounts', controller_factory=FakeController)
        self.assertTrue(broken.error)
        self.assertEqual(broken.snapshot()['items'], [])

    def test_a_deleted_registry_is_rebuilt_from_the_profile_directories(self):
        registry = Registry(self.root)
        first = registry.ensure()[0]
        (self.root / 'accounts.json').unlink()
        rebuilt = Registry(self.root).ensure()
        self.assertEqual([account.id for account in rebuilt], [first.id])

    def test_a_registry_without_any_known_marker_is_not_adopted(self):
        self.root.mkdir(parents=True)
        (self.root / 'backup').mkdir()
        (self.root / 'backup' / 'notes.txt').write_text('x', encoding='utf-8')
        accounts = Registry(self.root).ensure()
        self.assertEqual(len(accounts), 1)
        self.assertNotEqual(accounts[0].id, 'backup')


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'accounts'
        self.base = self.root.parent

    def legacy_store(self):
        store = Store(self.base / 'dorm',
                      protector=MagicMock(protect=lambda b: b[::-1], unprotect=lambda b: b[::-1]))
        store.save_settings(Settings(enabled=True, interval=600, location_source='simulation'))
        store.save_points(dorm_points.add(dorm_points.empty(), name='李园一舍',
                                          latitude=29.8236, longitude=106.4223,
                                          accuracy=100.0, saved_at='2026-10-04T21:00:00+08:00'))
        return store

    def test_legacy_single_account_data_moves_into_an_account_directory(self):
        self.legacy_store()
        (self.base / 'idm').mkdir()
        (self.base / 'idm' / 'credential.dat').write_bytes(b'legacy-idm')
        accounts = Registry(self.root).ensure()
        self.assertEqual(len(accounts), 1)
        profile = self.root / accounts[0].id
        self.assertFalse((self.base / 'dorm').exists())
        self.assertEqual(Store(profile).settings().interval, 600)
        self.assertEqual(dorm_points.find(Store(profile).points(),
                                         Store(profile).points()['active'])['name'], '李园一舍')
        self.assertEqual((profile / 'idm' / 'credential.dat').read_bytes(), b'legacy-idm')
        self.assertFalse((self.base / 'idm').exists())

    def test_migration_runs_once_and_leaves_existing_accounts_alone(self):
        self.legacy_store()
        first = Registry(self.root).ensure()
        again = Registry(self.root).ensure()
        self.assertEqual([account.id for account in again], [account.id for account in first])
        self.assertFalse((self.base / 'dorm').exists())

    def test_a_second_account_is_added_next_to_the_migrated_one(self):
        self.legacy_store()
        registry = Registry(self.root)
        migrated = registry.ensure()[0]
        other = registry.add('第二个')
        self.assertNotEqual(other.id, migrated.id)
        self.assertTrue(registry.exists(migrated.id))
        self.assertEqual(len(registry.accounts()), 2)


class DormAccountsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.accounts = build_accounts(self.tmp.name)

    def test_the_active_controller_is_built_once_per_account(self):
        controller = self.accounts.controller
        self.assertIs(self.accounts.controller, controller)
        self.assertTrue(controller.root.is_dir())
        self.assertEqual(controller.root.parent, Path(self.tmp.name) / 'accounts')

    def test_switching_is_a_view_change_and_leaves_every_controller_running(self):
        """第二阶段的关键变化：切换不再交接控制器，两个账号的控制器都活着。"""
        first = self.accounts.controller
        message = self.accounts.add('第二个')
        self.assertIn('第二个', message)
        second = self.accounts.controller
        self.assertIsNot(first, second)
        self.assertFalse(first.closed, '切走别人的账号不该把它的控制器关掉')
        self.assertNotEqual(first.root, second.root)
        self.assertEqual(self.accounts.registry().active_id(), second.root.name)

        self.accounts.switch(first.root.name)
        self.assertIs(self.accounts.controller, first)
        self.assertFalse(second.closed)
        self.assertEqual(sorted(c.root.name for c in self.accounts._controllers.values()),
                         sorted([first.root.name, second.root.name]))

    def test_switching_is_allowed_while_another_account_is_checking_in(self):
        """后台账号正在打卡时也要能切走——它跑它的，界面看别的账号。"""
        self.accounts.add('第二个')
        first_id = self.accounts.registry().accounts()[0].id
        self.accounts.switch(first_id)
        busy = self.accounts.controller
        busy.busy = True
        other = self.accounts.registry().accounts()[1]
        self.assertIn('已切换', self.accounts.switch(other.id))
        self.assertEqual(self.accounts.registry().active_id(), other.id)
        self.assertTrue(busy.busy, '后台账号的操作不受切换影响')
        self.assertFalse(busy.closed)

    def test_settings_points_and_login_state_do_not_leak_between_accounts(self):
        first_id = self.accounts.registry().active_id()
        first = self.accounts.controller
        first.store.save_settings(Settings(enabled=True, interval=600, location_source='simulation'))
        first.store.save_points(dorm_points.add(dorm_points.empty(), name='甲楼',
                                                latitude=29.8236, longitude=106.4223,
                                                accuracy=100.0, saved_at=''))
        first.store.save_token('first-token')

        self.accounts.add('第二个')
        second = self.accounts.controller
        self.assertEqual(second.store.settings().interval, 300)          # 默认值，不是甲账号的 600
        self.assertEqual(second.store.settings().location_source, 'windows')
        self.assertEqual(second.store.points()['points'], [])
        self.assertEqual(second.store.token(), '')

        self.accounts.switch(first_id)
        back = self.accounts.controller
        self.assertEqual(back.store.settings().interval, 600)
        self.assertEqual(back.store.token(), 'first-token')

    def test_each_account_owns_its_unified_credential_store(self):
        first = self.accounts.idm_store()
        self.accounts.add()
        second = self.accounts.idm_store()
        self.assertIsNot(first, second)
        self.assertNotEqual(first.root, second.root)
        self.assertEqual(first.root.parent.parent, Path(self.tmp.name) / 'accounts')
        self.accounts.switch(self.accounts.registry().accounts()[0].id)
        self.assertIs(self.accounts.idm_store(), first)

    def test_deleting_an_account_removes_its_directory_but_not_the_others(self):
        keep = self.accounts.registry().active_id()
        self.accounts.add('要删的')
        target = self.accounts.registry().active_id()
        target_root = self.accounts.controller.root
        self.assertTrue(target_root.is_dir())
        message = self.accounts.remove(target)
        self.assertIn('要删的', message)
        self.assertFalse(target_root.exists())
        self.assertEqual(self.accounts.registry().active_id(), keep)
        self.assertTrue((Path(self.tmp.name) / 'accounts' / keep).is_dir())
        with self.assertRaisesRegex(AccountError, '找不到'):
            self.accounts.registry().find(target)

    def test_the_last_account_cannot_be_deleted(self):
        only = self.accounts.registry().active_id()
        with self.assertRaisesRegex(AccountError, '至少保留一个账号'):
            self.accounts.remove(only)
        self.assertTrue(self.accounts.registry().exists(only))

    def test_poll_runs_every_account_and_labels_the_results(self):
        """多账号的核心：一拍里所有账号都跑，而且结果带着账号名字回到界面。"""
        first = self.accounts.controller
        first.results = [Result('signed', '今日打卡已完成')]
        self.accounts.add('第二个')
        second = self.accounts.controller
        second.results = [Result('waiting', '等待检查时段 21:00–23:15')]

        events = self.accounts.poll()
        self.assertEqual([name for name, _ in events], ['账号 1', '第二个'])
        self.assertEqual([result.state for _, result in events], ['signed', 'waiting'])
        self.assertEqual((first.polled, second.polled), (1, 1))

    def test_poll_skips_a_missing_profile_instead_of_failing_the_round(self):
        import shutil
        first = self.accounts.controller
        first.results = [Result('waiting', '等待检查时段 21:00–23:15')]
        self.accounts.add('要坏的')
        broken = self.accounts.controller
        shutil.rmtree(broken.root)
        events = self.accounts.poll()
        self.assertEqual([name for name, _ in events], ['账号 1'])
        self.assertEqual(first.polled, 1)
        self.assertEqual(broken.polled, 0, '档案目录没了的账号不该再被轮询')

    def test_each_account_gets_its_own_stagger_offset_in_registry_order(self):
        self.accounts.poll()                        # 建齐所有账号的控制器
        self.accounts.add('第二个')
        self.accounts.add('第三个')
        self.accounts.poll()
        offsets = {c.root.name: c.stagger_seconds
                   for c in self.accounts._controllers.values()}
        ordered = self.accounts.registry().accounts()
        self.assertEqual([offsets[account.id] for account in ordered],
                         [0, dorm_accounts.STAGGER_SECONDS, 2 * dorm_accounts.STAGGER_SECONDS])

    def test_any_busy_and_cancel_all_cover_every_account(self):
        first = self.accounts.controller
        self.accounts.add('第二个')
        second = self.accounts.controller
        self.assertFalse(self.accounts.any_busy())
        second.busy = True
        self.assertTrue(self.accounts.any_busy())
        self.accounts.cancel_all()
        self.assertTrue(second.cancelled)
        self.assertTrue(first.cancelled, '取消要发给所有账号，不只是正在忙的那个')

    def test_deleting_a_busy_account_is_refused_with_its_name(self):
        self.accounts.add('正在打卡的')
        target = self.accounts.registry().active_id()
        profile = self.accounts.controller.root
        self.accounts.controller.busy = True
        with self.assertRaisesRegex(AccountError, '正在打卡'):
            self.accounts.remove(target)
        self.assertTrue(profile.is_dir())
        self.assertEqual(len(self.accounts.registry().accounts()), 2)

    def test_snapshot_reports_each_account_today_state(self):
        first_id = self.accounts.registry().active_id()
        first = self.accounts.controller
        first.store.save_settings(Settings(enabled=True))
        first.store.mark_signed('2026-10-04', '2023000001')
        self.accounts.add('第二个')
        with patch.object(dorm_accounts, 'now', return_value=_at('2026-10-04T21:30:00+08:00')):
            block = self.accounts.snapshot()
        rows = {item['id']: item for item in block['items']}
        self.assertTrue(rows[first_id]['enabled'])
        self.assertTrue(rows[first_id]['signed_today'])
        self.assertFalse(rows[first_id]['busy'])
        second = self.accounts.registry().accounts()[1]
        self.assertFalse(rows[second.id]['enabled'])
        self.assertFalse(rows[second.id]['signed_today'])
        self.assertEqual(rows[second.id]['state'], 'idle')
        self.assertFalse(block['busy'])

    def test_snapshot_marks_a_busy_account_and_the_whole_pool(self):
        self.accounts.add('第二个')
        self.accounts.controller.busy = True
        block = self.accounts.snapshot()
        self.assertTrue(block['busy'])
        self.assertTrue(block['items'][1]['busy'])
        self.assertFalse(block['items'][0]['busy'])

    def test_poll_never_touches_a_missing_registry(self):
        broken = DormAccounts(Path(self.tmp.name) / 'broken2', controller_factory=FakeController)
        (Path(self.tmp.name) / 'broken2' / 'accounts.json').write_text('{', encoding='utf-8')
        broken = DormAccounts(Path(self.tmp.name) / 'broken2', controller_factory=FakeController)
        self.assertTrue(broken.error)
        self.assertEqual(broken.poll(), [])

    def test_snapshot_never_exposes_a_path(self):
        self.accounts.add('第二个')
        block = self.accounts.snapshot()
        self.assertEqual(block['max'], dorm_accounts.MAX_ACCOUNTS)
        self.assertEqual([item['active'] for item in block['items']], [False, True])
        self.assertNotIn(self.tmp.name, json.dumps(block))
        self.assertEqual([item['missing'] for item in block['items']], [False, False])

    def test_a_missing_profile_is_reported_and_blocks_that_account(self):
        self.accounts.add('损坏的')
        profile = self.accounts.controller.root
        import shutil
        shutil.rmtree(profile)
        block = self.accounts.snapshot()
        self.assertTrue(block['items'][1]['missing'])
        self.assertFalse(block['items'][0]['missing'])
        with self.assertRaisesRegex(AccountError, '档案目录不存在'):
            self.accounts.controller
        # 仍然可以切走并删除它，界面不会因此变成死局。
        self.accounts.switch(block['items'][0]['id'])
        self.assertEqual(self.accounts.registry().active_id(), block['items'][0]['id'])
        self.accounts.remove(block['items'][1]['id'])
        self.assertEqual(len(self.accounts.registry().accounts()), 1)

    def test_a_broken_registry_keeps_construction_alive_and_fails_every_operation(self):
        root = Path(self.tmp.name) / 'broken'
        root.mkdir(parents=True)
        (root / 'accounts.json').write_text('{"v": 1, "accounts": "nope"}', encoding='utf-8')
        broken = DormAccounts(root, controller_factory=FakeController)
        self.assertTrue(broken.error)
        self.assertEqual(broken.snapshot()['items'], [])
        self.assertIn('error', broken.snapshot())
        for operation in (lambda: broken.controller, lambda: broken.add(),
                          lambda: broken.switch('aaaaaaaaaaaa'),
                          lambda: broken.idm_store()):
            with self.assertRaises(AccountError):
                operation()
        self.assertEqual(broken.poll(), [])      # 调度安静跳过，不刷屏
        self.assertIsNone(broken.controller_or_none())
        broken.close()                            # 关闭也不能抛

    def test_deleting_an_account_cannot_slip_in_while_a_poll_is_starting_its_check(self):
        """池闸的意义：轮询正把某个账号的检查启动起来时，删账号必须等它、然后被忙状态挡住。

        没有这道闸就可能出现：轮询刚置忙，删除读到还没忙 → 关控制器（close 置取消位）
        → 一次已经发出的提交被打断，而且档案目录当场被删。
        """
        first = self.accounts.registry().active_id()
        self.accounts.add('第二个')
        target = self.accounts.registry().active_id()
        target_controller = self.accounts._controller_for(self.accounts.registry().find(target))
        self.accounts.switch(first)
        started, release = threading.Event(), threading.Event()

        def slow_poll():
            started.set()
            release.wait(5)
            target_controller.busy = True   # 真实控制器在 start() 里同步置忙

        target_controller.poll = slow_poll
        outcome = {}
        worker = threading.Thread(target=self.accounts.poll)
        worker.start()
        self.assertTrue(started.wait(2))
        remover = threading.Thread(
            target=lambda: self._record(outcome, lambda: self.accounts.remove(target)))
        remover.start()
        # 闸门还在轮询手里：给删除线程足够时间跑（跑得动就意味着没有闸），但它必须动不了。
        time.sleep(0.3)
        self.assertEqual(outcome, {}, '轮询还没结束，删除不该完成')
        release.set()
        worker.join(2)
        remover.join(2)
        self.assertIn('error', outcome, '轮询结束后删除必须看到忙状态并拒绝')
        self.assertEqual(len(self.accounts.registry().accounts()), 2)

    @staticmethod
    def _record(outcome, call):
        try:
            outcome['message'] = call()
        except AccountError as exc:
            outcome['error'] = str(exc)

    def test_close_stops_the_controller_and_refuses_to_restart(self):
        controller = self.accounts.controller
        self.accounts.close()
        self.assertTrue(controller.closed)
        with self.assertRaisesRegex(AccountError, '正在退出'):
            self.accounts.controller

    def test_close_stops_every_account_controller(self):
        first = self.accounts.controller
        self.accounts.add('第二个')
        second = self.accounts.controller
        self.accounts.close()
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)
        self.assertEqual(self.accounts.poll(), [], '退出之后不该再建控制器、再跑任何一拍')
        with self.assertRaisesRegex(AccountError, '正在退出'):
            self.accounts.controller


class MachineLoginBudgetTests(unittest.TestCase):
    """整机每天的自动登录预算：账号级上限挡不住「账号一多，整晚轮流拉浏览器」。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'login-budget.json'

    def test_a_fresh_machine_allows_the_first_attempts(self):
        budget = MachineLoginBudget(self.path, limit=2)
        self.assertEqual(budget.check('2026-10-04'), '')
        budget.spend('2026-10-04')
        budget.spend('2026-10-04')
        message = budget.check('2026-10-04')
        self.assertIn('上限 2 次', message)
        self.assertIn('手动完成', message)

    def test_yesterdays_attempts_do_not_consume_todays_budget(self):
        budget = MachineLoginBudget(self.path, limit=1)
        budget.spend('2026-10-03')
        budget.spend('2026-10-03')
        self.assertEqual(budget.check('2026-10-04'), '')
        budget.spend('2026-10-04')
        self.assertIn('上限 1 次', budget.check('2026-10-04'))
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')),
                         {'date': '2026-10-04', 'used': 1})

    def test_an_unreadable_counter_fails_closed_instead_of_allowing_unlimited_logins(self):
        self.path.write_text('{', encoding='utf-8')
        budget = MachineLoginBudget(self.path, limit=6)
        self.assertIn('无法读取', budget.check('2026-10-04'))
        budget.spend('2026-10-04')               # 记账失败也不能抛
        self.assertIn('无法读取', budget.check('2026-10-04'))

    def test_a_nonsense_counter_file_is_treated_as_damaged(self):
        self.path.write_text(json.dumps({'date': '2026-10-04', 'used': 'many'}), encoding='utf-8')
        self.assertIn('无法读取', MachineLoginBudget(self.path).check('2026-10-04'))

    def test_the_default_limit_is_the_documented_constant(self):
        self.assertEqual(MachineLoginBudget(self.path).limit,
                         dorm_accounts.MAX_DAILY_MACHINE_LOGIN_RENEWALS)

    def test_the_account_pool_wires_one_budget_and_one_gate_for_every_account(self):
        accounts = build_accounts(self.tmp.name)
        budget = accounts.login_budget
        self.assertIsInstance(budget, MachineLoginBudget)
        self.assertEqual(budget.path, Path(self.tmp.name) / 'accounts' / 'login-budget.json')
        self.assertIs(accounts.login_gate, dorm_login.LOGIN_GATE)


class CredentialPlumbingTests(unittest.TestCase):
    """统一认证凭据必须走当前账号那一份，绝不能落回全局目录。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.accounts = build_accounts(self.tmp.name)

    def global_store(self):
        store = MagicMock()
        store.load.return_value = 'global-credentials'
        patcher = patch('idm_credentials.IdmCredentialStore', return_value=store)
        patcher.start()
        self.addCleanup(patcher.stop)
        return store

    def test_resolve_idm_credentials_prefers_the_account_store(self):
        import dorm_login

        global_store = self.global_store()
        account_store = MagicMock()
        account_store.load.return_value = 'account-credentials'
        self.assertEqual(dorm_login.resolve_idm_credentials(None, account_store),
                         'account-credentials')
        global_store.load.assert_not_called()
        self.assertEqual(dorm_login.resolve_idm_credentials('explicit', account_store), 'explicit')

    def test_resolve_idm_credentials_falls_back_to_the_global_store(self):
        import dorm_login

        self.global_store()
        self.assertEqual(dorm_login.resolve_idm_credentials(None), 'global-credentials')

    def test_each_account_reads_its_own_credential_file(self):
        self.accounts.add('第二个')
        first, second = self.accounts.registry().accounts()
        with patch('idm_credentials.DpapiProtector', return_value=MagicMock(
                protect=lambda value: value, unprotect=lambda value: value)):
            from idm_credentials import IdmCredentials, IdmCredentialStore
            store_a = IdmCredentialStore(self.accounts.registry().profile(first.id) / 'idm')
            store_b = IdmCredentialStore(self.accounts.registry().profile(second.id) / 'idm')
            store_a.save(IdmCredentials('2023000001', 'pw-a'))
            self.assertEqual(store_a.load().username, '2023000001')
            self.assertIsNone(store_b.load())
            store_b.save(IdmCredentials('2023000002', 'pw-b'))
            self.assertEqual(store_a.load().username, '2023000001')
            self.assertEqual(store_b.load().username, '2023000002')


if __name__ == '__main__':
    unittest.main()
