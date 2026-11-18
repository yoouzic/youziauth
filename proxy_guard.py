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
from pathlib import Path

BACKUP_NAME = "proxy_backup.json"
REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
# 看门狗等待父进程的轮询间隔; 1 秒足够快, 又不会白烧 CPU。
POLL_SECONDS = 1.0
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
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, int(bool(enable)))
        winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, str(server))
        if override is not None:
            winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, str(override))
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


def write_backup(path, payload: dict) -> None:
    """原子写备份: 半截 JSON 会让看门狗读不出来, 等于没有保护。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, target)


def pid_alive(pid) -> bool:
    """进程还在吗。拿不到句柄(无权限/不存在)一律按「不在了」处理。"""
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
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
    finally:
        kernel32.CloseHandle(handle)


def restore_backup(path, *, expected_pid=None, reg_path: str = REG_PATH) -> bool:
    """按备份还原并删掉备份。返回是否真的写过注册表。"""
    data = read_backup(path)
    if data is None:
        return False
    owner = data.get("pid")
    try:
        owner = int(owner) if owner not in (None, "") else 0
    except (TypeError, ValueError):
        owner = 0
    if expected_pid is not None and owner and owner != int(expected_pid):
        # 备份已经被新的采集覆盖: 不能拿旧看门狗去还原它。
        return False
    write_proxy(data["enable"], data["server"], data.get("override"), reg_path)
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass
    return True


def recover_stale_backup(path, reg_path: str = REG_PATH) -> bool:
    """启动时收拾上次崩溃留下的代理。

    只有确认没有别的采集进程还在用这份备份时才动注册表; 备份很旧(pid 可能被
    复用)时也直接还原 —— 宁可恢复一个旧代理, 不能让浏览器一直连死端口。
    """
    data = read_backup(path)
    if data is None:
        return False
    owner = data.get("pid")
    try:
        owner = int(owner) if owner not in (None, "") else 0
    except (TypeError, ValueError):
        owner = 0
    if owner and owner != os.getpid() and pid_alive(owner):
        try:
            age = time.time() - Path(path).stat().st_mtime
        except OSError:
            age = 0.0
        if age < MAX_LIVE_OWNER_SECONDS:
            return False
    return restore_backup(path, reg_path=reg_path)


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


def guard_command(backup_path, parent_pid, reg_path: str = REG_PATH) -> list[str]:
    """拉起看门狗用的命令行; 冻结包直接复用自身 exe, 源码运行走脚本。"""
    if getattr(sys, "frozen", False):
        base = [sys.executable]
    else:
        base = [sys.executable,
                str(Path(__file__).resolve().with_name("campus_auth_desktop.py"))]
    return base + ["--proxy-guard", str(backup_path), str(int(parent_pid)), reg_path]


def spawn_guard(backup_path, parent_pid, reg_path: str = REG_PATH):
    """起一个脱离父进程的看门狗; 起不来就返回 None(调用方继续采集, 还有正常收尾兜底)。"""
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
            guard_command(backup_path, parent_pid, reg_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
            env=env,
        )
    except OSError:
        return None


def main(argv=None) -> int:
    """`--proxy-guard <备份路径> <父进程 pid> [注册表路径]`。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2:
        return 2
    try:
        backup_path = Path(argv[0])
        parent_pid = int(argv[1])
    except (TypeError, ValueError):
        return 2
    reg_path = argv[2] if len(argv) > 2 else REG_PATH
    _log(backup_path, f"看门狗启动 pid={os.getpid()} 监视父进程 {parent_pid}")
    missing_since = None
    while True:
        if read_backup(backup_path) is None:
            if missing_since is None:
                missing_since = time.monotonic()
            elif time.monotonic() - missing_since >= MISSING_BACKUP_GRACE_SECONDS:
                _log(backup_path, "备份已被正常收尾删除, 看门狗退出")
                return 0
        else:
            missing_since = None
        if not pid_alive(parent_pid):
            restored = restore_backup(
                backup_path, expected_pid=parent_pid, reg_path=reg_path)
            _log(backup_path,
                 "父进程已退出, 已还原系统代理" if restored else "父进程已退出, 无需还原")
            return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
