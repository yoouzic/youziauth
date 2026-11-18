"""Unified WebView2 desktop entry and isolated, synthetic browser preview."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import re
import sys
import threading
from ctypes import wintypes
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import agent_ipc
import campus_auth_gui as gui
import windows_tray
from app_update import read_current_version
from desktop_bridge import DesktopBridge, PreviewBridge


_STARTUP_EVENT_FLAG = '--startup-ready-event'
_STARTUP_EVENT_NAME = re.compile(r'Local\\youziauth-tray-ready-[a-f0-9]{32}')


def signal_startup_ready(name):
    """Signal the launcher-owned event only after the desktop has initialized."""
    if os.name != 'nt' or _STARTUP_EVENT_NAME.fullmatch(name) is None:
        return False
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    event = kernel32.OpenEventW(0x0002, False, name)  # EVENT_MODIFY_STATE
    if not event:
        return False
    try:
        return bool(kernel32.SetEvent(event))
    finally:
        kernel32.CloseHandle(event)


def current_session_id():
    if os.name != 'nt':
        return None
    identify = ctypes.windll.kernel32.ProcessIdToSessionId
    identify.argtypes = (wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    identify.restype = wintypes.BOOL
    session = wintypes.DWORD()
    if identify(os.getpid(), ctypes.byref(session)):
        return session.value
    return None


def _acknowledge_existing_desktop(event):
    try:
        status = gui.send_ui_action('status')
    except (OSError, ValueError):
        return False
    version = read_current_version(gui.resource_path('VERSION'))
    session = current_session_id()
    return (status.get('ok') is True and bool(version) and status.get('app_version') == version
            and session is not None and status.get('session_id') == session
            and signal_startup_ready(event))


def set_desktop_app_id():
    """Group source and packaged windows under the installed app identity on Windows."""
    try:
        function = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        function.argtypes = [ctypes.c_wchar_p]
        function.restype = ctypes.c_long
        return function('youziauth') == 0
    except (AttributeError, OSError):
        return False


class DesktopRuntime:
    def __init__(self, bridge, window, preview=False):
        self.bridge, self.window, self.preview = bridge, window, preview
        self.events = queue.Queue()
        self.closed = threading.Event()
        self.exiting = False
        self.tray = None
        self._app_version = read_current_version(gui.resource_path('VERSION'))
        self._session_id = current_session_id()
        self.bridge._window_action = self.events.put
        self.bridge._ui_dispatch = self._on_ui

    def _on_ui(self, callback):
        """RequestAccessAsync must originate on the foreground WinForms UI thread."""
        from System import Action
        done = threading.Event()
        result = {}
        def invoke():
            try:
                self.window.native.Activate()
                result['value'] = callback()
            except Exception as exc:
                result['error'] = exc
            finally:
                done.set()
        self.window.native.BeginInvoke(Action(invoke))
        if not done.wait(10):
            raise TimeoutError('UI permission request did not start')
        if 'error' in result:
            raise result['error']
        return result['value']

    def closing(self):
        if self.exiting:
            return True
        self.events.put('hide')
        return False

    def _show(self, page='overview'):
        self.window.show()
        self.window.restore()
        self.window.evaluate_js('window.desktopNavigate(' + json.dumps(page) + ')')

    def _handle(self, action):
        if action == 'quit':
            self.exiting = True
            self.bridge._close()
            if self.tray:
                self.tray.stop()
                self.tray = None
            self.closed.set()
            self.window.destroy()
        elif action == 'hide':
            if self.tray:
                self.window.hide()
            else:
                self.window.minimize()
        elif action in ('show', 'settings', 'dorm'):
            self._show({'settings':'network','dorm':'dorm'}.get(action,'overview'))
        elif action in ('check', 'retry'):
            if action == 'retry' and not self.preview:
                self.bridge._notification_tracker.mark_retry(self.bridge._last_agent)
            result = self.bridge.dispatch('network_check')
            if not result['ok']:
                self._show('network')
        elif action == 'suppress' and not self.preview:
            self.bridge._agent_command('suppress-notifications-for-boot')

    def _receive_ui_command(self, command):
        if command.command == 'status':
            active = not self.closed.is_set()
            return {'ok': active, 'app_version': self._app_version, 'session_id': self._session_id,
                    'tray_ready': self.tray is not None and active}
        self.events.put(command.command)
        return {'ok': True}

    def _ipc(self):
        server = agent_ipc.NamedPipeServer(gui.UI_PIPE_NAME, self._receive_ui_command,
                                           command_parser=agent_ipc.UiCommand.parse)
        while not self.closed.is_set():
            try:
                server.serve_once()
            except Exception:
                self.closed.wait(1)

    def run(self, startup=False, secondary=None, startup_event=None):
        self.window.events.loaded.wait()
        if not startup:
            self.window.show()
            self.window.restore()
        tray = windows_tray.WindowsTrayIcon(
            on_command=self.events.put, status=windows_tray.TrayStatus.STOPPED,
            detail='演示预览' if self.preview else '就绪',
            icon_paths=windows_tray.default_icon_paths(gui.resource_path('assets')),
        )
        if tray.start():
            self.tray = tray
        elif startup:
            self.window.show()
            self.window.minimize()
        if not self.preview:
            self.bridge._start_updates()
            threading.Thread(target=self._ipc, daemon=True, name='youziauth-desktop-ipc').start()
            if startup and not self.bridge._agent:
                self.bridge.dispatch('network_start')
        if secondary:
            self.events.put(secondary)
        if startup_event and not self.closed.is_set():
            signal_startup_ready(startup_event)
        while not self.closed.is_set():
            try:
                action = self.events.get(timeout=1)
                self._handle(action)
            except queue.Empty:
                pass
            except Exception:
                # A failed tray/IPC action must not stop background scheduling.
                if not self.preview:
                    self.bridge._state, self.bridge._message = 'error', '操作未完成，请打开设置检查'
            if self.closed.is_set():
                break
            try:
                self.bridge._tick()
                if self.tray:
                    if self.preview:
                        n = self.bridge.snapshot()['network']
                        status, message = n['state'], '演示预览 · ' + n['message']
                    else:
                        status, message = self.bridge._state, self.bridge._message
                    self.tray.update(windows_tray.TrayStatus(status), message)
            except Exception:
                if not self.preview:
                    self.bridge._state, self.bridge._message = 'error', '后台状态读取失败，请检查配置'


def preview_server(port):
    """Serve only bundled UI and in-memory demo data. Never instantiate DesktopBridge."""
    bridge = PreviewBridge()
    lock = threading.Lock()

    class Handler(SimpleHTTPRequestHandler):
        def _json(self, value, status=200):
            data = json.dumps(value, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == '/api/state':
                with lock:
                    return self._json(bridge.snapshot())
            if self.path == '/api/simulation-map':
                with lock:
                    return self._json(bridge.simulation_map())
            return super().do_GET()

        def do_POST(self):
            if self.path != '/api/action':
                return self._json({'ok':False},404)
            origin = self.headers.get('Origin')
            expected = f'http://127.0.0.1:{self.server.server_port}'
            if origin and origin != expected:
                return self._json({'ok':False},403)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16000:
                    raise ValueError()
                data = json.loads(self.rfile.read(length))
                with lock:
                    self._json(bridge.dispatch(data['action'],data.get('payload')))
            except (ValueError, KeyError, TypeError):
                self._json({'ok':False,'message':'无效的预览请求'},400)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1',port),partial(Handler,directory=str(gui.resource_path('desktop_ui'))))
    print(f'Isolated preview: http://127.0.0.1:{server.server_port}/?preview=1',flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ['--proxy-guard']:
        # 采集代理看门狗: 必须以自身 exe 再起一个独立进程。必须先于任何界面/加密
        # 组件初始化——主程序崩溃时, 这条路径是唯一还会执行的东西。
        import proxy_guard
        return proxy_guard.main(argv[1:])
    if argv[:1] == ['--recover-proxy']:
        # 支持/修复入口: 终端里跑一次, 把上次采集遗留的系统代理还原掉。
        import proxy_guard
        root = (Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local')))
                / 'youziauth' / 'lptiyu' / 'capture')
        restored = proxy_guard.recover_stale_backup(root / proxy_guard.BACKUP_NAME)
        print('restored the system proxy' if restored else 'no leftover capture proxy')
        return 0 if restored else 1
    if '--verify-update' in argv:
        # Update verifier. This must dispatch before any GUI setup: a windowed
        # build has no console, the caller reads only the exit code, and a
        # malformed invocation must fail fast instead of opening a window.
        from windows_update import verifier_main
        if len(argv) == 2 and argv[0] == '--verify-update':
            request = Path(argv[1])
            return verifier_main(request, request.with_name('report.json'))
        return 21  # unusable request; the caller reports a generic verification failure
    if len(argv) == 2 and argv[0] == '--location-self-test':
        # Explicit diagnostic: real local positioning only; no controller/school API.
        from dorm_location import probe_location
        result = probe_location()
        output = Path(argv[1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return 0 if result['state'] == 'ready' else 2
    if '--preview-server' in argv:
        parser = argparse.ArgumentParser()
        parser.add_argument('--preview-server',action='store_true')
        parser.add_argument('--port',type=int,default=8766)
        args = parser.parse_args(argv)
        preview_server(args.port)
        return 0
    location_diagnostic = '--location-diagnostic' in argv
    preview = '--preview' in argv or location_diagnostic
    set_desktop_app_id()
    # Keep Windows task/toast activation and elevated helper CLI compatibility.
    for argument in argv:
        if argument.startswith('--configure-system-startup='):
            return gui._run_elevated_startup_configuration(argument.split('=',1)[1])
    if len(argv)==2 and argv[0] in ('--dorm-self-test','--dorm-login-probe'):
        from dorm_selftest import run
        return run(argv[1], online_login=argv[0]=='--dorm-login-probe')
    try:
        import webview
    except ImportError:
        raise RuntimeError('缺少桌面组件。请先运行 python -m pip install -r requirements-desktop.txt') from None
    startup = gui.TRAY_STARTUP_ARGUMENT in argv or gui.LEGACY_STARTUP_ARGUMENT in argv
    startup_event = None
    if _STARTUP_EVENT_FLAG in argv:
        index = argv.index(_STARTUP_EVENT_FLAG) + 1
        if not startup or index >= len(argv) or _STARTUP_EVENT_NAME.fullmatch(argv[index]) is None:
            return 2
        startup_event = argv[index]
    secondary = gui.resolve_secondary_action(argv)
    hidden = gui.should_start_hidden(startup, secondary)
    instance = None
    if not preview:
        instance = gui.SingleInstanceLock()
        code = gui.handle_single_instance_startup(startup,instance,secondary_action=secondary)
        if code is not None:
            if code == 0 and startup_event and not _acknowledge_existing_desktop(startup_event):
                return 1
            return code
    bridge = None
    runtime = None
    try:
        bridge = PreviewBridge(real_location=location_diagnostic) if preview else DesktopBridge(startup_mode=startup)
        screen = webview.screens[0]
        window = webview.create_window(
            'youziauth · 校园日常' + (' · 定位诊断' if location_diagnostic else ' · 演示预览' if preview else ''),
            url=str(gui.resource_path('desktop_ui/index.html')), js_api=bridge,
            width=min(1160, screen.width - 60),height=min(820, screen.height - 100),
            min_size=(min(820, screen.width - 60), min(620, screen.height - 100)),hidden=hidden,
            background_color='#f4f2ec',text_select=True,
        )
        runtime = DesktopRuntime(bridge,window,preview)
        window.events.closing += runtime.closing
        window.events.closed += runtime.closed.set
        webview.start(runtime.run,(hidden,secondary,startup_event),gui='edgechromium',
                      icon=str(gui.resource_path('assets/yuzu_app.ico')))
        return 0
    finally:
        if runtime:
            runtime.closed.set()
            if runtime.tray:
                runtime.tray.stop()
        if bridge:
            bridge._close()
        if instance:
            instance.release()


if __name__ == '__main__':
    raise SystemExit(main())
