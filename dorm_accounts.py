"""账号档案：一个账号一份独立的本机数据目录。

为什么这一层这么薄（2026-10-04 定稿）
--------------------------------------
打卡子系统的全部状态本来就挂在**一个** `Store.root` 下 —— 设置、打卡点列表、模拟定位
样本、学校登录态、当天已打卡免重复判定、每日重试上限、历史与诊断日志。所以「多账号」
在这套结构里几乎不需要动数据层：把那个目录从「一台机器一份」改成「一个账号一份」，
其余代码原地复用，连地图选点与「距学校打卡点多远」的核对也自动按账号分开
（基准点取自当前账号的任务）。

这一层只负责三件事：
    * 账号清单 `accounts.json`（谁、叫什么、当前用哪个）；
    * 目录布局 `%LOCALAPPDATA%\\youziauth\\accounts\\<id>\\`，以及老的单账号目录迁移；
    * 「同一时刻只有一个活动账号」的控制器生命周期 —— 第一阶段只让活动账号自动打卡。

失败纪律与 Store 一致：账号清单读不出来就**显式报错**，绝不静默重建出一份空账号表
（那会让用户以为自己的账号和数据没了）。目录不存在、名称非法、数量超限同理。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import re
import shutil
import threading
import uuid
from pathlib import Path

import dorm_points
from windows_credentials import atomic_write_bytes

APP_DIR_NAME = 'youziauth'
ACCOUNTS_DIRNAME = 'accounts'
REGISTRY_NAME = 'accounts.json'
MACHINE_LOGIN_LOG = 'login-budget.json'
REGISTRY_VERSION = 1
IDM_DIRNAME = 'idm'
# 老版本把单账号数据放在这两个目录下；首次运行新版本时搬进账号目录。
LEGACY_DORM_DIRNAME = 'dorm'
LEGACY_IDM_DIRNAME = 'idm'
# 账号数量上限：自动行为必须有硬上限（同 MAX_DAILY_LOGIN_RENEWALS 的理由）。
# 每个账号都是一条独立的登录/打卡链路，数量无界增长只会把风控和账号安全风险乘上去。
MAX_ACCOUNTS = 5
# 账号之间的错峰间隔：第 i 个账号当天第一次自动检查要等窗口开始 + i×90 秒。
# 多账号共用同一个出口，同一秒一起打学校接口没有任何好处；90 秒也远小于常见时段长度
# （默认窗口 21:00–23:15 共 135 分钟，5 个账号最后一个也只在 +6 分钟开始）。
STAGGER_SECONDS = 90
# 整机每天自动登录（拉起浏览器）的总次数上限。每个账号各 3 次是账号级的保护，
# 但 2026-09-24 那次事故的后果是**本机账户被锁定** —— 那是机器级的，所以还需要这一层。
MAX_DAILY_MACHINE_LOGIN_RENEWALS = 6
MAX_NAME_LENGTH = dorm_points.MAX_NAME_LENGTH
# 账号目录名即账号标识，直接当路径分量用，因此**必须**限定字符集，挡住手改文件里的路径穿越。
ACCOUNT_ID_PATTERN = re.compile(r'[0-9a-f]{12}')
PROFILE_MARKERS = ('settings.json', 'location-points.json', 'location-sample.json',
                   'signed.json', 'daily.json', 'pending.json', 'history.log', 'session')
SHANGHAI = dt.timezone(dt.timedelta(hours=8))


class AccountError(RuntimeError):
    """账号档案层的受控错误：消息直接面向界面，必须是可执行的中文说明。

    与 CheckinError 分开：这里的失败跟学校、网络都无关，全是本机档案问题，
    界面需要把两者说清楚（前者提示重新登录，后者提示检查目录/权限）。
    """


def now() -> dt.datetime:
    return dt.datetime.now(SHANGHAI)


def local_appdata_root() -> Path:
    local = os.environ.get('LOCALAPPDATA')
    return Path(local) if local else Path.home() / 'AppData' / 'Local'


def default_root() -> Path:
    """账号目录的根：用户级 LOCALAPPDATA/youziauth/accounts。"""
    return local_appdata_root() / APP_DIR_NAME / ACCOUNTS_DIRNAME


def legacy_dorm_root(root: Path | None = None) -> Path:
    return (Path(root) if root is not None else default_root()).parent / LEGACY_DORM_DIRNAME


def legacy_idm_root(root: Path | None = None) -> Path:
    return (Path(root) if root is not None else default_root()).parent / LEGACY_IDM_DIRNAME


class MachineLoginBudget:
    """整机每天的自动登录次数预算（跨账号共享）。

    单账号的上限在 `dorm_panel.MAX_DAILY_LOGIN_RENEWALS`（每账号每天 3 次），
    但账号一多，「每个账号都在夜里反复拉起浏览器」的后果叠加在同一台机器、同一个出口上，
    而 2026-09-24 那次事故的结局是**本机账户被锁定** —— 那是机器级的边界。

    读不出来时按「已用尽」处理（fail-closed）：一个读不动的计数器不能成为无限自动登录的理由。
    """

    def __init__(self, path: Path, limit: int = MAX_DAILY_MACHINE_LOGIN_RENEWALS):
        self.path = Path(path)
        self.limit = int(limit)

    def _used(self, today):
        try:
            value = json.loads(self.path.read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            return 0
        if not isinstance(value, dict) or not isinstance(value.get('used'), int) or value['used'] < 0:
            raise ValueError('自动登录计数文件损坏')
        return value['used'] if value.get('date') == today else 0
        # 日期不是今天就当 0：旧的一天不占用今天的额度（写入时才覆盖）。

    def check(self, today) -> str:
        """空字符串 = 还允许自动登录；否则返回要告诉用户的原因。"""
        try:
            used = self._used(today)
        except (OSError, ValueError):
            return ('本机的自动登录计数无法读取，已跳过自动登录；'
                    '请检查账号目录，或点「学校登录 / 重新登录」手动完成')
        if used >= self.limit:
            return (f'本机今天已自动尝试登录 {used} 次（上限 {self.limit} 次），已停止自动重试；'
                    '请点「学校登录 / 重新登录」手动完成需要登录的账号')
        return ''

    def spend(self, today) -> None:
        try:
            used = self._used(today)
            atomic_write_bytes(self.path, json.dumps({'date': today, 'used': used + 1}).encode('utf-8'))
        except (OSError, ValueError):
            pass          # 记不上也照常尝试一次，只是这一层上限保护会失效一次


@dataclasses.dataclass(frozen=True)
class Account:
    id: str
    name: str
    created_at: str = ''


def _next_name(accounts) -> str:
    used = {account.name for account in accounts}
    for index in range(1, MAX_ACCOUNTS + 2):
        candidate = f'账号 {index}'
        if candidate not in used:
            return candidate
    return '账号'


class Registry:
    """accounts.json 的读写；每次读都落盘，和 Store 的读法一致（外部改动立刻生效）。"""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else default_root()
        self.path = self.root / REGISTRY_NAME
        self._ready = False

    # ---- 建立 ---------------------------------------------------------------
    def ensure(self):
        """首次运行时建立账号列表（含老数据迁移与目录收养），已存在则只读校验。"""
        if self.path.exists():
            self.accounts()
            self._ready = True
            return self.accounts()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self._migrate_legacy()
            records = [dataclasses.asdict(account) for account in self._discover()]
            if not records:
                records = [dataclasses.asdict(self._create_profile('账号 1'))]
            self._write(records[0]['id'], records)
        except OSError as exc:
            raise AccountError(
                f'账号目录无法创建或读取：{self.root}（{exc}）。'
                '请检查本机权限后重启程序；原有数据没有被改动。') from None
        self._ready = True
        return self.accounts()

    def _create_profile(self, name):
        account_id = uuid.uuid4().hex[:12]
        record = Account(account_id, name, now().isoformat(timespec='seconds'))
        self.profile(account_id).mkdir(parents=True, exist_ok=True)
        return record

    def _account_dirs(self):
        try:
            children = list(self.root.iterdir())
        except FileNotFoundError:
            return []
        return sorted((path for path in children
                       if path.is_dir() and ACCOUNT_ID_PATTERN.fullmatch(path.name)),
                      key=lambda path: path.name)

    @staticmethod
    def _looks_like_profile(path: Path) -> bool:
        if ACCOUNT_ID_PATTERN.fullmatch(path.name):
            return True
        return any((path / marker).exists() for marker in PROFILE_MARKERS)

    def _discover(self):
        """没有任何清单时按目录恢复账号：老目录已迁移，其余按目录顺序收养。

        为什么允许「收养」：账号清单被手工删除（或从备份恢复了一半）时，
        档案目录里的数据还在。重建成一份空表等于把这些数据变成孤儿，
        而按目录列出来既不删也不改任何东西，用户还能接着用。
        """
        return [Account(path.name, f'账号 {index}', '')
                for index, path in enumerate(
                    [child for child in sorted(self.root.iterdir(), key=lambda item: item.name)
                     if child.is_dir() and self._looks_like_profile(child)], start=1)]

    def _migrate_legacy(self):
        """把老版本的单账号目录搬进账号目录：整体改名，失败就显式报错，绝不半迁移。"""
        legacy = legacy_dorm_root(self.root)
        moved = None
        if legacy.is_dir() and not self._account_dirs():
            moved = self.root / uuid.uuid4().hex[:12]
            try:
                os.replace(legacy, moved)
            except OSError as exc:
                raise AccountError(
                    f'原来的打卡数据目录无法搬迁：{legacy} → {moved}（{exc}）。'
                    '请关闭可能占用该目录的程序后重启本程序；数据没有被删除。') from None
        # 老版本把统一认证凭据放在 accounts 之外的 idm 目录。只有能确定是谁的
        # （刚搬过来的那个账号，或者全机只有一个账号）才搬，否则宁可留着不动。
        legacy_idm = legacy_idm_root(self.root)
        if not legacy_idm.is_dir():
            return
        holders = [moved] if moved is not None else self._account_dirs()
        if len(holders) != 1 or (holders[0] / IDM_DIRNAME).exists():
            return
        try:
            os.replace(legacy_idm, holders[0] / IDM_DIRNAME)
        except OSError:
            pass  # 凭据没搬成功只是要重新保存一次，不影响打卡数据；下次启动还会再试

    # ---- 读 ----------------------------------------------------------------
    def _load(self):
        try:
            raw = self.path.read_text(encoding='utf-8-sig')      # 容忍记事本写的 BOM
        except FileNotFoundError:
            if not self._ready:
                return {'accounts': [], 'active': ''}            # ensure() 的第一步
            # 运行中被删/被移走：说清楚是什么事，别让上层报成「找不到这个账号」。
            raise AccountError(
                f'账号列表文件不存在：{self.path}。它可能被移动或删除；'
                '重启程序会按账号目录重新建立列表，各账号的数据目录不受影响。') from None
        except OSError as exc:
            raise AccountError(f'账号列表无法读取：{self.path}（{exc}）。') from None
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise AccountError(
                f'账号列表文件已损坏：{self.path}（{exc}）。请备份并删除它后重启程序，'
                '程序会按账号目录重新建立列表；各账号的数据目录不会被删除。') from None
        if not isinstance(value, dict) or value.get('v') != REGISTRY_VERSION:
            raise AccountError(
                f'账号列表文件版本不符：{self.path}。请备份并删除它后重启程序，'
                '程序会按账号目录重新建立列表。')
        records = value.get('accounts')
        if not isinstance(records, list) or not records:
            raise AccountError(
                f'账号列表里没有任何账号：{self.path}。请删除该文件后重启程序，'
                '程序会按账号目录重新建立列表。')
        accounts = []
        for index, item in enumerate(records, start=1):
            if not isinstance(item, dict):
                raise AccountError(f'账号列表内容无效：{self.path}。')
            account_id = item.get('id')
            if not isinstance(account_id, str) or not ACCOUNT_ID_PATTERN.fullmatch(account_id):
                raise AccountError(f'账号列表里的账号标识无效：{self.path}。')
            if any(account.id == account_id for account in accounts):
                raise AccountError(f'账号列表里有重复的账号：{self.path}。')
            accounts.append(Account(account_id,
                                    dorm_points.clean_name(item.get('name')) or f'账号 {index}',
                                    str(item.get('created_at') or '')))
        active = value.get('active')
        if active not in {account.id for account in accounts}:
            # 指向已删除的账号：退回第一个，不报错 —— 这不是数据损坏，只是清单过期。
            active = accounts[0].id
        return {'accounts': accounts, 'active': active}

    def accounts(self):
        return self._load()['accounts']

    def active_id(self):
        return self._load()['active']

    def find(self, account_id):
        for account in self.accounts():
            if account.id == account_id:
                return account
        raise AccountError('找不到这个账号，请刷新后重试。')

    def profile(self, account_id):
        if not ACCOUNT_ID_PATTERN.fullmatch(str(account_id or '')):
            raise AccountError('账号标识无效，请刷新后重试。')
        return self.root / account_id

    def exists(self, account_id):
        return self.profile(account_id).is_dir()

    # ---- 写 ----------------------------------------------------------------
    def add(self, name=''):
        state = self._load()
        if len(state['accounts']) >= MAX_ACCOUNTS:
            raise AccountError(f'最多 {MAX_ACCOUNTS} 个账号，请先删除一个再新建。')
        record = self._create_profile(
            dorm_points.clean_name(name) or _next_name(state['accounts']))
        self._write(record.id, [dataclasses.asdict(account) for account in state['accounts']]
                    + [dataclasses.asdict(record)])
        return record

    def rename(self, account_id, name):
        state = self._load()
        account = self.find(account_id)
        clean = dorm_points.clean_name(name)
        if not clean:
            raise AccountError(f'账号名称不能为空，最多 {MAX_NAME_LENGTH} 个字。')
        if any(other.name == clean and other.id != account_id for other in state['accounts']):
            raise AccountError('已有同名账号，请换一个名称。')
        records = [dataclasses.asdict(
            dataclasses.replace(other, name=clean) if other.id == account_id else other)
            for other in state['accounts']]
        self._write(state['active'], records)
        return clean

    def activate(self, account_id):
        state = self._load()
        account = self.find(account_id)
        if not self.exists(account_id):
            raise AccountError(
                f'账号「{account.name}」的本机档案目录不存在，无法切换；'
                '请恢复该目录，或删除这个账号后新建。')
        self._write(account.id, [dataclasses.asdict(item) for item in state['accounts']])
        return account

    def remove(self, account_id):
        """只从清单里移除；档案目录由调用方决定是否删除，便于测试与恢复。"""
        state = self._load()
        account = self.find(account_id)
        remaining = [item for item in state['accounts'] if item.id != account_id]
        if not remaining:
            raise AccountError('至少保留一个账号。')
        active = remaining[0].id if state['active'] == account_id else state['active']
        self._write(active, [dataclasses.asdict(item) for item in remaining])
        return account

    def _write(self, active, records):
        payload = {'v': REGISTRY_VERSION, 'active': active, 'accounts': records}
        try:
            atomic_write_bytes(self.path, json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        except OSError as exc:
            raise AccountError(f'账号列表无法写入：{self.path}（{exc}）。') from None


class DormAccounts:
    """账号控制器池：**每个账号一个** DormController，只有活动账号是「界面正在看的那个」。

    第二阶段与第一阶段的区别就在这里：第一阶段切换账号是「交接」（把唯一活着的控制器
    关掉再建一个），所以切换必须等打卡结束；现在每个账号都有自己的控制器、自己的引擎、
    自己的忙状态和事件队列，切换只是换个显示对象，后台账号继续跑自己的时段。

    跨账号的三道约束（其余都留在各自的控制器里，互不干扰）：
      * 错峰   —— 每个账号的第一拍按清单顺序错开 STAGGER_SECONDS，见 DormController._stagger_ready；
      * 登录闸 —— dorm_login.LoginGate：一台机器同时只跑一条学校登录链路；
      * 总预算 —— MachineLoginBudget：整机每天自动拉起的浏览器总数有上限。
    """

    def __init__(self, root: Path | None = None, controller_factory=None, login_gate=None,
                 login_budget=None, stagger_seconds=STAGGER_SECONDS):
        self.root = Path(root) if root is not None else default_root()
        self._factory = controller_factory or self._build_controller
        self._registry = None
        self._error = ''
        self._controllers = {}
        self._idm_stores = {}
        self._closed = False
        self.stagger_seconds = max(0, int(stagger_seconds))
        from dorm_login import LOGIN_GATE  # noqa: PLC0415 - 默认闸就是进程级单例

        self.login_gate = login_gate if login_gate is not None else LOGIN_GATE
        self.login_budget = (login_budget if login_budget is not None
                             else MachineLoginBudget(self.root / MACHINE_LOGIN_LOG))
        # 池闸：建/删控制器（可能关掉正在跑的控制器）与轮询之间互斥。
        self._gate = threading.Lock()
        try:
            self._registry = Registry(self.root)
            self._registry.ensure()
        except AccountError as exc:
            # 构造不抛：界面必须先能打开，并且把「档案读不出来」明明白白显示出来，
            # 而不是让整个程序起不来。此后每个操作都带着这条消息失败（fail-closed），
            # 绝不退回某个隐式的默认账号 —— 那才是真正的静默降级。
            self._error = str(exc)
        except OSError as exc:
            # 文件系统问题在 ensure() 里已经转成 AccountError；这里是最后一道兜底。
            self._error = f'账号档案目录无法读取：{self.root}（{exc}）。'

    # ---- 状态 ---------------------------------------------------------------
    @property
    def error(self) -> str:
        return self._error

    def registry(self) -> Registry:
        if self._registry is None:
            raise AccountError(self._error or '账号列表不可用。')
        return self._registry

    def _accounts(self):
        return self.registry().accounts()

    def _stagger_for(self, account_id, accounts=None) -> int:
        """按清单顺序分配错峰偏移：0、90、180… 秒。顺序是持久化的，所以重启也稳定。"""
        accounts = accounts if accounts is not None else self._accounts()
        for index, account in enumerate(accounts):
            if account.id == account_id:
                return index * self.stagger_seconds
        return 0

    def _controller_for(self, account, accounts=None):
        """取（必要时建立）这个账号的控制器。档案目录不在就抛，调用方决定怎么呈现。"""
        if self._closed:
            raise AccountError('程序正在退出，已停止打卡操作。')
        controller = self._controllers.get(account.id)
        if controller is not None:
            return controller
        registry = self.registry()
        if not registry.exists(account.id):
            raise AccountError(
                f'账号「{account.name}」的本机档案目录不存在：打卡设置、打卡点和登录态都无法读取。'
                '请恢复该目录，或删除这个账号后新建。')
        controller = self._factory(registry.profile(account.id),
                                   stagger_seconds=self._stagger_for(account.id, accounts))
        self._controllers[account.id] = controller
        return controller

    def any_busy(self) -> bool:
        return any(getattr(controller, 'busy', False)
                   for controller in list(self._controllers.values()))

    def cancel_all(self) -> None:
        """取消所有账号正在进行的操作（界面上的「取消当前操作」在多账号下就是这个语义）。"""
        for controller in list(self._controllers.values()):
            try:
                controller.cancel()
            except Exception:  # noqa: BLE001 - 取消不能因为某个控制器异常而中断
                pass

    def rows(self):
        """排程用的账号行：[(account, controller_or_None)]，顺序与清单一致。"""
        rows = []
        for account in self._accounts():
            rows.append((account, self._controllers.get(account.id)))
        return rows

    def snapshot(self) -> dict:
        """给界面用的账号列表：每个账号的名称、是否活动、档案是否还在、以及今天的打卡状态。

        特意**不含任何路径**；每个账号的状态取自它自己的 Store（设置、当天是否已确认完成）
        与控制器最近一次结果，读不动的账号降级成一行错误，绝不让整个快照失败。
        """
        if self._registry is None:
            return {'error': self._error, 'max': MAX_ACCOUNTS, 'active': '', 'items': [], 'busy': False}
        try:
            today = now().date().isoformat()
            active = self._registry.active_id()
            items, busy = [], False
            for account, controller in self.rows():
                item = {'id': account.id, 'name': account.name, 'active': account.id == active,
                        'missing': not self._registry.exists(account.id),
                        'enabled': False, 'state': 'idle', 'message': '尚未查询今日任务',
                        'busy': False, 'signed_today': False}
                if not item['missing']:
                    try:
                        controller = self._controller_for(account)
                        settings = controller.store.settings()
                        item['enabled'] = bool(settings.enabled)
                        item['signed_today'] = (controller.store.signed_record().get('date') == today)
                        item['state'] = controller.latest.state
                        item['message'] = controller.latest.message
                        item['busy'] = bool(controller.busy)
                        busy = busy or item['busy']
                    except AccountError as exc:
                        item.update(state='error', message=str(exc))
                    except Exception:  # noqa: BLE001
                        item.update(state='error', message='这个账号的打卡设置无法读取')
                else:
                    item.update(state='error', message='这个账号的本机档案目录不存在')
                items.append(item)
        except AccountError as exc:
            return {'error': str(exc), 'max': MAX_ACCOUNTS, 'active': '', 'items': [], 'busy': False}
        return {'error': '', 'max': MAX_ACCOUNTS, 'active': active, 'items': items, 'busy': busy}

    @property
    def controller(self):
        """活动账号的控制器（界面在看的那个）。"""
        if self._closed:
            raise AccountError('程序正在退出，已停止打卡操作。')
        registry = self.registry()
        account_id = registry.active_id()
        if not registry.exists(account_id):
            # 档案目录在运行中被删掉也要拦住：继续跑只会在空目录里重建一份状态，
            # 看起来像“数据还在”，实际已经不是用户原来那份。
            account = registry.find(account_id)
            raise AccountError(
                f'账号「{account.name}」的本机档案目录不存在：打卡设置、打卡点和登录态都无法读取。'
                '请恢复该目录，或删除这个账号后新建。')
        return self._controller_for(registry.find(account_id))

    def controller_or_none(self):
        """给旧版 Tk 界面用：档案不可用时返回 None，而不是让窗口起不来。"""
        try:
            return self.controller
        except AccountError:
            return None

    def idm_store(self, account_id=None):
        """本账号的统一认证凭据存储（DPAPI 密文放在该账号自己的档案目录里）。

        绝不能退回全局目录：那会让 A 账号的静默登录用上 B 账号的学号密码。
        """
        registry = self.registry()
        account_id = account_id or registry.active_id()
        if account_id not in self._idm_stores:
            self._idm_stores[account_id] = self._make_idm_store(registry.profile(account_id))
        return self._idm_stores[account_id]

    def _make_idm_store(self, profile):
        from idm_credentials import IdmCredentialStore  # noqa: PLC0415 - 只有真要用到才 import

        return IdmCredentialStore(Path(profile) / IDM_DIRNAME)

    def _build_controller(self, profile, stagger_seconds=0):
        from dorm_checkin import Store  # noqa: PLC0415
        from dorm_panel import DormController  # noqa: PLC0415

        return DormController(Store(profile), idm_store=self._make_idm_store(profile),
                              login_gate=self.login_gate, login_budget=self.login_budget,
                              stagger_seconds=stagger_seconds)

    # ---- 操作 ---------------------------------------------------------------
    def poll(self):
        """所有账号各跑一拍，返回 [(账号名, Result)] 供界面提醒。

        每个账号的节奏由它自己的控制器决定（开关、时段、间隔、错峰），跨账号的约束是：
        同一时刻只有一条登录链路（LoginGate）、整机每天自动登录总数有上限（预算）。
        档案层或某个账号读不出来时安静跳过 —— 错误由 snapshot 呈现，不刷屏。
        """
        if self._registry is None or self._closed:
            return []
        with self._gate:
            try:
                rows = self.rows()
            except AccountError:
                return []
            events = []
            for account, controller in rows:
                if not self._registry.exists(account.id):
                    continue        # 档案目录不见了：跳过这一拍，状态由 snapshot 说明
                if controller is None:
                    try:
                        controller = self._controller_for(account)
                    except AccountError:
                        continue
                try:
                    controller.poll()
                    for result in controller.drain():
                        events.append((account.name, result))
                except AccountError:
                    continue
        return events

    def switch(self, account_id):
        """切换「界面在看哪个账号」——纯显示行为，不再交接控制器。

        第二阶段每个账号都有自己的控制器在跑，所以切换不需要等谁结束：
        正在进行的打卡留在后台继续，直到它自己结束或被「取消当前操作」取消。
        """
        with self._gate:
            registry = self.registry()
            target = registry.find(account_id)
            if account_id == registry.active_id():
                return f'当前已经在使用账号「{target.name}」'
            if not registry.exists(account_id):
                raise AccountError(
                    f'账号「{target.name}」的本机档案目录不存在，无法切换；请恢复该目录，或删除这个账号。')
            registry.activate(account_id)
        return f'已切换到账号「{target.name}」；请确认这个账号的打卡设置与打卡点'

    def add(self, name=''):
        """新建账号并切到它。不影响任何正在跑的账号，所以不设忙状态闸。"""
        with self._gate:
            account = self.registry().add(name)
        return (f'已新建账号「{account.name}」，当前正在使用它；'
                '请为它单独保存打卡设置、统一认证凭据和打卡点')

    def rename(self, account_id, name):
        return f'账号已重命名为「{self.registry().rename(account_id, name)}」'

    def remove(self, account_id):
        with self._gate:
            registry = self.registry()
            if len(registry.accounts()) <= 1:
                raise AccountError('至少保留一个账号，不能删除最后一个。')
            account = registry.find(account_id)
            controller = self._controllers.get(account_id)
            if controller is not None and getattr(controller, 'busy', False):
                # 只挡正在跑的那个账号：删掉它等于在操作进行中抽走目录。
                raise AccountError(f'账号「{account.name}」正在打卡，请等待完成或先取消，再删除。')
            registry.remove(account_id)
            self._idm_stores.pop(account_id, None)
            self._controllers.pop(account_id, None)
        if controller is not None:
            try:
                controller.close()
            except Exception:  # noqa: BLE001 - 删都删了，关不掉旧控制器不该拦住这一步
                pass
        leftover = ''
        try:
            shutil.rmtree(registry.profile(account_id))
        except OSError:
            # 清单里已经没有了，残留目录只是占空间；说清楚，不假装删干净了。
            leftover = '（档案目录未能完全删除，可能被占用；可在退出程序后手动删除）'
        return f'已删除账号「{account.name}」及其本机数据{leftover}'

    def close(self):
        with self._gate:
            self._closed = True
            controllers, self._controllers = list(self._controllers.values()), {}
        for controller in controllers:
            try:
                controller.close()
            except Exception:  # noqa: BLE001 - 退出流程不能被某个控制器拖住
                pass
