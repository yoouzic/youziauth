"""凭证自动续期(保活): 低频探活 → 失效补号 → 跑步前提前确认。

为什么不是「定时刷新 token」: 服务端没有给出 refresh 接口 —— 新 token 只能由小程序
loginByCode 轮换产生, 旧号的死亡形态恒 status=101(实测结论见 lptiyu_store 模块头)。
所以「续期」在这里 = 守住「这台机器随时有一枚可用凭证」这件事:

  1) 真门探活(getTermList, 无副作用)确认手上这枚还能用 —— 这是唯一的存活判据;
  2) 只有 101 才判死; 网络故障只算「不知道」, 绝不清掉手上这枚凭证;
  3) 判死/无号时按 本机 → 研究库 → 主动采集 的阶梯补号, 每拿到一枚都探活收尾;
  4) 跑步前 renew_lead_min 分钟再确认一次, 让「到点自动跑」不因凭证失效而空跑;
  5) 后台动作全部有配额与冷却: 主动采集每天 ≤2 次、探活 ≤12 次、失败后冷却 20 分钟,
     绝不无限重试 —— 续期是维护, 不是刷接口。

state 是受控词表(与界面文案一一对应):
  ok        手上这枚还活着, 不需要换
  renewed   已经换到并确认了一枚新凭证
  need_user 需要用户在小程序里开一次跑步首页(研究库不在 / 采集次数用完)
  cooldown  上一次没成功, 冷却中
  capped    今天的探活次数用完了
  error     链路故障, 稍后按退避重试
  skipped   自动续期被关掉(手动点击不受此限)
"""
from __future__ import annotations

import dataclasses
import datetime as dt

from lptiyu_store import now, parse_time

MAX_DAILY_GRABS = 2          # 主动采集(要用户开一次小程序)的每日硬上限
MAX_DAILY_PROBES = 12        # 真门探活的每日硬上限: 界面每 5 秒一拍也不能把接口打爆
COOLDOWN_MINUTES = 20        # 一次没成功后不再纠缠的时间
BACKOFF_BASE_MINUTES = 15    # 链路故障的退避起点
BACKOFF_MAX_MINUTES = 120


@dataclasses.dataclass(frozen=True)
class RenewOutcome:
    """一次续期的结果。message 直接给界面看, next_due 给台账和「下次何时再试」。"""

    state: str
    message: str
    detail: str = ''
    next_due: str = ''


class CredentialRenewer:
    """凭证保活器。probe/grab 都由控制器注入, 单测里可不碰网络与子进程。

    probe(record) -> (True, '')      手上这枚可用
                   -> (False, '101') 真门 101, 判死
                   -> (None, '原因') 链路故障, 不知道死没死
    grab() -> (ok, reason)           起一次主动采集(用户要在小程序点一次)
    """

    def __init__(self, store, clock=None, probe=None, grab=None):
        self.store = store
        self._clock = clock or now
        self._probe = probe or (lambda record: (None, '未配置探活'))
        self._grab = grab or (lambda: (False, '未配置采集'))

    # ------------------------------------------------------------- 何时该续
    def due(self, settings=None):
        """返回此刻是否该做一次保活。

        两条独立理由, 任一成立即到期:
          * 台账上的 next_due 到了(低频探活);
          * 落在「跑步前 renew_lead_min 分钟」窗口里, 且今天这个窗口还没成功过。
        自动跑步没开时后台不续期: 关掉自动跑步的机器不该在后台拉采集链。
        """
        try:
            settings = settings or self.store.settings()
        except Exception:                       # noqa: BLE001 —— 设置坏了由快照呈现
            return False
        if not (settings.enabled and settings.auto_renew):
            return False
        at = self._clock()
        window = self._lead_window(settings, at)
        if window is not None:
            last = self._parse(self.store.renewal().get('last_success'))
            return last is None or last < window[0]
        next_due = self._parse(self.store.renewal().get('next_due'))
        return next_due is None or at >= next_due

    def _lead_window(self, settings, at):
        try:
            run_at = dt.datetime.combine(at.date(), parse_time(settings.run_time))
            start = run_at - dt.timedelta(minutes=int(settings.renew_lead_min))
        except (TypeError, ValueError, IndexError):
            return None
        return (start, run_at) if start <= at < run_at else None

    # ------------------------------------------------------------- 主流程
    def renew(self, force=False):
        """跑一遍续期阶梯。

        force=True 只越过「上次失败的冷却」(用户点了「立即续期」); 采集与探活的
        每日配额始终生效 —— 那两条是保护用户与接口的硬上限。
        """
        try:
            settings = self.store.settings()
        except Exception:                       # noqa: BLE001
            return RenewOutcome('error', '乐跑设置读不出来，稍后再试')
        if not force and not settings.auto_renew:
            return RenewOutcome('skipped', '自动续期已关闭')

        record = self.store.renewal()
        if not force:
            wait = self._cooldown_minutes(record)
            if wait > 0:
                return RenewOutcome('cooldown', f'上次续期没有成功，{wait} 分钟后再试',
                                    next_due=str(record.get('next_due') or ''))

        token = self._record()
        if token:
            if self._budget_spent('probe', MAX_DAILY_PROBES):
                return self._halt(settings, 'capped', '今天确认凭证的次数已用完，明天继续')
            alive, why = self._check(token)
            if alive:
                return self._settle(settings, 'ok', '凭证可用', '真门探活')
            if alive is None:
                # 探不通不等于死了: 保留手上这枚, 退避后再试。
                return self._defer(f'暂时确认不了凭证（{why}）')
            self.store.clear_token()
            self.store.record_probe(False)
            reason = '凭证已失效'
        else:
            reason = '本机还没有凭证'

        # 研究库优先: 它可能刚抓到新号, 不必再把用户赶去开一次小程序。
        adopted = self._sync_lab()
        if adopted:
            alive, why = self._check(adopted)
            if alive:
                return self._settle(settings, 'renewed', f'{reason}，已从研究库换上新的',
                                    '研究库')
            if alive is None:
                # 号换上了, 只是这会儿验不了; 下一拍再探, 不要因此判定「要用户动手」。
                return self._defer(f'已换上研究库的凭证，但暂时确认不了（{why}）')
            self.store.clear_token()
            self.store.record_probe(False)

        if not self.store.lab_dir():
            # 没有采集组件就没人能替用户取号; 让他去点「同步凭证」只会再失败一次。
            return self._need_user(f'{reason}：本机没有采集组件（研究库），'
                                   f'请先在跑步设置里填写研究库目录')
        # 配额是硬的: 手动点击只越过冷却, 不越过「今天最多采集两次」—— 采集链要用户
        # 真去开一次小程序, 点了没反应再点十次也只会更乱。
        if self._budget_spent('grab', MAX_DAILY_GRABS):
            return self._need_user(f'{reason}，今天的自动采集次数已用完：'
                                   f'在小程序里打开一次跑步首页后点「同步凭证」')
        ok, why = self._capture()
        if not ok:
            return self._need_user(f'{reason}：自动采集没有完成'
                                   + (f'（{why}）' if why else '，请打开一次小程序跑步首页'))
        adopted = self._sync_lab()
        if adopted:
            alive, why = self._check(adopted)
            if alive:
                return self._settle(settings, 'renewed', f'{reason}，已自动续期并确认可用',
                                    '自动采集')
            if alive is None:
                return self._settle(settings, 'renewed',
                                    f'{reason}，已换上新凭证（网络不通，稍后再确认）',
                                    '自动采集')
        return self._need_user(f'{reason}：请在小程序里打开一次跑步首页，然后点「同步凭证」')

    # ------------------------------------------------------------- 内部动作
    def _record(self):
        try:
            record = self.store.token_record()
        except OSError:
            self.store.clear_token()
            return None
        return record if record and record.get('token') else None

    def _check(self, record):
        """探活一次(计入每日配额), 返回 (True/False/None, 说明)。"""
        try:
            self.store.bump_daily(kind='probe')
        except Exception:                       # noqa: BLE001 —— 计数失败不拦主流程
            pass
        try:
            alive, why = self._probe(record)
        except Exception as exc:                # noqa: BLE001 —— 探活自己崩了按「不知道」
            return None, exc.__class__.__name__
        if alive not in (True, False, None):
            alive = bool(alive)
        return alive, str(why or '')

    def _sync_lab(self):
        """从研究库取号(含被研究库记成 consumed 的), 拿到就返回记录, 否则 None。"""
        try:
            changes, _message = self.store.sync_from_lab(include_consumed=True)
        except Exception:                       # noqa: BLE001
            return None
        if 'token' not in (changes or []):
            return None
        return self._record()

    def _capture(self):
        try:
            self.store.bump_daily(kind='grab')
        except Exception:                       # noqa: BLE001
            pass
        try:
            result = self._grab()
        except Exception as exc:                # noqa: BLE001
            return False, str(exc) or exc.__class__.__name__
        if isinstance(result, tuple):
            return bool(result[0]), str(result[1] or '')
        return bool(result), ''

    # ------------------------------------------------------------- 记账
    # record_renewal 自己会追加一行历史, 这里不再重复 note()。
    def _settle(self, settings, state, message, detail):
        next_due = self._clock() + dt.timedelta(minutes=int(settings.renew_interval_min))
        record = self.store.record_renewal(state, message,
                                           next_due=self._iso(next_due), detail=detail)
        return RenewOutcome(state, message, detail, record['next_due'])

    def _defer(self, message):
        record = self.store.renewal()
        try:
            failures = int(record.get('failures') or 0)
        except (TypeError, ValueError):
            failures = 0
        minutes = min(BACKOFF_MAX_MINUTES, BACKOFF_BASE_MINUTES * (2 ** min(failures, 4)))
        next_due = self._clock() + dt.timedelta(minutes=minutes)
        value = self.store.record_renewal('error', message,
                                          next_due=self._iso(next_due), detail='链路故障')
        return RenewOutcome('error', message, 'link', value['next_due'])

    def _need_user(self, message):
        next_due = self._clock() + dt.timedelta(minutes=COOLDOWN_MINUTES)
        value = self.store.record_renewal('need_user', message,
                                          next_due=self._iso(next_due), detail='需要用户')
        return RenewOutcome('need_user', message, 'capture', value['next_due'])

    def _halt(self, settings, state, message):
        """配额用尽: 也记一笔 next_due, 否则每 5 秒一拍都会重新排队续期。"""
        next_due = self._clock() + dt.timedelta(minutes=int(settings.renew_interval_min))
        value = self.store.record_renewal(state, message,
                                          next_due=self._iso(next_due), detail='配额')
        return RenewOutcome(state, message, 'cap', value['next_due'])

    # ------------------------------------------------------------- 小工具
    def _cooldown_minutes(self, record):
        if str(record.get('state') or '') in ('ok', 'renewed', ''):
            return 0
        next_due = self._parse(record.get('next_due'))
        if next_due is None:
            return 0
        left = (next_due - self._clock()).total_seconds() / 60.0
        return max(0, int(round(left)))

    def _budget_spent(self, kind, limit):
        try:
            return int(self.store.daily().get(kind, 0)) >= limit
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _parse(value):
        try:
            return dt.datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _iso(value):
        return value.isoformat(timespec='seconds')
