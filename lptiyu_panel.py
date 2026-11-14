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
import shutil
import subprocess
import threading
import time

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
        self._capture_switch_intent = False  # 「添加成员」发起: 抓到就切到新成员
        self._next_sweep = time.monotonic() + 300   # 全成员巡检: 启动 5 分钟后首巡(避开开机峰值)
        self._sweeping = False
        self._cert_cache = (0.0, None)
        self._flow_factory = flow_factory   # 测试注入; 正式路径现场构造 DailyFlow
        # 续期器也允许注入: 单测里给一个假的, 就不必碰网络和子进程。
        self.renewer = renewer or CredentialRenewer(
            self.store, clock=self._clock, can_capture=self.capture_ready,
            probe=self._probe_record, grab=self._grab_via_lab)
        self.events = queue.Queue()
        self.busy = False
        self.closed = False
        self._gate = threading.Lock()
        self._poll_at = 0.0
        self.cancel = threading.Event()
        self.latest = None
        # 多成员轮跑: 前缀贴到每个进度文案上, 告诉界面现在跑的是谁。
        self._run_member_prefix = ''
        self._sweeping_run = False   # 轮跑中不给单个成员走续期/采集阶梯
        self._next_run_all = 0.0     # 两轮轮跑之间的间隔, 失败重试不轰炸
        # 初始状态不是「发生过的事」: 每造一个控制器就往历史里写一行, 记录页会被
        # 「尚未开始今天的跑步」刷满(测试和预览尤其明显)。
        self._set_latest('idle', '尚未开始今天的跑步', record=False)

    # ------------------------------------------------------------- 状态
    def _set_latest(self, state, message, detail='', record=True, progress=None):
        result = {'state': state, 'message': message, 'detail': detail,
                  'at': self._clock().isoformat(timespec='seconds'),
                  'progress': progress if isinstance(progress, dict) else None}
        self.latest = result
        if record:
            try:
                self.store.record(state, message, detail)
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

    # ------------------------------------------------------------- 入口
    def start(self, action):
        if action not in ('run', 'dry', 'sync', 'renew', 'renew_auto', 'zones',
                          'capture', 'capture_add', 'zone_apply'):
            return False
        with self._gate:
            if self.closed or self.busy:
                return False
            self.busy = True
            self.cancel.clear()
        # 「添加成员」是显式的人为意图: 抓到新成员就切过去, 不用再手动换档。
        self._capture_switch_intent = (action == 'capture_add')

        def work():
            try:
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
                self._capture_switch_intent = False

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
        if not in_window:
            return
        if not settings.run_all_members:
            # 单成员(旧行为): 只跑活动成员这一个账号。
            if self.store.is_signed_today(current.date()):
                return
            if self.store.daily().get('auto', 0) >= MAX_DAILY_AUTO_ATTEMPTS:
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
            return
        # 多成员轮跑: 任何一个已添加、有凭证、今天还没跑完的账号都轮到。
        if not run_ready or time.monotonic() < self._next_run_all:
            return
        pending = self._pending_members()
        if not pending:
            return
        if not any(item.get('has_token') for item in pending):
            # 一个有凭证的都没有: 走原本的采集兜底, 不空跑一轮。
            if not self.store.has_token() and self.auto_capture():
                return
        self._start_run_all(pending)

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
        pending = []
        for item in members.get('items') or []:
            if not item.get('has_token'):
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
                    self.busy = False
                self._capture_auto = False
                self._capture_switch_intent = False
                self._next_run_all = time.monotonic() + 120

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
                    self._execute(submit=True)
                except LptiyuError as exc:
                    self._set_latest(exc.state, f'「{name}」{exc.message}')
                except Exception:  # noqa: BLE001
                    self._set_latest('error', f'「{name}」跑步操作未完成，请稍后重试')
                try:
                    store.bump_daily(kind='auto')
                except Exception:       # noqa: BLE001
                    pass
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

    def capture_diagnose(self):
        """采集自检: 一条条量给你看, 外加一次真实的代理链测试。

        这一套是为了回答「明明点了开始采集, 为什么一个请求都没有」——
        微信在不在、跑的什么版本、代理链通不通、上次采集收没收到连接。
        """
        from lptiyu_capture import proxy_self_test

        protocol = wechat_protocol_registered()
        installed = wechat_installed()
        running = _wechat_running()
        try:
            cert = self.capture.cert_status()
        except Exception:              # noqa: BLE001
            cert = {'installed': False, 'fingerprint': ''}
        try:
            enable, server, _override = self.capture.proxy.read()
            proxy = {'enable': int(enable), 'server': str(server)}
        except Exception:              # noqa: BLE001
            proxy = {'enable': None, 'server': ''}
        proxy_ok, proxy_detail = proxy_self_test(self.capture.ca)
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
            'proxy': proxy,
            'proxy_ok': proxy_ok,
            'proxy_detail': proxy_detail,
            'capture_state': self.capture.status().get('state'),
            'connects': connects,
            'log_tail': log_tail,
        }
        if not installed:
            verdict = '本机没有找到微信（精简版/商店版也算没有）：装官方经典版微信后再试'
        elif not running:
            verdict = '微信没在运行：先打开微信、确认能进小程序，再点开始采集'
        elif not proxy_ok:
            verdict = f'代理链不通（{proxy_detail}）：先关掉 Clash 之类的代理软件再试'
        elif connects == 0:
            verdict = ('代理链没问题，但微信没把流量交出来：'
                       '微信设置里不要单独设代理，或换经典版微信')
        else:
            verdict = '采集链正常：微信已经连上，只是还没拿到凭证'
        report['verdict'] = verdict
        return report

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
            suffix = '（自动采集）' if auto else ''
            return self._set_latest('synced',
                                    f'已为「{label}」归档凭证，活动成员未变{suffix}')
        client = self.slot.make_client(pace=0.0)
        try:
            alive = self._probe_alive(client)
        except ApiError as exc:
            raise LptiyuError('error', f'凭证已保存，但探活失败（{exc.code}）') from exc
        if not alive:
            self.slot.mark_dead()
            raise LptiyuError('need_token', '抓到的凭证当场失效了：在小程序里重新登录后再采集')
        self.store.record_probe(True)
        if self._capture_switch_intent and len(self.store.members_status()['items']) > 1:
            return self._set_latest(
                'synced', f'已添加「{label}」的凭证并切换过去，确认可用' if auto
                else f'已采集「{label}」的凭证并切换过去，确认可用')
        return self._set_latest('synced', f'已自动采集「{label}」的凭证，并确认可用' if auto
                                else f'已采集「{label}」的凭证，并确认可用')

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
        # 用户自己选过跑区时, 服务器那块会被他在设置里指定的替换掉: 先把他的选择
        # 推上去, 再以它为准遍历 —— 否则同步完会乖乖还原成小程序里的旧场地。
        chosen = str(self.store.settings().zone_id or '')
        if chosen and chosen != original:
            try:
                client.set_zone(chosen)
                switched = client.before_run()
                if isinstance(switched, dict) and \
                        str(switched.get('run_zone_id') or '') == chosen:
                    cfg, original = switched, chosen
            except ApiError:
                pass
        if not chosen:
            # 没选过就跟服务器走: 小程序里换了场地, 设置要跟着更新
            self.store.save_zone_selection(original, str(cfg.get('run_zone_name') or ''))
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
        client = self.slot.make_client(pace=0.8)
        try:
            cfg = client.before_run()
        except ApiError as exc:
            raise LptiyuError('need_token' if exc.code == 101 else 'error',
                              '读取跑区失败：凭证已失效' if exc.code == 101
                              else '读取跑区失败，接口暂时不可用') from exc
        current = str((cfg or {}).get('run_zone_id') or '')
        if current != zone_id:
            try:
                client.set_zone(zone_id)
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
        outcome = self.renewer.renew(force=True)
        return self._set_latest(RENEW_STATES.get(outcome.state, 'error'), outcome.message,
                                record=False)

    def _renew_auto(self):
        """后台保活: 成功不打扰界面, 只有「需要用户动一次」才冒泡。

        跑完当天也照常保活 —— 凭证是按天死的, 今天不用不代表明天不用。成功时
        不写 latest: 续期是维护动作, 不该把「今日跑步已完成」顶成「凭证可用」;
        台账(renewal.json)里有痕, 界面那行「上次 13:49」就是它。
        """
        outcome = self.renewer.renew(force=False)
        if (outcome.state == 'need_user'
                and not self.store.is_signed_today(self._clock().date())):
            # 今天已经跑完了就别再喊「需要凭证」: 状态栏该留住「今日已完成」,
            # 这次失败在续期台账里(界面那行会写出原因), 明天到点前还会再试。
            return self._set_latest('need_token', outcome.message, record=False)
        return None

    def _ensure_credentials(self, renew=True):
        """返回可用凭证; 拿不到就抛 LptiyuError(need_token)。

        阶梯统一交给 renewer: 探活 → 研究库 → 主动采集(借用研究库采集链),
        每一步都用真门探活收尾。到点自动跑或用户手动点, 都按 force=True 走 ——
        用户/日程已经表达了「现在要用凭证」, 不该再被台账冷却挡回去。
        多成员轮跑传 renew=False: 台账/采集阶梯是为「当前活动成员」设计的,
        轮跑里给旁席成员跑一遍会把队列挂在没人打开的小程序上 —— 凭证是否
        还活着交给 _execute 里那一拍探活, 死号记台账跳过。
        """
        outcome = self.renewer.renew(force=True) if renew else None
        record = None
        try:
            record = self.store.token_record()
        except OSError:
            self.store.clear_token()
        if record and record.get('token'):
            return record
        raise LptiyuError('need_token',
                          outcome.message if outcome is not None else '没有可用凭证')

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

    def _flow(self, settings, progress_fn=None):
        if self._flow_factory is not None:
            flow = self._flow_factory(self.slot, settings)
            # 测试/注入的流程对象没有 progress_fn 形参, 统一按属性挂上。
            flow.progress_fn = progress_fn or (lambda stage, text: None)
            return flow
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
                                     zone_sink=self._zone_sink,
                                     progress_fn=progress_fn)

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
            record = self._ensure_credentials(renew=not self._sweeping_run)
        except LptiyuError as exc:
            # 没有凭证但有采集能力: 交给下一拍自己采, 不用人点「同步凭证」。
            if exc.state == 'need_token' and self._auto_capture_due():
                self._capture_requested = True
                raise LptiyuError(
                    'need_token',
                    '本机没有可用凭证：正在自动采集，微信打开小程序后就会继续') from None
            raise
        self._progress(1, total, labels, '凭证就绪，正在确认账号状态…')
        client = self.slot.make_client(pace=0.8)
        if not self._probe_alive(client):
            self.slot.mark_dead()
            if self._sweeping_run:
                # 多成员轮跑绝不中途开续期/采集阶梯: 那会挂起整个队列去等一个
                # 不会有人打开的小程序。谁的号死了记下来跳过, 让本人重开小程序
                # 或导入新文件后再说。
                if submit:
                    self.store.bump_daily(kind='run')
                raise LptiyuError('need_token',
                                  f'{self.slot.label} 的凭证已失效，跳过（让本人重开一次小程序再导入）')
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

        flow = self._flow(settings, progress_fn=lambda stage, text:
                          self._progress(stage, total, labels, text))
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
