"""v3 主 API 客户端(加密信封 POST)。

请求形态(抓包实录): POST {API_ROOT}/{Module}/{method}
  body = ostype=5&data=<seal(json{公共参数+业务参数+sign})>
公共参数: uid, token, school_id, term_id, course_id, class_id, student_num,
          card_id, timestamp, version, nonce, ostype
鉴权模型(2026-11-11 实测修正): token 死亡形态恒 status=101, 且死亡只由
「下一次 loginByCode 轮换」触发 —— 无人重开小程序时 token 长效, 直连即可用。
门后业务层信任 uid 参数 —— 本客户端纪律: 只写当前成员自己的 uid。
"""
import json
import time
import urllib.parse

from . import spec, wire
from .cipher import nonce6, seal, sign


class ApiError(Exception):
    """code: 101=token 失效; 3=额度; 其它负值/4=通用失败; 9=用户中断。"""

    def __init__(self, code, msg=""):
        self.code = code
        self.msg = msg
        super().__init__(f"ApiError({code}) {str(msg)[:120]}")


class ApiClient:
    def __init__(self, uid, token, school_id, student_num="", card_id="",
                 term_id=0, course_id=0, class_id=0, host=spec.API_HOST,
                 timeout=15, proxy=None, pace=0.0, ca_bundle=""):
        self.host = host
        self.timeout = timeout
        self.proxy = proxy or None
        self.ca_bundle = ca_bundle or ""
        self.pace = pace            # 相邻请求最小间隔(秒), 0=不限
        self._last = 0.0
        self.identity = {
            "uid": uid,
            "token": token,
            "school_id": school_id,
            "term_id": term_id,
            "course_id": course_id,
            "class_id": class_id,
            "student_num": student_num,
            "card_id": card_id or student_num,
        }

    # -------------------------------------------------------------- 底层
    def _throttle(self):
        if self.pace <= 0:
            return
        gap = time.time() - self._last
        if gap < self.pace:
            time.sleep(self.pace - gap)
        self._last = time.time()

    def call(self, path: str, biz: dict = None, flag: bool = False) -> dict:
        """加密 POST → 解密响应 dict。status=101 抛 ApiError(101)。"""
        self._throttle()
        payload = dict(self.identity)
        payload.update({
            "timestamp": int(time.time()),
            "version": spec.PROTOCOL_VERSION,
            "nonce": nonce6(),
            "ostype": spec.OSTYPE,
        })
        if biz:
            payload.update(biz)
        payload["sign"] = sign(payload)
        body = urllib.parse.urlencode(
            {"ostype": spec.OSTYPE, "data": seal(json.dumps(payload, ensure_ascii=False))}
        ).encode("utf-8")

        conn = wire.connect(self.host, 443, self.timeout, self.proxy,
                            ca_bundle=self.ca_bundle)
        try:
            resp = wire.post_form(conn, spec.API_ROOT + path, body, flag=flag)
            raw = resp.read()
            status = resp.status
            encoding = resp.getheader("Content-Encoding")
        finally:
            conn.close()
        text = wire.decode(raw, encoding).decode("utf-8", "replace")
        try:
            reply = json.loads(text)
        except Exception as e:
            raise ApiError(4, f"{status} 非JSON响应: {text[:200]}") from e
        if isinstance(reply, dict) and isinstance(reply.get("data"), str) \
                and len(reply["data"]) > 32:
            try:
                from .cipher import unseal
                reply = json.loads(unseal(reply["data"]))
            except Exception:          # 未加密响应保持原样
                pass
        if isinstance(reply, dict) and reply.get("status") == 101:
            raise ApiError(101, f"{path} → 101 (token 失效)")
        return reply

    # -------------------------------------------------------------- 端点
    def school_info(self):
        """公开开口(免 token), 仅作学校信息面; 鉴权探测不要用它。"""
        return self.call(spec.EP_SCHOOL_INFO)

    def term_list(self):
        """真鉴权门: 无副作用、必经 token 校验。"""
        return self.call(spec.EP_TERM_LIST)

    def before_run(self):
        return self.call(spec.EP_BEFORE_RUN)

    def set_zone(self, zone_id):
        return self.call(spec.EP_SET_ZONE, {"run_zone_id": str(zone_id)})

    def term_records(self, term_id, page=1):
        return self.call(spec.EP_TERM_RECORDS,
                         {"term_id": str(term_id), "page": page})

    def server_time(self):
        r = self.call(spec.EP_SERVER_TIME)
        try:
            return int(r["timestamp"])
        except Exception:
            return int(time.time())

    def oss_sts(self):
        return self.call(spec.EP_OSS_STS)

    def stop_run(self, biz: dict):
        """提交跑步记录(观测: 真机唯一带 flag 头的业务接口)。"""
        return self.call(spec.EP_STOP_RUN, biz, flag=True)

    def record_detail(self, record_id, year=None):
        import datetime
        y = year or str(datetime.date.today().year)
        return self.call(spec.EP_RECORD_DETAIL,
                         {"record_id": str(record_id), "year_num": y})

    def login_by_code(self, code, ty="2"):
        return self.call(spec.EP_LOGIN_BY_CODE,
                         {"app_key": spec.APP_KEY, "nickname": "", "avatar": "",
                          "ty": ty, "code": code}, flag=True)
