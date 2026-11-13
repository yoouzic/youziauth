"""凭证密封文件的纪律测试: 加解密环路、错误 PIN、坏文件、扫描。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import lptiyu_credential as lc

CRED = {'token': 'ABC123TOKEN', 'uid': 17980001, 'student_num': '2025001',
        'school_id': 837, 'card_id': '2025001'}


class SealTests(unittest.TestCase):
    def test_roundtrip(self):
        sealed = lc.seal_credential(CRED, '654321')
        cred = lc.open_credential(sealed, '654321')
        self.assertEqual(cred['token'], 'ABC123TOKEN')
        self.assertEqual(cred['uid'], 17980001)
        # 文件本身不含明文 token, 也不含 PIN
        text = json.dumps(sealed)
        self.assertNotIn('ABC123TOKEN', text)
        self.assertNotIn('654321', text)

    def test_wrong_pin_is_detected_fast(self):
        sealed = lc.seal_credential(CRED, '654321')
        with self.assertRaises(lc.CredentialFileError):
            lc.open_credential(sealed, '654322')

    def test_short_pin_rejected_at_seal_time(self):
        with self.assertRaises(lc.CredentialFileError):
            lc.seal_credential(CRED, '12')

    def test_garbage_file(self):
        with self.assertRaises(lc.CredentialFileError):
            lc.open_credential({'kind': 'lptiyu-credential', 'v': 1,
                                'payload': '!!!'}, '654321')
        with self.assertRaises(lc.CredentialFileError):
            lc.open_credential({'kind': 'other'}, '654321')
        with self.assertRaises(lc.CredentialFileError):
            lc.open_credential({'kind': 'lptiyu-credential', 'v': 99,
                                'payload': 'AAAA'}, '654321')

    def test_missing_fields_rejected(self):
        sealed = lc.seal_credential({'token': 'X'}, '654321')
        with self.assertRaises(lc.CredentialFileError):
            lc.open_credential(sealed, '654321')

    def test_two_seals_same_pin_differ(self):
        a = lc.seal_credential(CRED, '654321')
        b = lc.seal_credential(CRED, '654321')
        self.assertNotEqual(a['payload'], b['payload'], 'IV 每次随机, 密文应不同')


class ScanTests(unittest.TestCase):
    def test_scan_finds_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            first = directory / 'lptiyu-credential-20261113-100000.json'
            second = directory / 'lptiyu-credential-20261113-110000.json'
            first.write_text('{}', encoding='utf-8')
            second.write_text('{}', encoding='utf-8')
            os.utime(first, (1000000, 1000000))
            os.utime(second, (2000000, 2000000))
            with mock.patch.object(lc, 'candidate_dirs', lambda: [directory]):
                self.assertEqual(lc.scan_export_files(), [second, first])
                self.assertEqual(lc.find_latest_export(), second)

    def test_export_file_name_shape(self):
        name = lc.export_file_name()
        self.assertTrue(name.startswith('lptiyu-credential-'))
        self.assertTrue(name.endswith('.json'))

    def test_empty_scan_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(lc, 'candidate_dirs', lambda: [Path(tmp)]):
                self.assertEqual(lc.scan_export_files(), [])
                self.assertIsNone(lc.find_latest_export())


class KnownFolderTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows 已知文件夹解析')
    def test_redirected_known_folders_are_resolved(self):
        # 桌面/下载被重定向到别的盘时, 必须走 KnownFolder, 不能 Path.home() 硬拼
        # (2026-11-13 事故: 拼出来的 C 盘空壳目录让导入找不到文件)。
        desktop = lc.desktop_dir()
        downloads = lc.downloads_dir()
        self.assertTrue(desktop.is_absolute())
        self.assertEqual(downloads.name.lower(), 'downloads')
        self.assertTrue(desktop.is_dir(), '解析出的桌面目录应真实存在')
        for directory in lc.candidate_dirs():
            self.assertTrue(directory.is_dir())


if __name__ == '__main__':
    unittest.main()
