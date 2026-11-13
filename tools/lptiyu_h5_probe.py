"""lptiyu H5「学号+密码+验证码」登录链探针 —— 路线 B 的决定性实验。

问题: 乐跑的 H5 面(ThinkPHP 独立应用)能不能不经过微信、直接用学号+密码登录,
并且登录产物能不能换发 v3 access_token(跑步提交链唯一认的钥匙)。

协议事实(来自研究库 lpcore/h5.py 的真机观测, 此处只读复述, 不依赖研究库):
  GET  /index/Login/getToken               -> {"status":1,"code":"<v-token>"}
  GET  /index/Login/verify?token=&random=  -> PNG 验证码 + Set-Cookie(会话)
  POST /index/Login/login                  -> school_id/student_num/password/
                                              verify/login_type=2/token
  校验顺序: 验证码 → 学号/密码。

⚠️ 账号锁定(2026-11-13 实测, 修正研究库「5 连错不锁定」的旧记录):
  同一学号累计 3 次密码错误后返回「账号已锁定」—— 本地记 60 分钟熔断,
  到点前拒绝再试。解锁靠等待或体育老师/管理员重置(重置也顺带解决
  「不知道乐跑密码」的问题)。**不要拿这个探针暴力试密码。**

凭据来源:
  --from-dorm 名字   复用寝室打卡已登记的统一认证凭据(同一 Windows 用户的
                     DPAPI 密文; 用哪个学号就试哪个人的 H5 登录)。
  --student 学号     手动模式, 配 --password-stdin 或交互输入。

纪律:
  * 密码只在内存使用, 绝不打印、不落盘、不进日志;
  * token 一律掩码(前 8 位);
  * 结论写 --report(默认 .scratch/lptiyu-h5-probe/ 下按时间戳一份),
    不含密码。

用法:
  py -3 tools/lptiyu_h5_probe.py --from-dorm 柚子
  py -3 tools/lptiyu_h5_probe.py --from-dorm 红兔 --captcha manual
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import getpass
import io
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

HOST = 'api2.lptiyu.com'
BASE = '/bdlp_h5_fitness_test/public/index.php'
# 端点与 UA/Referer 来自真机抓包(H5 面在微信内打开时的形态)
UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
      'MicroMessenger/8.0.49')
REFERER = 'https://servicewechat.com/wxb2043232183dac7f/13/page-frame.html'
DEFAULT_SCHOOL_ID = 837

ERR_CAPTCHA = '验证码错误'
ERR_CREDENTIAL = '学号或密码输入有误'

SECRET_KEYS = re.compile(r'(passw|pwd|secret)', re.I)
TOKEN_KEYS = re.compile(r'(token|code|openid|session)', re.I)


class H5Error(RuntimeError):
    pass


# ================================================================ 锁定熔断
# 2026-11-13 实测: 同一学号累计 3 次密码错误后 H5 返回「账号已锁定」
# (修正研究库「5 连错不锁定」的旧记录)。解锁靠等待或管理员重置。
LOCKOUT_MINUTES = 60
LOCKOUT_FILE = REPO / '.scratch' / 'lptiyu-h5-probe' / 'lockout.json'


def _load_lockouts():
    if not LOCKOUT_FILE.is_file():
        return {}
    try:
        return json.loads(LOCKOUT_FILE.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}


def lockout_guard(student):
    """熔断: 被锁过的学号在冷却期内拒绝再试, 防止把锁定续期。"""
    data = _load_lockouts()
    entry = data.get(student)
    if not entry:
        return
    try:
        locked_at = dt.datetime.fromisoformat(entry)
    except ValueError:
        return
    elapsed = (dt.datetime.now() - locked_at).total_seconds() / 60.0
    if elapsed >= LOCKOUT_MINUTES:
        data.pop(student, None)
        LOCKOUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOCKOUT_FILE.write_text(json.dumps(data, ensure_ascii=False),
                                encoding='utf-8')
        return
    remaining = int(LOCKOUT_MINUTES - elapsed)
    raise H5Error(f'账号 {student} 的 H5 登录在 {locked_at:%H:%M} 被锁定, '
                  f'冷却还剩约 {remaining} 分钟 —— 等待或走老师/管理员重置, '
                  f'不要现在重试(会续期锁定)')


def mark_locked(student):
    data = _load_lockouts()
    data[student] = dt.datetime.now().isoformat(timespec='seconds')
    LOCKOUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCKOUT_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    print(f'[锁定已记录] {LOCKOUT_MINUTES} 分钟内拒绝再试 {student}')


# ================================================================ HTTP(带最小 cookie 罐)
class H5Http:
    def __init__(self):
        self.cookies = {}

    def request(self, path, method='GET', form=None):
        url = f'https://{HOST}{BASE}{path}'
        headers = {'User-Agent': UA, 'Referer': REFERER, 'Host': HOST}
        if self.cookies:
            headers['Cookie'] = '; '.join(f'{k}={v}' for k, v in self.cookies.items())
        body = None
        if form is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            body = urllib.parse.urlencode(form).encode()
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=25) as resp:
            for header in resp.headers.get_all('Set-Cookie') or []:
                pair = header.split(';', 1)[0]
                name, _, value = pair.partition('=')
                if name.strip():
                    self.cookies[name.strip()] = value.strip()
            return resp.status, resp.read()


def mask(value, keep=8):
    text = str(value or '')
    return text[:keep] + '…' if len(text) > keep else text


# ================================================================ 凭据
def credentials_from_dorm(name):
    """从寝室打卡的账号档案读统一认证凭据(同用户 DPAPI, 不经过任何网络)。"""
    from idm_credentials import IdmCredentialStore
    local = os.environ.get('LOCALAPPDATA')
    base = Path(local) if local else Path.home() / 'AppData' / 'Local'
    accounts_root = base / 'youziauth' / 'accounts'
    registry = accounts_root / 'accounts.json'
    if not registry.is_file():
        raise H5Error(f'寝室打卡账号清单不存在: {registry}')
    data = json.loads(registry.read_text(encoding='utf-8-sig'))
    matches = [account for account in data.get('accounts', [])
               if str(account.get('name') or '') == name]
    if not matches:
        known = '、'.join(str(a.get('name') or '?')
                          for a in data.get('accounts', []))
        raise H5Error(f'没有叫「{name}」的账号(现有: {known})')
    profile = accounts_root / matches[0]['id']
    store = IdmCredentialStore(profile / 'idm')
    if not store.exists():
        raise H5Error(f'账号「{name}」没有登记统一认证凭据')
    credentials = store.load()
    return credentials.username, credentials.password


def credentials_from_args(args):
    if args.from_dorm:
        username, password = credentials_from_dorm(args.from_dorm)
        print(f'已从寝室打卡账号「{args.from_dorm}」读取统一认证凭据: '
              f'学号 {username}, 密码 {"*" * len(password)}')
        if not args.yes and input('用它继续实验? (y/N) ').strip().lower() not in ('y', 'yes'):
            raise H5Error('已取消')
        return username, password
    if not args.student:
        raise H5Error('需要 --from-dorm 名字 或 --student 学号')
    username = args.student
    if args.password_stdin:
        password = sys.stdin.readline().rstrip('\r\n')
    else:
        password = getpass.getpass(f'学号 {username} 的密码: ')
    return username, password


# ================================================================ 验证码
def binarize_to_idm(png: bytes) -> bytes:
    """lptiyu 验证码(200x62 彩色+干扰字符) -> IDM 风格 100x30 灰度图。

    代码字符是深色饱和的, 干扰字符是浅色的 —— Otsu 阈值一刀切掉干扰,
    再把「深字浅底」重映射到 IDM 分布(字 50 / 底 235), 现成的模型与
    训练管线就能直接吃。
    """
    from PIL import Image
    import numpy as np
    gray_image = Image.open(io.BytesIO(png)).convert('L').resize((100, 30), Image.BILINEAR)
    gray = np.asarray(gray_image, dtype=np.uint8)
    hist, _ = np.histogram(gray, bins=256, range=(0, 256))
    total = gray.size
    sum_all = float(np.dot(np.arange(256), hist))
    best, threshold, w_b, sum_b = -1.0, 128, 0, 0
    for t in range(256):
        w_b += int(hist[t])
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * int(hist[t])
        var = w_b * w_f * ((sum_b / w_b) - (sum_all - sum_b) / w_f) ** 2
        if var > best:
            best, threshold = var, t
    binary = np.where(gray < threshold, 50, 235).astype(np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(binary).save(buffer, format='PNG')
    return buffer.getvalue()


class ManualCaptcha:
    def solve(self, png):
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as handle:
            handle.write(png)
            path = handle.name
        print(f'验证码已保存: {path}')
        try:
            os.startfile(path)          # Windows: 用默认看图程序自动弹出
        except (OSError, AttributeError):
            pass                        # 非 Windows 或被安全软件拦截就手动打开
        return input('验证码: ').strip()


class ModelCaptcha:
    """用重训的 lptiyu 模型自动识别(槽位归一 + 4 头 CNN, 见 lptiyu_captcha.py)。"""

    def __init__(self, model_path):
        from lptiyu_captcha import SlotCaptchaSolver
        self.solver = SlotCaptchaSolver(model_path)

    def solve(self, png):
        code, conf = self.solver.solve(png)
        print(f'    验证码识别: {code or "(切分失败)"} (conf={conf:.2f})')
        return code or ''


# ================================================================ 登录链
def h5_login(http, student, password, school_id, captcha, attempts=4):
    """H5 登录链。返回 (ok, info dict); info 里是服务端应答(掩码后展示)。"""
    for attempt in range(1, attempts + 1):
        _status, body = http.request('/index/Login/getToken')
        token = (json.loads(body) or {}).get('code', '')
        if not token:
            return False, {'error': 'getToken 没有下发 v-token'}
        _status, png = http.request(
            f'/index/Login/verify?token={token}&random={int(time.time() * 1000)}')
        if not png.startswith(b'\x89PNG'):
            return False, {'error': f'verify 没有返回 PNG ({len(png)} bytes)'}
        code = captcha.solve(png)
        if not code:
            continue
        _status, body = http.request('/index/Login/login', method='POST', form={
            'school_id': school_id, 'student_num': student,
            'password': password, 'verify': code,
            'login_type': 2, 'token': token})
        try:
            reply = json.loads(body.decode('utf-8', 'replace'))
        except ValueError:
            return False, {'error': f'login 应答不是 JSON ({len(body)} bytes)',
                           'head': body[:120].decode('utf-8', 'replace')}
        info = reply if isinstance(reply, dict) else {'raw': str(reply)[:120]}
        print(f'  [登录 #{attempt}] status={info.get("status")} '
              f'info={info.get("info")}')
        if info.get('status') == 1:
            return True, info
        if str(info.get('info') or '') == ERR_CREDENTIAL:
            return False, {'verdict': 'credential_mismatch',
                           'info': str(info.get('info'))}
        if str(info.get('info') or '') != ERR_CAPTCHA:
            return False, {'verdict': 'other', 'info': str(info.get('info'))}
        # 验证码错误: 换一张重试
    return False, {'verdict': 'captcha_exhausted'}


def walk_secrets(node, path=''):
    """递归收集 token/uid 类字段(掩码), 顺带找 password 类字段只报键名。"""
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            found.extend(walk_secrets(value, f'{path}.{key}' if path else str(key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(walk_secrets(value, f'{path}[{index}]'))
    else:
        key = path.rsplit('.', 1)[-1]
        if SECRET_KEYS.search(key):
            found.append((path, '<隐去>'))
        elif TOKEN_KEYS.search(key) and node not in (None, '', 0):
            found.append((path, mask(node)))
    return found


# ================================================================ v3 决定性验证
def v3_term_list_test(token, uid, student, school_id):
    """拿 H5 产物去敲 v3 真鉴权门 getTermList —— 路线 B 的成败判据。"""
    from lptiyu.client import ApiClient, ApiError
    client = ApiClient(uid=int(uid), token=token, school_id=int(school_id),
                       student_num=str(student), card_id=str(student))
    try:
        terms = client.term_list()
    except ApiError as exc:
        return False, f'v3 拒绝 (ApiError {exc.code}: {exc})'
    except Exception as exc:              # noqa: BLE001
        return False, f'v3 调用失败 ({type(exc).__name__}: {exc})'
    if isinstance(terms, dict) and terms.get('status') == 101:
        return False, 'v3 返回 101 (token 无效)'
    count = len(terms) if isinstance(terms, list) else '?'
    return True, f'v3 真门通过: getTermList 返回 {count} 个学期'


# ================================================================ 报告
def write_report(path, lines):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = '# lptiyu H5 登录链实验(路线 B)\n\n- 时间: ' \
           f'{dt.datetime.now().isoformat(timespec="seconds")}\n\n' + \
           '\n'.join(lines) + '\n'
    path.write_text(text, encoding='utf-8')
    print(f'\n报告已写入 {path}')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--from-dorm', help='复用寝室打卡账号的统一认证凭据(按账号名)')
    parser.add_argument('--student', help='手动指定学号')
    parser.add_argument('--password-stdin', action='store_true', help='密码从 stdin 读一行')
    parser.add_argument('--captcha', choices=['model', 'manual'], default='model',
                        help='验证码求解方式(默认用重训模型)')
    parser.add_argument('--model',
                        default=str(REPO / 'data' / 'captcha' / 'lptiyu' / 'lptiyu_model.npz'),
                        help='lptiyu 验证码模型路径')
    parser.add_argument('--school-id', type=int, default=DEFAULT_SCHOOL_ID)
    parser.add_argument('--attempts', type=int, default=4)
    parser.add_argument('--yes', action='store_true', help='跳过使用凭据前的确认')
    parser.add_argument('--report',
                        default=str(REPO / '.scratch' / 'lptiyu-h5-probe' /
                                    f'probe-report-{dt.datetime.now():%Y%m%d-%H%M%S}.md'),
                        help='实验报告路径(默认按时间戳写到 .scratch, 不覆盖任何正式文档)')
    args = parser.parse_args()

    try:
        student, password = credentials_from_args(args)
    except H5Error as exc:
        print(f'错误: {exc}')
        return 2

    try:
        lockout_guard(student)
    except H5Error as exc:
        print(f'错误: {exc}')
        return 2

    if args.captcha == 'model':
        if not Path(args.model).is_file():
            print(f'错误: 没找到验证码模型 {args.model} (先用训练管线生成, '
                  '或改用 --captcha manual)')
            return 2
        captcha = ModelCaptcha(args.model)
    else:
        captcha = ManualCaptcha()

    http = H5Http()
    lines = [f'- 学号: {student}(凭据来源: {"寝室打卡「" + args.from_dorm + "」" if args.from_dorm else "手动输入"})',
             '- 通道: H5 面(免微信)']

    print('开始 H5 登录链…')
    ok, reply = h5_login(http, student, password, args.school_id, captcha,
                         attempts=args.attempts)
    secrets = walk_secrets(reply)
    for key, value in secrets:
        print(f'    {key} = {value}')
    if not ok:
        verdict = reply.get('verdict') or 'error'
        info_text = str(reply.get('info') or reply.get('error') or '')
        if '锁定' in info_text:
            mark_locked(student)
        lines.append(f'- 结果: **失败**({verdict}): {info_text}')
        if verdict == 'credential_mismatch':
            lines.append('- 结论: 统一认证密码**不通用**于乐跑 H5 —— '
                         '免微信路线需要乐跑专用密码才可继续。')
        write_report(args.report, lines)
        return 1

    lines.append('- 结果: **H5 登录成功**(验证码已过, 学号密码被接受)')
    lines.append(f'- 应答字段: {", ".join(key for key, _ in secrets) or "(无)"}')

    # 决定性一步: 产物能不能换发 v3 access_token?
    token, uid = find_access_token(reply)
    if token and uid:
        alive, detail = v3_term_list_test(token, uid, student, args.school_id)
        print(f'v3 决定性验证: {detail}')
        lines.append(f'- v3 换发验证: **{"通过" if alive else "不通过"}** — {detail}')
        lines.append(f'- v3 token(掩码): `{mask(token)}` uid={uid}')
    else:
        lines.append('- v3 换发验证: **无 v3 access_token 产物** — '
                     'H5 会话与 v3 token 是两个体系, 需要继续找桥接端点(或判死路线 B)。')
    write_report(args.report, lines)
    return 0 if ok else 1


def find_access_token(reply):
    """在登录应答里找 (access_token, uid); H5 面即便成功也可能不发 v3 token。"""
    result = {}

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get('token_data'), dict):
                result['token'] = node['token_data'].get('access_token')
            for key, value in node.items():
                if key == 'access_token' and isinstance(value, str) and value:
                    result.setdefault('token', value)
                if key == 'uid' and value:
                    result.setdefault('uid', value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(reply)
    return result.get('token'), result.get('uid')


if __name__ == '__main__':
    raise SystemExit(main())
