"""采集代理的独立看门狗: 主程序没了也要把系统代理还原。

背景(2026-11-18 现场): 乐跑凭证采集会把当前用户的系统代理临时指到
`127.0.0.1:<随机端口>`。正常收尾由 `CaptureSession.stop()` 还原; 但只要进程
被**强行结束**(卡死对话框里的「关闭程序」、任务管理器结束任务、断电前的强杀、
崩溃前没走到 finally), Python 的 finally/atexit 都不会执行, 系统代理就留在
一个人已经不在的端口上 —— 浏览器的每个请求都连 127.0.0.1 被拒, 表现为整台
机器断网。

所以采集开始时额外起一个**独立进程**: 它只做一件事 —— 等父进程消失, 然后按
备份文件把 HKCU 代理还原。这条路径只依赖标准库, 不碰 cryptography、不碰界面
组件, 保证在最坏情况下也能跑起来。

备份文件格式(与 `lptiyu_capture.SystemProxy` 共用):
    {"enable": 0|1, "server": "...", "override": "...",
     "pid": <采集进程>, "port": <采集端口>, "created": <时间戳>}
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from windows_credentials import atomic_write_bytes

BACKUP_NAME = "proxy_backup.json"
REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
# 看门狗等待父进程的轮询间隔; 1 秒足够快, 又不会白烧 CPU。
POLL_SECONDS = 1.0
RESTORE_ATTEMPTS = 5
# 备份文件被正常收尾删掉后, 再等这么久确认不是「写一半」再退出。
MISSING_BACKUP_GRACE_SECONDS = 3.0
# 备份里的 pid 还活着时, 最多容忍这么久; 超过就当成「pid 被复用」直接还原,
# 避免一次崩溃因为巧合的进程号复用而把用户永远留在死代理上。
MAX_LIVE_OWNER_SECONDS = 30 * 60


def _winreg():
    import winreg          # 只在 Windows 上调用; 延迟导入便于纯逻辑测试。
    return winreg


def read_proxy(reg_path: str = REG_PATH):
    """(enable, server, override); 值不存在时用直连空值。"""
    winreg = _winreg()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path) as key:
        try:
            enable = int(winreg.QueryValueEx(key, "ProxyEnable")[0])
        except FileNotFoundError:
            enable = 0
        try:
            server = str(winreg.QueryValueEx(key, "ProxyServer")[0])
        except FileNotFoundError:
            server = ""
        try:
            override = str(winreg.QueryValueEx(key, "ProxyOverride")[0])
        except FileNotFoundError:
            override = ""
    return enable, server, override


def notify_settings_changed() -> None:
    """让 WinINet 立刻重新读设置; 没有 WinINet 时静默跳过。"""
    try:
        import ctypes
        ctypes.windll.wininet.InternetSetOptionW(0, 39, 0, 0)   # SETTINGS_CHANGED
        ctypes.windll.wininet.InternetSetOptionW(0, 37, 0, 0)   # REFRESH
    except Exception:                  # noqa: BLE001 —— 无 WinINet 时忽略
        pass


def write_proxy(enable, server, override=None, reg_path: str = REG_PATH) -> None:
    winreg = _winreg()
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, reg_path, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE
    ) as key:
        if not enable:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, str(server))
        if override is not None:
            winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, str(override))
        if enable:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
    notify_settings_changed()


def read_backup(path) -> dict | None:
    """读备份; 坏文件当作没有(绝不因为一个坏备份去改用户的代理)。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        data["enable"] = int(data.get("enable") or 0)
    except (TypeError, ValueError):
        return None
    if "server" not in data:
        return None
    data["server"] = str(data.get("server") or "")
    data["override"] = str(data.get("override") or "")
    return data


def _local_data_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))


def shared_backup_path() -> Path:
    return _local_data_root() / "youziauth" / "lptiyu" / "capture" / BACKUP_NAME


def legacy_extractor_backup_path() -> Path:
    return _local_data_root() / "lptiyu-extractor" / "capture" / BACKUP_NAME


@contextmanager
def backup_lock(path):
    """Serialize proxy ownership and restoration across capture processes."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_suffix(".lock")
    # Closing the handle releases the lock, even when the owner is terminated.
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def write_backup(path, payload: dict) -> None:
    """Flush the previous proxy settings and atomically replace the backup."""
    atomic_write_bytes(Path(path), json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def pid_alive(pid) -> bool:
    """Treat access denied as a possibly live owner."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    wait_timeout = 0x00000102
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED
    try:
        return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
    finally:
        kernel32.CloseHandle(handle)


def backup_owner(data):
    try:
        return int(data.get("pid") or 0)
    except (TypeError, ValueError):
        return 0


def backup_is_active(path, data):
    owner = backup_owner(data)
    if data.get("release_requested") is True or not owner or not pid_alive(owner):
        return False
    try:
        return time.time() - Path(path).stat().st_mtime < MAX_LIVE_OWNER_SECONDS
    except OSError:
        return True


def restore_locked_backup(path, data, reg_path: str = REG_PATH) -> bool:
    """Restore only if the current proxy still points to the recorded capture."""
    port = data.get("port")
    enable = data["enable"]
    override = data["override"]
    if port is not None:
        current_enable, server, override = read_proxy(reg_path)
        if server.strip() != f"127.0.0.1:{port}":
            Path(path).unlink(missing_ok=True)
            return False
        # Leave a proxy that was disabled during capture disabled.
        enable = current_enable and enable
    write_proxy(enable, data["server"], override, reg_path)
    Path(path).unlink(missing_ok=True)
    return True


def restore_backup(path, *, expected_pid=None, capture_id=None, reg_path: str = REG_PATH) -> bool:
    with backup_lock(path):
        data = read_backup(path)
        if data is None:
            return False
        if expected_pid is not None and backup_owner(data) not in (0, int(expected_pid)):
            return False
        if capture_id is not None and data.get("capture_id") != capture_id:
            return False
        return restore_locked_backup(path, data, reg_path)


def recover_stale_backup(path, reg_path: str = REG_PATH) -> bool:
    """Leave an active capture alone and recover its predecessor after a crash."""
    if not Path(path).is_file():
        return False
    with backup_lock(path):
        data = read_backup(path)
        if data is None or backup_is_active(path, data):
            return False
        return restore_locked_backup(path, data, reg_path)


def _log(path, message: str) -> None:
    try:
        target = Path(path).with_name("proxy_guard.log")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and target.stat().st_size > 256 * 1024:
            target.replace(target.with_name(target.name + ".1"))
        with target.open("a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except OSError:
        pass


def guard_command(backup_path, parent_pid, reg_path: str = REG_PATH, *, capture_id=None) -> list[str]:
    """拉起看门狗用的命令行; 冻结包直接复用自身 exe, 源码运行走脚本。"""
    if getattr(sys, "frozen", False):
        base = [sys.executable]
    else:
        base = [sys.executable,
                str(Path(__file__).resolve().with_name("campus_auth_desktop.py"))]
    command = base + ["--proxy-guard", str(backup_path), str(int(parent_pid)), reg_path]
    return command + [capture_id] if capture_id is not None else command


def spawn_guard(backup_path, parent_pid, reg_path: str = REG_PATH, *, capture_id=None):
    """Start a detached watchdog. The caller waits for it before switching the proxy."""
    if os.name != "nt":
        return None
    flags = 0x08000000 | 0x00000008          # CREATE_NO_WINDOW | DETACHED_PROCESS
    flags |= 0x00000200                      # CREATE_NEW_PROCESS_GROUP
    env = None
    if getattr(sys, "frozen", False):
        # 单文件提取器退出时会清理临时运行库。看门狗要独立解包，才能在它退出后运行。
        env = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    try:
        return subprocess.Popen(
            guard_command(backup_path, parent_pid, reg_path, capture_id=capture_id),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
            env=env,
        )
    except OSError:
        return None


def guard_ready(path, capture_id, process, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and process.poll() is None:
        with backup_lock(path):
            data = read_backup(path)
        if data is None or data.get("capture_id") != capture_id:
            return False
        if data.get("guard_ready") is True:
            return True
        time.sleep(0.05)
    return False


def request_restore(path, capture_id):
    """Allow the watchdog to restore after a failed stop while its parent lives."""
    with backup_lock(path):
        data = read_backup(path)
        if data is None or data.get("capture_id") != capture_id:
            return
        data["release_requested"] = True
        write_backup(path, data)


def _finish_restore(path, parent_pid, capture_id, reg_path):
    for attempt in range(RESTORE_ATTEMPTS):
        try:
            restored = restore_backup(path, expected_pid=parent_pid, capture_id=capture_id,
                                      reg_path=reg_path)
        except OSError as exc:
            if attempt == RESTORE_ATTEMPTS - 1:
                _log(path, f"系统代理恢复失败，保留备份：{exc}")
                return 1
            time.sleep(POLL_SECONDS)
        else:
            _log(path, "已还原系统代理" if restored else "无需还原系统代理")
            return 0


def main(argv=None) -> int:
    """`--proxy-guard <备份路径> <父进程 pid> [注册表路径] [采集标识]`。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2:
        return 2
    try:
        backup_path = Path(argv[0])
        parent_pid = int(argv[1])
    except (TypeError, ValueError):
        return 2
    reg_path = argv[2] if len(argv) > 2 else REG_PATH
    capture_id = argv[3] if len(argv) > 3 else None
    if capture_id is not None:
        with backup_lock(backup_path):
            data = read_backup(backup_path)
            if data is None or data.get("capture_id") != capture_id or backup_owner(data) != parent_pid:
                return 1
            data["guard_ready"] = True
            write_backup(backup_path, data)
    _log(backup_path, f"看门狗启动 pid={os.getpid()} 监视父进程 {parent_pid}")
    missing_since = None
    while True:
        data = read_backup(backup_path)
        if data is not None and capture_id is not None and data.get("capture_id") != capture_id:
            return 0
        if data is None:
            if missing_since is None:
                missing_since = time.monotonic()
            elif time.monotonic() - missing_since >= MISSING_BACKUP_GRACE_SECONDS:
                _log(backup_path, "备份已被正常收尾删除, 看门狗退出")
                return 0
        else:
            missing_since = None
        released = capture_id is not None and data is not None and data.get("release_requested") is True
        if released or not pid_alive(parent_pid):
            return _finish_restore(backup_path, parent_pid, capture_id, reg_path)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
