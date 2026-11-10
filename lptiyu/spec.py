"""协议事实常量(2026-09 真机抓包观测;出处见 lptiyu-lab 的 NOTICE.md)。

这里只放「线上是什么」的事实,不放任何策略。上层模块只允许单向依赖本文件。
"""

# ---- 加密参数 ---------------------------------------------------------------
AES_KEY = b"Wet2C8d34f62ndi3"          # 16 字节, 小程序内置
AES_IV = b"K6iv85jBD8jgf32D"           # 16 字节 CBC IV, 同上
SIGN_SECRET = "rDJiNB9j7vD2"           # SignMD5: 按键升序 k+v 拼接 + secret, 取 MD5

# ---- 主机与端点 -------------------------------------------------------------
API_HOST = "api2.lptiyu.com"
API_ROOT = "/v3/api.php"               # POST {root}/{Module}/{method}

EP_SCHOOL_INFO = "/WpLogin/getSchoolInfo"      # 免 token 公开口(探测假阳性, 仅作信息面)
EP_LOGIN_BY_CODE = "/WpLogin/loginByCode"      # 微信 code 换 token(带 flag 头)
EP_BEFORE_RUN = "/Run2/beforeRunV260"          # 跑区/打卡点/时间规则
EP_SET_ZONE = "/Run/setRunZone"
EP_TERM_LIST = "/WpRun/getTermList"            # 真鉴权门(无副作用探测用它)
EP_TERM_RECORDS = "/WpRun/getTermRunRecord"
EP_SERVER_TIME = "/Run/getTimestampV278"
EP_OSS_STS = "/WpIndex/getOssSts"
EP_STOP_RUN = "/Run/stopRunV278"               # 提交(带 flag 头)
EP_RECORD_DETAIL = "/Run/recordDetailV270"     # 异步复核权威源

FLAG_HEADERS = {EP_LOGIN_BY_CODE, EP_STOP_RUN}  # 观测: 仅这两个业务端点带 flag 头

# ---- 真机指纹 ---------------------------------------------------------------
APP_KEY = "wxb2043232183dac7f"
WXHR_REFERER = f"https://servicewechat.com/{APP_KEY}/13/page-frame.html"

UA_ANDROID = (
    "Mozilla/5.0 (Linux; Android 10; Redmi Note 7 Build/QKQ1.190910.002; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/142.0.7444.173 "
    "Mobile Safari/537.36 XWEB/1420283 MMWEBSDK/20260604 MMWEBID/2837 "
    "MicroMessenger/8.0.77.3160(0x28004D35) WeChat/arm64 Weixin NetType/WIFI "
    "Language/zh_CN ABI/arm64 MiniProgramEnv/android")

# 业务 POST 头组: 小写头名 + 固定顺序(flag 插在 content-type 之后), 逐字节实录。
# flag 值是小程序端 String(Boolean) 的产物。
FLAG_LITERAL = "[object Boolean]"

# ---- 公共业务参数 -----------------------------------------------------------
OSTYPE = 5                              # 安卓小程序
PROTOCOL_VERSION = 1

# ---- OSS -------------------------------------------------------------------
OSS_HOST = "lptiyu-data.oss-cn-hangzhou.aliyuncs.com"
OSS_KEY_PREFIX = "Public/Upload/file/"   # 表单 key 前缀; 业务参数 record_file 不带前缀
OSS_POLICY_TTL_MS = 3600000
OSS_MAX_LEN = 1073741824

# ---- 轨迹 wire 语义 ---------------------------------------------------------
SAMPLE_DT = 2.833                       # 真机采样步长(秒)
GEO_QUANT = 3600000                     # 数学引擎坐标 = 1/3600000° 量化
CHECKIN_RADIUS_M = 40.0                 # 打卡点首通过判定半径(真人 13~40m 均被接受)
BJ_SLOWDOWN_M = 18.0                    # 数学引擎: 进入该半径减速/打 b 标
# log_data: coordinates=通过时刻 GPS; time=首通过墙钟; distance=累计公里(两位小数,
# 起点写 "0.0" 字符串); order=配置点序不排序; longitude/longtitude 双拼写都要
# record_file = run_record/{date}/{ms%1000:03d}/{ms}-{2位数}.txt  (两次 Date.now())

# ---- 服务端判定文本(观测) ---------------------------------------------------
R_AUTOCORRECT = "自动确认有效"          # record_failed_reason 里的肯定判定
R_NEGATIVE_HINTS = ("无效", "不", "异常")

# ---- 默认策略窗口(真人统计) -------------------------------------------------
DIST_LO_KM, DIST_HI_KM = 2.03, 2.35
PACE_LO, PACE_HI = 185, 560             # 秒/公里
