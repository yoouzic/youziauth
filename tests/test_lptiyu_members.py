"""乐跑成员登记表的纪律测试: 迁移、收养、增改删切换、损坏 fail-closed。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lptiyu_members import (MAX_MEMBERS, REGISTRY_VERSION, MemberError,
                            MemberRegistry)


def write_legacy_layout(root: Path, token='LEGACY-TOKEN'):
    """摆一个单槽位时代的成员目录: 与旧版 Store 的落盘布局一致。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / 'identity.json').write_text(json.dumps(
        {'name': '旧成员', 'student_num': '222025321102104', 'uid': '17987537',
         'school_id': '837', 'card_id': '222025321102104'}), encoding='utf-8')
    session = root / 'session'
    session.mkdir(exist_ok=True)
    (session / 'credential.dat').write_text('ZW5jcnlwdGVk', encoding='utf-8')
    (root / 'status.json').write_text('{"state": "synced"}', encoding='utf-8')
    (root / 'renewal.json').write_text('{"state": "ok"}', encoding='utf-8')
    (root / 'history.log').write_text('2026-11-11T01:00:00 [synced] 旧历史\n',
                                      encoding='utf-8')
    return root


class RegistryTests(unittest.TestCase):
    def build(self, root=None, legacy_root=None):
        directory = root or Path(tempfile.mkdtemp())
        return MemberRegistry(directory, legacy_root=legacy_root)

    def test_fresh_root_starts_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = self.build(Path(tmp) / 'members')
            registry.ensure()
            self.assertEqual(registry.members(), [])
            self.assertEqual(registry.active_id(), '')

    def test_legacy_layout_migrates_into_member_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = write_legacy_layout(Path(tmp) / 'lptiyu')
            registry = MemberRegistry(store_root / 'members', legacy_root=store_root)
            registry.ensure()
            members = registry.members()
            self.assertEqual(len(members), 1)
            self.assertEqual(members[0].name, '旧成员',
                             '迁移时用身份里的真名, 界面不用先看到「成员 1」')
            self.assertEqual(registry.active_id(), members[0].id)
            profile = registry.member_dir(members[0].id)
            # 文件整体搬走: 身份/凭证/台账进了成员目录, 旧根不再残留
            self.assertTrue((profile / 'identity.json').is_file())
            self.assertTrue((profile / 'session' / 'credential.dat').is_file())
            self.assertTrue((profile / 'renewal.json').is_file())
            self.assertTrue((profile / 'history.log').is_file())
            for name in ('identity.json', 'status.json', 'renewal.json', 'history.log'):
                self.assertFalse((store_root / name).exists(), name)
            self.assertFalse((store_root / 'session').exists())
            self.assertEqual(
                json.loads((profile / 'identity.json').read_text(encoding='utf-8'))['uid'],
                '17987537')

    def test_orphan_profile_dirs_are_adopted_after_registry_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = self.build(Path(tmp))
            first = registry.add('张三')
            # 模拟登记表丢失: 只剩成员目录
            registry.path.unlink()
            rebuilt = self.build(Path(tmp))
            rebuilt.ensure()
            rows = rebuilt.members()
            self.assertEqual([member.id for member in rows], [first.id])
            self.assertEqual(rows[0].name, '成员 1', '收养的目录拿回自动命名')

    def test_corrupt_registry_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = self.build(Path(tmp))
            registry.ensure()
            registry.path.write_text('{not json', encoding='utf-8')
            with self.assertRaises(MemberError):
                registry.members()

    def test_wrong_version_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = self.build(Path(tmp))
            registry.ensure()
            registry.path.write_text(json.dumps(
                {'v': REGISTRY_VERSION + 99, 'active': '', 'members': []}),
                encoding='utf-8')
            with self.assertRaises(MemberError):
                registry.members()

    def test_member_id_may_not_escape_the_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = self.build(Path(tmp))
            with self.assertRaises(MemberError):
                registry.member_dir('../escape')

    def test_add_activates_when_empty_and_respects_the_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = self.build(Path(tmp))
            first = registry.add()
            self.assertEqual(registry.active_id(), first.id, '空库的第一个成员直接成为活动成员')
            for _ in range(MAX_MEMBERS - 1):
                registry.add()
            with self.assertRaises(MemberError):
                registry.add()

    def test_rename_rejects_empty_and_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = self.build(Path(tmp))
            first = registry.add('张三')
            second = registry.add('李四')
            with self.assertRaises(MemberError):
                registry.rename(first.id, '  ')
            with self.assertRaises(MemberError):
                registry.rename(first.id, '李四')
            renamed = registry.rename(first.id, '  王五  ')
            self.assertEqual(renamed.name, '王五')
            _active, rows = registry._load()
            self.assertEqual([member.name for member in rows], ['王五', '李四'])
            self.assertIsNotNone(second)

    def test_remove_falls_active_to_first_remaining_and_allows_emptying(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = self.build(Path(tmp))
            first = registry.add('张三')
            second = registry.add('李四')
            registry.remove(first.id)
            self.assertEqual(registry.active_id(), second.id)
            registry.remove(second.id)
            self.assertEqual(registry.members(), [])
            self.assertEqual(registry.active_id(), '')

    def test_activate_refuses_missing_profile_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = self.build(Path(tmp))
            member = registry.add('张三')
            import shutil
            shutil.rmtree(registry.member_dir(member.id))
            with self.assertRaises(MemberError):
                registry.activate(member.id)


if __name__ == '__main__':
    unittest.main()
