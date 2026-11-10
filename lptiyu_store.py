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
    pace_target: int = 0         # 秒/公里; 0 = 模板自动
    anchor_max_m: float = 20.0
    max_per_day: int = 1
    time_lo: str = '06:00'
    time_hi: str = '23:00'
    api_proxy: str = ''          # 空 = 直连(默认); 也可指向采集链隧道
    lab_dir: str = ''            # 研究库目录(凭证同步与模板来源); 留空自动发现

    def validate(self):
        if self.dist_lo < 0.5 or self.dist_hi > 10.0 or self.dist_lo >= self.dist_hi:
            raise ValueError('距离范围无效：需在 0.5–10 公里内且下限小于上限')
        if self.pace_target:
            if not (120 <= self.pace_target <= 700):
                raise ValueError('目标配速无效：需在 120–700 秒/公里之间')
        if not (5.0 <= self.anchor_max_m <= 40.0):
            raise ValueError('采点精度无效：需在 5–40 米之间')
        if not (1 <= self.max_per_day <= 3):
            raise ValueError('每日次数无效：需在 1–3 次之间')
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

    # ------------------------------------------------------------- 研究库同步
    def sync_from_lab(self):
        """从研究库单向同步身份与凭证。凭证「取更新的一方」: 以 issued_at 为准。

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
        members = config.get('members') or {}
        if not members:
            return [], '研究库还没有成员'
        active = config.get('active')
        member = members.get(active) if active in members else None
        if member is None:
            member = next(iter(members.values()))
        changes = []
        identity = self.identity()
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
        if lab_token and not auth.get('consumed_at'):
            current = None
            try:
                current = self.token_record()
            except OSError:
                self.clear_token()
            newer = (not current) or (not current['issued_at']) \
                or (lab_issued and lab_issued > current['issued_at'])
            if newer:
                self.save_token_record(lab_token, issued_at=lab_issued,
                                       source='lab-sync')
                changes.append('token')
        if changes:
            settings = self.settings()
            if not settings.lab_dir:
                self.save_settings(dataclasses.replace(settings, lab_dir=lab))
            return changes, '已同步研究库的最新凭证'
        return [], '本机凭证已是最新'
