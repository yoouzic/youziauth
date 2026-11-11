"""轨迹生成双引擎(行为契约与线上观测一致, 实现为原创)。

template 引擎(主力): 真人模板做相似变换 —— 质心平移 + 循环相位对齐 +
平均尺度/旋转(圆均值) + 目标距离缩放 + GPS 噪声; 保留模板 s(速度)/b(停走)/
c(消耗) 序列的统计形态。模板属个人数据, 不随软件分发, 由本机配置提供。
ring 引擎(备用): 锚点环多圈 + 速度模型(热身/巡航/收尾) + 打卡点减速 +
AR(1) 抖动, 不依赖任何个人数据, 模板缺失时自动兜底。

坐标采用分纬度椭球米换算(比恒定 111320 m/° 更贴近真机测距)。
"""
import json
import math
import os
import random
import sys

from . import spec


# ---------------------------------------------------------------- 基础几何
def haversine_m(lat1, lng1, lat2, lng2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def m_per_deg_lat(lat_deg):
    """WGS84 子午线弧长(米/度纬度), 三阶近似。"""
    p = math.radians(lat_deg)
    return 111132.92 - 559.82 * math.cos(2 * p) + 1.175 * math.cos(4 * p)


def m_per_deg_lng(lat_deg):
    """WGS84 卯酉圈弧长(米/度经度)。"""
    p = math.radians(lat_deg)
    return 111412.84 * math.cos(p) - 93.5 * math.cos(3 * p)


def enu_of(lat, lng, lat0, lng0):
    return ((lng - lng0) * m_per_deg_lng(lat0),
            (lat - lat0) * m_per_deg_lat(lat0))


def geo_of(x, y, lat0, lng0):
    return (lat0 + y / m_per_deg_lat(lat0),
            lng0 + x / max(m_per_deg_lng(lat0), 1e-6))


def quantize_geo(v):
    """真机坐标 = 1/3600000° 量化后的 double。"""
    return int(round(v * spec.GEO_QUANT)) / spec.GEO_QUANT


def parse_jingwei(s):
    a, b = str(s).split(",")
    return float(a), float(b)


def path_len_m(coords):
    return sum(haversine_m(a[0], a[1], b[0], b[1])
               for a, b in zip(coords, coords[1:]))


def _path_winding(points):
    """路径绕质心的累计转角(弧度): 正=逆时针, 负=顺时针; 多圈时按圈数累加。"""
    if len(points) < 3:
        return 0.0
    clat = sum(p[0] for p in points) / len(points)
    clng = sum(p[1] for p in points) / len(points)
    xy = [enu_of(lat, lng, clat, clng) for lat, lng in points]
    total, prev = 0.0, None
    for x, y in xy:
        ang = math.atan2(y, x)
        if prev is not None:
            d = ang - prev
            while d > math.pi:
                d -= 2 * math.pi
            while d < -math.pi:
                d += 2 * math.pi
            total += d
        prev = ang
    return total


def used_seconds(track, min_s=300, max_s=2400, rnd=None):
    """模板用时: 按 s 字段(m/s)逐段积分; 缺 s 段按步长走; 超窗兜底。"""
    rnd = rnd or random.Random()
    total = 0.0
    for a, b in zip(track, track[1:]):
        d = haversine_m(a["a"], a["o"], b["a"], b["o"])
        v = b.get("s")
        total += d / max(0.6, v) if v else d / 2.5
    if not (min_s <= total <= max_s):
        total = rnd.uniform(600, 760)
    return int(total)


def find_template(candidates=()):
    """模板定位: 显式候选目录优先(env LP_TEMPLATE 仍最高), 逐个找同名明文模板。

    模板文件名固定沿用研究库约定(真人模板不入库, 由本机配置提供路径)。
    """
    name = "真实轨迹_035明文.json"
    cands = []
    env = os.environ.get("LP_TEMPLATE", "")
    if env:
        cands.append(env)
    for directory in candidates:
        cands.append(os.path.join(directory, name))
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", "") or ""
        cands.append(os.path.join(base, "data", name))
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""


def load_template(path=""):
    p = path or find_template()
    if not p or not os.path.isfile(p):
        raise FileNotFoundError("track template not available")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- 锚点环
def _anchor_ring(anchors):
    """[(lat,lng)] → 按质心方位角排序的环。"""
    clat = sum(a[0] for a in anchors) / len(anchors)
    clng = sum(a[1] for a in anchors) / len(anchors)

    def angle(a):
        return math.atan2(a[0] - clat, (a[1] - clng) * math.cos(math.radians(clat)))

    return sorted(anchors, key=angle), clat, clng


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _circular_mean(angles):
    s = sum(math.sin(a) for a in angles)
    c = sum(math.cos(a) for a in angles)
    return math.atan2(s, c)


def _norm_anchors(anchors):
    """接受 [(lat,lng)] 或 [{"latitude":..,"longtitude":..}] 两种形态, 统一为元组。"""
    out = []
    for a in anchors or []:
        if isinstance(a, dict):
            out.append((float(a["latitude"]), float(a["longtitude"])))
        else:
            out.append((float(a[0]), float(a[1])))
    return out


# ================================================================ template 引擎
class TemplateShift:
    """把真人模板迁移到当前跑区: 相位对齐 + 相似变换 + 距离缩放 + 噪声。"""

    def __init__(self, anchors, distance_km=None, seed=None, pace_factor=None,
                 template=None, template_path="", profile=None):
        self.rnd = random.Random(seed)
        self.anchors = _norm_anchors(anchors)
        self.distance_km = distance_km
        self.pace_factor = (pace_factor if pace_factor is not None
                            else self.rnd.uniform(0.82, 1.18))
        self.profile = dict(profile or {})     # 跑区特调(lptiyu.zones.profile)
        self.tpl = template if template is not None else load_template(template_path)
        self.mirrored = False
        want = str(self.profile.get("direction") or "").lower()
        if want in ("cw", "ccw") and len(self.tpl) >= 3:
            wind = _path_winding([(p["a"], p["o"]) for p in self.tpl])
            if abs(wind) > 0.5 and (want == "cw") != (wind < 0):
                # 模板绕向与跑区特调相反: 在锚点匹配之前镜像模板, 旋转/尺度会重新对齐,
                # 打卡点覆盖不受影响; s/b/c 序列原样保留, 只是换了绕向。
                clat = sum(p["a"] for p in self.tpl) / len(self.tpl)
                clng = sum(p["o"] for p in self.tpl) / len(self.tpl)
                flipped = []
                for p in self.tpl:
                    x, y = enu_of(p["a"], p["o"], clat, clng)
                    lat, lng = geo_of(-x, y, clat, clng)
                    q = dict(p)
                    q["a"], q["o"] = lat, lng
                    flipped.append(q)
                self.tpl = flipped
                self.mirrored = True

    def _map(self, sx, sy, rot, src_c, dst_c):
        cos_r, sin_r = math.cos(rot), math.sin(rot)
        out = []
        for p in self.tpl:
            x, y = enu_of(p["a"], p["o"], *src_c)
            out.append(geo_of(sx * (x * cos_r - y * sin_r),
                              sy * (x * sin_r + y * cos_r), *dst_c))
        return out

    def _axis_scales(self, pairs, rot, scale, src_c, dst_c):
        """各向异性缩放: 两块场地的长宽比不同时, 只用一个 scale 会把场地拉歪。

        旋转对齐之后分别量源环/目标环在 x/y 上的跨度, 得到两个方向的缩放;
        比值离整体尺度太远(点位太少或本身就是异形场地)就退回各向同性。
        """
        cos_r, sin_r = math.cos(rot), math.sin(rot)
        src_x, src_y, dst_x, dst_y = [], [], [], []
        for (sa, so), (da, do_) in pairs:
            x, y = enu_of(sa, so, *src_c)
            src_x.append(x * cos_r - y * sin_r)
            src_y.append(x * sin_r + y * cos_r)
            dx, dy = enu_of(da, do_, *dst_c)
            dst_x.append(dx)
            dst_y.append(dy)

        def span(values):
            return max(values) - min(values)

        ex, ey = span(src_x), span(src_y)
        gx, gy = span(dst_x), span(dst_y)
        fx = gx / ex if ex > 20 and gx > 20 else scale
        fy = gy / ey if ey > 20 and gy > 20 else scale
        fx = _clamp(fx, scale / 1.6, scale * 1.6)
        fy = _clamp(fy, scale / 1.6, scale * 1.6)
        return fx, fy

    def generate(self, return_meta=False):
        rnd = self.rnd
        # 模板自身锚点环(取前 N 个模板点作为"源环", 与跑区配置点数对齐)
        tpl_pts = [(p["a"], p["o"]) for p in self.tpl[:max(len(self.anchors), 4)]]
        src_ring, src_clat, src_clng = _anchor_ring(tpl_pts)
        if self.anchors:
            dst_ring, dst_clat, dst_clng = _anchor_ring(self.anchors)
            src_ring = src_ring[:len(dst_ring)]
            n = min(len(src_ring), len(dst_ring))
            best, best_cost = dst_ring, math.inf
            for off in range(n):
                cand = dst_ring[off:] + dst_ring[:off]
                cost = sum(haversine_m(s[0], s[1], d[0], d[1])
                           for s, d in zip(src_ring[:n], cand))
                if cost < best_cost:
                    best_cost, best = cost, cand
            pairs = list(zip(src_ring[:n], best))
        else:
            dst_ring, dst_clat, dst_clng = src_ring, src_clat, src_clng
            pairs = list(zip(src_ring, dst_ring))

        if pairs:
            scales, rots = [], []
            for (sa, so), (da, do_) in pairs:
                sx, sy = enu_of(sa, so, src_clat, src_clng)
                dx, dy = enu_of(da, do_, dst_clat, dst_clng)
                scales.append(math.hypot(dx, dy) / max(math.hypot(sx, sy), 1e-6))
                rots.append(math.atan2(dy, dx) - math.atan2(sy, sx))
            scale = sum(scales) / len(scales)
            rot = _circular_mean(rots)
            sx_scale, sy_scale = self._axis_scales(
                pairs, rot, scale, (src_clat, src_clng), (dst_clat, dst_clng))
        else:
            scale, rot = 1.0, 0.0
            sx_scale = sy_scale = scale

        coords = self._map(sx_scale, sy_scale, rot,
                           (src_clat, src_clng), (dst_clat, dst_clng))

        if self.distance_km:
            cur = path_len_m(coords)
            if cur > 100:
                f = self.distance_km * 1000.0 / cur
                coords = [geo_of(x * f, y * f, dst_clat, dst_clng)
                          for x, y in (enu_of(a, o, dst_clat, dst_clng)
                                       for a, o in coords)]

        track = []
        w = 0.0
        for i, p in enumerate(self.tpl):
            w = max(-9.0, min(9.0, w + rnd.gauss(0, 0.9)))
            jitter_x = w + rnd.gauss(0, 1.1)
            jitter_y = rnd.gauss(0, 1.3)
            x, y = enu_of(*coords[i], dst_clat, dst_clng)
            lat, lng = geo_of(x + jitter_x, y + jitter_y, dst_clat, dst_clng)
            q = {"a": lat, "o": lng}
            if "s" in p and i > 0:
                q["s"] = round(max(0.01, min(7.6, p["s"] * self.pace_factor
                                             + rnd.gauss(0, 0.05))), 2)
            if "b" in p:
                q["b"] = p["b"]
            q["c"] = p["c"]
            track.append(q)

        meta = {
            "center": (round(dst_clat, 6), round(dst_clng, 6)),
            "scale": round(scale, 4),
            "scale_xy": (round(sx_scale, 4), round(sy_scale, 4)),
            "rot_deg": round(math.degrees(rot), 1),
            "mirrored": self.mirrored,
            "pace_factor": round(self.pace_factor, 3),
            "path_m": round(path_len_m([(p["a"], p["o"]) for p in track])),
            "anchor_cover": [
                round(min(haversine_m(p["a"], p["o"], d[0], d[1]) for p in track), 1)
                for d in dst_ring],
        }
        return (track, meta) if return_meta else track


# ================================================================ ring 引擎
def ring_track(anchors, target_m, rnd=None, profile=None):
    """锚点环多圈轨迹。anchors: [(lat,lng,pid)]; profile: lptiyu_zones.profile()。

    跑区特调在这里落地: 路线顺着点位环的环向、从场地长轴一侧起步, 并按该跑区
    的车道宽度外扩 —— 换一块朝向不同的场地, 轨迹跟着转, 而不是每次随机起步。
    """
    from . import zones as zone_geom      # 延迟导入: zones 复用本模块的几何原语

    rnd = rnd or random.Random()
    prof = dict(profile or {})
    pts3 = []
    for index, a in enumerate(anchors):
        if isinstance(a, dict):
            lng = a["longitude"] if "longitude" in a else a["longtitude"]
            pts3.append((float(a["latitude"]), float(lng), str(a.get("point_id", index))))
        else:
            pts3.append((float(a[0]), float(a[1]),
                         str(a[2]) if len(a) > 2 else str(index)))
    plain = [(p[0], p[1]) for p in pts3]
    geom = zone_geom.analyze(plain, zone_id=prof.get("zone_id"))
    direction = prof.get("direction") or geom["winding"]
    lane = float(prof.get("lane_m") or 0.0)
    start_deg = prof.get("start_deg")

    route = []
    for _lap in range(200):
        lap = zone_geom.route_points(plain, lane_m=lane + rnd.uniform(-0.6, 0.6),
                                     direction=direction, start_deg=start_deg, rnd=rnd,
                                     zone_id=prof.get("zone_id"))
        if len(lap) < 2:
            break
        if route:
            route.pop()                    # 接圈处不留重复点
        route.extend(lap)
        if path_len_m(route) >= target_m * 1.15:
            break
    if len(route) < 2:                     # 退化: 两点场地就往返跑
        route = plain + plain[::-1]

    clat = sum(p[0] for p in route) / len(route)
    clng = sum(p[1] for p in route) / len(route)
    xy = [enu_of(p[0], p[1], clat, clng) for p in route]
    cum = [0.0]
    for a, b in zip(xy, xy[1:]):
        cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))

    def at(s):
        s = min(max(s, 0.0), cum[-1] - 0.01)
        lo, hi = 0, len(cum) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if cum[mid] < s:
                lo = mid + 1
            else:
                hi = mid
        i = max(lo - 1, 0)
        seg = cum[i + 1] - cum[i] or 1.0
        t = (s - cum[i]) / seg
        return (xy[i][0] + (xy[i + 1][0] - xy[i][0]) * t,
                xy[i][1] + (xy[i + 1][1] - xy[i][1]) * t)

    base_v = rnd.uniform(3.45, 3.95)
    period = rnd.uniform(150, 230)
    phase = rnd.uniform(0, 6.28)
    track, crossings = [], {}
    s = t = 0.0
    i = 0
    jl = jn = 0.0
    while True:
        v = base_v * (1 + 0.12 * math.sin(2 * math.pi * t / period + phase))
        v += rnd.gauss(0, 0.22)
        if t < 30:                     # 热身
            v *= 0.55 + 0.45 * (t / 30)
        if target_m - s < 100:         # 收尾减速
            v *= max(0.18, (target_m - s) / 100)
        mx, my = at(s)
        la, lo_ = geo_of(mx, my, clat, clng)
        for cp in pts3:                # 打卡点减速(真人停走形态)
            if haversine_m(la, lo_, cp[0], cp[1]) < spec.BJ_SLOWDOWN_M - 4 \
                    and rnd.random() < 0.45:
                v *= rnd.uniform(0.35, 0.6)
                break
        v = max(0.02, min(v, 6.2))
        jl = jl * 0.86 + rnd.gauss(0, 1.6)          # 米
        jn = jn * 0.86 + rnd.gauss(0, 1.9)
        if rnd.random() < 0.008:
            jl += rnd.uniform(-13.0, 13.0)
            jn += rnd.uniform(-13.0, 13.0)
        lat, lng = geo_of(mx + jl, my + jn, clat, clng)

        pt = {"a": quantize_geo(lat), "o": quantize_geo(lng)}
        if i > 0 and rnd.random() > 0.004:
            pt["s"] = round(max(0.02, v + rnd.gauss(0, 0.15)), 2)
        dmin = min(haversine_m(pt["a"], pt["o"], cp[0], cp[1]) for cp in pts3)
        b = None
        if dmin < spec.BJ_SLOWDOWN_M:
            b = 2 if rnd.random() < 0.75 else (1 if rnd.random() < 0.4 else None)
        elif rnd.random() < 0.32:
            b = 1
        if b is not None:
            pt["b"] = b
        c = abs(rnd.gauss(0.06, 0.05))
        if rnd.random() < 0.015:
            c += rnd.uniform(0.15, 0.5)
        pt["c"] = f"{min(c, 0.60):.2f}"
        track.append(pt)

        for cp in pts3:
            if cp[2] not in crossings \
                    and haversine_m(pt["a"], pt["o"], cp[0], cp[1]) <= spec.BJ_SLOWDOWN_M:
                crossings[cp[2]] = (i, s)
        s += v * (spec.SAMPLE_DT + rnd.uniform(-0.02, 0.02))
        t += spec.SAMPLE_DT
        i += 1
        if s >= target_m and all(cp[2] in crossings for cp in pts3):
            break
        if i > 460:
            break

    for _ in range(2):                 # 尾部滑停
        pt = dict(track[-1])
        pt["a"] = quantize_geo(track[-1]["a"] + rnd.gauss(0, 1.0) / 111000.0)
        pt["o"] = quantize_geo(track[-1]["o"] + rnd.gauss(0, 1.1) / 111000.0)
        pt["s"] = round(rnd.uniform(0.01, 0.06), 2)
        pt["b"] = 2
        track.append(pt)
        t += spec.SAMPLE_DT

    dist_m = path_len_m([(p["a"], p["o"]) for p in track])
    return track, crossings, int(round(t)), round(dist_m / 1000.0, 2)


# ================================================================ log/step 组装
def log_data_template(track, anchors, start_time, end_time, rnd=None):
    """template log_data(真人语义, 2026-09 实证):
    coordinates=通过时刻GPS; time=首通过墙钟; distance=累计公里(两位小数,
    近0写"0.0"); order=配置点序不排序; longitude/longtitude 双拼写。"""
    rnd = rnd or random.Random()
    n = len(track)
    cum_d = [0.0]
    cum_t = [0.0]
    for a, b in zip(track, track[1:]):
        d = haversine_m(a["a"], a["o"], b["a"], b["o"])
        cum_d.append(cum_d[-1] + d)
        v = b.get("s")
        cum_t.append(cum_t[-1] + (d / max(0.5, v) if v else d / spec.SAMPLE_DT))
    used = max(1, end_time - start_time)
    scale = used / max(1e-6, cum_t[-1])       # 轨迹时间 → 墙钟
    entries = []
    for p in anchors:
        la, lo = p["latitude"], p["longtitude"]
        idx = None
        for k in range(n):             # 首通过 = 第一个进入判定半径的点
            if haversine_m(track[k]["a"], track[k]["o"], la, lo) \
                    <= spec.CHECKIN_RADIUS_M:
                idx = k
                break
        if idx is None:                # 兜底: 最近点
            idx = min(range(n), key=lambda k: haversine_m(
                track[k]["a"], track[k]["o"], la, lo))
        t_off = max(0.0, min(float(used), cum_t[idx] * scale + rnd.uniform(-3, 3)))
        d_km = cum_d[idx] / 1000.0
        entries.append({
            "latitude": track[idx]["a"],
            "longitude": track[idx]["o"],
            "longtitude": track[idx]["o"],
            "point_id": str(p["point_id"]),
            "time": int(start_time + t_off),
            "distance": "0.0" if d_km < 0.005 else round(d_km, 2),
        })
    return entries


def log_data_ring(anchors, track, crossings, start_ts, scale=1.0):
    """ring log_data。scale 缩放时间轴(配速区间把引擎速度整体拉快/拉慢时用)。"""
    rows = []
    cum = [0.0]
    for a, b in zip(track, track[1:]):
        cum.append(cum[-1] + haversine_m(a["a"], a["o"], b["a"], b["o"]))
    for cp in anchors:
        pid = cp[2]
        if pid not in crossings:
            idx, _ = min(enumerate(track),
                         key=lambda it: haversine_m(it[1]["a"], it[1]["o"],
                                                    cp[0], cp[1]))
            crossings[pid] = (idx, 0.0)
        idx, _ = crossings[pid]
        p = track[idx]
        dkm = round(cum[min(idx, len(cum) - 1)] / 1000.0, 2)
        rows.append({
            "latitude": p["a"], "longitude": p["o"],
            "distance": "0.0" if dkm == 0 else dkm,
            "point_id": str(pid), "time": int(start_ts + idx * spec.SAMPLE_DT * scale),
            "longtitude": p["o"],
        })
    return rows


def step_info(used_time, rnd=None):
    """真机计步形态: 桶数=used//60+1, 稀疏不规则值, 总量约束 180~360。"""
    rnd = rnd or random.Random()
    n = max(2, used_time // 60 + 1)
    lst = []
    for _ in range(n):
        r = rnd.random()
        if r < 0.12:
            lst.append(rnd.randint(0, 6))
        elif r < 0.25:
            lst.append(rnd.randint(70, 92))
        else:
            lst.append(rnd.randint(7, 40))
    total = sum(lst)
    if total < 180:
        lst[rnd.randrange(n)] += 180 - total + rnd.randint(0, 40)
    elif total > 360:
        k = rnd.randrange(n)
        lst[k] = max(0, lst[k] - (total - rnd.randint(300, 355)))
    return {"interval": 60, "list": lst}
