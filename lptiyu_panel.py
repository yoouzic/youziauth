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
    「需要用户开一次小程序」才冒泡成 need_token。这一拍不受「今天已经打过卡」
    或「有效时段」限制: token 按天死, 而面板什么时候被打开由用户决定。
这样到点自动跑时手上已经有一枚确认过的凭证, 不会等到提交那一步才发现号死了。
"""
from __future__ import annotations

import datetime as dt
import os
import random
import queue
import subprocess
import threading
import time

import proxy_guard
from lptiyu import flow as lptiyu_flow
from lptiyu.client import ApiClient, ApiError
from lptiyu_capture import CaptureError, CaptureSession
from lptiyu_renew import CredentialRenewer
from lptiyu_members import MemberError
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


def _wechat_running():
    """微信进程在不在(tasklist 很轻, 一次 0.2s 左右)。"""
    try:
        done = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq WeChat.exe'],
                              capture_output=True, text=True, timeout=8,
                              creationflags=CREATE_NO_WINDOW, errors='replace')
        if 'WeChat.exe' in (done.stdout or ''):
            return True
        done = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq Weixin.exe'],
                              capture_output=True, text=True, timeout=8,
                              creationflags=CREATE_NO_WINDOW, errors='replace')
        return 'Weixin.exe' in (done.stdout or '')
    except (OSError, subprocess.SubprocessError):
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
VERIFY_PROBE_TIMEOUT_SECONDS = 15
TERM_COUNT_TIMEOUT_SECONDS = 25
MAX_TERM_COUNT_WORKERS = 3
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
        # 只有「开始采集」和「添加成员」会启动临时 Windows 代理。
        self.capture = capture or CaptureSession(self.store.root / 'capture',
                                                  clock=self._clock, system_proxy=True)
        # 采集时代为唤起小程序: 少一步「去微信里翻出它」。
        self._open_applet = open_applet or open_mini_program
        self._capture_switch_intent = False  # 「添加成员」发起: 抓到就切到新成员
        self._next_sweep = time.monotonic() + 300   # 全成员巡检: 启动 5 分钟后首巡(避开开机峰值)
        self._sweeping = False
        self._cert_cache = (0.0, None)
        self._flow_factory = flow_factory   # 测试注入; 正式路径现场构造 DailyFlow
        # 续期器也允许注入: 单测里给一个假的, 就不必碰网络和子进程。
        self.renewer = renewer or CredentialRenewer(
            self.store, clock=self._clock, probe=self._probe_record, check_cancelled=self._check_cancelled)
        self.events = queue.Queue()
        self.busy = False
        self.closed = False
        self._gate = threading.Lock()
        self._poll_at = 0.0
        self.cancel = threading.Event()
        self._capture_interrupted = threading.Event()
        self._verification_probe = None
        self._term_count_lock = threading.Lock()
        self._term_count_workers = {}
        self._term_count_results = {}
        self.latest = None
        # 多成员轮跑: 前缀贴到每个进度文案上, 告诉界面现在跑的是谁。
        self._run_member_prefix = ''
        self._sweeping_run = False   # 轮跑中不给单个成员走续期/采集阶梯
        self._next_run_all = 0.0     # 两轮轮跑之间的间隔, 失败重试不轰炸
        self._term_quota = {}        # 成员 ID → 当天已确认达到的学期上限
        # 初始状态不是「发生过的事」: 每造一个控制器就往历史里写一行, 记录页会被
        # 「尚未开始今天的跑步」刷满(测试和预览尤其明显)。
        self._set_latest('idle', '尚未开始今天的跑步', record=False)

    # ------------------------------------------------------------- 状态
    def _set_latest(self, state, message, detail='', record=True, progress=None, *,
                    term_run_limit=None):
        result = {'state': state, 'message': message, 'detail': detail,
                  'at': self._clock().isoformat(timespec='seconds'),
                  'progress': progress if isinstance(progress, dict) else None}
        self.latest = result
        if record:
            try:
                self.store.record(state, message, detail, term_run_limit=term_run_limit)
            except Exception:  # noqa: BLE001 —— 状态记录失败不影响主流程
                pass
        return result

    def _progress(self, step, total, labels, text):
        """跑步进行中的一拍进度: 只刷 latest, 不写运行记录(历史留给结果)。

        step/total 给界面画分段进度条, labels 是每段的固定叫法, text 是这一拍
        的具体动作。收尾状态(signed/error/…)不带 progress, 进度条自然消失。
        """
        if self._run_member_prefix:
            text = f'{self._run_member_prefix}{text}'
        return self._set_latest('running', text, record=False,
                                progress={'step': int(step), 'total': int(total),
                                          'labels': [str(x) for x in labels]})

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

    def term_progress(self, member_id):
        with self._term_count_lock:
            return self._term_count_results.get(member_id, {}).copy()

    def _query_member_term_count(self, member_id, stop):
        # 按成员 ID 读档。后台轮跑或用户换档后，读取仍属于发起时的成员。
        record = self.store.member_token_record(member_id)
        identity = self.store.member_identity(member_id)
        try:
            uid = int(identity.get('uid') or 0)
            school_id = int(identity.get('school_id') or 0)
        except (ValueError, TypeError):
            uid = school_id = 0
        if not record or not record.get('token') or not uid or not school_id:
            raise LptiyuError('need_token', '当前成员缺少可用凭证，请先同步或采集凭证')
        student_num = str(identity.get('student_num') or '')
        client = ApiClient(uid=uid, token=record['token'], school_id=school_id,
                           student_num=student_num,
                           card_id=str(identity.get('card_id') or student_num),
                           proxy=self.store.settings().api_proxy or None, pace=0.8)
        client.stop_event = stop
        return lptiyu_flow.query_term_count(client, stop)

    def start_term_count(self, member_id):
        """刷新一位成员的学期次数，不占用跑步和采集的操作闸。"""
        if not member_id or member_id != self.store.active_member_id():
            return False
        stop = threading.Event()
        with self._term_count_lock:
            if self.closed:
                return False
            if member_id in self._term_count_workers:
                return self._term_count_results[member_id]['state'] == 'loading'
            if len(self._term_count_workers) >= MAX_TERM_COUNT_WORKERS:
                return False
            self._term_count_workers[member_id] = stop
            progress = self._term_count_results.setdefault(member_id, {'count': None, 'checked_at': ''})
            progress.update(state='loading', message='')

        def expire():
            with self._term_count_lock:
                if self._term_count_workers.get(member_id) is stop and not stop.is_set():
                    stop.set()
                    progress.update(state='error', message='查询学期跑步次数超时，请稍后重试')

        timeout = threading.Timer(TERM_COUNT_TIMEOUT_SECONDS, expire)
        timeout.daemon = True

        def work():
            result = {'state': 'error', 'message': '未能读取学期跑步次数，请稍后重试'}
            try:
                count = self._query_member_term_count(member_id, stop)
                result = {'state': 'ready', 'count': count, 'message': '',
                          'checked_at': self._clock().isoformat(timespec='seconds')}
            except LptiyuError as exc:
                result = {'state': exc.state, 'message': exc.message}
            except ApiError as exc:
                if exc.code == 101:
                    result = {'state': 'need_token', 'message': '当前成员的凭证已失效，请先更新凭证'}
            except Exception:  # noqa: BLE001 —— 探测失败只影响学期次数
                pass
            finally:
                timeout.cancel()
                with self._term_count_lock:
                    if not stop.is_set():
                        progress.update(result)
                    del self._term_count_workers[member_id]

        threading.Thread(target=work, name='youziauth-lptiyu-term-count', daemon=True).start()
        timeout.start()
        return True

    # ------------------------------------------------------------- 入口
    def start(self, action):
        if action not in ('run', 'auto_run', 'dry', 'sync', 'renew', 'renew_auto', 'zones',
                          'capture', 'capture_add', 'zone_apply'):
            return False
        with self._gate:
            if self.closed or self.busy:
                return False
            self.busy = True
            self.cancel.clear()
            self._capture_interrupted.clear()
        # 「添加成员」是显式的人为意图: 抓到新成员就切过去, 不用再手动换档。
        self._capture_switch_intent = (action == 'capture_add')

        def work():
            try:
                self._check_cancelled()
                if action == 'sync':
                    result = self._sync()
                elif action == 'zones':
                    result = self._sync_zones()
                elif action == 'zone_apply':
                    result = self._apply_zone()
                elif action in ('capture', 'capture_add'):
                    result = self._capture_flow()
                elif action == 'renew':
                    result = self._renew()
                elif action == 'renew_auto':
                    result = self._renew_auto()
                elif action == 'dry':
                    result = self._execute(submit=False)
                elif action == 'auto_run':
                    result = self._execute_automatic()
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
                    self._capture_switch_intent = False
                    self.busy = False

        threading.Thread(target=work, name='youziauth-lptiyu-' + action,
                         daemon=True).start()
        return True

    def poll(self):
        if self.closed or time.monotonic() < self._poll_at:
            return
        self._poll_at = time.monotonic() + 5
        try:
            settings = self.store.settings()
        except Exception:  # noqa: BLE001 —— 设置坏了不轰炸，由快照呈现
            return
        if not settings.enabled:
            return
        # 全成员保活巡检: 旁席成员的 token 死了要尽早亮出来, 别等切过去那天才发现。
        if self._sweep_due(settings):
            self._start_sweep()
        current = self._clock()
        try:
            in_window = (parse_time(settings.time_lo) <= current.time()
                         <= parse_time(settings.time_hi))
            run_ready = current.time() >= parse_time(settings.run_time)
        except ValueError:
            return
        # 保活和「今天跑没跑完」「在不在有效时段」都无关: token 是按天死的, 晚上
        # 十一点打开面板也该立刻探一次, 而不是干等到第二天六点的窗口 —— 2026-11-12
        # 现场: 23:08 装上修好的版本, 16:49 的 next_due 一直挂着不动, 看着像没修。
        # 它仍然排在所有「跑步」闸门(已打卡、自动尝试次数上限、还没到点)前面。
        if self._renewal_due(settings):
            self.start('renew_auto')
            return
        if not in_window or not run_ready:
            return
        if not settings.run_all_members:
            # 单成员(旧行为): 只跑活动成员这一个账号。
            if self.store.is_signed_today(current.date()):
                return
            if self.store.daily().get('auto', 0) >= MAX_DAILY_AUTO_ATTEMPTS:
                return
            if (settings.term_run_limit and self._term_quota.get(self.store.active_member_id())
                    == (current.date(), settings.term_run_limit)):
                return
            self.start('auto_run')
            return
        # 多成员轮跑: 任何一个已添加、有凭证、今天还没跑完的账号都轮到。
        if time.monotonic() < self._next_run_all:
            return
        pending = self._pending_members()
        if not pending:
            return
        self._start_run_all(pending)

    def drain(self):
        values = []
        while True:
            try:
                result = self.events.get_nowait()
            except queue.Empty:
                return values
            if not result.get('notice_only'):
                self.latest = result
            values.append(result)

    def cancel_work(self):
        with self._gate:
            self.cancel.set()
            self._capture_interrupted.set()

    def _check_cancelled(self):
        if self.cancel.is_set():
            raise LptiyuError('cancelled', '已取消当前操作')

    def _wait_for_work(self, timeout=8.0):
        """等待在跑的一拍结束(测试与退出路径用)。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.busy:
            time.sleep(0.02)

    def close(self):
        with self._gate:
            self.closed = True
        self.cancel_work()
        with self._term_count_lock:
            for stop in self._term_count_workers.values():
                stop.set()
        try:
            self.capture.stop('idle', '')
        except Exception:  # noqa: BLE001 —— 退出流程不该被采集卡住
            pass

    # ------------------------------------------------------------- 成员巡检
    def _sweep_due(self, settings):
        if self._sweeping or self.busy or self.closed:
            return False
        try:
            interval = max(60, int(settings.renew_interval_min or 180))
        except (TypeError, ValueError):
            interval = 180
        if time.monotonic() >= self._next_sweep:
            self._next_sweep = time.monotonic() + interval * 60
            return True
        return False

    def _start_sweep(self):
        self._sweeping = True

        def work():
            try:
                self._sweep_members()
            finally:
                self._sweeping = False

        threading.Thread(target=work, name='youziauth-lptiyu-sweep',
                         daemon=True).start()

    def _sweep_members(self):
        """旁席成员保活巡检: 只读 getTermList, 结果写进各成员自己的台账。

        死了只亮出来、不自动修 —— 非活动成员的凭证只能由本人重开小程序
        (或重发一次提取文件)才有新号。链路故障不判死(与续期同一语义)。
        """
        store = self.store
        try:
            members = store.members_status()
        except Exception:               # noqa: BLE001 —— 巡检不能拖垮主循环
            return
        for item in members['items']:
            if item['active'] or not item['has_token']:
                continue
            record = store.member_token_record(item['id'])
            if not record or not record.get('token'):
                continue
            identity = store.member_identity(item['id'])
            try:
                uid = int(identity.get('uid') or 0) or int(item['uid'] or 0)
                school_id = int(identity.get('school_id') or 0)
                student_num = str(identity.get('student_num') or item['student_num'] or '')
            except (TypeError, ValueError):
                continue
            if not uid or not school_id:
                continue
            client = ApiClient(uid=uid, token=record['token'],
                               school_id=school_id, student_num=student_num,
                               card_id=str(identity.get('card_id') or student_num),
                               pace=0.0)
            try:
                alive = self._probe_alive(client)
            except Exception:           # noqa: BLE001 —— 网络类故障不判死
                continue
            store.record_probe(alive, member_id=item['id'])

    # ------------------------------------------------- 多成员自动跑步
    def _pending_members(self):
        """今天还没跑完、凭证还在、自动尝试次数没用完的成员清单。

        「今天」统一按 store 自己的时钟算 —— mark_signed/bump_daily 落盘用的
        都是它, 检查也用同一把尺, 免得两个时钟在跨零点时对不上号。
        """
        try:
            members = self.store.members_status()
        except Exception:               # noqa: BLE001 —— 读不动就不轮跑这拍
            return []
        day = now().date().isoformat()
        run_date = self._clock().date()
        pending = []
        for item in members.get('items') or []:
            if not item.get('has_token'):
                continue
            limit = item.get('term_run_limit')
            if limit is not None and self._term_quota.get(item['id']) == (run_date, limit):
                continue
            signed = self.store.member_signed_record(item['id'])
            if signed.get('date') == day:
                continue
            daily = self.store.member_daily(item['id']).get(day)
            attempts = int(daily.get('auto', 0) or 0) if isinstance(daily, dict) else 0
            if attempts >= MAX_DAILY_AUTO_ATTEMPTS:
                continue
            pending.append(item)
        return pending

    def _start_run_all(self, pending):
        with self._gate:
            if self.closed or self.busy:
                return False
            self.busy = True
            self.cancel.clear()

        def work():
            try:
                self._run_all_members(pending)
            except LptiyuError as exc:
                self._set_latest(exc.state, exc.message)
            except Exception:  # noqa: BLE001
                self._set_latest('error', '跑步操作未完成，请稍后重试')
            finally:
                with self._gate:
                    self._capture_auto = False
                    self._capture_switch_intent = False
                    self._next_run_all = time.monotonic() + 120
                    self.busy = False

        threading.Thread(target=work, name='youziauth-lptiyu-run-all',
                         daemon=True).start()
        return True

    def _run_all_members(self, pending):
        """把每个待跑成员轮着跑一遍: 顺序激活 → 跑 → 记台账 → 回到原活动成员。

        整条跑步链(凭证/续期/轨迹/提交/留档)都读写「活动成员」的目录 —— 与其复制
        一条旁路, 不如顺序激活每个成员让现有链路原样工作, 跑完把活动指针放回
        原处。谁的号死了记谁的台账然后跳过, 不拖垮队列; 全程可取消。
        """
        store = self.store
        original = ''
        try:
            original = store.members_status().get('active') or ''
        except Exception:               # noqa: BLE001
            return
        self._sweeping_run = True
        try:
            for index, item in enumerate(pending):
                if self.cancel.is_set():
                    break
                name = item.get('name') or item['id']
                self._run_member_prefix = f'「{name}」 '
                try:
                    store.switch_member(item['id'])
                except MemberError:
                    continue              # 成员中途被删: 跳过
                self._set_latest('running',
                                 f'自动跑步：正在为「{name}」打卡（{index + 1}/{len(pending)}）…',
                                 record=False)
                try:
                    self._execute_automatic()
                except LptiyuError as exc:
                    self._set_latest(exc.state, f'「{name}」{exc.message}')
                except Exception:  # noqa: BLE001
                    self._set_latest('error', f'「{name}」跑步操作未完成，请稍后重试')
                if index + 1 < len(pending):
                    # 成员之间歇一歇: 几个账号背靠背连着提交不像人
                    self.cancel.wait(random.uniform(20.0, 60.0))
        finally:
            self._sweeping_run = False
            self._run_member_prefix = ''
            try:
                if original and store.members_status().get('active') != original:
                    store.switch_member(original)
            except Exception:           # noqa: BLE001 —— 还原失败不掩盖跑步结果
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
                'port': int(status.get('port') or 0),
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

    def capture_diagnose(self):
        """从本机配置与上次采集的观测诊断，不主动访问服务。"""
        protocol = wechat_protocol_registered()
        installed = wechat_installed()
        running = _wechat_running()
        try:
            cert = self.capture.cert_status()
        except Exception:              # noqa: BLE001
            cert = {'installed': False, 'fingerprint': ''}
        capture = self.capture.status()
        connects = int(getattr(self.capture, 'connects', 0))
        try:
            log_tail = (self.store.root / 'capture' / 'capture.log').read_text(
                encoding='utf-8', errors='replace').splitlines()[-15:]
        except OSError:
            log_tail = []
        report = {
            'wechat_protocol': protocol,
            'wechat_installed': installed,
            'wechat_running': running,
            'cert': cert,
            'port': int(capture.get('port') or 0),
            'capture_state': capture.get('state'),
            'connects': connects,
            'log_tail': log_tail,
        }
        if not installed:
            verdict = '本机没有找到微信（精简版/商店版也算没有）：装官方经典版微信后再试'
        elif not running:
            verdict = '微信没在运行：先打开微信、确认能进小程序，再点开始采集'
        elif not cert.get('installed'):
            verdict = '采集证书尚未安装'
        elif connects == 0:
            verdict = '尚未收到乐跑请求，请开始采集并在微信小程序进入跑步首页'
        else:
            verdict = f'已收到 {connects} 次乐跑连接'
        report['verdict'] = verdict
        return report

    def capture_stop(self):
        self._capture_interrupted.set()
        self.capture.stop('idle', '已停止采集')
        status = self.capture.status()
        return self._set_latest(status.get('state', 'idle'),
                                status.get('message') or '已停止采集', record=False)

    def recover_network(self):
        """结束活动采集，并用可信的备份尝试恢复遗留代理。"""
        release_error = ''
        with self._gate:
            self._capture_interrupted.set()
            capture_state = self.capture.status().get('state')
            released = capture_state in ('capturing', 'waiting')
            if released or capture_state == 'error':
                self.capture.stop('idle', '已释放采集连接')
                status = self.capture.status()
                if status.get('state') == 'error':
                    release_error = status.get('message') or '采集代理尚未恢复，请稍后重试。'
        try:
            state = proxy_guard.restore_owned_capture_proxy(
                self.store.root / 'capture' / proxy_guard.BACKUP_NAME)
        except OSError:
            return {'state': 'error', 'message': '无法检查 Windows 代理，请在系统设置中核对。'}
        if state == 'restored':
            return {'state': 'restored', 'message': '已恢复采集前的 Windows 代理。'}
        if state == 'unverified':
            message = '本机代理端口不可用，无法确认原设置，请在 Windows 代理设置中核对。'
            try:
                os.startfile('ms-settings:network-proxy')
            except (AttributeError, OSError):
                pass
            return {'state': 'manual', 'message': message}
        if release_error:
            return {'state': 'error', 'message': release_error}
        if released:
            return {'state': 'released', 'message': '已释放采集连接。'}
        return {'state': 'unchanged', 'message': '当前没有需要恢复的采集代理。'}

    def _capture_flow(self):
        """等一次小程序登录, 把 token 与身份一起落盘, 然后当真门探活。"""
        cred = self._receive_capture()
        if not cred:
            status = self.capture.status()
            return self._set_latest('idle', status.get('message') or '已停止采集', record=False)
        return self._file_manual_capture(cred)

    def _receive_capture(self):
        self._check_cancelled()
        if self._capture_interrupted.is_set():
            return None
        try:
            self.capture.start(stop_event=self._capture_interrupted)
        except CaptureError as exc:
            if self.cancel.is_set():
                self._check_capture_error()
                self._check_cancelled()
            if self._capture_interrupted.is_set():
                return None
            raise LptiyuError('error', str(exc)) from exc
        try:
            self._check_cancelled()
            if self._capture_interrupted.is_set():
                return None
            return self._wait_for_capture()
        finally:
            self._stop_if_capturing()
            if self.cancel.is_set():
                self._check_capture_error()

    def _check_capture_error(self):
        status = self.capture.status()
        if status.get('state') == 'error':
            raise LptiyuError('error', status.get('message') or '采集停止时发生错误')

    def _stop_if_capturing(self):
        """兜底收尾: 采集还在等就立刻停掉(正常路径已经由 wait/poll 停过)。"""
        try:
            state = self.capture.status().get('state')
        except Exception:              # noqa: BLE001 —— 状态读不到也不影响退出
            return
        if state in ('waiting', 'capturing'):
            try:
                self.capture.stop('idle', '')
            except Exception:          # noqa: BLE001 —— 收尾必须尽力
                pass

    def _wait_for_capture(self):
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
        self._set_latest(
            'capturing',
            '已打开小程序，等待它发出第一个请求…' if launched
            else (reason or '请在微信里打开「数体智慧体育」并进入跑步首页'),
            record=False)
        started_at = time.monotonic()
        hint_at = started_at + 25
        last_check = started_at

        def tick():
            """检查本地监听存活，并在客户端未送来请求时提示。"""
            nonlocal hint_at, last_check
            stamp = time.monotonic()
            if stamp - last_check >= 10:
                last_check = stamp
                if self.capture.ensure_proxy():
                    return
            if hint_at and stamp >= hint_at and not getattr(self.capture, 'connects', 0):
                hint_at = 0
                if callable(log):
                    log('25 秒内未收到乐跑连接')
                self._set_latest('capturing', '还没收到乐跑请求，请在小程序进入跑步首页',
                                 record=False)

        self._check_cancelled()
        cred = self.capture.wait(on_tick=tick, stop_event=self._capture_interrupted)
        self._check_capture_error()
        self._check_cancelled()
        if self._capture_interrupted.is_set():
            return None
        if not cred:
            status = self.capture.status()
            if status.get('state') == 'idle':
                return None
            raise LptiyuError('need_token', status.get('message') or '没有抓到凭证')
        return cred

    def _file_manual_capture(self, cred):
        # 归档认身份不认活动指针: 抓到谁就归给谁(对不上就建新成员)。
        filed = self.store.file_captured_credential(cred)
        label = str(filed.get('name') or '成员')
        if self._capture_switch_intent and not filed.get('is_active'):
            # 「添加成员」的意图就是接下来给 TA 跑: 归档完直接切过去。
            self.store.switch_member(filed['id'])
            filed['is_active'] = True
        if not filed.get('is_active'):
            # 归档给了旁席成员: 探活属于活动成员的槽位, 不越权代查;
            # 换到那一档时真门探活自然会确认。
            return self._set_latest('synced',
                                    f'已为「{label}」归档凭证，活动成员未变')
        client = self.slot.make_client(pace=0.0)
        try:
            alive = self._probe_alive(client)
        except ApiError as exc:
            self._check_cancelled()
            raise LptiyuError('error', f'凭证已保存，但探活失败（{exc.code}）') from exc
        self._check_cancelled()
        if not alive:
            self.slot.mark_dead()
            raise LptiyuError('need_token', '抓到的凭证当场失效了：在小程序里重新登录后再采集')
        self.store.record_probe(True)
        if self._capture_switch_intent and len(self.store.members_status()['items']) > 1:
            return self._set_latest('synced', f'已采集「{label}」的凭证并切换过去，确认可用')
        return self._set_latest('synced', f'已采集「{label}」的凭证，并确认可用')

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
        """流水线读到真机点位后留档: 每块场地的朝向/宽窄不同, 特调各算各的。

        只在「还没选过跑区」时才把服务器当前跑区写进设置 —— 用户自己选了场地,
        选择权就在他手上, 反过来被小程序带跑会让每次跑完都跳回旧场地。
        """
        try:
            if zone_list:
                self.store.save_zone_list(zone_list)
            self.store.save_zone_points(zone_id, zone_name, points)
            if not self.store.settings().zone_id:
                self.store.save_zone_selection(zone_id, zone_name)
        except Exception:  # noqa: BLE001 —— 留档失败不影响这一次跑步
            pass

    def _sync_zones(self):
        """把每个跑区的打卡点各抓一遍。

        完成时保留用户选的跑区，取消时恢复同步前服务器上的跑区。
        """
        self._ensure_credentials()
        self._check_cancelled()
        client = self.slot.make_client(pace=0.8)
        try:
            cfg = client.before_run()
        except ApiError as exc:
            self._check_cancelled()
            raise LptiyuError('need_token' if exc.code == 101 else 'error',
                              '拉取跑区失败：凭证已失效' if exc.code == 101
                              else '拉取跑区失败，接口暂时不可用')
        self._check_cancelled()
        if not isinstance(cfg, dict):
            raise LptiyuError('error', '跑区信息格式异常，稍后再试')
        zones = [zone for zone in (cfg.get('run_zone_list') or [])
                 if isinstance(zone, dict) and str(zone.get('id') or '')]
        original = str(cfg.get('run_zone_id') or '')
        if not original:
            raise LptiyuError('error', '无法读取当前跑区，已停止同步')
        self.store.save_zone_list(zones)
        # 用户自己选过跑区时, 服务器那块会被他在设置里指定的替换掉: 先把他的选择
        # 推上去, 再以它为准遍历 —— 否则同步完会乖乖还原成小程序里的旧场地。
        chosen = str(self.store.settings().zone_id or '')
        current = last = original
        switched_zone = False
        done = set()
        try:
            if chosen and chosen != original:
                try:
                    self._check_cancelled()
                    switched_zone = True
                    client.set_zone(chosen)
                    self._check_cancelled()
                    switched = client.before_run()
                    self._check_cancelled()
                    if isinstance(switched, dict) and str(switched.get('run_zone_id') or '') == chosen:
                        cfg, current, last = switched, chosen, chosen
                except ApiError:
                    pass
            if not chosen:
                self.store.save_zone_selection(original, str(cfg.get('run_zone_name') or ''))
            self.store.save_zone_points(current, str(cfg.get('run_zone_name') or ''),
                                        self._points_of(cfg))
            done.add(current)
            for zone in zones[:MAX_ZONE_SYNC]:
                self._check_cancelled()
                zone_id = str(zone.get('id') or '')
                if not zone_id or zone_id in done:
                    continue
                last = zone_id
                try:
                    switched_zone = True
                    client.set_zone(zone_id)
                    self._check_cancelled()
                    other = client.before_run()
                except ApiError:
                    continue
                self._check_cancelled()
                if not isinstance(other, dict) or str(other.get('run_zone_id') or '') != zone_id:
                    continue
                self.store.save_zone_points(
                    zone_id, str(other.get('run_zone_name') or zone.get('name') or ''),
                    self._points_of(other))
                done.add(zone_id)
        finally:
            target = chosen or original
            if not self.cancel.is_set() and target != last:
                try:
                    switched_zone = True
                    client.set_zone(target)
                except (ApiError, OSError):
                    pass
            # 最后一次切换等待响应时也可能收到取消请求。
            if self.cancel.is_set() and switched_zone:
                try:
                    client.set_zone(original)
                except (ApiError, OSError) as exc:
                    raise LptiyuError('error', '已停止同步，但未能恢复原跑区') from exc
        self._check_cancelled()
        return self._set_latest('zones', f'已同步 {len(done)} 个跑区的点位')

    def _apply_zone(self):
        """把「我选的跑区」推到服务端: 小程序那边也会跟着切过去。

        跑区是会话态, 只能 setRunZone 改; 切完必须再读一次 beforeRun 确认,
        不然界面说切好了、实际还在旧场地 —— 那样提交的轨迹就是错的场地。
        """
        settings = self.store.settings()
        zone_id = str(settings.zone_id or '')
        if not zone_id:
            raise LptiyuError('error', '先在跑步设置里选一个跑区')
        self._ensure_credentials()
        self._check_cancelled()
        client = self.slot.make_client(pace=0.8)
        try:
            cfg = client.before_run()
        except ApiError as exc:
            raise LptiyuError('need_token' if exc.code == 101 else 'error',
                              '读取跑区失败：凭证已失效' if exc.code == 101
                              else '读取跑区失败，接口暂时不可用') from exc
        self._check_cancelled()
        current = str((cfg or {}).get('run_zone_id') or '')
        if current != zone_id:
            try:
                client.set_zone(zone_id)
                if self.cancel.is_set():
                    return self._set_latest('cancelled', '跑区切换请求已发送，已停止后续确认')
                cfg = client.before_run()
            except ApiError as exc:
                raise LptiyuError('error', f'切换跑区失败：{exc}') from exc
        got = str((cfg or {}).get('run_zone_id') or '')
        if got != zone_id:
            raise LptiyuError('error',
                              f'切换没生效：服务器还是 {got or "未知"}，稍后再试')
        name = str((cfg or {}).get('run_zone_name') or settings.zone_name or '')
        try:
            self.store.save_zone_points(zone_id, name, self._points_of(cfg))
            self.store.save_zone_list([zone for zone in ((cfg or {}).get('run_zone_list') or [])
                                       if isinstance(zone, dict)])
            self.store.save_zone_selection(zone_id, name)
        except Exception:              # noqa: BLE001 —— 留档失败不影响切换结果
            pass
        return self._set_latest('zones', f'已切到 {name or zone_id}，小程序也会用这块场地')

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
        outcome = self._renew_credentials(force=True)
        return self._set_latest(RENEW_STATES.get(outcome.state, 'error'), outcome.message,
                                record=False)

    def _renew_auto(self):
        """后台保活: 成功不打扰界面, 只有「需要用户动一次」才冒泡。

        跑完当天也照常保活 —— 凭证是按天死的, 今天不用不代表明天不用。成功时
        不写 latest: 续期是维护动作, 不该把「今日跑步已完成」顶成「凭证可用」;
        台账(renewal.json)里有痕, 界面那行「上次 13:49」就是它。
        """
        outcome = self._renew_credentials(force=False)
        if outcome.state != 'need_user':
            return None
        at = self._clock()
        if self.store.is_signed_today(at.date()):
            return {'state': 'need_token', 'message': outcome.message,
                    'at': at.isoformat(timespec='seconds'), 'notice_only': True}
        return self._set_latest('need_token', outcome.message, record=False)

    def _renew_credentials(self, force):
        self._check_cancelled()
        outcome = self.renewer.renew(force=force)
        self._check_cancelled()
        return outcome

    def _ensure_credentials(self, renew=True):
        """返回可用凭证; 拿不到就抛 LptiyuError(need_token)。

        阶梯统一交给 renewer: 探活 → 同步已有凭证文件,
        每一步都用真门探活收尾。到点自动跑或用户手动提交, 都按 force=True 走 ——
        用户/日程已经表达了「现在要用凭证」, 不该再被台账冷却挡回去。
        多成员轮跑传 renew=False: 续期设置针对当前活动成员。手动验证也只检查
        当前凭证, 由 _execute 决定如何处理探活结果。
        """
        self._check_cancelled()
        outcome = self._renew_credentials(force=True) if renew else None
        record = None
        try:
            record = self.store.token_record()
        except OSError:
            self.store.clear_token()
        if record and record.get('token'):
            return record
        raise LptiyuError('need_token',
                          outcome.message if outcome is not None else '没有可用凭证')

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

    def _probe_for_verification(self, client):
        self._check_cancelled()
        if self._verification_probe is not None and self._verification_probe.is_alive():
            raise LptiyuError('error', '上次凭证查询仍在结束，请稍后重试')

        stop = threading.Event()
        if isinstance(client, ApiClient):
            client.stop_event = stop
        responses = queue.Queue(maxsize=1)

        def probe():
            # DNS 或接收响应可能阻塞。只有当前验证能根据返回值修改凭证。
            try:
                alive = self._probe_alive(client)
            except Exception:  # noqa: BLE001
                responses.put(None)
            else:
                responses.put(alive)

        self._verification_probe = threading.Thread(target=probe, daemon=True,
                                                    name='youziauth-lptiyu-verify-credential')
        self._verification_probe.start()
        deadline = time.monotonic() + VERIFY_PROBE_TIMEOUT_SECONDS
        try:
            while True:
                self._check_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LptiyuError('error', '凭证查询超时，请稍后重试')
                try:
                    alive = responses.get(timeout=min(0.1, remaining))
                except queue.Empty:
                    continue
                self._check_cancelled()
                if alive is None:
                    raise LptiyuError('error', '暂时无法确认凭证，请检查网络后重试')
                return alive
        finally:
            stop.set()

    def _flow(self, settings, progress_fn=None):
        if self._flow_factory is not None:
            flow = self._flow_factory(self.slot, settings)
            # 测试/注入的流程对象没有 progress_fn 形参, 统一按属性挂上。
            flow.progress_fn = progress_fn or (lambda stage, text: None)
            return flow
        pace_lo, pace_hi = settings.pace_range()
        policy = {
            'max_per_day': settings.max_per_day,
            'term_run_limit': settings.term_run_limit,
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
                                     zone_sink=self._zone_sink,
                                     progress_fn=progress_fn)

    def _execute_automatic(self):
        result = None
        try:
            result = self._execute(submit=True)
            return result
        finally:
            if result is None or result['state'] != 'term_quota':
                try:
                    self.store.bump_daily(kind='auto')
                except Exception:  # noqa: BLE001 —— 台账写入失败不覆盖跑步结果
                    pass

    def _execute(self, submit):
        # 界面的分段进度条: 提交跑 4 段, 本地验证不提交只有 3 段。
        labels = ['准备凭证', '生成轨迹', '提交成绩', '等待判定'] if submit \
            else ['准备凭证', '生成轨迹', '本地校验']
        total = len(labels)
        self._progress(1, total, labels,
                       '开始今天的跑步：正在检查凭证与账号状态…' if submit
                       else '开始验证：正在检查凭证与账号状态…')
        settings = self.store.settings()
        try:
            record = self._ensure_credentials(renew=submit and not self._sweeping_run)
        except LptiyuError as exc:
            if exc.state == 'need_token' and not submit:
                raise LptiyuError('need_token', '当前成员没有可用凭证，请更新或导入凭证后再验证') from None
            raise
        self._progress(1, total, labels, '凭证就绪，正在确认账号状态…')
        client = self.slot.make_client(pace=0.8)
        alive = self._probe_alive(client) if submit else self._probe_for_verification(client)
        self._check_cancelled()
        if not alive:
            self.slot.mark_dead()
            if not submit:
                raise LptiyuError('need_token', '凭证已失效，请更新或导入凭证后再验证')
            if self._sweeping_run:
                # 多成员轮跑绝不中途开续期/采集阶梯: 那会挂起整个队列去等一个
                # 不会有人打开的小程序。谁的号死了记下来跳过, 让本人重开小程序
                # 或导入新文件后再说。
                self.store.bump_daily(kind='run')
                raise LptiyuError('need_token',
                                  f'{self.slot.label} 的凭证已失效，跳过（让本人重开一次小程序再导入）')
            # 续期刚过、起跑前这半秒号又死了(小程序那边刚重登): 再走一遍续期阶梯,
            # 拿到新号就直接继续, 不让这一拍白跑。
            outcome = self._renew_credentials(force=True)
            fresh = None
            try:
                fresh = self.store.token_record()
            except OSError:
                self.store.clear_token()
            if not fresh or not fresh.get('token') or fresh['token'] == record['token']:
                self.store.bump_daily(kind='run')
                raise LptiyuError('need_token', outcome.message)
            client = self.slot.make_client(pace=0.8)

        self._check_cancelled()
        flow = self._flow(settings, progress_fn=lambda stage, text:
                          self._progress(stage, total, labels, text))
        summary, code = flow.run(submit=submit, zone_override=settings.zone_id or None)
        if code == 9:
            return self._set_latest('cancelled', '已取消当前操作')
        if submit and settings.term_run_limit is not None:
            member_id = self.store.active_member_id()
            if code == lptiyu_flow.TERM_QUOTA_REACHED:
                self._term_quota[member_id] = self._clock().date(), settings.term_run_limit
                count, limit = summary['term_count'], summary['term_run_limit']
                return self._set_latest('term_quota', self._run_member_prefix +
                                        f'本学期有效跑步已达上限（{count}/{limit} 次），本次跳过',
                                        term_run_limit=limit)
            self._term_quota.pop(member_id, None)
            if code == lptiyu_flow.TERM_COUNT_UNAVAILABLE:
                return self._set_latest('term_count_unavailable', self._run_member_prefix +
                                        '未能读取本学期有效跑步次数，本次跳过',
                                        term_run_limit=settings.term_run_limit)
        if submit and isinstance(summary, dict):
            self._record_sample(summary, settings)
        if not submit:
            if code == 0:
                return self._set_latest('checked', '全链验证通过，未提交')
            if code == 2:
                return self._set_latest('need_token', '验证时发现凭证已失效，请先更新凭证')
            return self._set_latest('error', '验证未通过，请稍后重试')

        if code in (0, 5, lptiyu_flow.SUBMITTED_REVIEW_CANCELLED) and isinstance(summary, dict):
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
            if code == lptiyu_flow.SUBMITTED_REVIEW_CANCELLED:
                return self._set_latest('uncertain', '记录已提交，已停止等待判定',
                                        detail=f'{zone} · {distance_text}')
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
        if code == 2:
            raise LptiyuError('need_token', '凭证已失效：在小程序里打开一次跑步首页，然后点「同步凭证」')
        return self._set_latest('error', '提交未完成，请查看运行记录')
