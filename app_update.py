from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import tempfile
import threading
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from windows_update import install_msi, verify_msi


REPOSITORY = 'yoouzic/youziauth'
LATEST_RELEASE_URL = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
MAX_PACKAGE_BYTES = 1024 * 1024 * 1024
# Every release must carry the installer plus both detached authenticators; a
# release missing any of them is refused rather than silently downgraded.
RELEASE_ASSETS = ('youziauth.msi', 'SHA256SUMS.txt', 'youziauth.msi.ed25519')


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


def open_url(url):
    validate_url(url)
    request = Request(url, headers={'User-Agent': 'youziauth-updater',
                                   'Accept': 'application/vnd.github+json' if url == LATEST_RELEASE_URL else 'application/octet-stream',
                                   'X-GitHub-Api-Version': '2022-11-28'})
    return build_opener(GitHubRedirectHandler()).open(request, timeout=20)


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


def file_digest(path):
    hasher = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(128 * 1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


class UpdateController:
    def __init__(self, current_version, cache_dir, executable=None):
        self._cache = Path(cache_dir)
        self._executable = executable
        self._lock = threading.RLock()
        self._gate = threading.Lock()
        self._closed = threading.Event()
        self._worker = None
        self._package = None
        self._data = dict(state='idle', current_version=current_version, latest_version='', progress=0,
                          downloaded_bytes=0, total_bytes=0, checked='',
                          message='启动后自动检查 GitHub 正式版；有更新时自动下载，安装前会征求确认。')

    def snapshot(self):
        with self._lock:
            return dict(self._data, busy=self._gate.locked())

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
        with self._lock:
            self._start(self._check, 'checking', '正在检查 GitHub 最新正式版…')
        return '正在后台检查；发现新版本后会自动下载，不影响正常使用。'

    def install(self, confirmed, version):
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
            self._set(state='error', message=message)
        except (URLError, TimeoutError, ConnectionError):
            self._set(state='error', message='无法连接 GitHub 或下载超时，请检查网络后重新检查；其他功能不受影响。')
        except RuntimeError as exc:
            self._set(state='error', message=str(exc))
        except (ValueError, TypeError, KeyError):
            self._set(state='error', message='云端版本或校验信息无效，已停止更新，请稍后重新检查。')
        except OSError:
            self._set(state='error', message='无法保存或读取更新文件，请检查磁盘空间和本机权限后重试。')
        except Exception:
            self._set(state='error', message='更新未完成，请重新检查；校园网和寝室功能不受影响。')
        finally:
            if self._data['state'] == 'error':
                self._package = None
            self._set(checked=dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            self._gate.release()

    def _check(self):
        self._package = None
        self._set(latest_version='', progress=0, downloaded_bytes=0, total_bytes=0)
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
        digest = read_checksum(assets['SHA256SUMS.txt']['browser_download_url'])
        signature = read_signature(assets['youziauth.msi.ed25519']['browser_download_url'])
        self._ensure_open()
        self._cache.mkdir(parents=True, exist_ok=True)
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

    def _download(self, url, size, destination, version, digest, signature):
        self._set(state='downloading', message=f'发现 v{version}，正在后台下载安装包…')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self._cache, prefix='.download-', suffix='.msi', delete=False) as stream:
                temporary = Path(stream.name)
                received = 0
                hasher = hashlib.sha256()
                with open_url(url) as response:
                    if response.status != 200:
                        raise RuntimeError('下载响应不完整，请重新检查。')
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
                if received != size or hasher.hexdigest() != digest:
                    raise RuntimeError('安装包不完整或 SHA-256 校验失败，请重新检查以下载完整文件。')
            self._ensure_open()
            self._set(state='verifying', message='下载完成，正在校验发布者签名和安装包版本…')
            verify_msi(temporary, self._executable, version, digest, signature)
            self._ensure_open()
            temporary.replace(destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

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
