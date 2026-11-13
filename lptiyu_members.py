"""乐跑成员登记表: 一个成员一份独立的本机数据目录。

与寝室打卡(dorm_accounts.py)同一套纪律, 但按乐跑的形态裁剪:

  * 数据层本来就挂在 Store.root 下, 所以「多成员」= 把成员级的文件
    (identity/凭证/状态/台账)从「一台机器一份」改成「一个成员一份」,
    其余代码原地复用 —— 成员级方法路由到 members/<id>/ 即可。
  * settings / zones / samples / capture 是本机共享的知识与组件, 留在根目录。
  * 成员可以为空(删光 = 「退出账号」的语义), 这与 dorm 的「最后一个不能删」
    不同: 乐跑的成员目录里没有共享设置, 空库只是「还没有凭证」的常态。
  * 损坏的登记表显式报错, 绝不静默重建出一份空表(那会让用户以为成员没了);
    删掉 members.json 是受支持的修复 —— 下次启动按目录名收养幸存成员。

遗留迁移(单槽位 → 多成员): 老版本把成员级文件放在 lptiyu 根目录。首次运行
新版本时把它们整体挪进 members/<新id>/, 登记为「成员 1」并激活。先挪文件、
最后写登记表(登记表是提交点), 中途断电由「收养孤儿目录 + 挪剩余文件」兜底。
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import re
import uuid
from pathlib import Path

from windows_credentials import atomic_write_bytes

MEMBERS_DIRNAME = 'members'
REGISTRY_NAME = 'members.json'
REGISTRY_VERSION = 1
# 每个成员都是一条独立的采集/提交链路, 数量无界增长只会放大风控与误操作面。
MAX_MEMBERS = 5
MAX_NAME_LENGTH = 24
# 成员目录名即路径分量, 必须限定字符集挡住手改文件里的路径穿越。
MEMBER_ID_PATTERN = re.compile(r'[0-9a-f]{12}')

# 单槽位时代的成员级文件; 迁移时逐个挪进成员目录。
LEGACY_MEMBER_FILES = ('identity.json', 'status.json', 'history.log',
                       'daily.json', 'renewal.json', 'sync.json', 'signed.json')
LEGACY_SESSION_DIRNAME = 'session'
# 目录里只要有其中一样, 就认得这是一个成员档案(收养孤儿目录用)。
MEMBER_MARKERS = ('identity.json', 'status.json', 'daily.json', 'renewal.json',
                  'history.log', 'sync.json', 'signed.json', LEGACY_SESSION_DIRNAME)


def now():
    return dt.datetime.now()


class MemberError(RuntimeError):
    """给界面看的一句话; 登记表损坏时带修复指引。"""


@dataclasses.dataclass(frozen=True)
class Member:
    id: str
    name: str
    created_at: str = ''


def _clean_name(name, fallback):
    """清洗用户输入的名字: 空白折叠、限长; 清不出来的用自动命名。"""
    text = ' '.join(str(name or '').split())
    if not text or len(text) > MAX_NAME_LENGTH:
        return fallback
    return text


def _next_name(members):
    used = {member.name for member in members}
    for index in range(1, len(members) + 2):
        candidate = f'成员 {index}'
        if candidate not in used:
            return candidate
    return f'成员 {len(members) + 1}'


class MemberRegistry:
    """members.json 的读写。每次访问都重读磁盘, 与 dorm_accounts.Registry 一致。

    root 是成员容器目录(`<lptiyu>/members/`), 登记表与成员目录都挂它下面;
    legacy_root 是单槽位时代成员级文件所在的旧根(默认容器的父目录),
    迁移只从那里搬文件。
    """

    def __init__(self, root: Path | None = None, legacy_root: Path | None = None):
        self.root = Path(root)
        self.legacy_root = Path(legacy_root) if legacy_root is not None else self.root.parent
        self.path = self.root / REGISTRY_NAME

    # ------------------------------------------------------------- 生命周期
    def ensure(self):
        """首次访问: 迁移遗留单槽位 → 收养孤儿目录 → 落一份登记表。"""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MemberError(f'成员目录建不起来: {exc}') from exc
        if self.path.is_file():
            self._load()                       # 损坏在这里显式暴露
            return
        # 没有登记表: 先收养(可能上次迁移/删除登记表留下的), 再迁移遗留文件,
        # 最后写登记表 —— 登记表是提交点, 写之前的一切都可被这一套兜底重来。
        adopted = self._discover()
        migrated = self._migrate_legacy(adopted)
        self._write(migrated.active, migrated.members)

    # ------------------------------------------------------------- 读取
    def _load(self):
        try:
            text = self.path.read_text(encoding='utf-8-sig')
        except FileNotFoundError:
            raise MemberError('成员登记表不存在，请重启一次让它重建') from None
        except OSError as exc:
            raise MemberError(f'成员登记表读不出来: {exc}') from exc
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise MemberError(
                '成员登记表损坏了。'
                '若确认要清空成员，可关闭程序后删除该文件再启动：' + str(self.path)
            ) from exc
        if not isinstance(data, dict) or data.get('v') != REGISTRY_VERSION:
            raise MemberError('成员登记表版本不匹配，请升级或删除后重启：' + str(self.path))
        records = data.get('members')
        active = str(data.get('active') or '')
        if not isinstance(records, list):
            raise MemberError('成员登记表内容无效，请删除后重启：' + str(self.path))
        members, seen = [], set()
        for record in records:
            if not isinstance(record, dict):
                raise MemberError('成员登记表里有坏行，请删除后重启：' + str(self.path))
            member_id = str(record.get('id') or '')
            if not MEMBER_ID_PATTERN.fullmatch(member_id):
                raise MemberError(f'成员 id 不合法({member_id!r})，请删除后重启')
            if member_id in seen:
                raise MemberError(f'成员 id 重复({member_id})，请删除后重启')
            seen.add(member_id)
            name = str(record.get('name') or '').strip() or _next_name(members)
            members.append(Member(id=member_id, name=name,
                                  created_at=str(record.get('created_at') or '')))
        if active and active not in seen:
            active = members[0].id if members else ''
        return active, members

    def members(self):
        self.ensure()
        _active, rows = self._load()
        return rows

    def active_id(self):
        self.ensure()
        active, _rows = self._load()
        return active

    def find(self, member_id):
        member_id = str(member_id or '')
        for member in self.members():
            if member.id == member_id:
                return member
        return None

    def member_dir(self, member_id):
        member_id = str(member_id or '')
        if not MEMBER_ID_PATTERN.fullmatch(member_id):
            raise MemberError(f'成员 id 不合法({member_id!r})')
        return self.root / member_id

    def exists(self, member_id):
        return self.find(member_id) is not None

    # ------------------------------------------------------------- 变更
    def add(self, name=''):
        self.ensure()
        active, rows = self._load()
        if len(rows) >= MAX_MEMBERS:
            raise MemberError(f'最多保存 {MAX_MEMBERS} 个成员，先删掉一个再添加')
        member_id = uuid.uuid4().hex[:12]
        fallback = _next_name(rows)
        member = Member(id=member_id, name=_clean_name(name, fallback),
                        created_at=now().isoformat(timespec='seconds'))
        try:
            self.member_dir(member_id).mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise MemberError(f'成员目录建不起来: {exc}') from exc
        self._write(active or member_id, rows + [member])
        return member

    def rename(self, member_id, name):
        self.ensure()
        active, rows = self._load()
        member = self.find(member_id)
        if member is None:
            raise MemberError('没有这个成员')
        others = [row for row in rows if row.id != member.id]
        fallback = _next_name(others)
        cleaned = _clean_name(name, '')
        if not cleaned:
            raise MemberError('成员名字不能为空')
        if any(row.name == cleaned for row in others):
            raise MemberError(f'已经有叫「{cleaned}」的成员了')
        renamed = Member(id=member.id, name=cleaned, created_at=member.created_at)
        self._write(active, [renamed if row.id == member.id else row for row in rows])
        return renamed

    def activate(self, member_id):
        self.ensure()
        active, rows = self._load()
        member = self.find(member_id)
        if member is None:
            raise MemberError('没有这个成员')
        if not self.member_dir(member.id).is_dir():
            raise MemberError(f'成员「{member.name}」的数据目录不见了')
        if member.id != active:
            self._write(member.id, rows)
        return member

    def remove(self, member_id):
        """从登记表摘掉一个成员; 允许删空(目录由调用方负责删)。"""
        self.ensure()
        active, rows = self._load()
        member = self.find(member_id)
        if member is None:
            raise MemberError('没有这个成员')
        rest = [row for row in rows if row.id != member.id]
        next_active = member.id if member.id != active else (rest[0].id if rest else '')
        self._write(next_active, rest)
        return member

    # ------------------------------------------------------------- 落盘
    def _write(self, active, members):
        payload = {'v': REGISTRY_VERSION, 'active': str(active or ''),
                   'members': [{'id': member.id, 'name': member.name,
                                'created_at': member.created_at}
                               for member in members]}
        try:
            atomic_write_bytes(self.path, json.dumps(
                payload, ensure_ascii=False).encode('utf-8'))
        except OSError as exc:
            raise MemberError(f'成员登记表写不进去: {exc}') from exc

    # ------------------------------------------------------------- 迁移/收养
    def _account_dirs(self):
        return sorted(path for path in self.root.iterdir()
                      if path.is_dir() and MEMBER_ID_PATTERN.fullmatch(path.name))

    @staticmethod
    def _looks_like_profile(path: Path):
        # 与 dorm_accounts 一致: id 形状的目录名本身就认, 标记文件只是补充凭据。
        return bool(MEMBER_ID_PATTERN.fullmatch(path.name)) or \
            any((path / marker).exists() for marker in MEMBER_MARKERS)

    def _discover(self):
        """收养没有登记在册的成员目录(登记表被删/迁移中断后的自救)。"""
        rows = []
        for path in self._account_dirs():
            if not self._looks_like_profile(path):
                continue
            rows.append(Member(id=path.name, name=_next_name(rows),
                               created_at=''))
        return rows

    def _migrate_legacy(self, adopted):
        """把单槽位时代的成员级文件挪进一个成员目录; 返回迁移/收养后的成员表。"""
        rows = list(adopted)
        if rows:
            # 有收养对象: 根目录残留的遗留文件并给第一个成员(它缺什么补什么)。
            self._move_legacy_files_into(self.member_dir(rows[0].id))
            return _MemberTable(rows[0].id if rows else '', rows)
        legacy = [name for name in LEGACY_MEMBER_FILES
                  if (self.legacy_root / name).exists()]
        if not legacy and not (self.legacy_root / LEGACY_SESSION_DIRNAME).is_dir():
            return _MemberTable('', [])
        member_id = uuid.uuid4().hex[:12]
        target = self.member_dir(member_id)
        try:
            target.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise MemberError(f'成员目录建不起来: {exc}') from exc
        self._move_legacy_files_into(target)
        # 身份文件里若有真名, 迁移完直接用它, 界面不用先看到「成员 1」
        name = '成员 1'
        try:
            identity = json.loads((target / 'identity.json').read_text(encoding='utf-8-sig'))
            name = _clean_name(identity.get('name'), '成员 1')
        except (OSError, ValueError):
            pass
        rows.append(Member(id=member_id, name=name,
                           created_at=now().isoformat(timespec='seconds')))
        return _MemberTable(member_id, rows)

    def _move_legacy_files_into(self, target: Path):
        moved = False
        for name in LEGACY_MEMBER_FILES:
            source = self.legacy_root / name
            if not source.exists():
                continue
            destination = target / name
            if destination.exists():
                # 收养分支才会走到这: 目录里已经有同名文件, 保目录里的新数据。
                source.unlink()
                moved = True
                continue
            os.replace(source, destination)
            moved = True
        legacy_session = self.legacy_root / LEGACY_SESSION_DIRNAME
        if legacy_session.is_dir():
            destination = target / LEGACY_SESSION_DIRNAME
            if destination.exists():
                for stale in legacy_session.iterdir():
                    stale.unlink()
                legacy_session.rmdir()
            else:
                os.replace(legacy_session, destination)
            moved = True
        return moved


class _MemberTable:
    """迁移中间态: (active, members) 的轻量持有者。"""

    def __init__(self, active, members):
        self.active = active
        self.members = members
