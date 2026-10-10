import hashlib
import importlib.util
import io
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError


ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://github.com/yoouzic/youziauth/releases/download/v1.5.0/'
PACKAGE = b'fictional MSI bytes for offline tests'
DIGEST = hashlib.sha256(PACKAGE).hexdigest()
# Any 64-byte hex string: app_update only transports the signature, the Ed25519
# check itself is exercised in tests/test_windows_update.py.
SIGNATURE = 'ab' * 64


def release(version='1.5.0', notes=None):
    """A release payload. ``notes`` attaches the optional hand-written notes asset."""
    base = f'https://github.com/yoouzic/youziauth/releases/download/v{version}/'
    assets = [{'name': 'youziauth.msi', 'browser_download_url': base + 'youziauth.msi',
               'size': len(PACKAGE), 'state': 'uploaded'},
              {'name': 'SHA256SUMS.txt', 'browser_download_url': base + 'SHA256SUMS.txt',
               'size': 80, 'state': 'uploaded'},
              {'name': 'youziauth.msi.ed25519',
               'browser_download_url': base + 'youziauth.msi.ed25519',
               'size': 129, 'state': 'uploaded'}]
    if notes is not None:
        assets.append({'name': 'release-notes.md',
                       'browser_download_url': base + 'release-notes.md',
                       'size': len(notes.encode('utf-8')), 'state': 'uploaded'})
    return {'tag_name': 'v' + version, 'draft': False, 'prerelease': False,
            'html_url': f'https://github.com/yoouzic/youziauth/releases/tag/v{version}',
            'assets': assets}


def comparison(*messages, status='ahead', total=None):
    """A compare API response carrying one commit per message string."""
    commits = [{'sha': '%040x' % (index + 1), 'commit': {
        'message': message,
        'committer': {'date': f'2026-10-{index + 1:02d}T03:44:01Z'}}}
        for index, message in enumerate(messages)]
    return {'status': status, 'ahead_by': len(commits),
            'total_commits': len(commits) if total is None else total, 'commits': commits}


NOTES = """## 1.5.0

这是给用户看的中文说明。

- 校园网：代理开着时不再误报「能上网」
- **重点**：寝室打卡支持每个账号独立时段
- 定位来源切换后立刻对自动打卡生效
- 校园网：代理开着时不再误报「能上网」

尾注不会显示。
"""


class Response(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.status = 200


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('app_update'), 'The updater module is missing')
        import app_update
        self.update = app_update
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.controller = app_update.UpdateController('1.4.4', Path(self.tmp.name), Path('youziauth.exe'))
        self.addCleanup(self.controller.close)
        self.requests = []
        self.data = release()
        # 更新内容默认有一条：没写说明时会顺着 compare 端点取它。
        self.commits = comparison('fix(update): show what changed in this update')
        self.notes = NOTES.encode('utf-8')

    def open(self, url):
        self.requests.append(url)
        if url == self.update.LATEST_RELEASE_URL:
            return Response(json.dumps(self.data).encode())
        if url == self.update.compare_url('1.4.4', '1.5.0'):
            return Response(json.dumps(self.commits).encode())
        if url == BASE + 'SHA256SUMS.txt':
            return Response((DIGEST.upper() + '  youziauth.msi\n').encode())
        if url == BASE + 'youziauth.msi.ed25519':
            return Response((SIGNATURE.upper() + '\n').encode())
        if url == BASE + 'youziauth.msi':
            return Response(PACKAGE)
        if url == BASE + 'release-notes.md':
            # 说明资产在 self.data 里：挂上就回内容，没挂就当这一版没写说明。
            notes = [asset for asset in self.data.get('assets', ())
                     if asset.get('name') == self.update.NOTES_ASSET]
            if notes:
                return Response(self.notes)
            raise HTTPError(url, 404, 'Not found', {}, None)
        raise AssertionError('Unexpected URL: ' + url)

    def check(self):
        self.controller.check()
        self.controller._worker.join(3)
        self.assertFalse(self.controller._worker.is_alive())
        return self.controller.snapshot()

    def test_new_release_downloads_and_becomes_ready_only_after_verification(self):
        observations = []
        def verify(path, executable, version, digest, signature):
            observations.append((path.read_bytes(), version, digest, signature,
                                 self.controller.snapshot()['state']))
        with patch('app_update.open_url', side_effect=self.open), patch('app_update.verify_msi', side_effect=verify):
            state = self.check()
        self.assertEqual(state['state'], 'ready')
        self.assertEqual(state['latest_version'], '1.5.0')
        self.assertEqual(state['progress'], 100)
        self.assertFalse(state['busy'])
        self.assertEqual(observations,
                         [(PACKAGE, '1.5.0', DIGEST, SIGNATURE, 'verifying')])
        self.assertEqual(list(Path(self.tmp.name).glob('*.msi'))[0].read_bytes(), PACKAGE)
        self.assertNotIn('path', state)

    def test_older_and_equal_releases_never_download_or_require_new_assets(self):
        for version in ('1.1.3', '1.4.4'):
            self.requests.clear()
            self.data = release(version)
            self.data['assets'] = []
            with patch('app_update.open_url', side_effect=self.open):
                state = self.check()
            self.assertEqual(state['state'], 'up_to_date')
            self.assertEqual(len(self.requests), 1)
            self.assertFalse(list(Path(self.tmp.name).iterdir()))

    def test_versions_are_compared_numerically(self):
        self.assertGreater(self.update.version_tuple('1.10.0'), self.update.version_tuple('1.9.9'))
        for value in ('../1.5.0', '1.5.0-rc.1', 'v1.5.0', '1.05.0', '1.5', '1.5.0\n', '256.0.0'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.update.version_tuple(value)

    def test_drafts_and_prereleases_are_ignored(self):
        for flag in ('draft', 'prerelease'):
            self.data = release()
            self.data[flag] = True
            with patch('app_update.open_url', side_effect=self.open):
                state = self.check()
            self.assertEqual(state['state'], 'up_to_date')
            self.assertEqual(state['latest_version'], '')

    def test_invalid_release_metadata_never_downloads(self):
        variants = [None, [], {}, dict(release(), tag_name='v1.5.0-rc.1'),
                    dict(release(), draft=1), dict(release(), prerelease='false')]
        for field, value in (('assets', []), ('assets', [{}])):
            variants.append(dict(release(), **{field: value}))
        for data in variants:
            self.data = data
            self.requests.clear()
            with self.subTest(data=data), patch('app_update.open_url', side_effect=self.open):
                self.assertEqual(self.check()['state'], 'error')
                self.assertEqual(len(self.requests), 1)

    def test_asset_sources_and_sizes_are_validated_before_download(self):
        for key, value in (('browser_download_url', 'https://example.invalid/youziauth.msi'),
                           ('browser_download_url', BASE.replace('https:', 'http:') + 'youziauth.msi'),
                           ('size', 0), ('size', True), ('size', 2**40), ('state', 'new')):
            self.data = release()
            self.data['assets'][0][key] = value
            self.requests.clear()
            with self.subTest(key=key, value=value), patch('app_update.open_url', side_effect=self.open):
                self.assertEqual(self.check()['state'], 'error')
                self.assertEqual(len(self.requests), 1)

    def test_a_renamed_github_account_does_not_break_installed_clients(self):
        # 2026-10-04 实测：账号 Cyzmmd → yoouzic 之后，已装客户端内置的是旧名字，
        # 而 API 返回的是新地址 —— 必须认 API 报告的仓库，否则所有人再也更新不了。
        renamed = release()
        renamed['html_url'] = 'https://github.com/yoouzic/youziauth/releases/tag/v1.5.0'
        with patch.object(self.update, 'REPOSITORY', 'Cyzmmd/youziauth'):
            assets = self.update.release_assets(renamed, '1.5.0')
        self.assertEqual(set(assets), set(self.update.RELEASE_ASSETS))

    def test_an_asset_from_another_repository_is_still_refused(self):
        foreign = release()
        foreign['html_url'] = 'https://github.com/yoouzic/youziauth/releases/tag/v1.5.0'
        foreign['assets'][0] = dict(foreign['assets'][0],
                                    browser_download_url=BASE.replace('yoouzic', 'attacker') + 'youziauth.msi')
        with self.assertRaises(RuntimeError):
            self.update.release_assets(foreign, '1.5.0')

    def test_duplicate_assets_and_checksums_are_rejected(self):
        self.data['assets'].append(self.data['assets'][0].copy())
        with patch('app_update.open_url', side_effect=self.open):
            self.assertEqual(self.check()['state'], 'error')
        self.data = release()
        original = self.open
        def duplicated(url):
            if url.endswith('SHA256SUMS.txt'):
                return Response(((DIGEST + '  youziauth.msi\n') * 2).encode())
            return original(url)
        with patch('app_update.open_url', side_effect=duplicated):
            self.assertEqual(self.check()['state'], 'error')
        self.assertFalse(list(Path(self.tmp.name).glob('*.msi')))

    def test_corruption_truncation_and_oversized_downloads_never_become_ready(self):
        original = self.open
        for contents in (b'x' * len(PACKAGE), PACKAGE[:-1], PACKAGE + b'extra'):
            def corrupted(url):
                return Response(contents) if url.endswith('youziauth.msi') else original(url)
            with self.subTest(contents=contents), patch('app_update.open_url', side_effect=corrupted):
                state = self.check()
            self.assertEqual(state['state'], 'error')
            self.assertFalse(list(Path(self.tmp.name).iterdir()))

    def test_signature_rejection_is_visible_and_leaves_no_installable_package(self):
        with patch('app_update.open_url', side_effect=self.open), patch('app_update.verify_msi', side_effect=RuntimeError('安装包签名无效')):
            state = self.check()
        self.assertEqual(state['state'], 'error')
        self.assertIn('签名', state['message'])
        self.assertFalse(list(Path(self.tmp.name).iterdir()))
        with self.assertRaises(RuntimeError):
            self.controller.install(True, '1.5.0')

    def test_hand_written_chinese_notes_win_over_the_commit_list(self):
        self.data = release(notes=NOTES)
        with patch('app_update.open_url', side_effect=self.open), patch('app_update.verify_msi'):
            changes = self.check()['changes']
        self.assertEqual([entry['subject'] for entry in changes['entries']], [
            '校园网：代理开着时不再误报「能上网」',
            '寝室打卡支持每个账号独立时段',
            '定位来源切换后立刻对自动打卡生效',
        ])
        self.assertEqual([entry['kind'] for entry in changes['entries']], ['', '重点', ''])
        self.assertEqual(changes['total'], 3)          # 重复的那条只算一次
        self.assertEqual(changes['note'], '这是给用户看的中文说明。')
        self.assertNotIn(self.update.compare_url('1.4.4', '1.5.0'), self.requests,
                         'Notes must win, so no commit comparison is needed')
        # 手写说明没有提交日期，界面不能凭空造一个。
        self.assertTrue(all(entry['date'] == '' for entry in changes['entries']))

    def test_broken_or_mismatched_notes_fall_back_to_the_commit_list(self):
        cases = {
            'missing heading': '- 只有条目没有版本号\n',
            'wrong version': '## 1.4.0\n\n- 上一个版本的说明\n',
            'no entries': '## 1.5.0\n\n只有一段话，没有列表。\n',
            'not utf-8': None,
            'empty': '',
        }
        for label, text in cases.items():
            self.requests.clear()
            self.tmp.cleanup()
            self.tmp = tempfile.TemporaryDirectory()
            self.addCleanup(self.tmp.cleanup)
            self.controller = self.update.UpdateController('1.4.4', Path(self.tmp.name),
                                                           Path('youziauth.exe'))
            self.addCleanup(self.controller.close)
            if label == 'not utf-8':
                raw = b'\xff\xfe\x00 not utf-8 at all'
                self.data = release(notes='x' * len(raw))
                self.data['assets'][-1]['size'] = len(raw)
            else:
                raw = text.encode('utf-8')
                self.data = release(notes=text)

            def served(url, raw=raw):
                return Response(raw) if url.endswith('release-notes.md') else self.open(url)

            with self.subTest(label=label), patch('app_update.open_url', side_effect=served), \
                    patch('app_update.verify_msi'):
                state = self.check()
            self.assertEqual(state['state'], 'ready')
            self.assertIn(self.update.compare_url('1.4.4', '1.5.0'), self.requests,
                          label + ' must fall back to the commit comparison')
            self.assertEqual([entry['kind'] for entry in state['changes']['entries']], ['fix'])

    def test_notes_version_must_match_the_release_being_installed(self):
        # 版本号对不上就不能用：宁可显示英文提交，也不能把别的版本的说明讲给用户。
        # `v` 前缀可有可无；`1.5` / `1.5.0.1` 这种写法一律不算 1.5.0。
        for heading in ('## 1.5.0', '# 1.5.0', '## v1.5.0', '## 1.5.0 '):
            with self.subTest(heading=heading):
                block = self.update.read_notes(f'{heading}\n\n- 一条说明\n'.encode('utf-8'), '1.5.0')
                self.assertEqual(block['entries'][0]['subject'], '一条说明')
        for declared in ('1.4.0', '1.10.0', '1.5', '1.5.0.1', 'v1.5', '#1.5.0', ''):
            with self.subTest(declared=declared):
                with self.assertRaises(ValueError):
                    self.update.read_notes(f'## {declared}\n\n- 一条说明\n'.encode('utf-8'), '1.5.0')

    def test_a_hand_written_note_stays_on_one_line_and_keeps_literal_marks_out(self):
        entry = '很长的说明' * 60
        block = self.update.read_notes(
            f'## 1.5.0\n\n- 用 `代码` 和 **星号** 写的说明\n- {entry}\n'.encode('utf-8'), '1.5.0')
        self.assertEqual(block['entries'][0]['subject'], '用 代码 和 星号 写的说明')
        self.assertTrue(block['entries'][1]['subject'].endswith('…'))
        self.assertLessEqual(len(block['entries'][1]['subject']), 201)
        self.assertIn('\n', NOTES)      # the fixture really does carry multiple lines
        self.assertTrue(all('\n' not in item['subject'] for item in block['entries']))

    def test_a_notes_asset_that_cannot_be_fetched_does_not_block_the_update(self):
        self.data = release(notes=NOTES)
        original = self.open

        def refused(url):
            if url.endswith('release-notes.md'):
                raise URLError('offline notes')
            return original(url)

        with patch('app_update.open_url', side_effect=refused), patch('app_update.verify_msi'):
            state = self.check()
        self.assertEqual(state['state'], 'ready')
        self.assertEqual([entry['kind'] for entry in state['changes']['entries']], ['fix'])

    def test_an_oversized_or_untrusted_notes_asset_is_ignored(self):
        # 大小或来源不对的说明资产根本不进候选，等于这一版没写说明。
        for key, value in (('size', self.update.MAX_NOTES_BYTES + 1),
                           ('size', 0), ('size', True),
                           ('state', 'new'),
                           ('browser_download_url', BASE.replace('yoouzic', 'attacker') + 'release-notes.md')):
            self.data = release(notes=NOTES)
            self.data['assets'][-1][key] = value
            with self.subTest(key=key, value=value):
                assets = self.update.release_assets(self.data, '1.5.0')
                self.assertNotIn(self.update.NOTES_ASSET, assets)
                self.assertEqual(set(self.update.RELEASE_ASSETS) - set(assets), set(),
                                 'the mandatory assets must still be accepted')
        # 说明资产是多余的：没有它，必备清单照样完整。
        self.assertEqual(set(self.update.release_assets(release(), '1.5.0')),
                         set(self.update.RELEASE_ASSETS))

    def test_changes_show_what_the_user_gains_and_drop_maintenance_noise(self):
        # 一次真实升级（v1.5.0 → v1.8.5）里有 36 个提交，其中一半是 chore/test/ci。
        self.commits = comparison(
            'feat(dorm): give every account its own profile',
            'chore: version 1.8.5',
            'fix(auth): stop reporting a stale portal session as a healthy uplink',
            'Merge pull request #12 from yoouzic/fix',
            'test(ui): cover the account picker',
            'docs: record the release audit',
            'ci: gate the front-end suite',
            'feat!: drop the legacy login page',
            'perf(network): stop re-probing every second',
            'fix(auth): stop reporting a stale portal session as a healthy uplink',
            'chore(deps): bump ruff',
            'revert: bring back the retry button',
        )
        with patch('app_update.open_url', side_effect=self.open), patch('app_update.verify_msi'):
            state = self.check()
        self.assertEqual(state['state'], 'ready')
        changes = state['changes']
        self.assertEqual([entry['subject'] for entry in changes['entries']], [
            'give every account its own profile',
            'stop reporting a stale portal session as a healthy uplink',
            'drop the legacy login page',
            'stop re-probing every second',
            'bring back the retry button',
        ])
        self.assertEqual([entry['kind'] for entry in changes['entries']],
                         ['feat', 'fix', 'feat', 'perf', 'revert'])
        self.assertEqual(changes['entries'][0]['date'], '2026-10-01')
        self.assertEqual(changes['total'], 5)     # 重复的 fix 只算一次
        self.assertFalse(changes['more'])
        self.assertEqual(changes['note'], '')
        # 快照会直接交给界面，所以只能有这几个字段，不带 sha/url/路径。
        for entry in changes['entries']:
            self.assertEqual(set(entry), {'subject', 'kind', 'date'})
        self.assertEqual(set(changes), {'entries', 'total', 'more', 'note'})

    def test_a_long_change_list_is_capped_and_flagged(self):
        self.commits = comparison(*[f'fix(auth): repair defect {index}' for index in range(200)],
                                  total=200)
        with patch('app_update.open_url', side_effect=self.open), patch('app_update.verify_msi'):
            changes = self.check()['changes']
        self.assertEqual(len(changes['entries']), self.update.MAX_CHANGES_ENTRIES)
        self.assertEqual(changes['total'], 200)
        self.assertTrue(changes['more'])

    def test_updates_without_user_facing_commits_say_so_instead_of_showing_nothing(self):
        self.commits = comparison('chore: version 1.5.0', 'test: tidy up', 'ci: cache pip')
        with patch('app_update.open_url', side_effect=self.open), patch('app_update.verify_msi'):
            state = self.check()
        self.assertEqual(state['state'], 'ready')
        self.assertEqual(state['latest_version'], '1.5.0')
        self.assertEqual(state['changes']['entries'], [])
        self.assertEqual(state['changes']['total'], 0)
        self.assertIn('内部维护', state['changes']['note'])
        # status=identical：两个标签指向同一处，说明这次没有实际改动。
        identical = self.update.read_changes(json.dumps(comparison(status='identical')).encode())
        self.assertEqual(identical['note'], '这个版本与当前已安装版本内容相同。')

    def test_every_changelog_failure_leaves_the_update_off_itself_intact(self):
        over = b'x' * (self.update.MAX_CHANGES_BYTES + 1)
        broken = {'status': 'ahead', 'total_commits': 1, 'commits': {'not': 'a list'}}
        failures = [
            URLError('offline'),                        # no network
            HTTPError(self.update.compare_url('1.4.4', '1.5.0'), 404, 'Not found', {}, None),
            HTTPError(self.update.compare_url('1.4.4', '1.5.0'), 403, 'Rate limited', {}, None),
            Response(over),                             # the size guard trips
            Response(b'not json'),
            Response(json.dumps(broken).encode()),
            Response(json.dumps({'status': 'whatever', 'total_commits': 1, 'commits': []}).encode()),
            RuntimeError('update server refused'),
        ]
        for failure in failures:
            self.data = release()
            with self.subTest(failure=str(failure)[:60]):
                original = self.open
                def degraded(url, failure=failure, original=original):
                    if url == self.update.compare_url('1.4.4', '1.5.0'):
                        if callable(failure):
                            raise failure
                        return failure
                    return original(url)
                with patch('app_update.open_url', side_effect=degraded), patch('app_update.verify_msi'):
                    state = self.check()
                self.assertEqual(state['state'], 'ready', 'the update itself must survive')
                self.assertEqual(state['latest_version'], '1.5.0')
                self.assertEqual(state['changes']['entries'], [])
                self.assertEqual(state['changes']['total'], 0)
                self.assertEqual(state['changes']['note'], self.update.NO_CHANGES)
            # 每个子用例都换个干净的缓存目录，免得上一个用例的缓存把请求吃掉。
            self.tmp.cleanup()
            self.tmp = tempfile.TemporaryDirectory()
            self.addCleanup(self.tmp.cleanup)
            self.controller = self.update.UpdateController('1.4.4', Path(self.tmp.name),
                                                           Path('youziauth.exe'))
            self.addCleanup(self.controller.close)

    def test_change_notes_are_fetched_once_and_then_served_from_cache(self):
        compare = self.update.compare_url('1.4.4', '1.5.0')
        with patch('app_update.open_url', side_effect=self.open), patch('app_update.verify_msi'):
            first = self.check()['changes']
            self.assertIn(compare, self.requests)
            self.requests.clear()
            second = self.check()['changes']
        self.assertEqual(first, second)
        self.assertNotIn(compare, self.requests, 'A cached note must not re-hit GitHub')
        # 缓存文件本身也不该带仓库地址或本机路径。
        cached = [path.name for path in Path(self.tmp.name).iterdir()]
        self.assertIn('changes-v1.4.4-v1.5.0.json', cached)
        text = (Path(self.tmp.name) / 'changes-v1.4.4-v1.5.0.json').read_text(encoding='utf-8')
        self.assertNotIn('github.com', text)
        self.assertNotIn(str(self.tmp.name), text)

    def test_a_corrupt_change_cache_is_refetched_rather_than_trusted(self):
        compare = self.update.compare_url('1.4.4', '1.5.0')
        (Path(self.tmp.name) / 'changes-v1.4.4-v1.5.0.json').write_text('{ not json', encoding='utf-8')
        with patch('app_update.open_url', side_effect=self.open), patch('app_update.verify_msi'):
            state = self.check()
        self.assertEqual(state['state'], 'ready')
        self.assertIn(compare, self.requests)
        self.assertEqual(len(state['changes']['entries']), 1)

    def test_the_front_end_cap_matches_the_snapshot_cap(self):
        # app.js 靠 BACKEND_MAX_CHANGES 判断「total 是不是可信的真实总数」，
        # 两个数字一旦漂移，界面就会报出一个编出来的条数。
        source = (ROOT / 'desktop_ui' / 'app.js').read_text(encoding='utf-8')
        match = re.search(r'^\s*const\s+BACKEND_MAX_CHANGES\s*=\s*(\d+)\s*;', source, re.MULTILINE)
        self.assertIsNotNone(match, 'app.js must declare BACKEND_MAX_CHANGES')
        self.assertEqual(int(match.group(1)), self.update.MAX_CHANGES_ENTRIES)

    def test_verified_cache_is_reused_but_revalidated(self):
        with patch('app_update.open_url', side_effect=self.open), patch('app_update.verify_msi') as verify:
            self.assertEqual(self.check()['state'], 'ready')
            self.requests.clear()
            self.assertEqual(self.check()['state'], 'ready')
        self.assertNotIn(BASE + 'youziauth.msi', self.requests)
        self.assertEqual(verify.call_count, 2)

    def test_cached_download_works_without_python_311_hashlib_api(self):
        with patch('app_update.open_url', side_effect=self.open), patch('app_update.verify_msi'):
            self.assertEqual(self.check()['state'], 'ready')
            with patch.object(self.update.hashlib, 'file_digest', None, create=True):
                self.assertEqual(self.check()['state'], 'ready')

    def test_network_errors_and_rate_limits_can_be_retried(self):
        errors = [URLError('private diagnostic'), HTTPError(self.update.LATEST_RELEASE_URL, 403, 'Forbidden', {}, None),
                  HTTPError(self.update.LATEST_RELEASE_URL, 429, 'Too many requests', {}, None)]
        for error in errors:
            with patch('app_update.open_url', side_effect=error):
                state = self.check()
            self.assertEqual(state['state'], 'error')
            self.assertNotIn('private diagnostic', state['message'])
            if isinstance(error, HTTPError):
                self.assertIn('频率', state['message'])
                self.assertTrue(error.closed, 'HTTP error responses must be closed')
        with patch('app_update.open_url', side_effect=self.open), patch('app_update.verify_msi'):
            self.assertEqual(self.check()['state'], 'ready')

    def test_no_release_has_a_clear_message(self):
        error = HTTPError(self.update.LATEST_RELEASE_URL, 404, 'Not found', {}, None)
        with patch('app_update.open_url', side_effect=error):
            state = self.check()
        self.assertEqual(state['state'], 'up_to_date')
        self.assertIn('暂无', state['message'])

    def test_metadata_limit_and_bad_checksum_stop_before_package_download(self):
        original = self.open
        for body in (b'x' * (1024 * 1024 + 1), b'not json'):
            with patch('app_update.open_url', return_value=Response(body)):
                self.assertEqual(self.check()['state'], 'error')
        for checksum in ('', '0' * 64 + '  other.msi', 'not-a-hash  youziauth.msi'):
            def bad_checksum(url):
                return Response(checksum.encode()) if url.endswith('SHA256SUMS.txt') else original(url)
            self.requests.clear()
            with patch('app_update.open_url', side_effect=bad_checksum):
                self.assertEqual(self.check()['state'], 'error')
            self.assertNotIn(BASE + 'youziauth.msi', self.requests)

    def test_check_is_nonblocking_and_duplicate_requests_are_rejected(self):
        entered, finish = threading.Event(), threading.Event()
        original = self.open
        def blocked(url):
            entered.set()
            finish.wait(3)
            return original(url)
        with patch('app_update.open_url', side_effect=blocked), patch('app_update.verify_msi'):
            self.controller.check()
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(self.controller.snapshot()['busy'])
                with self.assertRaises(RuntimeError):
                    self.controller.check()
            finally:
                finish.set()
                self.controller._worker.join(3)

    def test_closing_during_download_cleans_partial_file_and_never_offers_install(self):
        original = self.open
        controller = self.controller
        class CancelResponse(Response):
            def read(self, size=-1):
                data = super().read(size)
                controller.close()
                return data
        def cancelled(url):
            return CancelResponse(PACKAGE) if url.endswith('youziauth.msi') else original(url)
        with patch('app_update.open_url', side_effect=cancelled):
            state = self.check()
        self.assertNotEqual(state['state'], 'ready')
        self.assertFalse(list(Path(self.tmp.name).iterdir()))
        with self.assertRaises(RuntimeError):
            self.controller.check()

    def test_install_requires_confirmation_and_matching_ready_version(self):
        with self.assertRaises(RuntimeError):
            self.controller.install(True, '1.5.0')
        with patch('app_update.open_url', side_effect=self.open), patch('app_update.verify_msi'):
            self.check()
        for confirmed, version in ((False, '1.5.0'), ('true', '1.5.0'), (True, '1.6.0')):
            with self.subTest(confirmed=confirmed, version=version), self.assertRaises(RuntimeError):
                self.controller.install(confirmed, version)
        launched = []
        def install(path, executable, version, digest, on_launch, signature):
            launched.append((path.read_bytes(), version, digest, signature))
            on_launch()
            return 0
        with patch('app_update.install_msi', side_effect=install):
            self.controller.install(True, '1.5.0')
            self.controller._worker.join(3)
        self.assertEqual(launched, [(PACKAGE, '1.5.0', DIGEST, SIGNATURE)])
        self.assertEqual(self.controller.snapshot()['state'], 'launched')

    def test_installer_cancel_allows_retry_but_verification_failure_does_not(self):
        with patch('app_update.open_url', side_effect=self.open), patch('app_update.verify_msi'):
            self.check()
        with patch('app_update.install_msi', return_value=1602):
            self.controller.install(True, '1.5.0')
            self.controller._worker.join(3)
        self.assertEqual(self.controller.snapshot()['state'], 'ready')
        self.assertIn('取消', self.controller.snapshot()['message'])
        with patch('app_update.install_msi', side_effect=RuntimeError('安装包已变化，请重新下载')):
            self.controller.install(True, '1.5.0')
            self.controller._worker.join(3)
        self.assertEqual(self.controller.snapshot()['state'], 'error')

    def test_redirects_allow_github_asset_cdn_but_reject_untrusted_destinations(self):
        from urllib.request import Request
        handler = self.update.GitHubRedirectHandler()
        request = Request(BASE + 'youziauth.msi')
        for url in ('http://github.com/file', 'https://github.com.evil.invalid/file',
                    'https://github.com@evil.invalid/file', 'https://example.invalid/file',
                    'https://github.com:444/file', 'file:///tmp/file'):
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                handler.redirect_request(request, None, 302, 'Found', {}, url)
        result = handler.redirect_request(request, None, 302, 'Found', {},
                                          'https://release-assets.githubusercontent.com/asset?token=example')
        self.assertEqual(result.host, 'release-assets.githubusercontent.com')


if __name__ == '__main__':
    unittest.main()
