"""乐跑模块的控制器: 线程编排 + 每日自动流水线(沿用寝室打卡的控制面板模式)。

自动流水线(每天 run_time 之后一拍):
  读取凭证 → 真门探测(101 才判死) → 死了先向研究库同步 → 仍无凭证且研究库
  可用则后台采集(等用户开一次小程序) → 拿到凭证跑完整流水线 → 结果落盘。
所有自动行为都有硬上限: 每天最多自动尝试 3 次、自动采集 2 次, 绝不无限重试。

凭证自动续期(lptiyu_renew.CredentialRenewer)在这里挂两条线:
  * 手动: 「立即续期」= start('renew'), 越过冷却(采集/探活的每日配额仍是硬的),
    结果直接进界面;
  * 自动: poll() 每拍先问 renewer.due() —— 低频探活到期, 或进了「跑步前
    renew_lead_min 分钟」保活窗 —— 就在后台跑一遍, 成功不打扰界面, 只有
    「需要用户开一次小程序」才冒泡成 need_token。
这样到点自动跑时手上已经有一枚确认过的凭证, 不会等到提交那一步才发现号死了。
"""
from __future__ import annotations

import datetime as dt
import os
import queue
import shutil
import subprocess
import threading
import time

from lptiyu import flow as lptiyu_flow
from lptiyu.client import ApiClient, ApiError
from lptiyu_capture import CaptureError, CaptureSession
from lptiyu_renew import CredentialRenewer
from lptiyu_store import Store, Settings, now, parse_time

# 微信注册的 weixin:// 协议: 直接拉起乐跑小程序(与研究库一致的那个 appid)。
OFFICIAL_MINI_PROGRAM_URI = 'weixin://launchapplet/?app_id=wxb2043232183dac7f'


# 唤起失败时留给界面/日志的一句话(测试也会读它)。
LAST_LAUNCH_REASON = ''

WECHAT_EXES = (
    r'C:\Program Files\Tencent\Weixin\Weixin.exe',          # 微信 4.x
    r'C:\Program Files (x86)\Tencent\WeChat\WeChat.exe',    # 微信 3.x
    r'C:\Program Files\Tencent\WeChat\WeChat.exe',
)


def wechat_protocol_registered():
    """`weixin://` 有没有被注册 —— 没有就是没装微信/装的是精简版。"""
    import winreg
    for root in (winreg.HKEY_CLASSES_ROOT, winreg.HKEY_CURRENT_USER):
        try:
            winreg.OpenKey(root, r'Software\Classes\weixin').Close()
            return True
        except OSError:
            continue
    try:
        winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r'weixin').Close()
        return True
    except OSError:
        return False


def wechat_installed():
    return wechat_protocol_registered() or any(os.path.isfile(p) for p in WECHAT_EXES)


def open_mini_program(uri=OFFICIAL_MINI_PROGRAM_URI, timeout=6.0):
    """让微信打开小程序; 返回是否真的发起了, 并把原因写进 LAST_LAUNCH_REASON。

    换过账号/换过电脑最常见的失败: 装的是精简版/微软商店版微信, `weixin://`
    没注册; 或者根本没装。这两种情况 ShellExecute 只会弹一句「选择一个应用」,
    或者干脆失败 —— 采集流程要能继续等, 而不是卡死。
    """
    global LAST_LAUNCH_REASON
    LAST_LAUNCH_REASON = ''
    if not wechat_installed():
        LAST_LAUNCH_REASON = '本机没有找到微信（weixin:// 未注册，也没有常见安装路径）'
        return False
    result = {}

    def run():
        try:
            os.startfile(uri)      # ShellExecute 走 weixin:// 协议
            result['ok'] = True
        except (OSError, AttributeError) as exc:
            result['ok'] = False
            result['error'] = str(exc)

    worker = threading.Thread(target=run, name='youziauth-open-applet', daemon=True)
    worker.start()
    worker.join(timeout)
    if not result.get('ok'):
        LAST_LAUNCH_REASON = (result.get('error')
                              or '微信没有响应 weixin:// （可能被安全软件拦了）')
    return bool(result.get('ok'))


MAX_DAILY_AUTO_ATTEMPTS = 3
# 没有可用凭证时的自愈节奏: 冷却 20 分钟, 一天最多自动开三次小程序。
AUTO_CAPTURE_COOLDOWN_MIN = 20
AUTO_CAPTURE_DAILY_CAP = 3
GRAB_TIMEOUT_SECONDS = 540
MAX_ZONE_SYNC = 8                # 同步跑区时最多切几次场地(服务端一般就 4 个)

# 续期结果 → 界面状态词(与前端 LP_NAMES 对齐)。
RENEW_STATES = {'ok': 'token_ok', 'renewed': 'renewed', 'need_user': 'need_token',
                'cooldown': 'cooldown', 'capped': 'cooldown', 'error': 'error',
                'skipped': 'idle'}

# 创建进程时不弹控制台窗口(Windows); 其他平台没有这个标志, 保持默认。
CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class LptiyuError(Exception):
    """state 是受控词表, message 是给界面看的一句话。"""

    def __init__(self, state, message):
        self.state = state
        self.message = message
        super().__init__(message)


class TokenSlot:
    """flow.DailyFlow 需要的槽位协议: 身份 + 凭证 + 生命周期回调。"""

    def __init__(self, store):
        self.store = store

    @property
    def label(self):
        identity = self.store.identity()
        return identity.get('name') or identity.get('student_num') or '成员'

    @property
    def uid(self):
        try:
            return int(self.store.identity().get('uid') or 0) or None
        except (TypeError, ValueError):
            return None

    @property
    def school_id(self):
        try:
            return int(self.store.identity().get('school_id') or 0) or 0
        except (TypeError, ValueError):
            return 0

    @property
    def student_num(self):
        return str(self.store.identity().get('student_num') or '')

    @property
    def card_id(self):
        return str(self.store.identity().get('card_id') or self.student_num or '')

    @property
    def token(self):
        return self.store.token()

    def require_uid(self):
        if not self.uid:
            raise LptiyuError('need_token', '还没有完成首次同步，先在小程序登录一次再同步凭证')
        return self.uid

    def make_client(self, pace=0.0):
        settings = self.store.settings()
        return ApiClient(
            uid=self.require_uid(), token=self.token,
            school_id=self.school_id, student_num=self.student_num,
            card_id=self.card_id, proxy=settings.api_proxy or None,
            pace=pace)

    def after_submission(self, client):
        """提交完成后探测一次: 活着就保留给下一次(跨天复用), 101 才判死。

        ApiError 也可能是别的失败(4=通用失败之类); 只有 101 才是 token 死亡,
        其余留给下一次探测 —— 一次抖动就把手上的凭证丢掉, 等于逼用户重开小程序。
        """
        try:
            client.term_list()
        except ApiError as exc:
            if getattr(exc, 'code', None) == 101:
                self.mark_dead()
            return
        except Exception:              # noqa: BLE001 —— 链路故障不判死, 留待下次探测
            return
        self.store.record_probe(True)

    def mark_dead(self):
        self.store.clear_token()
        self.store.record_probe(False)


class LptiyuController:
    def __init__(self, store=None, slot=None, clock=None, flow_factory=None,
                 renewer=None, capture=None, open_applet=None):
        self.store = store or Store()
        self.slot = slot or TokenSlot(self.store)
        self._clock = clock or now
        # 采集链: 没有研究库的机器靠它自己拿 token。证书检查很慢(certutil),
        # 所以在 capture_status 里做 60 秒缓存。
        self.capture = capture or CaptureSession(self.store.root / 'capture',
                                                 clock=self._clock)
        # 采集时代为唤起小程序: 少一步「去微信里翻出它」。
        self._open_applet = open_applet or open_mini_program
        self._capture_requested = False      # 某次跑批发现没凭证 → 下一拍自动采集
        self._capture_auto = False           # 这次采集是不是自动发起的
        self._cert_cache = (0.0, None)
        self._flow_factory = flow_factory   # 测试注入; 正式路径现场构造 DailyFlow
        # 续期器也允许注入: 单测里给一个假的, 就不必碰网络和子进程。
        self.renewer = renewer or CredentialRenewer(
            self.store, clock=self._clock,
            probe=self._probe_record, grab=self._grab_via_lab)
        self.events = queue.Queue()
        self.busy = False
        self.closed = False
        self._gate = threading.Lock()
        self._poll_at = 0.0
        self.cancel = threading.Event()
        self.latest = None
        # 初始状态不是「发生过的事」: 每造一个控制器就往历史里写一行, 记录页会被
        # 「尚未开始今天的跑步」刷满(测试和预览尤其明显)。
        self._set_latest('idle', '尚未开始今天的跑步', record=False)

    # ------------------------------------------------------------- 状态
    def _set_latest(self, state, message, detail='', record=True):
        result = {'state': state, 'message': message, 'detail': detail,
                  'at': self._clock().isoformat(timespec='seconds')}
        self.latest = result
        if record:
            try:
                self.store.record(state, message, detail)
            except Exception:  # noqa: BLE001 —— 状态记录失败不影响主流程
                pass
        return result

    def _push(self, result):
        self.events.put(result)

    def schedule_text(self):
        try:
            s = self.store.settings()
            if not s.enabled:
                return '自动跑步：关闭'
            return f'自动跑步：开启 · 每天 {s.run_time}（有效时段 {s.time_lo}–{s.time_hi}）'
        except Exception:  # noqa: BLE001
            return '自动跑步：配置无效'

    # ------------------------------------------------------------- 入口
    def start(self, action):
        if action not in ('run', 'dry', 'sync', 'renew', 'renew_auto', 'zones',
                          'capture'):
            return False
        with self._gate:
            if self.closed or self.busy:
                return False
            self.busy = True
            self.cancel.clear()

        def work():
            try:
                if action == 'sync':
                    result = self._sync()
                elif action == 'zones':
                    result = self._sync_zones()
                elif action == 'capture':
                    result = self._capture_flow()
                elif action == 'renew':
                    result = self._renew()
                elif action == 'renew_auto':
                    result = self._renew_auto()
                elif action == 'dry':
                    result = self._execute(submit=False)
                else:
                    result = self._execute(submit=True)
                if result is not None:
                    self._push(result)
            except LptiyuError as exc:
                result = self._set_latest(exc.state, exc.message)
                self._push(result)
            except Exception:  # noqa: BLE001
                result = self._set_latest('error', '跑步操作未完成，请稍后重试')
                self._push(result)
            finally:
                with self._gate:
                    self.busy = False
                self._capture_auto = False     # 自动标记只属于这一次采集

        threading.Thread(target=work, name='youziauth-lptiyu-' + action,
                         daemon=True).start()
        return True

    def poll(self):
        if self.closed or time.monotonic() < self._poll_at:
            return
        self._poll_at = time.monotonic() + 5
        # 手动/定时跑步撞上「没有凭证」时, 这里把采集接过来, 不用人再点一次。
        if self._capture_requested and not self.busy:
            self._capture_requested = False
            if self.auto_capture():
                return
        try:
            settings = self.store.settings()
        except Exception:  # noqa: BLE001 —— 设置坏了不轰炸，由快照呈现
            return
        if not settings.enabled or self.store.is_signed_today():
            return
        current = self._clock()
        try:
            if not (parse_time(settings.time_lo) <= current.time() <= parse_time(settings.time_hi)):
                return
            run_ready = current.time() >= parse_time(settings.run_time)
        except ValueError:
            return
        if self.store.daily().get('auto', 0) >= MAX_DAILY_AUTO_ATTEMPTS:
            return
        # 保活先于起跑: 到点时手上必须已经有一枚确认过的凭证, 否则那一拍只会白报
        # 「需要凭证」。续期成功后台账会推到 interval 之后, 所以这里不会连环触发。
        if self._renewal_due(settings):
            self.start('renew_auto')
            return
        if not run_ready:
            return
        if not self.store.has_token() and self.auto_capture():
            return
        self.start('run')
        try:
            self.store.bump_daily(kind='auto')
        except Exception:  # noqa: BLE001
            pass

    def drain(self):
        values = []
        while True:
            try:
                self.latest = self.events.get_nowait()
                values.append(self.latest)
            except queue.Empty:
                return values

    def cancel_work(self):
        self.cancel.set()

    def _wait_for_work(self, timeout=8.0):
        """等待在跑的一拍结束(测试与退出路径用)。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.busy:
            time.sleep(0.02)

    def close(self):
        with self._gate:
            self.closed = True
            self.cancel_work()
        try:
            self.capture.stop('idle', '')
        except Exception:  # noqa: BLE001 —— 退出流程不该被采集卡住
            pass

    # ------------------------------------------------------------- 步骤
    def _sync(self):
        changes, message = self.store.sync_from_lab()
        state = 'synced' if changes else 'idle'
        if not changes and message.startswith('未找到'):
            state = 'error'
        if changes or message == '本机凭证已是最新':
            self.store.record_sync(message)
        return self._set_latest(state, message)

    # ------------------------------------------------------------- 采集
    def capture_status(self):
        """采集与证书的状态(给快照)。证书检查走缓存, 不让 1.5 秒一拍去 spawn certutil。"""
        try:
            status = self.capture.status()
        except Exception:  # noqa: BLE001
            status = {'state': 'idle', 'message': '', 'port': 0, 'remaining_s': 0}
        now_ts = time.monotonic()
        at, cached = self._cert_cache
        if cached is None or now_ts - at > 60:
            try:
                cached = self.capture.cert_status()
            except Exception:  # noqa: BLE001
                cached = {'installed': False, 'fingerprint': ''}
            self._cert_cache = (now_ts, cached)
        return {'state': status.get('state', 'idle'),
                'message': status.get('message', ''),
                'remaining_s': int(status.get('remaining_s') or 0),
                'cert_installed': bool(cached.get('installed')),
                'fingerprint': str(cached.get('fingerprint') or '')[:16],
                'available': True}

    def capture_cert_install(self):
        try:
            result = self.capture.ca.install()
        except CaptureError as exc:
            raise LptiyuError('error', str(exc)) from exc
        self._cert_cache = (0.0, None)
        return result

    def capture_cert_remove(self):
        try:
            result = self.capture.ca.remove()
        except CaptureError as exc:
            raise LptiyuError('error', str(exc)) from exc
        self._cert_cache = (0.0, None)
        return result

    def capture_ready(self):
        """采集能不能自己跑: 证书装好就行(证书安装是明确的用户动作, 代劳不了)。"""
        try:
            return bool(self.capture.cert_status().get('installed'))
        except Exception:              # noqa: BLE001 —— certutil 不在也当没装
            return False

    def _auto_capture_due(self):
        if not self.capture_ready():
            return False
        state = self.store.capture_state()
        today = self._clock().date().isoformat()
        if int(state['attempts'].get(today, 0)) >= AUTO_CAPTURE_DAILY_CAP:
            return False
        if state['last_attempt']:
            try:
                last = dt.datetime.fromisoformat(state['last_attempt'])
            except ValueError:
                last = None
            if last and (self._clock() - last).total_seconds() < AUTO_CAPTURE_COOLDOWN_MIN * 60:
                return False
        return True

    def auto_capture(self):
        """静默自愈: 起代理、唤起小程序、拿凭证、还原代理, 全程不需要人参与。"""
        if self.busy or self.closed or not self._auto_capture_due():
            return False
        try:
            self.store.record_capture_attempt(self._clock())
        except Exception:              # noqa: BLE001 —— 记账失败不该拦住采集
            pass
        self._capture_auto = True
        return bool(self.start('capture'))

    def capture_stop(self):
        self.capture.stop('idle', '已停止采集')
        return self._set_latest('idle', '已停止采集', record=False)

    def _capture_flow(self):
        """等一次小程序登录, 把 token 与身份一起落盘, 然后当真门探活。"""
        auto = self._capture_auto
        try:
            self.capture.start()
        except CaptureError as exc:
            raise LptiyuError('error', str(exc)) from exc
        launched = False
        try:
            launched = bool(self._open_applet())
        except Exception:              # noqa: BLE001 —— 唤不起只是少一步, 不能中断采集
            launched = False
        reason = LAST_LAUNCH_REASON
        # 注入的采集器不一定带日志接口, 记不上就算了, 不能因此中断采集。
        log = getattr(self.capture, 'log', None)
        if callable(log):
            log('已请求微信打开小程序' if launched else '没能唤起微信, 等用户手动打开')
        prefix = '正在自动采集凭证：' if auto else ''
        self._set_latest(
            'capturing',
            (prefix + '已打开小程序，等待它发出第一个请求…') if launched
            else (prefix + (reason or '请在微信里打开「数体智慧体育」并进入跑步首页')),
            record=False)
        started_at = time.monotonic()
        hint_at = started_at + 25
        last_check = started_at

        def tick():
            """每秒一次: 代理被抢走就抢回来; 25 秒还没流量就给一句诊断。"""
            nonlocal hint_at, last_check
            stamp = time.monotonic()
            if stamp - last_check >= 10:
                last_check = stamp
                self.capture.ensure_proxy()
            if hint_at and stamp >= hint_at and not getattr(self.capture, 'connects', 0):
                hint_at = 0
                self.capture.log('25 秒内没有任何连接: 微信可能没走系统代理, 或被别的代理软件接管')
                self._set_latest(
                    'capturing',
                    '还没看到流量：确认微信用的是系统代理（微信设置里不要单独设代理），'
                    '并真正打开小程序页', record=False)

        cred = self.capture.wait(on_tick=tick)
        if not cred:
            status = self.capture.status()
            if status.get('state') == 'idle':
                return self._set_latest('idle', status.get('message') or '已停止采集',
                                        record=False)
            raise LptiyuError('need_token', status.get('message') or '没有抓到凭证')
        identity = self.store.identity()
        student_num = str(cred.get('student_num') or identity.get('student_num') or '')
        self.store.save_identity({
            'name': identity.get('name') or student_num or '成员',
            'student_num': student_num,
            'uid': str(cred.get('uid') or identity.get('uid') or ''),
            'school_id': str(cred.get('school_id') or identity.get('school_id') or ''),
            'card_id': str(cred.get('card_id') or student_num
                           or identity.get('card_id') or ''),
        })
        self.store.save_token_record(
            cred['token'],
            issued_at=cred.get('issued_at') or self._clock().isoformat(timespec='seconds'),
            source='capture')
        client = self.slot.make_client(pace=0.0)
        try:
            alive = self._probe_alive(client)
        except ApiError as exc:
            raise LptiyuError('error', f'凭证已保存，但探活失败（{exc.code}）') from exc
        if not alive:
            self.slot.mark_dead()
            raise LptiyuError('need_token', '抓到的凭证当场失效了：在小程序里重新登录后再采集')
        self.store.record_probe(True)
        return self._set_latest('synced', '已自动采集凭证，并确认可用' if auto
                                else '已采集凭证，并确认可用')

    # ------------------------------------------------------------- 样本
    def _record_sample(self, summary, settings):
        """把本次跑区的几何与结果留档: 事后校准朝向/形状就靠这些样本。"""
        try:
            self.store.record_sample({
                'at': self._clock().isoformat(timespec='seconds'),
                'record_id': summary.get('record_id'),
                'zone_id': str(summary.get('game_id') or settings.zone_id or ''),
                'zone_name': summary.get('zone_name') or '',
                'distance': summary.get('distance'),
                'used_time': summary.get('used_time'),
                'record_status': summary.get('record_status'),
                'record_failed_reason': summary.get('record_failed_reason') or '',
                'style': summary.get('style'),
                'zone_points': summary.get('zone_points') or [],
                'zone_axis_deg': summary.get('zone_axis_deg'),
                'zone_axis_source': summary.get('zone_axis_source'),
                'passed_points': summary.get('passed_points') or [],
            })
            self._learn_axis(str(summary.get('game_id') or settings.zone_id or ''))
        except Exception:              # noqa: BLE001 —— 留档失败不该影响跑步结果
            pass

    def _learn_axis(self, zone_id):
        """攒够了真实通过点就把朝向学下来: 没有实测先验的跑区靠它自我修正。"""
        if not zone_id:
            return
        from lptiyu_share import track_axis, zone_passes
        deg, share = track_axis(zone_passes(self.store.samples(), zone_id))
        if deg:
            self.store.save_zone_axis(zone_id, deg, share)

    # ------------------------------------------------------------- 跑区
    @staticmethod
    def _points_of(cfg):
        """beforeRun 的 point_list → [{id, lat, lng}]。解析不了的点直接丢。"""
        points = []
        for point in ((cfg.get('run_line_info') or {}).get('point_list') or []):
            try:
                lat, lng = lptiyu_flow.TRK.parse_jingwei(str(point.get('jingwei') or '').strip())
            except (TypeError, ValueError):
                continue
            points.append({'id': str(point.get('id') or ''), 'lat': lat, 'lng': lng})
        return points

    def _zone_sink(self, zone_id, zone_name, points, zone_list):
        """流水线读到真机点位后留档: 每块场地的朝向/宽窄不同, 特调各算各的。"""
        try:
            if zone_list:
                self.store.save_zone_list(zone_list)
            self.store.save_zone_points(zone_id, zone_name, points)
        except Exception:  # noqa: BLE001 —— 留档失败不影响这一次跑步
            pass

    def _sync_zones(self):
        """把每个跑区的打卡点各抓一遍。

        切跑区会改服务端上的「当前跑区」, 所以收尾必须切回用户选的那个:
        同步是只读的准备工作, 不该顺手改掉用户的选择。
        """
        self._ensure_credentials()
        client = self.slot.make_client(pace=0.8)
        try:
            cfg = client.before_run()
        except ApiError as exc:
            raise LptiyuError('need_token' if exc.code == 101 else 'error',
                              '拉取跑区失败：凭证已失效' if exc.code == 101
                              else '拉取跑区失败，接口暂时不可用')
        if not isinstance(cfg, dict):
            raise LptiyuError('error', '跑区信息格式异常，稍后再试')
        zones = [zone for zone in (cfg.get('run_zone_list') or [])
                 if isinstance(zone, dict) and str(zone.get('id') or '')]
        self.store.save_zone_list(zones)
        original = str(cfg.get('run_zone_id') or '')
        last = original
        done = set()
        if original:
            self.store.save_zone_points(original, str(cfg.get('run_zone_name') or ''),
                                        self._points_of(cfg))
            done.add(original)
        for zone in zones[:MAX_ZONE_SYNC]:
            zone_id = str(zone.get('id') or '')
            if not zone_id or zone_id in done:
                continue
            try:
                client.set_zone(zone_id)
                other = client.before_run()
            except ApiError:
                continue
            last = zone_id
            if not isinstance(other, dict) or str(other.get('run_zone_id') or '') != zone_id:
                continue
            self.store.save_zone_points(
                zone_id, str(other.get('run_zone_name') or zone.get('name') or ''),
                self._points_of(other))
            done.add(zone_id)
        # 收尾: 同步过程中账号被切到最后一个跑区, 必须切回用户选的那个。
        target = str(self.store.settings().zone_id or original)
        try:
            if target and target != last:
                client.set_zone(target)
        except ApiError:
            pass
        return self._set_latest('zones', f'已同步 {len(done)} 个跑区的点位')

    # ------------------------------------------------------------- 凭证续期
    def _renewal_due(self, settings):
        try:
            return bool(self.renewer.due(settings))
        except Exception:  # noqa: BLE001 —— 续期判定坏了不该拖住到点起跑
            return False

    def _renew(self):
        """手动「立即续期」: 用户已经表达了意图, 越过冷却(配额仍是硬的)。

        结果只进内存态与续期台账, 不写 status.json —— 续期不是「今天的跑步」,
        重启后不该让界面把昨天的续期结果当成今天的任务状态。
        """
        outcome = self.renewer.renew(force=True)
        return self._set_latest(RENEW_STATES.get(outcome.state, 'error'), outcome.message,
                                record=False)

    def _renew_auto(self):
        """后台保活: 成功不打扰界面, 只有「需要用户动一次」才冒泡。

        成功时不写 latest —— 续期是维护动作, 不该把「今日跑步已完成」顶成
        「凭证可用」。台账(renewal.json)与历史里已经留了痕。
        """
        if self.store.is_signed_today():
            return None
        outcome = self.renewer.renew(force=False)
        if outcome.state == 'need_user':
            return self._set_latest('need_token', outcome.message, record=False)
        return None

    def _ensure_credentials(self):
        """返回可用凭证; 拿不到就抛 LptiyuError(need_token)。

        阶梯统一交给 renewer: 探活 → 研究库 → 主动采集(借用研究库采集链),
        每一步都用真门探活收尾。到点自动跑或用户手动点, 都按 force=True 走 ——
        用户/日程已经表达了「现在要用凭证」, 不该再被台账冷却挡回去。
        """
        outcome = self.renewer.renew(force=True)
        record = None
        try:
            record = self.store.token_record()
        except OSError:
            self.store.clear_token()
        if record and record.get('token'):
            return record
        raise LptiyuError('need_token', outcome.message)

    def _grab_via_lab(self):
        """借用研究库的采集链抓一次凭证(需要小程序冷启动一次)。

        返回 (ok, reason); ok 只表示采集链自己走完了, 拿到没拿到由随后的
        探活/同步裁决 —— 采集退出码成功但用户没开小程序是常态。
        """
        lab = self.store.lab_dir()
        script = os.path.join(lab, 'lpctl.py')
        selector = self.store.identity().get('student_num') or ''
        python = shutil.which('py')
        if python:
            command = [python, '-3', script]
        else:
            python = shutil.which('python')
            if not python:
                return False, '本机没有可用的 Python'
            command = [python, script]
        command += ['grab', '--timeout', '480']
        if selector:
            command += ['--member', selector]
        try:
            completed = subprocess.run(command, capture_output=True,
                                       timeout=GRAB_TIMEOUT_SECONDS,
                                       creationflags=CREATE_NO_WINDOW, cwd=lab)
        except subprocess.TimeoutExpired:
            return False, '采集等待超时'
        except OSError:
            return False, '采集链启动失败'
        return completed.returncode == 0, '' if completed.returncode == 0 else '采集链退出异常'

    def _probe_record(self, record=None):
        """续期器的探活回调: (True=活, False=101 判死, None=链路故障不知道)。"""
        try:
            client = self.slot.make_client(pace=0.0)
        except LptiyuError as exc:
            return None, exc.message
        try:
            alive = self._probe_alive(client)
        except ApiError as exc:
            return None, f'链路故障({exc.code})'
        except Exception:  # noqa: BLE001 —— 连不上不等于 token 死了
            return None, '链路故障'
        self.store.record_probe(alive)
        return (True, '') if alive else (False, '真门 101')

    def _probe_alive(self, client):
        try:
            client.term_list()
            return True
        except ApiError as exc:
            if exc.code == 101:
                return False
            raise                     # 网络类故障不判死, 让上层按 error 呈现
        return True

    def _flow(self, settings):
        if self._flow_factory is not None:
            return self._flow_factory(self.slot, settings)
        pace_lo, pace_hi = settings.pace_range()
        policy = {
            'max_per_day': settings.max_per_day,
            'style': 'template',
            'dist_lo': settings.dist_lo,
            'dist_hi': settings.dist_hi,
            'pace_lo': pace_lo,          # 0/0 = 交给模板
            'pace_hi': pace_hi,
            'anchor_max_m': settings.anchor_max_m,
            'time_lo': settings.time_lo,
            'time_hi': settings.time_hi,
        }
        template_path = ''
        candidates = self.store.template_candidates()
        found = lptiyu_flow.TRK.find_template(candidates)
        if found:
            template_path = found
        return lptiyu_flow.DailyFlow(self.slot, policy=policy,
                                     stop_event=self.cancel,
                                     log_fn=lambda message: None,
                                     template_path=template_path,
                                     zone_tuning=self.store.zone_tuning(settings.zone_id),
                                     zone_sink=self._zone_sink)

    def _execute(self, submit):
        settings = self.store.settings()
        try:
            record = self._ensure_credentials()
        except LptiyuError as exc:
            # 没有凭证但有采集能力: 交给下一拍自己采, 不用人点「同步凭证」。
            if exc.state == 'need_token' and self._auto_capture_due():
                self._capture_requested = True
                raise LptiyuError(
                    'need_token',
                    '本机没有可用凭证：正在自动采集，微信打开小程序后就会继续') from None
            raise
        self._set_latest('running', '正在处理今天的跑步…' if submit else '正在验证全链路…',
                         record=False)
        client = self.slot.make_client(pace=0.8)
        if not self._probe_alive(client):
            self.slot.mark_dead()
            # 续期刚过、起跑前这半秒号又死了(小程序那边刚重登): 再走一遍续期阶梯,
            # 拿到新号就直接继续, 不让这一拍白跑。
            outcome = self.renewer.renew(force=True)
            fresh = None
            try:
                fresh = self.store.token_record()
            except OSError:
                self.store.clear_token()
            if not fresh or not fresh.get('token') or fresh['token'] == record['token']:
                if submit:
                    self.store.bump_daily(kind='run')
                raise LptiyuError('need_token', outcome.message)
            client = self.slot.make_client(pace=0.8)

        flow = self._flow(settings)
        summary, code = flow.run(submit=submit, zone_override=settings.zone_id or None)
        if submit and isinstance(summary, dict):
            self._record_sample(summary, settings)
        if not submit:
            if code == 0:
                return self._set_latest('checked', '全链验证通过，未提交')
            if code == 2:
                return self._set_latest('need_token', '验证时发现凭证已失效，请先更新凭证')
            return self._set_latest('error', '验证未通过，请稍后重试')

        if code in (0, 5) and isinstance(summary, dict):
            # 只把「学校受理、记录已创建」的提交计入今日次数; 额度满被拒的那一拍没有真实跑步
            self.store.bump_daily(kind='run')
            distance = summary.get('distance')
            used = summary.get('used_time')
            try:
                pace = lptiyu_flow.pace_text(float(used) / float(distance)) if distance and used else ''
            except (TypeError, ZeroDivisionError):
                pace = ''
            zone = summary.get('zone_name') or settings.zone_name or '当前跑区'
            distance_text = f'{float(distance):.2f}km' if distance else ''
            pace_text = f' · 配速 {pace}/km' if pace else ''
            headline = f'今日跑步已完成 · {distance_text}{pace_text}'
            if code == 5:
                reason = summary.get('record_failed_reason') or ''
                return self._set_latest('uncertain',
                                        '记录已提交，学校判定未通过' + (f'（{reason}）' if reason else ''),
                                        detail=f'{zone} · {distance_text}')
            self.store.mark_signed(now().date().isoformat(), {
                'distance': distance_text, 'pace': pace, 'zone': zone,
                'record_id': summary.get('record_id') or '',
            })
            return self._set_latest('signed', headline, detail=zone)
        if code == 3:
            self.store.mark_signed(now().date().isoformat(), {'quota': True})
            return self._set_latest('quota', '今日额度已完成，无需再跑')
        if code == 9:
            return self._set_latest('cancelled', '已取消当前操作')
        if code == 2:
            raise LptiyuError('need_token', '凭证已失效：在小程序里打开一次跑步首页，然后点「同步凭证」')
        return self._set_latest('error', '提交未完成，请查看运行记录')
