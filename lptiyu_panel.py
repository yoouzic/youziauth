"""乐跑模块的控制器: 线程编排 + 每日自动流水线(沿用寝室打卡的控制面板模式)。

自动流水线(每天 run_time 之后一拍):
  读取凭证 → 真门探测(101 才判死) → 死了先向研究库同步 → 仍无凭证且研究库
  可用则后台采集(等用户开一次小程序) → 拿到凭证跑完整流水线 → 结果落盘。
所有自动行为都有硬上限: 每天最多自动尝试 3 次、自动采集 2 次, 绝不无限重试。
"""
from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import time

from lptiyu import flow as lptiyu_flow
from lptiyu.client import ApiClient, ApiError
from lptiyu_store import Store, Settings, now, parse_time

MAX_DAILY_AUTO_ATTEMPTS = 3
MAX_DAILY_GRABS = 2
GRAB_TIMEOUT_SECONDS = 540

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
        """提交完成后探测一次: 活着就保留给下一次(跨天复用), 101 才判死。"""
        try:
            client.term_list()
        except ApiError:
            self.mark_dead()
            return
        except Exception:              # noqa: BLE001 —— 链路故障不判死, 留待下次探测
            return
        self.store.record_probe(True)

    def mark_dead(self):
        self.store.clear_token()
        self.store.record_probe(False)


class LptiyuController:
    def __init__(self, store=None, slot=None, clock=None, flow_factory=None):
        self.store = store or Store()
        self.slot = slot or TokenSlot(self.store)
        self._clock = clock or now
        self._flow_factory = flow_factory   # 测试注入; 正式路径现场构造 DailyFlow
        self.events = queue.Queue()
        self.busy = False
        self.closed = False
        self._gate = threading.Lock()
        self._poll_at = 0.0
        self.cancel = threading.Event()
        self.latest = None
        self._set_latest('idle', '尚未开始今天的跑步')

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
        if action not in ('run', 'dry', 'sync'):
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
                elif action == 'dry':
                    result = self._execute(submit=False)
                else:
                    result = self._execute(submit=True)
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
        if not settings.enabled or self.store.is_signed_today():
            return
        current = self._clock()
        try:
            if not (parse_time(settings.time_lo) <= current.time() <= parse_time(settings.time_hi)):
                return
            if current.time() < parse_time(settings.run_time):
                return
        except ValueError:
            return
        if self.store.daily().get('auto', 0) >= MAX_DAILY_AUTO_ATTEMPTS:
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

    # ------------------------------------------------------------- 步骤
    def _sync(self):
        changes, message = self.store.sync_from_lab()
        state = 'synced' if changes else 'idle'
        if not changes and message.startswith('未找到'):
            state = 'error'
        return self._set_latest(state, message)

    def _ensure_credentials(self):
        """返回可用凭证; 拿不到就抛 LptiyuError(need_token)。

        顺序: 本机凭证 → 研究库同步 → 后台采集(借用研究库采集链)。
        采集受每日硬上限约束, 且只在研究库存在时才尝试。
        """
        record = None
        try:
            record = self.store.token_record()
        except OSError:
            self.store.clear_token()
        if record and record['token']:
            return record
        self.store.sync_from_lab()
        try:
            record = self.store.token_record()
        except OSError:
            self.store.clear_token()
        if record and record['token']:
            return record
        grabs = self.store.daily().get('grab', 0)
        if grabs < MAX_DAILY_GRABS and self.store.lab_dir():
            self.store.bump_daily(kind='grab')
            self._set_latest('need_token', '等待凭证更新：请在微信中打开小程序「数体智慧体育」，'
                                            '到跑步首页停留片刻，其余自动完成', record=False)
            self._grab_via_lab()
            self.store.sync_from_lab()
            try:
                record = self.store.token_record()
            except OSError:
                self.store.clear_token()
            if record and record['token']:
                return record
        raise LptiyuError('need_token', '还没有可用的跑步凭证：在小程序里打开一次跑步首页，'
                                        '然后点「同步凭证」')

    def _grab_via_lab(self):
        """借用研究库的采集链抓一次凭证(需要小程序冷启动一次)。"""
        lab = self.store.lab_dir()
        script = os.path.join(lab, 'lpctl.py')
        selector = self.store.identity().get('student_num') or ''
        python = shutil.which('py')
        if python:
            command = [python, '-3', script]
        else:
            python = shutil.which('python')
            if not python:
                raise LptiyuError('need_token', '本机没有可用的 Python，无法自动采集凭证')
            command = [python, script]
        command += ['grab', '--timeout', '480']
        if selector:
            command += ['--member', selector]
        try:
            subprocess.run(command, capture_output=True, timeout=GRAB_TIMEOUT_SECONDS,
                           creationflags=CREATE_NO_WINDOW, cwd=lab)
        except (OSError, subprocess.TimeoutExpired):
            pass                  # 采集失败不致命: 下一步同步不到凭证自然报需要凭证

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
        policy = {
            'max_per_day': settings.max_per_day,
            'style': 'template',
            'dist_lo': settings.dist_lo,
            'dist_hi': settings.dist_hi,
            'pace_target': settings.pace_target or None,
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
                                     template_path=template_path)

    def _execute(self, submit):
        settings = self.store.settings()
        record = self._ensure_credentials()
        self._set_latest('running', '正在处理今天的跑步…' if submit else '正在验证全链路…',
                         record=False)
        client = self.slot.make_client(pace=0.8)
        if not self._probe_alive(client):
            self.slot.mark_dead()
            # 死号先补一次同步, 拿到新号就直接继续, 不让这一拍白跑
            self.store.sync_from_lab()
            fresh = None
            try:
                fresh = self.store.token_record()
            except OSError:
                self.store.clear_token()
            if not fresh or not fresh['token'] or fresh['token'] == record['token']:
                if submit:
                    self.store.bump_daily(kind='run')
                raise LptiyuError('need_token', '凭证已失效：在小程序里打开一次跑步首页，'
                                                '然后点「同步凭证」')
            client = self.slot.make_client(pace=0.8)

        flow = self._flow(settings)
        summary, code = flow.run(submit=submit, zone_override=settings.zone_id or None)
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
