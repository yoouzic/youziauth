"""乐跑模块的本地存储: 设置、身份、凭证(DPAPI 加密)、状态与历史。

凭证形态沿用寝室打卡的边界纪律:
  * token 只以 DPAPI(用户域)密文落盘(session/credential.dat), 明文只在内存;
  * 快照只回答「有没有」(has_token), 绝不回传 token 本身;
  * 非 secret 的 JSON 容忍 BOM, 但损坏即 fail-closed, 绝不静默降级。

token 生命周期(2026-11-11 实测修订):
  token 的死亡只由「下一次 loginByCode 轮换」触发 —— 无人重开小程序时,
  token 跨天有效且直连可用(抓取 1 小时后直连真门仍返回有效学期列表)。
  所以这里不做「提交即消耗」的簿记: 提交成功后探测一次, 活着就保留,
  只有真门返回 101 才判死并清除。真门探测是唯一的死亡判据。
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
from pathlib import Path

from windows_credentials import CredentialStore, DpapiProtector, atomic_write_bytes

HISTORY_MAX_LINES = 99


def now():
    return dt.datetime.now()


def parse_time(text):
    hour, minute = str(text).strip().split(':')
    return dt.time(int(hour), int(minute))


@dataclasses.dataclass(frozen=True)
class Settings:
    enabled: bool = False
    run_time: str = '06:30'
    zone_id: str = ''            # 空 = 使用账号当前跑区
    zone_name: str = ''
    dist_lo: float = 2.03
    dist_hi: float = 2.35
    pace_lo: int = 0             # 秒/公里; 0/0 = 模板自动(不指定配速区间)
    pace_hi: int = 0
    pace_target: int = 0         # 旧版单点配速(兼容老 settings.json); 新区间优先
    anchor_max_m: float = 20.0
    max_per_day: int = 1
    time_lo: str = '06:00'
    time_hi: str = '23:00'
    api_proxy: str = ''          # 空 = 直连(默认); 也可指向采集链隧道
    lab_dir: str = ''            # 研究库目录(凭证同步与模板来源); 留空自动发现
    auto_renew: bool = True      # 自动续期: 后台把凭证保活, 不等到跑步那一刻才发现失效
    renew_lead_min: int = 45     # 跑步前多久做一次「今天必过」的保活
    renew_interval_min: int = 180  # 低频探活间隔(token 没有绝对过期时间, 只需确认还活着)

    def pace_range(self):
        """有效的配速区间 (lo, hi) 秒/公里; (0, 0) = 交给模板。

        旧版只有一个 pace_target: 迁移期把它当成「上下限相同」的区间,
        这样老配置的行为不变, 新界面一保存就会写成显式区间并清掉旧字段。
        """
        try:
            lo, hi = int(self.pace_lo), int(self.pace_hi)
        except (TypeError, ValueError):
            lo = hi = 0
        if lo or hi:
            return lo, hi
        try:
            legacy = int(self.pace_target)
        except (TypeError, ValueError):
            legacy = 0
        return (legacy, legacy) if legacy else (0, 0)

    def validate(self):
        if self.dist_lo < 0.5 or self.dist_hi > 10.0 or self.dist_lo >= self.dist_hi:
            raise ValueError('距离范围无效：需在 0.5–10 公里内且下限小于上限')
        try:
            pace_lo, pace_hi = int(self.pace_lo), int(self.pace_hi)
        except (TypeError, ValueError):
            raise ValueError('配速区间无效：请写成 4\'05" 这样的分钟加秒') from None
        if pace_lo or pace_hi:
            if not (pace_lo and pace_hi):
                raise ValueError('配速区间无效：上下限要同时填写，或都留空为自动')
            if not (120 <= pace_lo <= 700) or not (120 <= pace_hi <= 700):
                raise ValueError('配速无效：需在 2\'00"–11\'40" 每公里之间')
            if pace_lo > pace_hi:
                raise ValueError('配速区间无效：下限要比上限快（数值更小）')
        if self.pace_target:
            try:
                legacy = int(self.pace_target)
            except (TypeError, ValueError):
                raise ValueError('目标配速无效：需在 120–700 秒/公里之间') from None
            if not (120 <= legacy <= 700):
                raise ValueError('目标配速无效：需在 120–700 秒/公里之间')
        if not (5.0 <= self.anchor_max_m <= 40.0):
            raise ValueError('采点精度无效：需在 5–40 米之间')
        if not (1 <= self.max_per_day <= 3):
            raise ValueError('每日次数无效：需在 1–3 次之间')
        try:
            lead = int(self.renew_lead_min)
            interval = int(self.renew_interval_min)
        except (TypeError, ValueError):
            raise ValueError('续期设置无效：提前量与间隔都要是整数分钟') from None
        if not (0 <= lead <= 240):
            raise ValueError('提前保活时间无效：需在 0–240 分钟内')
        if not (30 <= interval <= 1440):
            raise ValueError('续期间隔无效：需在 30–1440 分钟内')
        try:
            lo, hi = parse_time(self.time_lo), parse_time(self.time_hi)
        except (ValueError, IndexError):
            raise ValueError('有效时段格式无效') from None
        if hi <= lo:
            raise ValueError('有效时段结束必须晚于开始')
        try:
            run_at = parse_time(self.run_time)
        except (ValueError, IndexError):
            raise ValueError('自动跑步时间格式无效') from None
        if not (lo <= run_at <= hi):
            raise ValueError(f'自动跑步时间需在有效时段 {self.time_lo}–{self.time_hi} 内')
        return self


class Store:
    def __init__(self, root: Path | None = None, protector=None):
        self.root = Path(root) if root is not None else Path(
            os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local'))
        ) / 'youziauth' / 'lptiyu'
        self._protector = protector

    # ------------------------------------------------------------- 基础读写
    def _credentials(self):
        return CredentialStore(self.root / 'session',
                               self._protector or DpapiProtector(machine_scope=False))

    def _read(self, name, default):
        try:
            return json.loads((self.root / name).read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            return default

    def _write(self, name, data):
        atomic_write_bytes(self.root / name,
                           json.dumps(data, ensure_ascii=False).encode('utf-8'))

    # ------------------------------------------------------------- 设置与身份
    def settings(self):
        return Settings(**self._read('settings.json', {})).validate()

    def save_settings(self, settings):
        self._write('settings.json', dataclasses.asdict(settings.validate()))

    def identity(self):
        value = self._read('identity.json', {})
        return value if isinstance(value, dict) else {}

    def save_identity(self, identity):
        clean = {k: str(identity[k]) for k in
                 ('name', 'student_num', 'uid', 'school_id', 'card_id') if identity.get(k)}
        self._write('identity.json', clean)

    # ------------------------------------------------------------- 凭证(加密)
    def has_token(self):
        """本机是否保存着凭证。**只看密文文件在不在，不解密。**"""
        return (self.root / 'session' / 'credential.dat').exists()

    def token_record(self):
        """解密并返回 {'token','issued_at','source'}; 无凭证返回 None; 损坏抛 OSError。"""
        if not self.has_token():
            return None
        value = self._credentials().load_password()
        try:
            record = json.loads(value)
        except ValueError:
            # 旧格式: 单行裸 token
            record = {'token': value, 'issued_at': '', 'source': 'legacy'}
        if not isinstance(record, dict) or not isinstance(record.get('token'), str) \
                or not record['token'] or any(c in record['token'] for c in '\r\n'):
            raise OSError('凭证内容无效，请重新同步')
        return {'token': record['token'],
                'issued_at': str(record.get('issued_at') or ''),
                'source': str(record.get('source') or '')}

    def token(self):
        record = self.token_record()
        return record['token'] if record else ''

    def token_issued_at(self):
        record = self.token_record()
        return record['issued_at'] if record else ''

    def save_token_record(self, token, issued_at='', source=''):
        if not isinstance(token, str) or not token.strip() \
                or any(c in token for c in '\r\n'):
            raise ValueError('凭证格式无效')
        payload = json.dumps({'v': 1, 'token': token, 'issued_at': issued_at,
                              'source': source}, ensure_ascii=False)
        self._credentials().save_password(payload)

    def clear_token(self):
        (self.root / 'session' / 'credential.dat').unlink(missing_ok=True)

    # ------------------------------------------------------------- 状态与历史
    def status(self):
        value = self._read('status.json', {})
        return value if isinstance(value, dict) else {}

    def sync_record(self):
        try:
            value = self._read('sync.json', {})
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def record_sync(self, message=''):
        """本地同步时刻: 界面「多久前同步」只认这本台账, 不认服务器签发时间。"""
        self._write('sync.json', {'at': now().isoformat(timespec='seconds'),
                                  'message': str(message or '')[:160]})

    def _append_history(self, line):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / 'history.log'
        try:
            lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            lines = []
        lines.append(line)
        atomic_write_bytes(path,
                           ('\n'.join(lines[-HISTORY_MAX_LINES:]) + '\n').encode('utf-8'))

    def record(self, state, message, detail='', at=None):
        stamp = (at or now()).isoformat(timespec='seconds')
        self._write('status.json', {'state': state, 'message': message,
                                    'detail': detail, 'at': stamp})
        line = f'{stamp} [{state}] {message}' + (f' · {detail}' if detail else '')
        self._append_history(line)

    def record_probe(self, alive):
        probes = self.status().get('probe_evidence') or []
        probes.append({'at': now().isoformat(timespec='seconds'), 'alive': bool(alive)})
        status = self.status()
        status['probe_evidence'] = probes[-20:]
        self._write('status.json', status)

    # ------------------------------------------------------------- 续期台账
    MAX_SAMPLES = 200

    def samples(self):
        """留档的轨迹样本(最近优先): 跑区几何 + 本次结果, 用来事后校准朝向/形状。"""
        try:
            rows = json.loads((self.root / 'samples.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            rows = []
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)][:self.MAX_SAMPLES]

    def record_sample(self, entry):
        """记一次样本; 同一个 record_id 只留一份。返回是否真的写进去了。"""
        if not isinstance(entry, dict):
            return False
        rows = self.samples()
        record_id = str(entry.get('record_id') or '')
        if record_id and any(str(row.get('record_id') or '') == record_id for row in rows):
            return False
        rows.insert(0, entry)
        self._write('samples.json', rows[:self.MAX_SAMPLES])
        return True

    def capture_state(self):
        """自动采集的次数与最近一次尝试(按天记账, 免得一天到晚抢着开小程序)。"""
        try:
            data = json.loads((self.root / 'capture.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        attempts = data.get('attempts')
        return {'last_attempt': str(data.get('last_attempt') or ''),
                'attempts': dict(attempts) if isinstance(attempts, dict) else {}}

    def record_capture_attempt(self, at):
        state = self.capture_state()
        day = at.date().isoformat()
        attempts = state['attempts']
        attempts[day] = int(attempts.get(day, 0)) + 1
        # 只留最近 7 天, 这个文件只服务于「今天还能不能再来一次」
        for key in sorted(attempts)[:-7]:
            attempts.pop(key, None)
        self._write('capture.json', {
            'last_attempt': at.isoformat(timespec='seconds'),
            'attempts': attempts})

    def renewal(self):
        """最近一次续期的结果与下次到期时间。

        台账是簿记而非凭证: 坏掉只丢「下次何时再试」, 不该让整个乐跑区块降级,
        所以这里容忍损坏(与 settings.json 的 fail-closed 不同)。
        """
        try:
            value = self._read('renewal.json', {})
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def record_renewal(self, state, message='', next_due='', detail=''):
        """写续期台账 + 追加一行历史(一次续期一条, 不重复记)。"""
        record = self.renewal()
        stamp = now().isoformat(timespec='seconds')
        settled = state in ('ok', 'renewed')
        try:
            previous_failures = int(record.get('failures') or 0)
        except (TypeError, ValueError):
            previous_failures = 0
        events = [event for event in (record.get('events') or [])
                  if isinstance(event, dict)]
        text = str(message or '')[:160]
        events.append({'at': stamp, 'state': state, 'message': text,
                       'detail': str(detail or '')[:160]})
        value = {
            'state': state,
            'message': text,
            'last_attempt': stamp,
            'last_success': stamp if settled else str(record.get('last_success') or ''),
            'failures': 0 if settled else previous_failures + 1,
            'next_due': str(next_due or ''),
            'events': events[-20:],
        }
        self._write('renewal.json', value)
        if state != 'ok' and not self._same_as_last_history(text):
            # 例行探活不是「发生过的事」: 台账里有记录就够了, 运行记录页只留
            # 换号 / 需要人动手 / 链路故障这类需要人看的行; 同一句话重复十几次
            # (没有采集组件时的自动续期)也只留第一次。
            self._append_history(f'{stamp} [{state}] {text}'
                                 + (f' · {detail}' if detail else ''))
        return value

    def _same_as_last_history(self, text):
        """上一行历史是不是同一句话(去掉时间戳与状态之后比)。"""
        try:
            tail = self.history().strip().splitlines()[-1]
        except (OSError, IndexError):
            return False
        return str(text) and str(text) in tail

    def history(self):
        try:
            return (self.root / 'history.log').read_text(encoding='utf-8', errors='replace')
        except OSError:
            return ''

    # ------------------------------------------------------------- 每日计数
    def daily(self, date=None):
        day = (date or now()).date().isoformat()
        value = self._read('daily.json', {})
        if not isinstance(value, dict):
            value = {}
        value = {k: v for k, v in value.items() if k >= (now().date() - dt.timedelta(days=30)).isoformat()}
        return value.get(day, {}) if isinstance(value.get(day), dict) else {}

    def bump_daily(self, date=None, kind='run'):
        day = (date or now()).date().isoformat()
        value = self._read('daily.json', {})
        if not isinstance(value, dict):
            value = {}
        value = {k: v for k, v in value.items() if k >= (now().date() - dt.timedelta(days=30)).isoformat()}
        day_value = value.get(day) if isinstance(value.get(day), dict) else {}
        day_value[kind] = int(day_value.get(kind, 0)) + 1
        value[day] = day_value
        self._write('daily.json', value)
        return day_value[kind]

    def signed_record(self):
        value = self._read('signed.json', {})
        return value if isinstance(value, dict) else {}

    def mark_signed(self, date, summary=None):
        record = {'date': date}
        if isinstance(summary, dict):
            record.update(summary)
        self._write('signed.json', record)

    def is_signed_today(self):
        return self.signed_record().get('date') == now().date().isoformat()

    # ------------------------------------------------------------- 跑区目录
    def zones(self):
        """跑区目录: {zone_id: {id,name,is_default,points,tuning,...}}。

        与续期台账一样是簿记: 坏了只丢「这块场地长什么样」, 不该拖垮乐跑区块。
        """
        try:
            value = self._read('zones.json', {})
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def save_zone_list(self, items):
        """把 beforeRun 的 run_zone_list 并进目录(只记 id/name/默认标记)。"""
        catalog = self.zones()
        changed = False
        for item in items or []:
            if not isinstance(item, dict):
                continue
            zone_id = str(item.get('id') or '')
            if not zone_id:
                continue
            entry = catalog.get(zone_id) if isinstance(catalog.get(zone_id), dict) else {}
            updated = dict(entry)
            updated.update({'id': zone_id,
                            'name': str(item.get('name') or entry.get('name') or ''),
                            'is_default': int(item.get('is_default') or 0)})
            if updated != entry:
                catalog[zone_id] = updated
                changed = True
        if changed:
            self._write('zones.json', catalog)
        return catalog

    def save_zone_points(self, zone_id, name, points, tuning=None):
        """记录某跑区的打卡点 —— 每块场地的选点特调都以它为依据。"""
        from lptiyu import zones as zone_geom  # noqa: PLC0415

        zone_id = str(zone_id or '')
        if not zone_id:
            return None
        clean = []
        for point in points or []:
            try:
                clean.append({'id': str(point.get('id', '')),
                              'lat': float(point['lat']), 'lng': float(point['lng'])})
            except (KeyError, TypeError, ValueError):
                continue
        if not clean:
            return None
        catalog = self.zones()
        entry = dict(catalog.get(zone_id) or {})
        entry.update({'id': zone_id,
                      'name': str(name or entry.get('name') or ''),
                      'points': clean,
                      'points_at': now().isoformat(timespec='seconds')})
        if tuning:
            entry['tuning'] = dict(entry.get('tuning') or {},
                                   **zone_geom.normalize_tuning(tuning))
        entry['tuning'] = zone_geom.normalize_tuning(entry.get('tuning'))
        catalog[zone_id] = entry
        self._write('zones.json', catalog)
        return entry

    def save_zone_tuning(self, zone_id, tuning):
        """改某跑区的选点特调(方向/贴点半径/跑道偏移)。"""
        from lptiyu import zones as zone_geom  # noqa: PLC0415

        zone_id = str(zone_id or '')
        if not zone_id:
            return None
        catalog = self.zones()
        entry = dict(catalog.get(zone_id) or {'id': zone_id})
        entry['tuning'] = zone_geom.normalize_tuning(tuning)
        catalog[zone_id] = entry
        self._write('zones.json', catalog)
        return entry

    def zone_tuning(self, zone_id):
        from lptiyu import zones as zone_geom  # noqa: PLC0415

        entry = self.zones().get(str(zone_id or '')) or {}
        tuning = zone_geom.normalize_tuning(entry.get('tuning'))
        # 用户没手填朝向时, 用本机历史轨迹学出来的那条补上
        if not tuning.get('axis_deg'):
            learned = entry.get('axis_learned') or {}
            try:
                tuning['axis_deg'] = round(float(learned.get('deg') or 0.0) % 180.0, 1)
            except (TypeError, ValueError):
                tuning['axis_deg'] = 0.0
        return tuning

    def save_zone_axis(self, zone_id, deg, share=0.0):
        """把从真实通过点学到的朝向写进跑区记录(不动用户特调)。"""
        key = str(zone_id or '')
        catalog = self.zones()
        entry = catalog.get(key)
        if not key or not isinstance(entry, dict):
            return False
        entry['axis_learned'] = {'deg': round(float(deg) % 180.0, 1),
                                 'share': round(float(share or 0.0), 2)}
        self._write('zones.json', catalog)
        return True

    def zone_summary(self, zone_id):
        """某跑区的几何摘要(给界面); 没有点位时返回 None。"""
        from lptiyu import zones as zone_geom  # noqa: PLC0415

        entry = self.zones().get(str(zone_id or '')) or {}
        points = entry.get('points') or []
        pairs = [(p['lat'], p['lng']) for p in points
                 if isinstance(p, dict) and 'lat' in p and 'lng' in p]
        if len(pairs) < 3:
            return None
        summary = zone_geom.summary(pairs, entry.get('tuning'))
        summary['name'] = entry.get('name', '')
        summary['zone_id'] = str(zone_id)
        return summary

    # ------------------------------------------------------------- 模板来源
    def template_candidates(self):
        candidates = [str(self.root)]
        lab = self.lab_dir()
        if lab:
            candidates.append(str(Path(lab) / 'data'))
        return candidates

    # ------------------------------------------------------------- 研究库目录
    LAB_HINTS = (
        Path('workspace_zcode') / 'reverse' / 'lptiyu-lab',
    )

    @staticmethod
    def _known_desktop_dirs():
        """用户桌面的真实位置(桌面常被重定向到非系统盘, Path.home() 会指错)。

        用 SHGetKnownFolderPath 解析 FOLDERID_Desktop; 失败时退回 home/Desktop。
        """
        dirs = []
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                            ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]

            # FOLDERID_Desktop {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
            folder = GUID(0xB4BFCC3A, 0xDB2C, 0x424C,
                          (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9,
                                               0x9A, 0x87, 0xC6, 0x41))
            pointer = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(
                    ctypes.byref(folder), 0, None, ctypes.byref(pointer)) == 0:
                dirs.append(Path(pointer.value))
                ctypes.windll.ole32.CoTaskMemFree(pointer)
        except Exception:  # noqa: BLE001 —— 解析不出就退回 home 拼接
            pass
        dirs.append(Path.home() / 'Desktop')
        return dirs

    def lab_dir(self):
        configured = str(self.settings().lab_dir or '').strip()
        if configured and (Path(configured) / 'lpctl.py').is_file():
            return configured
        for desktop in self._known_desktop_dirs():
            for hint in self.LAB_HINTS:
                candidate = desktop / hint
                if (candidate / 'lpctl.py').is_file():
                    return str(candidate)
        return ''

    def lab_status(self):
        """采集组件的状态: 配了没有、找没找到、为什么没找到。

        凭证只能由小程序 loginByCode 产生, 而这一步的采集链在「研究库」里 ——
        类里不带它, 所以「没有研究库」必须是一个能被界面解释清楚的状态, 而不是
        让用户去点一个必然失败的「同步凭证」。
        """
        try:
            settings = self.settings()
        except Exception:  # noqa: BLE001 —— 设置坏了由快照降级
            return {'configured': '', 'found': '', 'ok': False, 'reason': 'settings'}
        configured = str(settings.lab_dir or '').strip()
        found = self.lab_dir()
        if found:
            return {'configured': configured, 'found': found, 'ok': True, 'reason': ''}
        if configured:
            return {'configured': configured, 'found': '', 'ok': False,
                    'reason': 'missing_lpctl'}
        return {'configured': '', 'found': '', 'ok': False, 'reason': 'not_configured'}

    # ------------------------------------------------------------- 研究库同步
    @staticmethod
    def _lab_member(config, identity):
        """按本机身份挑研究库成员: 学号 → uid → active → 第一个。

        研究库是多成员白名单, `active` 只是「上次在用的那个」。直接按 active 取,
        在换过成员的机器上会把别人的号装进来 —— 凭证是认人的, 这里必须先认身份。
        """
        members = config.get('members') or {}
        if not isinstance(members, dict):
            return None
        wanted = {str(identity.get('student_num') or ''), str(identity.get('uid') or '')}
        wanted.discard('')
        if wanted:
            for member in members.values():
                if not isinstance(member, dict):
                    continue
                keys = {str(member.get('student_num') or ''), str(member.get('uid') or '')}
                if wanted & keys:
                    return member
        active = config.get('active')
        if isinstance(members.get(active), dict):
            return members[active]
        for member in members.values():
            if isinstance(member, dict):
                return member
        return None

    def sync_from_lab(self, include_consumed=False):
        """从研究库单向同步身份与凭证。凭证「取更新的一方」: 以 issued_at 为准。

        include_consumed: 研究库把「提交完」的号记成 consumed, 但 2026-11-11 实测
        说明 token 只由下一次 loginByCode 轮换才死。自动续期走这条路时允许采纳
        consumed 号, 由真门探活裁决 —— 否则每次都要把用户赶去重新开一次小程序。
        默认 False 保持「同步凭证」按钮原有的语义。

        返回 (changes:list[str], message:str)。不做任何破坏性写入——youziauth
        只在被同步方更新时采纳，绝不反向覆盖研究库。
        """
        lab = self.lab_dir()
        if not lab:
            return [], '未找到研究库目录，请先在设置中填写路径'
        config_path = Path(lab) / 'config.json'
        try:
            config = json.loads(config_path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            return [], '研究库配置无法读取'
        if not isinstance(config, dict) or not (config.get('members') or {}):
            return [], '研究库还没有成员'
        identity = self.identity()
        member = self._lab_member(config, identity)
        if member is None:
            return [], '研究库还没有成员'
        changes = []
        student_num = str(member.get('student_num') or '')
        if student_num and identity.get('student_num') != student_num:
            self.save_identity({
                'name': member.get('alias') or member.get('name') or student_num,
                'student_num': student_num,
                'uid': member.get('uid') or '',
                'school_id': member.get('school_id') or '',
                'card_id': member.get('card_id') or student_num,
            })
            changes.append('identity')

        auth = member.get('auth') or {}
        lab_token = str(auth.get('token') or '')
        lab_issued = str(auth.get('issued_at') or '')
        consumed = bool(auth.get('consumed_at'))
        if lab_token and (include_consumed or not consumed):
            current = None
            try:
                current = self.token_record()
            except OSError:
                self.clear_token()
            if current and current['token'] == lab_token:
                # 同一枚号只补签发时间, 不重复落盘, 免得台账每天多一条「已同步」。
                if lab_issued and current['issued_at'] != lab_issued:
                    self.save_token_record(lab_token, issued_at=lab_issued,
                                           source=current['source'] or 'lab-sync')
            else:
                newer = (not current) or (not current['issued_at']) \
                    or (lab_issued and lab_issued > current['issued_at'])
                if newer:
                    self.save_token_record(
                        lab_token, issued_at=lab_issued,
                        source='lab-consumed' if consumed else 'lab-sync')
                    changes.append('token')
        if changes:
            settings = self.settings()
            if not settings.lab_dir:
                self.save_settings(dataclasses.replace(settings, lab_dir=lab))
            return changes, '已同步研究库的最新凭证'
        return [], '本机凭证已是最新'
