"""record_file 命名仿真 + OSS STS multipart 上传(线上逐字节形态)。

观测清单(2026-09-07 真机实录):
  * record_file(业务参数) = "run_record/{date}/{dir3}/{ms}-{2位}.txt"
  * OSS 表单 key = "Public/Upload/file/" + record_file
  * dir3 = 构造 key 时刻 ms%1000; 文件名毫秒晚于 dir 毫秒几~几十(两次 Date.now())
  * 表单字段顺序: OSSAccessKeyId, key, policy, signature, x-oss-security-token, file
  * policy: {"expiration":"<UTC毫秒>","conditions":[["content-length-range",0,1G]]}
  * file 部分 filename = 本地纯毫秒名(比 key 文件名早几十 ms)
"""
import base64
import datetime
import hashlib
import hmac
import http.client
import random

from . import spec, tls


def iso_utc_ms(ms):
    ms = int(ms)
    s = datetime.datetime.fromtimestamp(ms / 1000.0, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.")
    return s + f"{ms % 1000:03d}Z"


def make_record_name(rnd=None):
    """返回 (fname_ms, record_file, fname)。dir 段与 fname 来自不同时刻。"""
    rnd = rnd or random.Random()
    now_ms = int(datetime.datetime.now().timestamp() * 1000)
    dir_ms = now_ms - rnd.randint(8, 60)
    dir3 = f"{dir_ms % 1000:03d}"
    fname_ms = dir_ms + rnd.randint(5, 40)
    fname = f"{fname_ms}-{rnd.randint(10, 99)}.txt"
    record_file = f"run_record/{datetime.date.today().isoformat()}/{dir3}/{fname}"
    return fname_ms, record_file, fname


def upload_to_oss(file_text, sts, now_ms, record_file, rnd=None,
                  oss_host=spec.OSS_HOST):
    """复刻 wx.uploadFile → OSS POST。返回 (http_status, body)。"""
    rnd = rnd or random.Random()
    key = spec.OSS_KEY_PREFIX + record_file
    policy_src = ('{"expiration":"%s","conditions":[["content-length-range",0,%d]]}'
                  % (iso_utc_ms(now_ms + spec.OSS_POLICY_TTL_MS), spec.OSS_MAX_LEN))
    policy = base64.b64encode(policy_src.encode()).decode()
    signature = base64.b64encode(hmac.new(sts["AccessKeySecret"].encode(),
                                          policy.encode(),
                                          hashlib.sha1).digest()).decode()
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    boundary = "----MultipartBoundary--" + "".join(rnd.choices(alphabet, k=27))

    def field(name, value):
        return (f"--{boundary}\r\nContent-Disposition: form-data; "
                f"name=\"{name}\"\r\n\r\n{value}\r\n").encode()

    local_fname = f"{int(now_ms) - rnd.randint(20, 90)}.txt"
    file_part = (f"--{boundary}\r\n"
                 f"Content-Disposition: form-data; name=\"file\"; "
                 f"filename=\"{local_fname}\"\r\n"
                 f"Content-Type: text/plain\r\n\r\n").encode() \
        + file_text.encode() + b"\r\n"
    body = b"".join([
        field("OSSAccessKeyId", sts["AccessKeyId"]),
        field("key", key),
        field("policy", policy),
        field("signature", signature),
        field("x-oss-security-token", sts["SecurityToken"]),
        file_part,
        f"--{boundary}--\r\n".encode(),
    ])

    conn = http.client.HTTPSConnection(oss_host, timeout=30,
                                       context=tls.build_context())
    try:
        conn.default_headers.clear()
    except Exception:                  # pragma: no cover
        pass
    conn.putrequest("POST", "/", skip_accept_encoding=True)
    conn.putheader("Connection", "keep-alive")
    conn.putheader("Content-Length", str(len(body)))
    conn.putheader("charset", "utf-8")
    conn.putheader("content-type", f"multipart/form-data; boundary={boundary}")
    conn.putheader("Referer", spec.WXHR_REFERER)
    conn.putheader("User-Agent", spec.UA_ANDROID)
    conn.putheader("Accept-Encoding", "gzip, deflate, br")
    conn.endheaders(body)
    resp = conn.getresponse()
    status = resp.status
    try:
        rbody = resp.read().decode("utf-8", "replace")
    except Exception:                  # pragma: no cover
        rbody = ""
    conn.close()
    return status, rbody
