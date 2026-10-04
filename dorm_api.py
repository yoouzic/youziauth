"""SWU dormitory protocol adapter. Protocol reference: swu-daka 42d8973.

No historical form IDs, coordinates, plaintext caches or raw-response logging.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from dorm_checkin import CheckinError, Task, now

ORIGIN = 'https://of.swu.edu.cn'
BASE = '/gateway/fighter-baida/api/'
SELECT = BASE + 'form-instance/select'
SAVE = BASE + 'form-instance/save'

# 单次 API 调用的超时。它决定"学校只是在峰值排队"会不会被报成"连不上"：
# 2026-09-28 两台机器都在 21:05 失败，耗时都比平时多约 20 秒 —— 正好是这个值。
REQUEST_TIMEOUT_SECONDS = 20
# 只读调用在**同一轮**里的重试间隔（失败 → 等 → 再试）。落库那次 POST 绝不重试：
# 它的重发必须由回查证明"服务端没记录"，见 dorm_checkin.Engine._readback。
TRANSIENT_RETRY_DELAYS = (1.0, 3.0)
# 需要经本机代理上网时才打开（学校 API 默认直连，见 pick_route）。
SYSTEM_PROXY_ENV = 'YOUZIAUTH_DORM_SYSTEM_PROXY'
DIRECT = 'direct'
SYSTEM = 'system'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an authentication header to a login redirect or another host.
        raise CheckinError('login_required', '登录已失效，请重新登录')


def system_proxy() -> str:
    """本机系统代理的 `host:port`（没配置则空串）。带凭据的部分一律丢掉，不落盘。"""
    try:
        proxies = urllib.request.getproxies()
    except Exception:  # noqa: BLE001
        return ''
    value = proxies.get('https') or proxies.get('http') or ''
    if not value:
        return ''
    try:
        parts = urllib.parse.urlsplit(value if '://' in value else 'http://' + value)
    except ValueError:
        return ''
    if not parts.hostname:
        return ''
    return f'{parts.hostname}:{parts.port}' if parts.port else parts.hostname


def pick_route() -> str:
    """学校 API 走哪条链路：默认直连，只有显式设置环境变量才用系统代理。

    为什么默认绕开系统代理（2026-09-28 复盘）：urllib 的 build_opener 默认带
    ProxyHandler，会**静默**继承系统代理；本机实测开着 Clash（系统代理
    127.0.0.1:7897 + TUN），于是每一次学校请求都多经过一跳本地代理，而程序既读不到、
    也记不下这一跳 —— 出事时无法判断失败发生在学校还是本机代理。直连让链路确定；
    代理确实是唯一出口的机器，用 YOUZIAUTH_DORM_SYSTEM_PROXY=1 打开（仍会记进诊断）。
    """
    value = os.environ.get(SYSTEM_PROXY_ENV, '').strip().lower()
    return SYSTEM if value in ('1', 'true', 'yes', 'on') else DIRECT


def _opener(route: str):
    handlers = [NoRedirect()]
    if route == DIRECT:
        handlers.append(urllib.request.ProxyHandler({}))     # 明确忽略系统代理
    return urllib.request.build_opener(*handlers)


def failure_kind(exc: BaseException) -> str:
    """把传输层异常归类成受控文案用的短标签（不含异常原文）。"""
    reason = getattr(exc, 'reason', None) or exc
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return '响应超时'
    if isinstance(reason, socket.gaierror):
        return '域名解析失败'
    if isinstance(reason, ConnectionRefusedError):
        return '连接被拒绝'
    if isinstance(reason, (ConnectionResetError, ConnectionAbortedError,
                           http.client.RemoteDisconnected, http.client.BadStatusLine,
                           http.client.IncompleteRead)):
        return '连接被中断'
    if isinstance(reason, ssl.SSLEOFError):
        # 2026-10-04 实测：TLS 握手被对端/中间设备在完成前切断（学校流量被代理送到
        # 境外节点时就是这个签名：连得上、握手谈不完、每次约 5 秒）。
        return 'TLS 握手被切断'
    if isinstance(reason, ssl.SSLError):
        return 'TLS 握手失败'
    # Windows 的 WSA 错误码不走 errno 子类，只能查表（实测 Clash/校园网都会给这些）。
    return {10060: '响应超时', 110: '响应超时',
            10061: '连接被拒绝', 111: '连接被拒绝',
            10054: '连接被中断', 10053: '连接被中断', 104: '连接被中断',
            11001: '域名解析失败', 11004: '域名解析失败'}.get(
                getattr(reason, 'errno', None), '连接失败')


def transport_message(kind: str) -> str:
    """同一句"请检查网络"曾经同时代表超时、被重置、DNS 失败 —— 用户和我们都无从下手。"""
    return {
        '响应超时': f'学校接口响应超时（{REQUEST_TIMEOUT_SECONDS:.0f} 秒未回应），请稍后重试',
        '域名解析失败': '无法解析学校接口域名，请检查网络或代理后重试',
        '连接被拒绝': '学校接口拒绝连接，请稍后重试',
        '连接被中断': '与学校接口的连接被中断，请稍后重试',
        'TLS 握手被切断': '与学校接口的 TLS 握手被切断（链路中间有设备没让它谈完），请稍后重试',
        'TLS 握手失败': '与学校接口的加密连接失败，请稍后重试',
    }.get(kind, '无法连接学校接口，请检查网络后重试')


def failure_note(kind: str, route: str, started: float, attempts: int = 1) -> str:
    """一条受控的现场记录：失败类别 + 耗时 + 走了哪条链路 + 本轮试了几次。"""
    seconds = max(0.0, time.monotonic() - started)
    proxy = system_proxy()
    where = '直连' if route == DIRECT else f'经系统代理 {proxy or "（未检测到地址）"}'
    parts = [f'{kind} {seconds:.1f}s', where]
    if route == DIRECT and proxy:
        parts.append(f'本机系统代理 {proxy} 已绕过')
    if attempts > 1:
        parts.append(f'第 {attempts} 次尝试')
    return '／'.join(parts)


def request(method, path, token, *, query=None, body=None, form=None):
    if not path.startswith('/gateway/') or any(c in token for c in '\r\n'):
        raise CheckinError('error', '接口参数无效')
    url = ORIGIN + path
    if query:
        url += '?' + urllib.parse.urlencode(query)
    headers = {
        'Accept': 'application/json', 'fighter-auth-token': token,
        'Origin': ORIGIN, 'Referer': ORIGIN + '/baidaForm/',
        'User-Agent': 'Mozilla/5.0 AliApp(DingTalk/7.8.5.1) com.alibaba.android.rimet.diswu',
        'X-Requested-With': 'com.alibaba.android.rimet.diswu',
        'Cookie': 'SESSION=SESSION; access_token=' + token,
    }
    data = None
    if form is not None:
        boundary = 'youziauth' + uuid.uuid4().hex
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
                 for key, value in form.items()]
        data = (''.join(parts) + f'--{boundary}--\r\n').encode()
        headers['Content-Type'] = 'multipart/form-data; boundary=' + boundary
    elif body is not None:
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json;charset=UTF-8'
    route = pick_route()
    started = time.monotonic()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener(route).open(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read(2_000_001)
    except urllib.error.HTTPError as exc:
        # 服务器答了话：链路是通的，这一步不该被当成网络故障。响应体不读、也不留引用。
        exc.close()
        if exc.code in (401, 403):
            raise CheckinError('login_required', '登录已失效，请重新登录') from None
        raise CheckinError('network_error', f'学校接口暂不可用（HTTP {exc.code}），请稍后重试',
                           detail=failure_note(f'HTTP {exc.code}', route, started)) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        kind = failure_kind(exc)
        raise CheckinError('network_error', transport_message(kind),
                           detail=failure_note(kind, route, started)) from None
    try:
        if len(raw) > 2_000_000:
            raise ValueError('oversized response')
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise CheckinError('error', '学校接口返回格式不正确，请重新登录或稍后重试') from None
    if not isinstance(result, dict):
        raise CheckinError('error', '学校接口返回格式不正确')
    if str(result.get('code')) in ('401', '403'):
        raise CheckinError('login_required', '登录已失效，请重新登录')
    if result.get('code') != 200:
        raise CheckinError('error', '学校接口未确认操作，请在学校页面核实')
    return result.get('data')


def required(data, key):
    value = data.get(key)
    if not isinstance(value, (str, int)) or not str(value).strip():
        raise CheckinError('error', '今日任务字段不完整，已停止')
    return str(value)


class SwuApi:
    def __init__(self, transport=request, retry_delays=None):
        self.transport = transport
        self.retry_delays = TRANSIENT_RETRY_DELAYS if retry_delays is None else tuple(retry_delays)

    def _read(self, method, path, token, **kwargs):
        """只读调用：一次瞬时网络失败先在本轮重试，别把一个抖动拖成 5 分钟。

        落库那次 POST 不走这里 —— 它的重发必须由回查证明"服务端没记录"才允许
        （见 dorm_checkin.Engine），所以 submit() 仍然只发一次。
        """
        delays = list(self.retry_delays)
        for attempt in range(len(delays) + 1):
            if attempt:
                time.sleep(delays[attempt - 1])
            try:
                return self.transport(method, path, token, **kwargs)
            except CheckinError as exc:
                if exc.state != 'network_error':
                    raise                      # 需要人处理（登录/定位/格式），重试没有意义
                if attempt == len(delays):
                    if not attempt:
                        raise
                    note = f'本轮已重试 {attempt} 次'
                    raise CheckinError(exc.state, str(exc),
                                       detail=f'{exc.detail}／{note}' if exc.detail
                                       else note) from None
        raise CheckinError('network_error', '无法连接学校接口，请检查网络后重试')

    def user(self, token):
        data = self._read('GET', '/gateway/fighter-middle/api/auth/user', token,
                          query={'appType': 'fighter-portal'})
        subject = data.get('subject') if isinstance(data, dict) else None
        if not isinstance(subject, dict) or not (subject.get('username') or subject.get('loginName')):
            raise CheckinError('login_required', '无法确认登录身份，请重新登录')
        return str(subject.get('username') or subject['loginName'])

    def today(self, token, student, current):
        records = []
        for page in range(1, 11):
            data = self._read('POST', BASE + 'cqtj/getTransitionByToday', token,
                              form={'pageNum': str(page), 'pageSize': '50'})
            if not isinstance(data, dict) or not isinstance(data.get('records'), list):
                raise CheckinError('error', '今日任务列表格式不正确')
            batch = data['records']
            records.extend(batch)
            total = data.get('total')
            if not batch or (total is not None and len(records) >= int(total)) or (total is None and len(batch) < 50):
                break
        else:
            raise CheckinError('error', '任务数量异常，无法完整确认今日任务')
        candidates = {str(r.get('id')): r for r in records if isinstance(r, dict)
                      and r.get('tsrq') == current.date().isoformat()
                      and '查寝' in json.dumps(r, ensure_ascii=False)}
        if not candidates:
            return None
        if len(candidates) != 1:
            raise CheckinError('error', '发现多条今日查寝任务，请在学校页面选择处理')
        record = next(iter(candidates.values()))
        task_id, form_id = required(record, 'id'), required(record, 'formId')
        data = self._read('GET', SELECT, token,
                          query={'dataId': task_id, 'formId': form_id, 'procDefId': ''})
        if not isinstance(data, dict) or required(data, 'xh') != student:
            raise CheckinError('error', '任务账号与登录账号不一致，已停止')
        if data.get('tsrq') and data['tsrq'] != record['tsrq']:
            raise CheckinError('error', '任务表单日期不一致，已停止')
        if data.get('id') and str(data['id']) != task_id:
            raise CheckinError('error', '任务表单编号不一致，已停止')
        publish_id = required(data, 'cqfbid')
        times = data.get('qdsj') or [record.get('qdkssj'), record.get('qdjssj')]
        if isinstance(times, str):
            try:
                times = json.loads(times) if times.startswith('[') else times.split(',')
            except ValueError:
                times = []
        if not isinstance(times, list) or len(times) != 2:
            raise CheckinError('error', '今日任务时段不明确，已停止')
        payload = dict(data, id=task_id, cqfbid=publish_id, xh=student, tsrq=record['tsrq'])
        dorm = self._read('POST', BASE + 'cqlc/getDormitory', token, body=payload)
        fields = {}
        if isinstance(dorm, dict):
            for item in dorm.get('columnList') or []:
                if not isinstance(item, dict):
                    continue
                if item.get('prop') in ('qsqddd', 'qdbj') and item.get('value'):
                    fields[item['prop']] = str(item['value'])
                if item.get('address'):
                    fields.setdefault('qsqddd', str(item['address']))
                if item.get('qdbj'):
                    fields.setdefault('qdbj', str(item['qdbj']) + '米')
                # The school also states where its own check-in point is; the map picker needs it
                # to centre and to draw the acceptance radius. Absent on older forms.
                if item.get('latitude'):
                    fields.setdefault('latitude', str(item['latitude']))
                if item.get('longitude'):
                    fields.setdefault('longitude', str(item['longitude']))
        return Task(task_id, form_id, publish_id, student, record['tsrq'],
                    str(record.get('cqzmc') or record.get('title') or '今日查寝')[:120],
                    times[0], times[1], str(data.get('qdjg')) == '1',
                    fields.get('qsqddd') or str(data.get('qsqddd') or ''),
                    fields.get('qdbj') or str(data.get('qdbj') or ''), str(data.get('formId') or ''),
                    fields.get('latitude') or '', fields.get('longitude') or '')

    def verify(self, token, task, position):
        data = self._read('POST', BASE + 'cqlc/verify', token,
                          query={'businessKey': task.id}, body={'mapData': position})
        if not isinstance(data, dict) or data.get('isArea') is not True:
            raise CheckinError('location_required', '学校未确认当前位置在打卡范围内，未提交')

    def submit(self, token, task, position):
        """POST the form and read the record the school writes back.

        The save response carries the stored instance itself, so `qdjg` is the same
        success value the read-back looks for and the outcome is known at once. Any
        other answer - including an empty body - stays unconfirmed.
        """
        location = dict(position, isArea=True, tip='当前在签到范围内')
        data = self.transport('POST', SAVE, token,
                              query={'formId': task.form_id, 'isSubmitProcess': 'false'}, body={
            'id': task.id, 'businessKey': task.id, 'formId': task.form_id, 'cqfbid': task.publish_id,
            'xh': task.student, 'tsrq': task.date, 'dksj': now().strftime('%Y-%m-%d %H:%M'),
            'qdjg': '0', '$qdjg': '未签到', 'qdtj': '1', 'ycdksfcl': '', 'isArchive': '',
            'qdsj': [task.start, task.end], 'qsqddd': task.address, 'qdbj': task.radius, 'qddz': location,
        })
        return isinstance(data, dict) and str(data.get('qdjg')) == '1'

    def is_signed(self, token, task):
        data = self._read('GET', SELECT, token,
                          query={'dataId': task.id, 'formId': task.form_id, 'procDefId': ''})
        return (isinstance(data, dict) and str(data.get('xh')) == task.student
                and (not data.get('id') or str(data['id']) == task.id)
                and (not data.get('tsrq') or data['tsrq'] == task.date)
                and str(data.get('qdjg')) == '1')
