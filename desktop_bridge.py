"""Narrow desktop UI bridge. School/network operations remain in their existing engines."""
from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import math
import os
import sys
import threading
import time
from pathlib import Path

from app_update import UpdateController, read_current_version

import agent_ipc
import campus_auth
import campus_auth_gui as gui
import dorm_points
import proxy_rules
import windows_notifications
from dorm_accounts import MAX_ACCOUNTS, AccountError, DormAccounts
from dorm_checkin import Settings, now
from dorm_location import (PICK_SOURCE, distance_metres, gcj02_to_wgs84, map_pick_sample,
                           probe_location, radius_metres)
# 默认控制器类型由 dorm_accounts 构造；这里保留导入是因为测试会用它断言
# 「演示预览不得创建真实控制器」，并且它仍是本模块对外的控制器符号。
from dorm_panel import DormController  # noqa: F401


# The picker draws a real map, so tiles come from a third party. OpenStreetMap stays first because
# it needs no key and its licence is clear, but its tile servers are run by volunteers who block
# app-like traffic - measured here as HTTP 200 plus an "Access blocked" PNG and an `x-blocked`
# response header - and they are frequently unreachable from mainland China. So one keyless
# fallback sits behind it: 高德 answers the same z/x/y grid, and the UI applies the GCJ02 offset
# itself, so a campus still lands in the right place. With both gone the picker falls back to the
# offline grid, and everything except the tiles (grid, radius, markers, distance) keeps working.
TILE_PROVIDERS = (
    {'id': 'osm', 'name': 'OpenStreetMap', 'crs': 'wgs84', 'max_zoom': 19,
     'url': 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
     'attribution': '© OpenStreetMap contributors'},
    {'id': 'amap', 'name': '高德地图', 'crs': 'gcj02', 'max_zoom': 18,
     'url': 'https://webrd01.is.autonavi.com/appmaptile'
            '?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',
     'attribution': '© 高德地图'},
)
# The preferred provider stays readable under the names the bridge used before it had a fallback.
TILE_URL = TILE_PROVIDERS[0]['url']
TILE_ATTRIBUTION = TILE_PROVIDERS[0]['attribution']
TILE_MAX_ZOOM = TILE_PROVIDERS[0]['max_zoom']
SIMULATION_SAMPLE = 'location-sample.json'
# Reading the proxy's config means parsing the generated config too, so it is
# cached. The UI polls the snapshot often and the answer changes only when the
# user edits their proxy.
PROXY_REPORT_TTL_SECONDS = 60


def tile_providers():
    """A fresh copy per call: the UI must never be able to mutate the module's own table."""
    return [dict(provider) for provider in TILE_PROVIDERS]


def network_settings(payload, previous):
    username = str(payload.get('username', '')).strip()
    if not username or username == 'YOUR_STUDENT_ID':
        raise ValueError('请填写你的校园网账号')
    interval = int(payload.get('interval', 60))
    if not 5 <= interval <= 3600:
        raise ValueError('检测间隔应为 5–3600 秒')
    if type(payload.get('startup', False)) is not bool:
        raise ValueError('开机自启动设置无效')
    return gui.GuiSettings(username, str(payload.get('password', '')), interval, previous.log_file)


def dorm_settings(payload):
    return Settings(payload.get('enabled', False), str(payload.get('start', '')),
                    str(payload.get('end', '')), int(payload.get('interval', 300)),
                    payload.get('location_source', 'windows')).validate()


def sample_point(sample):
    """Map-ready view of a stored simulation sample; None when it cannot be read."""
    if not isinstance(sample, dict):
        return None
    try:
        latitude, longitude = float(sample['latitude']), float(sample['longitude'])
        accuracy = float(sample.get('accuracy', 0.0))
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        return None
    source = str(sample.get('source') or '')
    return {'latitude': latitude, 'longitude': longitude, 'accuracy': accuracy,
            'source': source, 'picked': source == PICK_SOURCE}


def reference_point(task):
    """The school's own check-in point, converted from its GCJ02 frame into WGS84 for the map."""
    if task is None or not getattr(task, 'latitude', '') or not getattr(task, 'longitude', ''):
        return None
    try:
        latitude, longitude = gcj02_to_wgs84(float(task.latitude), float(task.longitude))
    except (TypeError, ValueError):
        return None
    return {'latitude': latitude, 'longitude': longitude, 'address': task.address,
            'radius_m': radius_metres(task.radius)}


def range_check(point, reference):
    """Distance to the school's point and whether it is inside the radius.

    Both sides are WGS84 here; the GCJ02 offset is very nearly constant across a campus, so the
    distance is within a metre of the frame the school measures in - and the school's own verify
    call remains the authority.
    """
    if not point or not reference:
        return None, None
    distance = distance_metres(point['latitude'], point['longitude'],
                               reference['latitude'], reference['longitude'])
    limit = reference.get('radius_m')
    return distance, (None if not limit else distance <= limit)


def saved_point_message(point, reference):
    return '模拟定位点已保存：' + _where_message(point, reference)


def _where_message(point, reference):
    """Distance wording shared by every action that makes a point active."""
    if reference is None:
        return '先查询今日任务，即可核对与学校基准点的距离。本次未提交打卡。'
    distance, in_range = range_check(point, reference)
    where = f'距学校基准点约 {round(distance)} 米'
    if in_range is False:
        return f'{where}，超出 {reference["radius_m"]:g} 米打卡范围，学校可能拒绝。本次未提交打卡。'
    return f'{where}，在打卡范围内。本次未提交打卡。'


def _point_sample(point):
    """A named point replayed as a sample: identical shape and source label to a fresh pick."""
    return {'latitude': float(point['latitude']), 'longitude': float(point['longitude']),
            'accuracy': float(point.get('accuracy', 100.0)), 'timestamp': time.time(),
            'source': point.get('source') or PICK_SOURCE}


def _same_position(sample, point):
    """Same spot within ~0.1 m, which is all the reconciliation needs to compare files."""
    if not sample or not point:
        return False
    return (abs(sample['latitude'] - float(point['latitude'])) < 1e-6
            and abs(sample['longitude'] - float(point['longitude'])) < 1e-6)


def _read_json(store, name):
    """Read one store file as a location sample; anything unreadable is treated as absent."""
    try:
        value = store._read(name, None)
    except (OSError, ValueError):
        return None
    return sample_point(value)


class LocationProbe:
    def _init_location(self):
        self._ui_dispatch = None
        self._location_gate = threading.Lock()
        self._location_worker = None
        self._reset_location('windows')

    def _reset_location(self, source):
        message = ('使用本机已保存的模拟定位样本，并非当前位置；检测不会提交打卡。'
                   if source == 'simulation' else '点击下方按钮请求系统授权并检测实时定位，不会提交打卡。')
        self._location_state = {'state':'idle', 'message':message, 'source':source,
                                'accuracy':None, 'checked':''}

    def _location_snapshot(self, source):
        if self._location_state.get('source') != source:
            self._reset_location(source)
        return dict(self._location_state, busy=self._location_gate.locked())

    def _start_location_probe(self, *, source='windows', sample_path=None, label=''):
        if source == 'windows' and self._ui_dispatch is None:
            raise RuntimeError('请在桌面主窗口中使用定位授权功能。')
        if not self._location_gate.acquire(blocking=False):
            raise RuntimeError('正在等待授权或定位结果，请稍候。')
        message = (f'正在读取选点「{label}」的位置，不会读取实时位置或提交打卡。'
                   if source == 'simulation' and label else
                   '正在读取本机模拟定位样本，不会读取实时位置或提交打卡。' if source == 'simulation' else
                   '请在系统提示中选择允许，随后等待实时定位（约 30 秒内）。')
        self._location_state = {'state':'checking','message':message, 'source':source,
                                'accuracy':None,'checked':''}
        def work():
            try:
                self._location_state = dict(probe_location(ui_dispatch=self._ui_dispatch, source=source,
                                                           sample_path=sample_path, label=label),
                                            source=source)
            except Exception:
                self._location_state = {'state':'error','message':'定位检测未完成，请检查所选来源后重试。',
                                        'source':source,'accuracy':None,'checked':''}
            finally:
                self._location_gate.release()
        self._location_worker = threading.Thread(target=work,daemon=True,name='youziauth-location-probe')
        self._location_worker.start()
        return '正在检测所选定位来源；本次不会提交打卡。'


class DesktopBridge(LocationProbe):
    def __init__(self, config_path=None, dorm=None, startup_mode=False, accounts=None):
        self._init_location()
        self._config = gui.ensure_user_config(config_path or gui.DEFAULT_CONFIG_PATH)
        # 多账号：正式运行时由账号层提供「当前活动账号」的控制器（同一时刻只有一个活着，
        # 只有它在自动打卡）。注入 dorm（测试与旧调用点）时不叠账号层，直接用注入的控制器。
        self._dorm_override = dorm
        self._accounts = accounts
        if self._accounts is None and self._dorm_override is None:
            self._accounts = DormAccounts()
        self._lock = threading.RLock()
        self._mutation = threading.Lock()
        self._network_gate = threading.Lock()
        self._stop = threading.Event()
        self._closed = threading.Event()
        self._worker = None
        self._monitoring = False
        self._startup_mode = startup_mode
        self._agent = gui.is_startup_enabled()
        self._state = 'stopped'
        self._message = '等待检测校园网连接'
        self._checked = ''
        self._notice = None
        self._notification_tracker = windows_notifications.NotificationTracker()
        self._last_agent = None
        self._proxy_cache = None
        self._window_action = lambda action: None
        self._updates = UpdateController(read_current_version(gui.resource_path('VERSION')),
                                         self._config.parent / 'updates',
                                         Path(sys.executable) if getattr(sys, 'frozen', False) else None)

    def _start_updates(self):
        try:
            self._updates.check()
        except RuntimeError:
            pass

    @property
    def _dorm(self):
        """当前活动账号的控制器。

        账号档案不可用时这里会抛 AccountError —— 调用方分别处置：轮询与退出安静跳过
        （错误由快照呈现），界面操作用户会看到具体原因。绝不退回某个隐式默认账号。
        """
        if self._accounts is not None:
            return self._accounts.controller
        return self._dorm_override

    def _dorm_busy(self) -> bool:
        """是否有账号正在打卡（多账号下「忙」不再只等于当前账号忙）。"""
        if self._accounts is not None:
            return self._accounts.any_busy()
        return bool(self._dorm.busy)

    def _accounts_snapshot(self):
        """账号列表：只给界面「谁、叫什么、当前用哪个、今天什么状态」，不含任何路径。"""
        empty = {'error': '', 'max': 0, 'active': '', 'items': [], 'busy': False}
        if self._accounts is None:
            return empty
        block = self._accounts.snapshot()
        for item in block['items']:
            try:
                status = self._idm_credential_status(self._accounts.idm_store(item['id']))
            except AccountError:
                status = {'has_idm_credentials': False, 'idm_username': ''}
            item.update(status)
        return block

    def _idm_status_for_active(self):
        """当前账号的统一认证凭据状态。

        账号层读不出来时只降级这一小块：**绝不能让整个快照失败** —— 那会让前端
        显示「界面暂时无法连接后台」，把「账号文件坏了」误报成「后台连不上」。
        """
        try:
            store = self._active_idm_store() if self._accounts is not None else None
        except AccountError:
            return {'has_idm_credentials': False, 'idm_username': ''}
        return self._idm_credential_status(store)

    def _dorm_has_session(self):
        """学校登录态：本机是否保存着学校会话。

        只用 store.has_session()（看密文文件在不在），**绝不调用 store.token() 去解密** ——
        快照是只读的，会话令牌属于「绝不进快照」的那一类秘密，这条边界有测试守着
        （test_snapshot_excludes_secrets）。宁可老实说「未登录」（代价是多点一次登录），
        也不能为了把状态说准而把令牌读进内存。

        只回答「有没有」，不校验会话是否仍然有效 —— 有效性由一次真实查询确认。
        这个方向的误报是安全的：把「需要重新登录」说成「已登录」才会让人对着一个
        点了没反应的按钮发呆。任何异常都降级为「没有」，不让整页快照失败。
        """
        try:
            return bool(self._dorm.store.has_session())
        except Exception:  # noqa: BLE001
            return False

    def _idm_credential_status(self, store=None):
        """统一认证凭据状态：仅返回「是否已保存」与学号，绝不回传密码。

        任何异常都降级为「未保存」——凭据读取失败不应让整个界面快照出错。
        store 为空时读全局默认目录（注入控制器的旧调用点）；
        多账号下由调用方传入该账号自己的存储，避免把别人的学号显示给当前账号。
        """
        try:
            if store is None:
                from idm_credentials import IdmCredentialStore  # noqa: PLC0415

                store = IdmCredentialStore()
            if not store.exists():
                return {'has_idm_credentials': False, 'idm_username': ''}
            creds = store.load()
            return {'has_idm_credentials': True,
                    'idm_username': (creds.username if creds else '')}
        except Exception:  # noqa: BLE001
            return {'has_idm_credentials': False, 'idm_username': ''}

    def _active_idm_store(self, account_id=None):
        """当前（或指定）账号的统一认证凭据存储；账号层缺席时退回全局目录。"""
        if self._accounts is not None:
            return self._accounts.idm_store(account_id)
        from idm_credentials import IdmCredentialStore  # noqa: PLC0415

        return IdmCredentialStore()

    def _proxy_report(self, force=False):
        """Cached: does the local proxy route the Windows probe hosts DIRECT?

        On a machine whose traffic goes through a proxy, those two hostnames
        decide both Windows' captive-portal verdict and whether this app can tell
        a healthy uplink from a broken one. When the rules are absent we can offer
        to add them -- but only ever with the user's agreement, and only from the
        UI process, because the config lives in the user's own AppData.
        """
        now = time.monotonic()
        with self._lock:
            cached = self._proxy_cache
            if not force and cached is not None and now - cached[0] < PROXY_REPORT_TTL_SECONDS:
                return cached[1]
        try:
            report = proxy_rules.inspect()
        except Exception:  # noqa: BLE001 - a broken proxy config must not break the panel.
            report = proxy_rules.ProxyRuleReport(blocked_by='代理配置无法读取')
        with self._lock:
            self._proxy_cache = (now, report)
        return report

    def _proxy_snapshot(self):
        report = self._proxy_report()
        return {
            'found': report.found,
            'client': report.client,
            'file': str(report.rules_file) if report.rules_file else '',
            'missing': list(report.missing_hosts),
            'pending': list(report.pending_hosts),
            'ok': report.ok,
            'blocked_by': report.blocked_by,
            'message': proxy_rules.describe(report),
            # Only worth offering when there is something we can actually write.
            'can_apply': report.needs_rule,
            'needs_reload': report.needs_reload,
            'rules': list(report.existing_rules),
        }

    def snapshot(self):
        """Read only. Never expose passwords, tokens, coordinates or raw school payloads."""
        with self._lock:
            settings = gui.load_gui_settings(self._config)
            # 边界隔离：某个设置文件读不动时，只降级「寝室打卡」区块，
            # **绝不让整个快照失败**。否则前端 refresh() 的 catch 会显示
            # 「界面暂时无法连接后台」，把「一个设置文件坏了」误报成「后台连不上」，
            # 让人完全找不到方向（实测踩过：一个 BOM 就让整个面板断连）。
            # 注意这不改变 fail-closed 语义：Engine 仍会因读不动配置而拒绝执行。
            settings_error = ''
            bus = False
            result = None
            try:
                controller = self._dorm
                ds = controller.store.settings()
                location_source = ds.location_source
                dorm_settings = dataclasses.asdict(ds)
                schedule = controller.schedule_text()
                bus = bool(controller.busy)
                result = controller.latest
            except AccountError as exc:
                # 账号档案出了问题：说的是「哪个文件怎么了」，不是含糊的「设置读不动」。
                fallback = Settings()
                location_source = fallback.location_source
                dorm_settings = dataclasses.asdict(fallback)
                schedule = settings_error = str(exc)
            except Exception:  # noqa: BLE001
                fallback = Settings()
                location_source = fallback.location_source
                dorm_settings = dataclasses.asdict(fallback)
                schedule = '打卡设置无法读取，请打开寝室打卡设置重新保存'
                settings_error = schedule
            task = result.task if result is not None else None
            try:
                dorm_log = self._dorm.store.history()
            except Exception:  # noqa: BLE001
                dorm_log = ''
            accounts_error = self._accounts.error if self._accounts is not None else ''
            return {
                'preview': False,
                'update': self._updates.snapshot(),
                'location': self._location_snapshot(location_source),
                'accounts': self._accounts_snapshot(),
                'network': {
                    'username': '' if settings.username == 'YOUR_STUDENT_ID' else settings.username,
                    'interval': settings.check_interval_seconds, 'startup': self._agent,
                    'monitoring': self._monitoring or self._agent,
                    'busy': self._network_gate.locked(), 'state': self._state,
                    'message': self._message, 'checked': self._checked,
                    'has_password': (self._config.parent / 'credential.dat').exists(),
                },
                'dorm': {
                    'state': result.state if result is not None else 'error',
                    'message': accounts_error or settings_error
                               or (result.message if result is not None else '打卡状态无法读取'),
                    'busy': bus, 'settings': dorm_settings,
                    'schedule': schedule,
                    'task': ({k: getattr(task, k) for k in
                              ('title', 'date', 'start', 'end', 'address', 'signed')} if task else None),
                    # 学校登录态：只暴露「本机有没有会话」，绝不回传会话内容本身。
                    'has_session': self._dorm_has_session(),
                    # 只暴露「是否已保存」与学号，绝不回传密码；多账号下读的是当前账号那一份。
                    **self._idm_status_for_active(),
                },
                'logs': {
                    'network': gui.tail_log(gui.resolve_log_path(self._config, settings.log_file)),
                    'dorm': dorm_log,
                },
                'proxy': self._proxy_snapshot(),
            }

    def dispatch(self, action, payload=None):
        if not isinstance(payload, (dict, type(None))):
            return {'ok': False, 'message': '设置格式无效'}
        if self._closed.is_set():
            return {'ok': False, 'message': '程序正在退出'}
        with self._mutation:
            try:
                message = self._dispatch(action, payload or {})
                return {'ok': True, 'message': message or '操作已完成'}
            except (ValueError, TypeError):
                return {'ok': False, 'message': '请检查填写内容：账号不能为空，时间须为 HH:MM，间隔须在允许范围内。'}
            except RuntimeError as exc:
                return {'ok': False, 'message': str(exc)}
            except Exception:
                # Do not send raw OS/network exceptions across the JS bridge.
                return {'ok': False, 'message': '操作未完成，请检查本机权限、配置和网络后重试。'}

    def _account_action(self, action, payload):
        """账号管理：新建 / 切换 / 重命名 / 删除。

        第二阶段每个账号都有自己的控制器在后台跑，所以「切换」只是换界面在看哪个账号：
        不再需要等打卡结束，也不再交接控制器 —— 正在进行的打卡留在后台跑完（或由
        「取消当前操作」取消）。只有「删除正在打卡的那个账号」会被账号层拒绝。
        界面上「删除」与后端一样要显式确认，前端弹窗只是第一道，这里是第二道。
        """
        if self._accounts is None:
            raise RuntimeError('当前运行方式不支持多账号。')
        if self._location_gate.locked():
            raise RuntimeError('请等待定位检测完成后再管理账号。')
        if action == 'account_add':
            return self._after_account_change(self._accounts.add(payload.get('name')))
        if action == 'account_switch':
            return self._after_account_change(
                self._accounts.switch(str(payload.get('id') or '')))
        if action == 'account_rename':
            return self._accounts.rename(str(payload.get('id') or ''), payload.get('name'))
        if payload.get('confirmed') is not True:
            raise RuntimeError('请先确认删除该账号及其在本机的全部数据。')
        return self._after_account_change(
            self._accounts.remove(str(payload.get('id') or '')))

    def _after_account_change(self, message):
        """换人之后：定位状态与通知去重键都跟人走，否则界面会沿用上一个账号的判定。"""
        self._notice = None
        try:
            source = self._dorm.store.settings().location_source
        except Exception:  # noqa: BLE001 - 新账号设置读不动时，定位状态回到默认展示
            source = 'windows'
        self._reset_location(source)
        return message

    def _dispatch(self, action, payload):
        if action == 'update_check':
            return self._updates.check()
        if action == 'update_install':
            if self._dorm_busy() or self._location_gate.locked() or self._network_gate.locked():
                raise RuntimeError('请先停止本地后台检测，并等待当前网络、打卡或定位操作完成后，再确认安装更新。')
            return self._updates.install(payload.get('confirmed'), payload.get('version'))
        if action in ('account_add', 'account_switch', 'account_rename', 'account_delete'):
            return self._account_action(action, payload)
        if action == 'location_authorize':
            source = self._dorm.store.settings().location_source
            return self._start_location_probe(source=source,
                                              sample_path=self._dorm.store.root / 'location-sample.json',
                                              label=self._active_point_label() if source == 'simulation' else '')
        if action == 'network_save':
            settings = network_settings(payload, gui.load_gui_settings(self._config))
            startup = payload.get('startup', False)
            if startup != self._agent and (self._monitoring or self._network_gate.locked()):
                raise RuntimeError('请先停止本地后台检测并等待当前检测完成，再更改开机自启动。')
            gui.save_gui_settings(self._config, settings)
            if startup != self._agent:
                gui.set_startup_enabled(startup)
                self._agent = gui.is_startup_enabled()
                if self._agent != startup:
                    raise RuntimeError('账号已保存；开机自启动未变更，请完成 Windows 管理员授权后重试。')
            if self._agent:
                self._agent_command('reload-config')
            return '校园网设置已保存'
        if action == 'proxy_rules_check':
            return proxy_rules.describe(self._proxy_report(force=True))
        if action == 'proxy_rules_apply':
            # Only ever reached because the user pressed the button. The write
            # happens here, in the UI process, so the file keeps belonging to the
            # user rather than to the SYSTEM agent.
            report = self._proxy_report(force=True)
            if report.blocked_by:
                raise RuntimeError(report.blocked_by)
            if not report.needs_rule:
                return proxy_rules.describe(report)
            written = proxy_rules.apply(report)
            after = self._proxy_report(force=True)
            if after.needs_reload:
                return (f'已写入 {written.name}，并备份了原文件；'
                        '请到 Clash Verge 重新加载配置后才会生效')
            return f'已写入 {written.name}，并备份了原文件；规则已生效'
        if action in ('network_check', 'network_start'):
            if self._agent:
                self._agent_command('retry' if action == 'network_check' else 'reload-config')
                return '已请求系统认证代理执行'
            if self._monitoring or not self._network_gate.acquire(blocking=False):
                raise RuntimeError('校园网检测正在运行，请等待完成或先停止后台检测。')
            try:
                config = gui.build_auth_config(self._config)
                if not config.username or config.username == 'YOUR_STUDENT_ID':
                    raise ValueError('请先保存校园网账号')
            except Exception:
                self._network_gate.release()
                raise
            self._stop.clear()
            self._monitoring = action == 'network_start'
            self._state, self._message = 'checking', '正在检测校园网连接…'
            self._worker = threading.Thread(target=self._network_work,
                                            args=(config, self._monitoring), daemon=True)
            self._worker.start()
            return '已开始后台检测' if self._monitoring else '正在检测，请稍候'
        if action == 'network_stop':
            if self._agent:
                raise RuntimeError('系统代理由开机自启动管理；如需关闭，请取消开机自启动并保存。')
            self._stop.set()
            return '正在停止，当前网络请求结束后生效'
        if action in ('dorm_login', 'dorm_query', 'dorm_submit', 'dorm_logout'):
            if not self._dorm.start(action.removeprefix('dorm_')):
                raise RuntimeError('打卡操作正在进行，请等待完成或取消当前操作。')
            return '操作已开始，请查看任务状态'
        if action == 'dorm_cancel':
            # 多账号：每个账号都有自己的控制器在跑，「取消当前操作」必须能取消所有账号里
            # 正在进行的那一个，否则用户看着 A 的页面就取消不了 B 的操作。
            if self._accounts is not None:
                self._accounts.cancel_all()
            else:
                self._dorm.cancel()
            return '已请求取消所有账号正在进行的操作；已发出的提交不能撤回，自动打卡设置不会改变。'
        if action == 'dorm_save':
            if self._dorm.busy or self._location_gate.locked():
                raise RuntimeError('请等待当前打卡或定位检测完成后再保存设置。')
            settings = dorm_settings(payload)
            self._dorm.save(settings)
            if settings.location_source != self._location_state.get('source'):
                self._reset_location(settings.location_source)
            return '定位来源与自动打卡设置已保存'
        if action == 'location_source_save':
            return self._save_location_source(payload.get('location_source', ''))
        if action == 'simulation_point_save':
            return self._save_simulation_point(payload)
        if action == 'simulation_point_select':
            return self._select_simulation_point(payload)
        if action == 'simulation_point_rename':
            return self._rename_simulation_point(payload)
        if action == 'simulation_point_delete':
            return self._delete_simulation_point(payload)
        if action == 'location_settings':
            os.startfile('ms-settings:privacy-location')
            return '已打开 Windows 定位设置'
        if action == 'idm_credentials_save':
            username = str(payload.get('idm_username') or '').strip()
            password = str(payload.get('idm_password') or '')
            try:
                from idm_credentials import IdmCredentials  # noqa: PLC0415

                # 存进当前账号自己的档案目录：多账号下学号密码绝不能互相串。
                self._active_idm_store().save(
                    IdmCredentials(username=username, password=password).validate())
            except ValueError as exc:
                raise RuntimeError(str(exc)) from None
            return '统一认证凭据已加密保存，登录时将自动填写并识别验证码'
        if action == 'idm_credentials_clear':
            try:
                self._active_idm_store().clear()
            except Exception:  # noqa: BLE001
                raise RuntimeError('统一认证凭据清除失败，请检查本机权限后重试') from None
            return '统一认证凭据已清除，登录将需要人工输入账号密码和验证码'
        if action in ('hide', 'quit'):
            self._window_action(action)
            return '已隐藏到托盘' if action == 'hide' else '正在退出'
        raise RuntimeError('不支持的操作')

    def _save_location_source(self, source):
        """Change only the location source; the schedule stays exactly as saved."""
        if source not in ('windows', 'simulation'):
            raise ValueError('定位来源无效')
        if self._dorm.busy or self._location_gate.locked():
            raise RuntimeError('请等待当前打卡或定位检测完成后再切换定位来源。')
        return self._apply_location_source(source)

    def _apply_location_source(self, source):
        current = self._dorm.store.settings()
        if current.location_source != source:
            self._dorm.save(dataclasses.replace(current, location_source=source))
            self._reset_location(source)
        return ('定位来源已切换为模拟定位（非实时）' if source == 'simulation'
                else '定位来源已切换为真实定位（Windows / Wi-Fi）')

    def simulation_map(self):
        """Model for the map picker: saved points, the active one and the school's reference.

        Deliberately the one place that reports coordinates to the UI, and only while the saved
        source is 模拟定位: these are points the user chooses to submit, never a live Windows fix.
        """
        store = self._dorm.store
        if store.settings().location_source != 'simulation':
            return {'ok': False, 'message': '请先把定位来源保存为模拟定位，再使用地图选点。'}
        state = self._sync_simulation_points(store)
        point = sample_point(store.sample())
        reference = reference_point(self._dorm.latest.task)
        distance, in_range = range_check(point, reference)
        return {'ok': True, 'point': point, 'reference': reference,
                'distance_m': distance, 'in_range': in_range,
                'points': [dict(entry, active=entry['id'] == state['active']) for entry in state['points']],
                'active_id': state['active'],
                'tile_url': TILE_URL, 'attribution': TILE_ATTRIBUTION, 'max_zoom': TILE_MAX_ZOOM,
                'providers': tile_providers()}

    def _sync_simulation_points(self, store):
        """Make the list and the replayed sample agree, with the list as the source of truth.

        Runs before the picker is shown and before every point action, so the two files cannot
        drift apart - which is exactly how 1.5.2 lost a point: 恢复上一个样本 rewrote the sample
        without telling the list. Positions the list does not hold yet are adopted instead of
        dropped: a captured Windows fix, the 1.5.2 undo file, or a file from any older version.
        """
        state = store.points()
        if not (store.root / 'location-points.json').exists():
            # First run for this store: ship the built-in starter positions instead of an empty list.
            state = dorm_points.seeded(now().isoformat(timespec='seconds'))
            store.save_points(state)
        adopted = []
        for sample, label in ((_read_json(store, 'location-sample.previous.json'),
                               dorm_points.PREVIOUS_SAMPLE_NAME),
                              (sample_point(store.sample()), dorm_points.SAMPLE_NAME)):
            if sample and dorm_points.holding(state, sample['latitude'], sample['longitude']) is None:
                state = dorm_points.add(state, name=label, source=dorm_points.SAMPLE_SOURCE,
                                        latitude=sample['latitude'], longitude=sample['longitude'],
                                        accuracy=sample['accuracy'],
                                        saved_at=now().isoformat(timespec='seconds'))
                adopted.append(label)
        # The mirror is what the program actually replays, so it wins the active flag.
        mirror = sample_point(store.sample())
        if mirror is not None:
            held = dorm_points.holding(state, mirror['latitude'], mirror['longitude'])
            if held is not None and held['id'] != state['active']:
                state = dorm_points.activate(state, held['id'])
        if not state['active'] and state['points']:
            state = dorm_points.activate(state, state['points'][0]['id'])
        if adopted or (store.points() != state):
            store.save_points(state)
        store.drop_legacy_backup()
        active = dorm_points.find(state, state['active'])
        if active is None:
            if mirror is not None:
                store.clear_sample()          # no selectable point means nothing to replay
        elif not _same_position(mirror, active):
            store.save_sample(_point_sample(active))
        return state

    def _active_point_label(self):
        """The name of the point that is currently replayed, for messages that name it."""
        state = self._sync_simulation_points(self._dorm.store)
        active = dorm_points.find(state, state['active'])
        return active['name'] if active else ''

    def _simulation_write_guard(self):
        store = self._dorm.store
        if store.settings().location_source != 'simulation':
            raise RuntimeError('请先把定位来源保存为模拟定位，再使用地图选点。')
        if self._dorm.busy or self._location_gate.locked():
            raise RuntimeError('请等待当前打卡或定位检测结束后，再修改模拟定位点。')
        return store

    def _save_simulation_point(self, payload):
        store = self._simulation_write_guard()
        self._sync_simulation_points(store)
        try:
            sample = map_pick_sample(payload.get('latitude'), payload.get('longitude'))
        except ValueError as exc:
            raise RuntimeError(str(exc)) from None
        before = store.points()
        try:
            state = dorm_points.add(before, name=payload.get('name'),
                                    latitude=sample['latitude'], longitude=sample['longitude'],
                                    accuracy=sample['accuracy'],
                                    saved_at=now().isoformat(timespec='seconds'))
        except ValueError as exc:
            raise RuntimeError(str(exc)) from None
        point = dorm_points.find(state, state['active'])
        store.save_points(state)
        store.save_sample(_point_sample(point))
        self._reset_location('simulation')
        verb = (f'已更新选点「{point["name"]}」的位置'
                if dorm_points.find(before, point['id']) else f'已新建选点「{point["name"]}」')
        return (f'{verb}：' + _where_message(sample_point(_point_sample(point)),
                                             reference_point(self._dorm.latest.task)))

    def _select_simulation_point(self, payload):
        store = self._simulation_write_guard()
        self._sync_simulation_points(store)
        try:
            state = dorm_points.activate(store.points(), str(payload.get('id') or ''))
        except ValueError as exc:
            raise RuntimeError(str(exc)) from None
        point = dorm_points.find(state, state['active'])
        store.save_points(state)
        store.save_sample(_point_sample(point))
        self._reset_location('simulation')
        return (f'已切换到选点「{point["name"]}」：'
                + _where_message(sample_point(_point_sample(point)),
                                 reference_point(self._dorm.latest.task)))

    def _rename_simulation_point(self, payload):
        store = self._simulation_write_guard()
        self._sync_simulation_points(store)
        try:
            state = dorm_points.rename(store.points(), str(payload.get('id') or ''), payload.get('name'))
        except ValueError as exc:
            raise RuntimeError(str(exc)) from None
        store.save_points(state)
        return f'选点已重命名为「{dorm_points.find(state, str(payload.get("id") or ""))["name"]}」。'

    def _delete_simulation_point(self, payload):
        store = self._simulation_write_guard()
        state = self._sync_simulation_points(store)
        point = dorm_points.find(state, str(payload.get('id') or ''))
        try:
            remaining = dorm_points.remove(state, str(payload.get('id') or ''))
        except ValueError as exc:
            raise RuntimeError(str(exc)) from None
        if remaining['points'] and not remaining['active']:
            remaining = dorm_points.activate(remaining, remaining['points'][0]['id'])
        store.save_points(remaining)
        active = dorm_points.find(remaining, remaining['active'])
        label = point['name'] if point else ''
        if active is None:
            store.clear_sample()
            self._reset_location('simulation')
            return (f'已删除选点「{label}」：模拟定位现在没有可用位置，自动打卡会因缺少位置而跳过；'
                    '请在地图上重新选点。')
        store.save_sample(_point_sample(active))
        self._reset_location('simulation')
        return (f'已删除选点「{label}」，已自动切换到「{active["name"]}」。'
                if active['id'] != (point or {}).get('id') else f'已删除选点「{label}」。')

    def _agent_command(self, command):
        agent_ipc.send_command('youziauth-agent', agent_ipc.AgentCommand(command), timeout_ms=3000)

    def _network_work(self, config, monitor):
        attempt = 0
        try:
            while not self._closed.is_set():
                logger = campus_auth.configure_logging(config.log_file, verbose=False)
                ok = campus_auth.run_once(campus_auth.CampusAuthClient(config, logger), logger)
                with self._lock:
                    self._state = 'online' if ok else 'offline'
                    self._message = '网络连接正常' if ok else '认证未成功，请检查账号与网络'
                    self._checked = dt.datetime.now().strftime('%H:%M:%S')
                if not monitor:
                    break
                delay = gui.next_monitor_delay(ok, config.check_interval_seconds, attempt, self._startup_mode)
                attempt = 0 if ok else attempt + 1
                if self._stop.wait(delay):
                    break
                config = gui.build_auth_config(self._config)
        except Exception:
            with self._lock:
                self._state, self._message = 'error', '网络检测未完成，请检查设置后重试'
        finally:
            with self._lock:
                self._monitoring = False
                if self._stop.is_set():
                    self._state, self._message = 'stopped', '后台检测已停止'
            self._network_gate.release()

    def _collect_dorm(self):
        """所有账号各跑一拍 + 通知。账号档案读不出来时安静跳过：错误由快照呈现，
        而不是每秒把同一句话抛进托盘状态里。"""
        if self._accounts is not None:
            if self._accounts.error:
                return
            try:
                events = self._accounts.poll()
            except AccountError:
                return
        else:
            try:
                self._dorm.poll()
                events = [('', result) for result in self._dorm.drain()]
            except AccountError:
                return
        for label, result in events:
            # 文案也进去重键：状态没变但文案升级了（Engine._track_transient 在连续失败
            # 第 5 次会把"该去改什么"写进去）时必须再提醒一次，否则一晚 58 次失败
            # 只会弹第一条，人根本不知道要去动代理设置。
            # 账号名同样进去重键：多账号下两个账号的失败文案可能一模一样，
            # 少了这一项，第二个账号的问题会被当成重复通知吞掉。
            notice = (label, result.at[:10], result.task.key if result.task else '',
                      result.state, result.message)
            if result.state in ('signed', 'login_required', 'location_required', 'uncertain', 'error', 'network_error') and notice != self._notice:
                self._notice = notice
                windows_notifications.show_toast(windows_notifications.build_dorm_toast(
                    f'{label}：{result.message}' if label else result.message))

    def _tick(self):
        self._collect_dorm()
        if self._agent:
            try:
                snapshot = agent_ipc.read_snapshot(self._config.parent / 'runtime.json')
                state = {'online_external':'online', 'online_campus':'online',
                         'waiting_for_network':'checking', 'auth_failed':'offline'}.get(snapshot.state, 'error')
                with self._lock:
                    self._state, self._message = state, snapshot.detail or snapshot.state
                    self._last_agent = snapshot
                toast = self._notification_tracker.evaluate(snapshot)
                if toast:
                    windows_notifications.show_toast(toast)
            except (OSError, ValueError):
                self._state, self._message = 'checking', '等待系统认证代理…'

    def _close(self):
        self._closed.set()
        self._stop.set()
        self._updates.close()
        close = self._accounts.close if self._accounts is not None else None
        try:
            if close is not None:
                close()
            else:
                self._dorm.close()
        except AccountError:
            pass  # 档案层已经不可用，退出流程照样要走完


class PreviewBridge(LocationProbe):
    """Explicit in-memory demo. No real controllers, credentials, timers or system actions."""
    def __init__(self, real_location=False):
        self._init_location()
        self._real_location = real_location
        self._window_action = lambda action: None
        self._update_started = None
        self._data = {
            'preview': True,
            'update': dict(state='idle', current_version=read_current_version(gui.resource_path('VERSION')),
                           latest_version='', progress=0, downloaded_bytes=0, total_bytes=0,
                           checked='', busy=False, message='演示预览：更新只使用虚拟数据，不联网、不下载、不安装。'),
            'network': {'username':'2026000000', 'interval':60, 'startup':False,
                        'monitoring':False, 'busy':False, 'state':'stopped',
                        'message':'尚未检测，点击即可查看连接状态', 'checked':'', 'has_password':True},
            # 演示里默认演「检测到代理缺少直连规则」，好让这条提示能被看见和点。
            'proxy': {'found':True, 'client':'clash-verge-rev', 'file':'演示/RULES01.yaml',
                      'missing':['msftconnecttest.com', 'msftncsi.com'], 'pending':[],
                      'ok':False, 'blocked_by':'', 'can_apply':True, 'needs_reload':False,
                      'rules':[], 'message':'演示：检测到代理缺少 Windows 探测域名的直连规则'},
            'dorm': {'state':'idle', 'message':'查询今日任务，开始今晚的安排', 'busy':False,
                     'settings':dataclasses.asdict(Settings()), 'schedule':'自动打卡：关闭', 'task':None,
                     # 真实 bridge 会补上这三项；演示里也让它们跟着账号走，界面文案才自洽。
                     'has_session':True,
                     'has_idm_credentials':True, 'idm_username':'2026000000'},
            'accounts': {'error':'', 'max':MAX_ACCOUNTS, 'active':'demo1', 'busy':False,
                         'items':[{'id':'demo1', 'name':'演示账号一', 'active':True,
                                   'has_idm_credentials':True, 'idm_username':'2026000000',
                                   # 演示里也要自洽：切到哪个账号，登录卡就跟着说谁的登录态。
                                   'has_session':True,
                                   'missing':False, 'enabled':True, 'state':'signed',
                                   'message':'今日打卡已完成，当天不再重复检查',
                                   'busy':False, 'signed_today':True},
                                  {'id':'demo2', 'name':'演示账号二', 'active':False,
                                   'has_idm_credentials':False, 'idm_username':'',
                                   'has_session':False,
                                   'missing':False, 'enabled':False, 'state':'idle',
                                   'message':'尚未查询今日任务', 'busy':False,
                                   'signed_today':False}]},
            'simulation': {'point': {'latitude': 29.823693, 'longitude': 106.422310},
                           'points': dorm_points.add(
                               dorm_points.empty(), name='演示·宿舍楼下',
                               latitude=29.823693, longitude=106.422310, accuracy=100.0,
                               saved_at=now().isoformat(timespec='seconds'))},
            'logs': {'network':'', 'dorm':''},
        }

    def _preview_reference(self):
        """Fixed demo check-in point, in the same WGS84 frame the picker works in."""
        return {'latitude': 29.823940, 'longitude': 106.422470,
                'address': '示例宿舍（演示）', 'radius_m': 800.0}

    def simulation_map(self):
        if self._data['dorm']['settings']['location_source'] != 'simulation':
            return {'ok': False, 'message': '请先把定位来源保存为模拟定位，再使用地图选点。'}
        sample = self._data['simulation']['point']
        point = sample_point(dict(sample, accuracy=sample.get('accuracy', 100.0),
                                  source=sample.get('source', PICK_SOURCE)))
        reference = self._preview_reference()
        distance, in_range = range_check(point, reference)
        named = self._data['simulation']['points']
        return {'ok': True, 'point': point, 'reference': reference,
                'distance_m': distance, 'in_range': in_range,
                'points': [dict(entry, active=entry['id'] == named['active']) for entry in named['points']],
                'active_id': named['active'],
                'tile_url': TILE_URL, 'attribution': TILE_ATTRIBUTION, 'max_zoom': TILE_MAX_ZOOM,
                'providers': tile_providers()}

    def _preview_points(self):
        return self._data['simulation']['points']

    def _save_preview_point(self, payload):
        if self._data['dorm']['settings']['location_source'] != 'simulation':
            return {'ok': False, 'message': '请先把定位来源保存为模拟定位，再使用地图选点。'}
        before = self._preview_points()
        try:
            sample = map_pick_sample(payload.get('latitude'), payload.get('longitude'))
            state = dorm_points.add(before, name=payload.get('name'),
                                    latitude=sample['latitude'], longitude=sample['longitude'],
                                    accuracy=sample['accuracy'],
                                    saved_at=now().isoformat(timespec='seconds'))
        except ValueError as exc:
            return {'ok': False, 'message': str(exc)}
        simulation = self._data['simulation']
        point = dorm_points.find(state, state['active'])
        simulation['point'] = _point_sample(point)
        simulation['points'] = state
        verb = (f'已更新选点「{point["name"]}」的位置'
                if dorm_points.find(before, point['id']) else f'已新建选点「{point["name"]}」')
        return {'ok': True, 'message': f'演示：{verb}：'
                                       + _where_message(sample_point(_point_sample(point)),
                                                        self._preview_reference())}

    def _select_preview_point(self, point_id):
        try:
            state = dorm_points.activate(self._preview_points(), str(point_id or ''))
        except ValueError as exc:
            return {'ok': False, 'message': str(exc)}
        point = dorm_points.find(state, state['active'])
        simulation = self._data['simulation']
        simulation['point'] = _point_sample(point)
        simulation['points'] = state
        return {'ok': True, 'message': f'演示：已切换到选点「{point["name"]}」：'
                                       + _where_message(sample_point(_point_sample(point)),
                                                        self._preview_reference())}

    def _rename_preview_point(self, payload):
        try:
            state = dorm_points.rename(self._preview_points(), str(payload.get('id') or ''), payload.get('name'))
        except ValueError as exc:
            return {'ok': False, 'message': str(exc)}
        self._data['simulation']['points'] = state
        return {'ok': True, 'message': f'演示：选点已重命名为「{dorm_points.find(state, str(payload.get("id") or ""))["name"]}」。'}

    def _delete_preview_point(self, point_id):
        state = self._preview_points()
        point = dorm_points.find(state, str(point_id or ''))
        try:
            remaining = dorm_points.remove(state, str(point_id or ''))
        except ValueError as exc:
            return {'ok': False, 'message': str(exc)}
        if remaining['points'] and not remaining['active']:
            remaining = dorm_points.activate(remaining, remaining['points'][0]['id'])
        simulation = self._data['simulation']
        simulation['points'] = remaining
        active = dorm_points.find(remaining, remaining['active'])
        label = point['name'] if point else ''
        if active is None:
            return {'ok': True, 'message': f'演示：已删除选点「{label}」：模拟定位现在没有可用位置，'
                                           '自动打卡会因缺少位置而跳过；请在地图上重新选点。'}
        simulation['point'] = _point_sample(active)
        return {'ok': True, 'message': f'演示：已删除选点「{label}」，已自动切换到「{active["name"]}」。'}

    def snapshot(self):
        if self._update_started is not None:
            elapsed = time.monotonic() - self._update_started
            update = self._data['update']
            if elapsed >= 4:
                update.update(state='ready', busy=False, progress=100, downloaded_bytes=update['total_bytes'],
                              checked='演示', message='新版本已准备好（演示）；未下载或校验真实安装包。')
                self._update_started = None
            elif elapsed >= 2:
                update.update(state='verifying', progress=100, downloaded_bytes=update['total_bytes'],
                              message='正在演示签名校验，未运行系统校验程序。')
        return dict(copy.deepcopy(self._data),
                    location=self._location_snapshot(self._data['dorm']['settings']['location_source']),
                    location_diagnostic=self._real_location)

    def dispatch(self, action, payload=None):
        payload = payload or {}
        n, d = self._data['network'], self._data['dorm']
        try:
            if action == 'proxy_rules_apply':
                proxy = self._data['proxy']
                if not proxy['can_apply']:
                    return {'ok': False, 'message': proxy['message']}
                proxy.update(can_apply=False, ok=True, missing=[],
                             message='演示：已模拟补写直连规则（没有读写任何代理配置）。')
                return {'ok': True, 'message': '演示：已补写直连规则，并备份了原文件。'}
            if action == 'proxy_rules_check':
                return {'ok': True, 'message': self._data['proxy']['message']}
            if action == 'update_check':
                if self._data['update']['busy']:
                    return {'ok': False, 'message': '演示更新正在进行，请稍候。'}
                self._update_started = time.monotonic()
                self._data['update'].update(state='downloading', latest_version='9.0.0', progress=35,
                                            downloaded_bytes=36700160, total_bytes=104857600, busy=True,
                                            message='正在演示后台下载，不会连接 GitHub 或写入安装包。')
            elif action == 'update_install':
                update = self._data['update']
                if payload.get('confirmed') is not True or update['state'] != 'ready' or payload.get('version') != update['latest_version']:
                    return {'ok': False, 'message': '请等待演示下载完成，并重新确认版本。'}
                update.update(state='launched', busy=False, message='演示安装确认已完成；没有打开真实安装程序。')
            elif action == 'location_authorize':
                source = d['settings']['location_source']
                if self._real_location and source == 'windows':
                    try:
                        return {'ok':True,'message':self._start_location_probe()}
                    except RuntimeError as exc:
                        return {'ok':False,'message':str(exc)}
                active = dorm_points.find(self._preview_points(), self._preview_points()['active'])
                named = f'当前使用选点「{active["name"]}」，' if source == 'simulation' and active else ''
                message = (f'模拟定位检测通过（演示）：{named}未读取本机样本或实时位置。' if source == 'simulation' else
                           '定位检测通过（演示）；未读取真实位置。')
                self._location_state = {'state':'ready','message':message,'source':source,
                                        'accuracy':50,'checked':'演示'}
            elif action == 'network_save':
                value = network_settings(payload, gui.GuiSettings())
                n.update(username=value.username, interval=value.check_interval_seconds, startup=payload['startup'])
            elif action in ('network_check', 'network_start'):
                n.update(state='online', message='网络连接正常（演示）', checked=dt.datetime.now().strftime('%H:%M:%S'))
                n['monitoring'] = action == 'network_start'
                self._data['logs']['network'] += '演示 · 校园网连接检测完成\n'
            elif action == 'network_stop':
                n.update(monitoring=False, state='stopped', message='后台检测已停止（演示）')
            elif action == 'location_source_save':
                source = payload.get('location_source', '')
                if source not in ('windows', 'simulation'):
                    return {'ok':False, 'message':'定位来源无效'}
                if self._location_gate.locked():
                    return {'ok':False, 'message':'请等待定位检测完成后再切换定位来源。'}
                if source != d['settings']['location_source']:
                    self._reset_location(source)
                    d['settings']['location_source'] = source
            elif action == 'simulation_point_save':
                return self._save_preview_point(payload)
            elif action == 'simulation_point_select':
                return self._select_preview_point(payload.get('id'))
            elif action == 'simulation_point_rename':
                return self._rename_preview_point(payload)
            elif action == 'simulation_point_delete':
                return self._delete_preview_point(payload.get('id'))
            elif action == 'dorm_save':
                if self._location_gate.locked():
                    return {'ok':False, 'message':'请等待定位检测完成后再保存设置。'}
                settings = dorm_settings(payload)
                if settings.location_source != d['settings']['location_source']:
                    self._reset_location(settings.location_source)
                d['settings'] = dataclasses.asdict(settings)
                d['schedule'] = '自动打卡：' + ('开启（演示）' if d['settings']['enabled'] else '关闭')
            elif action == 'dorm_login':
                d.update(state='logged_in', message='学校登录成功（演示，不会打开真实认证）',
                         has_session=True)
            elif action in ('dorm_query', 'dorm_submit'):
                signed = action == 'dorm_submit'
                d.update(state='signed' if signed else 'ready', message='今日打卡已完成（演示）' if signed else '今日任务待完成（演示）',
                         task={'title':'晚间寝室打卡', 'date':dt.date.today().isoformat(),
                               'start':'21:00', 'end':'23:30', 'address':'示例宿舍', 'signed':signed})
                self._data['logs']['dorm'] += '演示 · ' + d['message'] + '\n'
            elif action == 'dorm_logout':
                d.update(state='login_required', message='请重新登录学校账号', task=None, has_session=False)
                d['settings']['enabled'] = False
                d['schedule'] = '自动打卡：关闭'
            elif action in ('account_add', 'account_switch', 'account_rename', 'account_delete'):
                return self._preview_account(action, payload)
            elif action in ('hide', 'quit'):
                self._window_action(action)
            elif action not in ('dorm_cancel', 'location_settings'):
                return {'ok':False, 'message':'不支持的操作'}
            return {'ok':True, 'message':'演示操作完成，未访问学校服务或修改真实设置'}
        except (ValueError, TypeError):
            return {'ok':False, 'message':'请检查账号、时间和间隔，结束时间须晚于开始时间。'}

    def _preview_account(self, action, payload):
        """演示账号：只改内存里这一份快照，不碰任何真实档案、凭据或目录。"""
        accounts = self._data['accounts']
        items = accounts['items']
        active = next((item for item in items if item['active']), None)
        if action == 'account_add':
            if len(items) >= accounts['max']:
                return {'ok': False, 'message': f"演示：最多 {accounts['max']} 个账号。"}
            clean = dorm_points.clean_name(payload.get('name')) or f'演示账号{len(items)+1}'
            for item in items:
                item['active'] = False
            items.append({'id': f'demo{len(items)+1}', 'name': clean, 'active': True,
                          'has_idm_credentials': False, 'idm_username': '', 'missing': False,
                          'enabled': False, 'state': 'idle', 'message': '尚未查询今日任务',
                          'busy': False, 'signed_today': False})
            accounts['active'] = items[-1]['id']
            return {'ok': True, 'message': f'演示：已新建账号「{clean}」，未写入任何本机数据。'}
        if action == 'account_switch':
            target = next((item for item in items if item['id'] == payload.get('id')), None)
            if target is None:
                return {'ok': False, 'message': '演示：找不到这个账号。'}
            for item in items:
                item['active'] = item is target
            accounts['active'] = target['id']
            # 演示也要自洽：学号、「已保存凭据」和登录态都跟着换人，否则切换后界面还在说上一个账号。
            self._data['dorm']['has_idm_credentials'] = target['has_idm_credentials']
            self._data['dorm']['idm_username'] = target['idm_username']
            self._data['dorm']['has_session'] = target['has_session']
            return {'ok': True, 'message': f"演示：已切换到账号「{target['name']}」。"
                                           '未读取或写入真实档案。'}
        if action == 'account_rename':
            target = next((item for item in items if item['id'] == payload.get('id')), None)
            if target is None:
                return {'ok': False, 'message': '演示：找不到这个账号。'}
            clean = dorm_points.clean_name(payload.get('name'))
            if not clean:
                return {'ok': False, 'message': '演示：账号名称不能为空。'}
            target['name'] = clean
            return {'ok': True, 'message': f'演示：账号已重命名为「{clean}」。'}
        if payload.get('confirmed') is not True:
            return {'ok': False, 'message': '演示：请先确认删除该账号。'}
        if len(items) <= 1:
            return {'ok': False, 'message': '演示：至少保留一个账号。'}
        target = next((item for item in items if item['id'] == payload.get('id')), None)
        if target is None:
            return {'ok': False, 'message': '演示：找不到这个账号。'}
        remaining = [item for item in items if item is not target]
        if not any(item['active'] for item in remaining):
            remaining[0]['active'] = True          # 删掉的是活动账号：顶上一个，且只能有一个
        items.clear()
        items.extend(remaining)
        accounts['active'] = next(item['id'] for item in items if item['active'])
        return {'ok': True, 'message': f"演示：已删除账号「{target['name']}」，未删除任何真实数据。"}

    def _tick(self):
        pass

    def _close(self):
        pass
