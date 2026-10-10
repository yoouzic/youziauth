from __future__ import annotations

import datetime as dt
import hashlib
import inspect
import json
import os
import re
import sys
import tempfile
import threading
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from windows_update import install_msi, verify_msi


REPOSITORY = 'yoouzic/youziauth'
LATEST_RELEASE_URL = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
COMPARE_URL = f'https://api.github.com/repos/{REPOSITORY}/compare/'
MAX_PACKAGE_BYTES = 1024 * 1024 * 1024
# 安装包与校验材料的必备清单；缺任何一个都拒绝更新，不降级放行。
RELEASE_ASSETS = ('youziauth.msi', 'SHA256SUMS.txt', 'youziauth.msi.ed25519')
# 中文更新说明是**可选**资产：界面有它就用它，没有就退回提交列表。
# 它刻意不进 RELEASE_ASSETS —— 一旦列为必备，任何忘记带说明的发布都会让所有
# 老客户端彻底无法自助更新，这个代价比少一段说明大得多。
NOTES_ASSET = 'release-notes.md'
MAX_NOTES_BYTES = 32 * 1024

# --- 更新内容 ---------------------------------------------------------------
# 首选是发布者手写的中文说明（`release-notes.md` 资产，源文件在 docs/release-notes/）。
# 它取不到时退回 compare API 的提交列表，范围是 `<已安装版本>...<最新版本>`，也就是
# 用户这次升级真正跨过的提交 —— 顺序不能反：GitHub 的 `--generate-notes` 对常规提交
# 只给一行 compare 链接（实测 v1.8.5 的正文只有 80 字节），提交标题又都是英文。
#
# 上限按实测放：36 个提交（v1.5.0 → v1.8.5）的响应是 1.1 MiB，所以 256 KiB 那种
# 「小响应」的限额会把正常升级直接判成超限。这里留 4 MiB，远超我们一两次发布的
# 提交量；GitHub 自己在 250 个提交处拒绝比较，不会给出无界响应。
MAX_CHANGES_BYTES = 4 * 1024 * 1024
MAX_CHANGES_ENTRIES = 60     # 后端上限，防止快照无界增长；界面据此判断总数是否可信
_CHANGES_CACHE = 'changes-v{current}-v{latest}.json'
# 提交标题里的内部前缀：`fix(auth): ...`、`feat!: ...` 等，展示时去掉。
_CONVENTIONAL = re.compile(r'^(?P<kind>[a-z][a-z0-9]*)'
                           r'(?:\([^()]{1,32}\))?!?:\s+(?P<subject>\S.*)$')
# 只报给用户的变化类型；`chore: version 1.9.0`、`test:`、`ci:`、`docs:` 属于维护
# 噪音，列出来只会把真正的修复和功能挤掉。
_USER_FACING_KINDS = ('feat', 'fix', 'perf', 'refactor', 'revert')
# 没有可比对的说明时给一句实话；界面会在这句话下面单独给一个「在 GitHub 上查看发布
# 记录」的链接。所以这句话**不要**自己再提 GitHub，否则同一个出口会出现两遍。
NO_CHANGES = '没能自动获取这次更新的说明。'

# 手写中文说明支持的 markdown 子集：标题行写版本号，`- ` 列表项是一行说明。
# 粗体前缀 `- **重点**：…` 标成「重点」，正文里的 `**`、`*`、反引号一律去掉 ——
# 界面走 textContent，标记留着只会显示成星号。
_NOTES_HEADING = re.compile(r'^#{1,6}\s+v?(?P<version>[0-9]+\.[0-9]+\.[0-9]+)\s*$')
_NOTES_TITLE = re.compile(r'^#{1,6}\s+(?P<title>\S.*)$')
_NOTES_BULLET = re.compile(r'^\s*(?:[-*+]|\d+[.)])\s+(?P<text>\S.*)$')
_NOTES_HIGHLIGHT = re.compile(r'^\*\*(?P<label>[^*\s]{1,8})\*\*[：:、,，]?\s*(?P<text>.*)$')
_LONG_ENTRY = 200

_CHANGELOG_FAILURES = (HTTPError, URLError, TimeoutError, ConnectionError, OSError,
                       ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError)
# 取更新内容时**任何**异常都只能让界面少一块内容，不能影响「有新版本」这条主链路：
# read_small 超限、validate_url 拒绝、JSON 形状不对，全部算在里面。
_FAILED_CHANGELOG = (RuntimeError,) + _CHANGELOG_FAILURES


def compare_url(current_version, latest_version):
    """已安装版本到最新版本之间的提交对比地址。"""
    return f'{COMPARE_URL}v{current_version}...v{latest_version}'


def _no_changes(note=NO_CHANGES, total=0):
    """「没有可比对的说明」的块，界面据此只显示一句实话 + 一个手工出口。

    只带这些字段，不携带仓库地址或路径：快照会直接交给界面。
    """
    return {'entries': [], 'total': total, 'more': False, 'note': note}


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', value):
        raise ValueError('版本号无效')
    result = tuple(map(int, value.split('.')))
    if any(part > limit for part, limit in zip(result, (255, 255, 65535))):
        raise ValueError('版本号超出 Windows 安装包范围')
    return result


def read_current_version(path):
    try:
        value = Path(path).read_text(encoding='utf-8').strip()
        version_tuple(value)
        return value
    except (OSError, ValueError):
        return ''


def validate_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.hostname not in ('api.github.com', 'github.com', 'release-assets.githubusercontent.com',
                                       'objects.githubusercontent.com')):
        raise RuntimeError('更新下载地址不可信，已停止下载。')


class GitHubRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def interactive_user_proxy():
    """The proxy the signed-in user actually configured, if any.

    A service running as SYSTEM has its own registry hive: it cannot see the interactive
    user's ``HKCU Internet Settings``, and that is where a desktop proxy client
    (Clash Verge, v2rayN, ...) writes its setting. WinHTTP is usually left at "direct", so
    such a process goes straight to the Internet -- which on a domestic link is exactly
    where GitHub is least reliable. The unattended updater runs as SYSTEM, so without this
    it downloads over a path nobody configured while the user's own browser flies through
    the proxy next to it.

    Reads the loaded users' hives through HKEY_USERS. Returns ``None`` when there is
    nothing usable, so the caller behaves exactly as before when no proxy is configured.
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    try:
        users = winreg.OpenKey(winreg.HKEY_USERS, "")
    except OSError:
        return None
    try:
        index = 0
        candidates = []
        while True:
            try:
                sid = winreg.EnumKey(users, index)
            except OSError:
                break
            index += 1
            # 只认交互用户的 SID：S-1-5-21-<机器>-<用户 RID>，且不是服务账户。
            if not sid.startswith("S-1-5-21-") or sid.endswith(("-18", "-19", "-20")):
                continue
            candidates.append(sid)
        # 多个已登录用户时取第一个有可用代理的（单用户机器上只有一个）。
        for sid in candidates:
            server = _proxy_from_hive(winreg, sid)
            if server:
                return server
    except OSError:
        return None
    finally:
        winreg.CloseKey(users)
    return None


def _proxy_from_hive(winreg, sid):
    key_path = sid + "\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings"
    try:
        key = winreg.OpenKey(winreg.HKEY_USERS, key_path)
    except OSError:
        return None
    try:
        enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
        if not enabled:
            return None
        server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return None
    finally:
        winreg.CloseKey(key)
    if not isinstance(server, str) or not server.strip():
        return None
    server = server.strip()
    # ProxyServer 可能是 "host:port" 或按协议分列（"http=h:p;https=h:p"）。
    for scheme in ("https", "http"):
        for part in server.split(";"):
            part = part.strip()
            if part.startswith(scheme + "="):
                server = part.split("=", 1)[1].strip()
                break
    if not server:
        return None
    if "=" in server or ";" in server:
        return None
    if not server.startswith(("http://", "https://")):
        server = "http://" + server
    return server


def configured_proxy():
    """Proxy for our outbound requests: explicit setting, else the user's, else system.

    ``YOUZIAUTH_UPDATE_PROXY`` wins so an operator can pin one; ``""`` (set but empty)
    forces direct, which is also the escape hatch if a detected proxy is unwanted.
    """
    override = os.environ.get("YOUZIAUTH_UPDATE_PROXY")
    if override is not None:
        return override.strip() or None
    return interactive_user_proxy()


def open_url(url, headers=None):
    validate_url(url)
    # 每一个 api.github.com 端点都要 JSON 的 Accept —— 只认 LATEST_RELEASE_URL 的话，
    # compare 端点会直接回 415 Unsupported Media Type（实测）。附件下载仍走 octet-stream。
    json_api = url.startswith('https://api.github.com/')
    request_headers = {'User-Agent': 'youziauth-updater',
                       'Accept': 'application/vnd.github+json' if json_api else 'application/octet-stream',
                       'X-GitHub-Api-Version': '2022-11-28'}
    if headers:
        # 断点续传要带 Range；调用方只能追加，不能改写上面这几个固定头。
        request_headers.update(headers)
    request = Request(url, headers=request_headers)
    proxy = configured_proxy()
    handlers = [GitHubRedirectHandler()]
    if proxy:
        handlers.append(ProxyHandler({'http': proxy, 'https': proxy}))
    else:
        # 显式声明直连：默认 ProxyHandler 会去读本进程环境里的代理设置。
        handlers.append(ProxyHandler({}))
    return build_opener(*handlers).open(request, timeout=20)


def _open(url, headers=None):
    """``open_url`` with optional request headers, tolerating a one-argument double.

    Tests replace ``open_url`` with a simple ``def open(url)``. Passing a Range header
    positionally would break every one of them for no reason, so the extra argument is
    offered only to callables that can accept it.
    """
    if headers:
        try:
            if inspect.signature(open_url).parameters:
                accepts = inspect.signature(open_url)
                if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in accepts.parameters.values()) \
                        or "headers" in accepts.parameters:
                    return open_url(url, headers=headers)
        except (TypeError, ValueError):
            pass
    return open_url(url)


def read_small(url, limit):
    with open_url(url) as response:
        if response.status != 200:
            raise RuntimeError('更新服务器返回异常，请稍后重试。')
        value = response.read(limit + 1)
    if len(value) > limit:
        raise RuntimeError('云端更新信息超出大小限制，已停止更新。')
    return value


def release_origin(data) -> str:
    """发布附件所在仓库的 URL 前缀（校验附件来源用）。

    以 API 自己报的 `html_url` 为准，而不是写死的 REPOSITORY：GitHub 账号改名
    （2026-10-04 实测 `Cyzmmd` → `yoouzic`）之后 API 返回新地址，而**已安装的客户端
    内置的是旧名字** —— 实测旧客户端因此把合法发布判成「更新附件来源无效，已停止下载」，
    彻底无法自助更新，只能手动装一次新包。认 API 报告的仓库，其余检查（主机、路径、
    附件名、state、大小、SHA-256、Ed25519 签名）一项都不放松。
    """
    html = data.get('html_url')
    if isinstance(html, str):
        match = re.fullmatch(
            r'https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/releases/tag/[^/]+', html)
        if match:
            return f'https://github.com/{match.group(1)}'
    return f'https://github.com/{REPOSITORY}'


def release_assets(data, version):
    assets = data.get('assets')
    if not isinstance(assets, list) or any(not isinstance(asset, dict) for asset in assets):
        raise ValueError('发布附件格式无效')
    origin = release_origin(data)
    result = {}
    for name in RELEASE_ASSETS:
        matches = [asset for asset in assets if asset.get('name') == name]
        if len(matches) != 1:
            raise RuntimeError(f'最新版本缺少唯一的发布附件 {name}，请等待发布完成后重试。')
        asset = matches[0]
        url = f'{origin}/releases/download/v{version}/{name}'
        if asset.get('browser_download_url') != url or asset.get('state') != 'uploaded':
            raise RuntimeError('更新附件来源无效或尚未上传完成，已停止下载。')
        size = asset.get('size')
        if type(size) is not int or not 0 < size <= (MAX_PACKAGE_BYTES if name.endswith('.msi') else 32768):
            raise ValueError('更新附件大小无效')
        result[name] = asset
    # 中文说明是可选的：只有发布真的上传了一个形状合法的 release-notes.md，才给出它
    # 的来源地址。缺席、重复或形状不对都等于「这一版没写说明」，调用方据此退回提交对比。
    # 用一个**已校验**的地址而不是按约定拼地址：拼出来的地址对「没上传」和「上传了」
    # 是同一个字符串，而这两件事要走不同的路径。
    notes = [asset for asset in assets if asset.get('name') == NOTES_ASSET]
    if len(notes) == 1:
        asset = notes[0]
        size = asset.get('size')
        if (asset.get('browser_download_url') == f'{origin}/releases/download/v{version}/{NOTES_ASSET}'
                and asset.get('state') == 'uploaded'
                and type(size) is int and 0 < size <= MAX_NOTES_BYTES):
            result[NOTES_ASSET] = asset
    return result


def read_checksum(url):
    text = read_small(url, 32768).decode('utf-8-sig')
    values = [match[1].lower() for line in text.splitlines()
              if (match := re.fullmatch(r'([0-9a-fA-F]{64}) [ *]youziauth\.msi', line))]
    if len(values) != 1:
        raise RuntimeError('安装包校验清单无效，请等待发布者修复后重试。')
    return values[0]


def read_signature(url):
    text = read_small(url, 32768).decode('utf-8-sig').strip()
    if re.fullmatch(r'[0-9a-fA-F]{128}', text) is None:
        raise RuntimeError('安装包发布签名无效，请等待发布者修复后重试。')
    return text.lower()


# --- 更新内容 ---------------------------------------------------------------

def clean_subject(message):
    """把一条提交标题变成用户能读的一行，并回报它的类型。

    返回 ``(类型, 标题)``：类型取自 ``fix(auth): ...`` 这类约定式前缀，标题去掉
    前缀本身。没有前缀就用 ``''`` 作类型 —— 老提交和手写合并都得算进更新内容。
    """
    if not isinstance(message, str):
        return '', ''
    first = message.splitlines()[0].strip() if message.strip() else ''
    if not first:
        return '', ''
    match = _CONVENTIONAL.match(first)
    if match is None:
        return '', first
    kind = match.group('kind').lower()
    subject = match.group('subject').strip()
    if not subject:
        # `fix:` 后面什么都没有，去掉前缀就只剩空行；保留原文反而更有信息量。
        return '', first
    return kind, subject


def _commit_date(commit):
    """提交的日期（只取 YYYY-MM-DD），取不到就留空。

    `committer` 在 GitHub 上可能是 null（提交者账号已删或没关联），所以退回
    `author`；两个都没有时界面只显示标题，而不是编一个日期出来。
    """
    if not isinstance(commit, dict):
        return ''
    payload = commit.get('commit')
    if not isinstance(payload, dict):
        return ''
    for field in ('committer', 'author'):
        who = payload.get(field)
        date = who.get('date') if isinstance(who, dict) else None
        if isinstance(date, str) and len(date) >= 10 and date[4] == '-' and date[7] == '-':
            return date[:10]
    return ''


def _changelog_entries(commits):
    """提交列表 -> ``(条目, 总条数, 是否截断)``，只留用户看得见的变化。"""
    entries, seen, count = [], set(), 0
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        payload = commit.get('commit')
        if not isinstance(payload, dict):
            continue
        kind, subject = clean_subject(payload.get('message'))
        if not subject or kind not in _USER_FACING_KINDS:
            continue
        key = (kind, subject.casefold())
        if key in seen:
            continue
        seen.add(key)
        count += 1
        if len(entries) < MAX_CHANGES_ENTRIES:
            entries.append({'subject': subject, 'kind': kind, 'date': _commit_date(commit)})
    return entries, count, count > len(entries)


def _plain(text):
    """去掉行内的 markdown 标记，界面只显示纯文本。"""
    return text.replace('**', '').replace('`', '').replace('*', '').strip()


def read_notes(raw, version):
    """把手写的中文更新说明解析成 ``changes`` 块。

    只认 ``docs/release-notes/v<版本>.md`` 里的约定格式：一级标题写版本号，``- `` 列表
    项各是一行说明。版本对不上就直接判无效 —— 宁可退回提交列表，也不能把上一个版本的
    说明当成这个版本的讲给用户听。
    """
    try:
        text = raw.decode('utf-8-sig')
    except (AttributeError, UnicodeError):
        raise ValueError('更新说明不是 UTF-8 文本') from None
    entries, declared, title, seen = [], '', '', set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('#'):
            match = _NOTES_HEADING.match(stripped)
            if match is not None:
                declared = match.group('version')
                continue
            if not title and (named := _NOTES_TITLE.match(stripped)) is not None:
                title = _plain(named.group('title'))
            continue
        match = _NOTES_BULLET.match(stripped)
        if match is None:
            # 标题之外的第一段正文当作简介；代码块/引用行不算。
            if not title and not stripped.startswith(('>', '```')):
                title = _plain(stripped)
            continue
        raw_entry = match.group('text').strip()
        if not raw_entry:
            continue
        # 先认粗体标签，再去标记：反过来 `**重点**：…` 的星号会先被删掉，标签就丢了。
        # 标签只认短词（`\S{1,8}`）——`**一整句话**：…` 是作者的强调，不是分类，把整句
        # 当成标签会在界面上显示成「一整句话 · 剩下的内容」。
        kind, subject = '', raw_entry
        if (highlight := _NOTES_HIGHLIGHT.match(raw_entry)) is not None:
            kind, subject = _plain(highlight.group('label')), _plain(highlight.group('text'))
            if not subject:
                kind, subject = '', raw_entry
        subject = _plain(subject)
        if not subject:
            continue
        if len(subject) > _LONG_ENTRY:
            subject = subject[:_LONG_ENTRY].rstrip() + '…'
        if subject.casefold() in seen:
            continue
        seen.add(subject.casefold())
        entries.append({'subject': subject, 'kind': kind, 'date': ''})
    if declared != version:
        raise ValueError('更新说明的版本与发布版本不一致')
    if not entries:
        raise ValueError('更新说明没有任何条目')
    if len(entries) > MAX_CHANGES_ENTRIES:
        entries = entries[:MAX_CHANGES_ENTRIES]
    return {'entries': entries, 'total': len(entries), 'more': False, 'note': title}


def read_changes(raw):
    """校验 compare API 的响应并抽出变更条目（只读一个已知结构）。"""
    try:
        data = json.loads(raw.decode('utf-8-sig'))
    except (AttributeError, ValueError, UnicodeError):
        raise ValueError('更新说明不是有效的 JSON') from None
    if not isinstance(data, dict):
        raise ValueError('更新说明格式无效')
    if data.get('status') not in ('ahead', 'behind', 'identical', 'diverged'):
        raise ValueError('更新说明状态无效')
    total = data.get('total_commits')
    commits = data.get('commits')
    if type(total) is not int or not 0 <= total <= 100000 or not isinstance(commits, list):
        raise ValueError('更新说明条目无效')
    entries, count, truncated = _changelog_entries(commits)
    note = ''
    if not count:
        # status=identical 表示两个标签指向同一处（重发或补发版本号）；其余情况是
        # 这次升级没有用户可见的变化。
        note = ('这次升级只包含内部维护改动。' if total or data['status'] != 'identical'
                else '这个版本与当前已安装版本内容相同。')
    return {'entries': entries, 'total': count, 'more': truncated, 'note': note}


def load_changes(path):
    """读取缓存过的更新内容；缓存不可用就返回 ``None`` 重新取。"""
    try:
        cached = json.loads(Path(path).read_text(encoding='utf-8'))
        if (not isinstance(cached, dict) or not isinstance(cached.get('entries'), list)
                or type(cached.get('total')) is not int or not isinstance(cached.get('note'), str)
                or type(cached.get('more')) is not bool):
            return None
        return {'entries': [dict(entry) for entry in cached['entries']],
                'total': cached['total'], 'more': cached['more'], 'note': cached['note']}
    except (OSError, ValueError, TypeError, RecursionError, AttributeError):
        return None


def save_changes(path, changes):
    """尽力缓存更新内容：写不进去也不能影响更新本身。"""
    try:
        path = Path(path)
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_text(json.dumps(changes, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)
    except (OSError, ValueError, TypeError):
        pass


def file_digest(path):
    hasher = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(128 * 1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


# 提权端发布的更新状态允许出现的字段。多一个都不收：快照是外部数据，界面只渲染
# 它认识的形状。与 auto_update.UpdateStatus 保持一致。
_AGENT_UPDATE_FIELDS = frozenset({
    'state', 'current_version', 'latest_version', 'progress', 'checked', 'message',
    'detail', 'changes',
})

# 提权端会用的全部状态词，界面按这个词表校验。
_AGENT_UPDATE_STATES = frozenset({
    'idle', 'checking', 'downloading', 'verifying', 'ready', 'up_to_date',
    'installing', 'installed', 'error',
})


class UpdateController:
    """Holds the update state the interface shows.

    In the installed product the state is **sourced from the SYSTEM agent**
    (``auto_update``), which owns checking, verifying and silently installing; the
    interface is a passive reader. ``manual=False`` disables the network paths below so a
    desktop process can never start a second, competing download -- and it has no business
    installing anything itself, because that is what used to cost a UAC prompt per release.
    """

    def __init__(self, current_version, cache_dir, executable=None, manual=True):
        self._cache = Path(cache_dir)
        self._executable = executable
        self._manual = bool(manual)
        self._lock = threading.RLock()
        self._gate = threading.Lock()
        self._closed = threading.Event()
        self._worker = None
        self._package = None
        self._data = dict(state='idle', current_version=current_version, latest_version='', progress=0,
                          downloaded_bytes=0, total_bytes=0, checked='', changes=_no_changes(), transient=False, detail='',
                          message='启动后自动检查 GitHub 正式版；有更新时自动下载，安装前会征求确认。')

    def adopt(self, block, current_version=None):
        """Take the state the privileged agent published.

        The block is the privileged agent's own ``UpdateStatus``; every field is
        shape-checked here so a corrupt or hostile snapshot cannot inject unknown keys
        into what the interface renders. Returns ``True`` when it was accepted.
        """
        if not isinstance(block, dict):
            return False
        if set(block) - _AGENT_UPDATE_FIELDS:
            return False
        state = block.get('state')
        if state not in _AGENT_UPDATE_STATES:
            return False
        progress = block.get('progress', 0)
        if type(progress) is not int or not 0 <= progress <= 100:
            progress = 0
        changes = block.get('changes')
        text = lambda name: block.get(name) if isinstance(block.get(name), str) else ''
        values = dict(
            state=state, latest_version=text('latest_version'), progress=progress,
            checked=text('checked'), message=text('message'),
            changes=changes if isinstance(changes, dict) else _no_changes(),
        )
        if isinstance(current_version, str) and current_version:
            values['current_version'] = current_version
        self._set(**values)
        return True

    def disconnect(self, message='后台自动更新未在运行，请先开启后台检测。'):
        """The agent is gone: stop showing a stale "ready" the interface cannot act on."""
        self._package = None
        self._set(state='idle', latest_version='', progress=0, downloaded_bytes=0,
                  total_bytes=0, changes=_no_changes(), message=message)
        return True

    def snapshot(self):
        with self._lock:
            return dict(self._data, busy=self._gate.locked())

    def package(self):
        """The verified installer this controller downloaded, as ``(path, version, digest, signature)``.

        Only the privileged self-updater calls this, and only after a check reported
        ``ready``. The tuple is what the worker locks and re-verifies before installing,
        so handing it on does not weaken anything: the digest and signature are inputs to
        a check that runs again, not a claim of trust.
        """
        with self._lock:
            if self._package is None or self._data['state'] != 'ready':
                raise RuntimeError('安装包尚未下载并校验，无法安装。')
            return self._package

    def _set(self, **values):
        with self._lock:
            self._data.update(values)

    def _ensure_open(self):
        if self._closed.is_set():
            raise RuntimeError('程序正在退出，更新操作已停止。')

    def _start(self, work, state, message):
        self._ensure_open()
        if not self._gate.acquire(blocking=False):
            raise RuntimeError('更新操作正在进行，请稍候。')
        self._set(state=state, message=message)
        self._worker = threading.Thread(target=self._run, args=(work,), daemon=True, name='youziauth-update')
        try:
            self._worker.start()
        except Exception:
            self._gate.release()
            self._set(state='error', message='无法启动更新任务，请重新检查。')
            raise

    def check(self):
        if not self._manual:
            raise RuntimeError('更新由后台代理负责，请在「校园网」页开启后台检测。')
        with self._lock:
            self._start(self._check, 'checking', '正在检查 GitHub 最新正式版…')
        return '正在后台检查；发现新版本后会自动下载，不影响正常使用。'

    def install(self, confirmed, version):
        if not self._manual:
            raise RuntimeError('更新由后台代理静默完成，这里无需手动安装。')
        with self._lock:
            if confirmed is not True or self._data['state'] != 'ready' or version != self._data['latest_version'] or not self._package:
                raise RuntimeError('请等待下载校验完成，并重新确认要安装的版本。')
            self._start(self._install, 'launching', '正在重新校验安装包，随后打开 Windows 安装向导…')
        return '正在校验并打开安装向导，请按 Windows 提示完成管理员授权。'

    def _run(self, work):
        try:
            work()
        except HTTPError as exc:
            exc.close()
            message = ('GitHub 请求频率受限，请稍后重新检查。' if exc.code in (403, 429) else
                       'GitHub 更新请求失败，请稍后重新检查。')
            self._set(state='error', message=message, transient=True)
        except (URLError, TimeoutError, ConnectionError) as exc:
            # 标记成「传输问题」：后台自动更新据此决定要不要在本次尝试内重试。
            # 靠错误文案去猜是不可靠的，而且文案是给人看的，随时会改。
            self._set(state='error', transient=True, detail=type(exc).__name__,
                      message='无法连接 GitHub 或下载超时，请检查网络后重新检查；其他功能不受影响。')
        except RuntimeError as exc:
            self._set(state='error', transient=False, message=str(exc))
        except (ValueError, TypeError, KeyError):
            self._set(state='error', transient=False,
                      message='云端版本或校验信息无效，已停止更新，请稍后重新检查。')
        except OSError:
            self._set(state='error', transient=False,
                      message='无法保存或读取更新文件，请检查磁盘空间和本机权限后重试。')
        except Exception:
            self._set(state='error', transient=False,
                      message='更新未完成，请重新检查；校园网和寝室功能不受影响。')
        finally:
            if self._data['state'] == 'error':
                self._package = None
            self._set(checked=dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            self._gate.release()

    def _check(self):
        self._package = None
        self._set(latest_version='', progress=0, downloaded_bytes=0, total_bytes=0, changes=_no_changes())
        if not self._data['current_version']:
            raise RuntimeError('无法读取本机版本，已停止自动更新；请使用完整的官方安装版。')
        current = version_tuple(self._data['current_version'])
        try:
            data = json.loads(read_small(LATEST_RELEASE_URL, 1024 * 1024))
        except HTTPError as exc:
            if exc.code != 404:
                raise
            exc.close()
            self._set(state='up_to_date', message='GitHub 暂无可用的正式发布，稍后可重新检查。')
            return
        self._ensure_open()
        if not isinstance(data, dict) or any(type(data.get(flag)) is not bool for flag in ('draft', 'prerelease')):
            raise ValueError('无效的发布信息')
        if data['draft'] or data['prerelease']:
            self._set(state='up_to_date', message='暂无新的正式版本；已忽略草稿或预发布版本。')
            return
        tag = data.get('tag_name', '')
        if not isinstance(tag, str) or not tag.startswith('v'):
            raise ValueError('无效的发布标签')
        version = tag[1:]
        latest = version_tuple(version)
        self._set(latest_version=version)
        if latest <= current:
            self._set(state='up_to_date', message=('当前已是最新正式版。' if latest == current else
                                                  '当前版本高于云端最新正式版，无需更新，不会降级。'))
            return
        assets = release_assets(data, version)
        package = assets['youziauth.msi']
        # 缓存目录要先建好：说明是**先**取的，而它要写进这个目录。旧顺序里说明排在下载
        # 之后，所以目录晚一步建也没人发现。
        try:
            self._cache.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise RuntimeError('无法保存或读取更新文件，请检查磁盘空间和本机权限后重试。') from None
        # 先取「这次改了什么」，再动安装包。安装包有几十 MB，下载要几分钟；把说明放在
        # 下载之后，用户在整个下载期间只知道「有新版本」，却不知道新版本改了什么 ——
        # 而「要不要升」正是他此刻想知道的事。说明只是一份小文本，先拿它不花时间。
        self._load_changes(self._data['current_version'], version, assets.get(NOTES_ASSET))
        self._ensure_open()
        digest = read_checksum(assets['SHA256SUMS.txt']['browser_download_url'])
        signature = read_signature(assets['youziauth.msi.ed25519']['browser_download_url'])
        self._ensure_open()
        destination = self._cache / f'youziauth-{version}.msi'
        size = package['size']
        self._set(total_bytes=size)
        if destination.is_file() and destination.stat().st_size == size and file_digest(destination) == digest:
            self._set(state='verifying', message='正在重新校验已下载的安装包…', downloaded_bytes=size, progress=100)
            verify_msi(destination, self._executable, version, digest, signature)
        else:
            self._download(package['browser_download_url'], size, destination, version, digest, signature)
        self._ensure_open()
        self._package = (destination, version, digest, signature)
        self._set(state='ready', progress=100, downloaded_bytes=size,
                  message=f'v{version} 已下载，哈希与发布者签名校验通过；确认后即可安装。')

    def _load_changes(self, current_version, version, notes_asset=None):
        """填 ``changes`` 块：优先发布者手写的中文说明，其次提交对比，最后实话实说。

        纯展示信息：任何失败都只让界面少一块内容，绝不影响「有新版本、已下载、
        校验通过」这条主链路。成功一次就缓存到本机，重复检查和重装同一版本都不再
        请求 GitHub（也避开未登录的 60 次/小时限流）。

        ``current_version`` 必须是 ``'1.4.4'`` 这样的版本号字符串 —— ``_check`` 里的
        ``current`` 已是三元组，传进来会拼出 ``v(1, 4, 4)...`` 这种地址。
        """
        try:
            cache = self._cache / _CHANGES_CACHE.format(current=current_version, latest=version)
        except (AttributeError, ValueError, TypeError):
            self._set(changes=_no_changes())
            return
        cached = load_changes(cache)
        if cached is not None:
            self._set(changes=cached)
            return
        # 先把「有新版本、正在取说明」摆出去，再发请求。这一步是**订阅**：调用方在
        # 取说明期间读到的快照就有 latest_version，而不是等到下载都开始了才知道有新版。
        self._set(state='checking', latest_version=version, changes=_no_changes(),
                  message=f'发现 v{version}，正在读取更新说明…')
        changes = None
        if isinstance(notes_asset, dict) and isinstance(notes_asset.get('browser_download_url'), str):
            try:
                changes = read_notes(
                    read_small(notes_asset['browser_download_url'], MAX_NOTES_BYTES), version)
            except _FAILED_CHANGELOG:
                changes = None      # 说明坏了就退回提交对比，不让这一版空着
        if changes is None:
            try:
                changes = read_changes(read_small(compare_url(current_version, version), MAX_CHANGES_BYTES))
            except _FAILED_CHANGELOG:
                self._set(changes=_no_changes())
                return
        self._ensure_open()
        save_changes(cache, changes)
        self._set(changes=changes)

    def _download(self, url, size, destination, version, digest, signature):
        self._set(state='downloading', message=f'发现 v{version}，正在后台下载安装包…')
        # 断点续传。66 MB 的包在这条线路上要下几分钟，中途断一次很常见；从头再来等于
        # 每次都在赌整段传输不断。半成品固定放在一个文件里，并配一份小元数据记录「这是谁
        # 的半成品」——换了版本或换了包就必须丢掉，否则会把上一版的字节拼进这一版。
        # 后缀刻意不是 .msi：半成品绝不能被当成「一个可安装的包」被别处看见。
        partial = self._cache / f'.partial-{version}.msi.part'
        meta = self._cache / f'.partial-{version}.meta.json'
        resume_from = self._resume_offset(partial, meta, url, size, digest)
        stream = None
        try:
            if resume_from:
                stream = partial.open('ab')
                received = resume_from
            else:
                partial.unlink(missing_ok=True)
                stream = partial.open('wb')
                received = 0
                self._write_partial_meta(meta, url, size, digest)
            hasher = hashlib.sha256()
            if received:
                # 已有的前缀必须进哈希，最后才能对上整包的摘要。
                with partial.open('rb') as prefix:
                    for block in iter(lambda: prefix.read(1024 * 1024), b''):
                        hasher.update(block)
            headers = {'Range': f'bytes={received}-'} if received else None
            with _open(url, headers) as response:
                if received and response.status == 200:
                    # 服务器忽略了 Range：只能从头来，把已有字节丢掉重写。
                    stream.close()
                    stream = partial.open('wb')
                    received = 0
                    hasher = hashlib.sha256()
                elif response.status not in ((206,) if received else (200,)):
                    raise RuntimeError('下载响应不完整，请重新检查。')
                self._set(downloaded_bytes=received, progress=received * 100 // size)
                while True:
                    self._ensure_open()
                    chunk = response.read(128 * 1024)
                    self._ensure_open()
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > size:
                        raise RuntimeError('安装包大小与发布信息不符，已停止下载，请重新检查。')
                    stream.write(chunk)
                    hasher.update(chunk)
                    self._set(downloaded_bytes=received, progress=received * 100 // size)
            stream.close()
            stream = None
            if received != size or hasher.hexdigest() != digest:
                raise RuntimeError('安装包不完整或 SHA-256 校验失败，请重新检查以下载完整文件。')
            self._ensure_open()
            self._set(state='verifying', message='下载完成，正在校验发布者签名和安装包版本…')
            verify_msi(partial, self._executable, version, digest, signature)
            self._ensure_open()
            partial.replace(destination)
            meta.unlink(missing_ok=True)
        finally:
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def _resume_offset(self, partial, meta, url, size, digest):
        """How many bytes of ``partial`` may be trusted, or 0 to start over.

        只有「同一个 URL、同一个大小、同一个摘要」的半成品才敢接着下：任何一项不同都
        意味着这是别的包留下的字节，拼起来就是坏文件。
        """
        try:
            if not partial.is_file():
                return 0
            recorded = json.loads(Path(meta).read_text(encoding='utf-8'))
            if not isinstance(recorded, dict):
                return 0
            if (recorded.get('url') != url or recorded.get('bytes') != size
                    or recorded.get('sha256') != digest):
                return 0
            have = partial.stat().st_size
        except (OSError, ValueError, TypeError, RecursionError):
            return 0
        return have if 0 < have < size else 0

    def _write_partial_meta(self, meta, url, size, digest):
        try:
            Path(meta).write_text(json.dumps({'url': url, 'bytes': size, 'sha256': digest}),
                                  encoding='utf-8')
        except OSError:
            pass

    def _install(self):
        path, version, digest, signature = self._package
        def launched():
            self._set(state='launched', message='Windows 安装向导已打开，请完成授权和安装；安装完成后重新打开程序。')
        code = install_msi(path, self._executable, version, digest, launched, signature)
        if code == 1602:
            self._set(state='ready', message='安装已取消，当前版本未更新；可以稍后再次确认安装。')
        elif code in (0, 3010):
            self._set(state='launched', message=('安装程序已完成，请重启电脑后打开程序。' if code == 3010 else
                                                '安装程序已完成，请重新打开程序以使用新版本。'))
        else:
            raise RuntimeError(f'Windows 安装未完成（返回码 {code}），请关闭其他安装向导后重新检查。')

    def close(self):
        self._closed.set()
