"""跑区几何: 从打卡点推算运动场的朝向、长宽、环向, 再给出每块场地的选点特调。

为什么需要它: 每个运动场的长轴朝向、宽窄、打卡点排布都不一样 —— 同一套「绕圈 +
固定贴点半径 + 随机起步」跑在不同场地上, 长轴方向与场地错开、窄场地贴点半径过大,
轨迹就会穿过看台或明显脱离跑道。这里把「这块场地长什么样」变成一组可解释的参数:

  * long_axis_deg  长轴罗盘方位(0=南北, 90=东西), 决定起步点与直道方向
  * length/width   长轴/短轴跨度(米), 窄场地自动收紧贴点半径
  * winding        点位环向(ccw/cw), 决定默认跑动方向
  * lane_m         跑道偏移: 生成轨迹沿点位环外扩的车道宽度
  * anchor_m       贴点半径: 轨迹必须靠到每个打卡点多近

全部是纯函数: 输入打卡点, 输出参数; 不读配置、不发网络。跑区特调(方向/半径/偏移)
以 tuning 覆写进来, 由调用方持久化。
"""
from __future__ import annotations

import math
import random

from .tracks import enu_of, geo_of, haversine_m

DEFAULT_ANCHOR_M = 20.0        # 与 Settings.anchor_max_m 的默认一致
MIN_ANCHOR_M = 8.0
MAX_ANCHOR_M = 20.0
MIN_LANE_M = 2.0
MAX_LANE_M = 8.0
DIRECTIONS = ('auto', 'ccw', 'cw')


def _points(points):
    """接受 [(lat,lng)] / [(lat,lng,pid)] / [{'latitude','longtitude'}]。"""
    out = []
    for p in points or []:
        if isinstance(p, dict):
            lng = p["longitude"] if "longitude" in p else p["longtitude"]
            out.append((float(p["latitude"]), float(lng)))
        else:
            out.append((float(p[0]), float(p[1])))
    return out


def _centroid(pts):
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _bearing_deg(dx, dy):
    """ENU 向量(东, 北) → 罗盘方位角, 归一到 [0, 180)。"""
    deg = math.degrees(math.atan2(dx, dy)) % 360.0
    return deg % 180.0


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def analyze(points):
    """跑区几何画像。点数不足时退化为 'single'/'line', 不抛错。"""
    pts = _points(points)
    if not pts:
        return {'count': 0, 'centroid': (0.0, 0.0), 'long_axis_deg': 0.0,
                'length_m': 0.0, 'width_m': 0.0, 'perimeter_m': 0.0,
                'winding': 'ccw', 'shape': 'single'}
    clat, clng = _centroid(pts)
    xy = [enu_of(lat, lng, clat, clng) for lat, lng in pts]
    if len(xy) == 1:
        return {'count': 1, 'centroid': (clat, clng), 'long_axis_deg': 0.0,
                'length_m': 0.0, 'width_m': 0.0, 'perimeter_m': 0.0,
                'winding': 'ccw', 'shape': 'single'}

    n = len(xy)
    sxx = sum(x * x for x, _ in xy) / n
    syy = sum(y * y for _, y in xy) / n
    sxy = sum(x * y for x, y in xy) / n
    # 主成分方向: 协方差矩阵的最大特征向量; 各向同性时 atan2(0,0)=0, 视为南北向
    angle = 0.5 * math.atan2(2 * sxy, sxx - syy)
    ux, uy = math.cos(angle), math.sin(angle)
    vx, vy = -uy, ux
    proj_u = [x * ux + y * uy for x, y in xy]
    proj_v = [x * vx + y * vy for x, y in xy]
    length = max(proj_u) - min(proj_u)
    width = max(proj_v) - min(proj_v)
    perimeter = sum(haversine_m(pts[i][0], pts[i][1],
                                pts[(i + 1) % n][0], pts[(i + 1) % n][1])
                    for i in range(n))
    area2 = 0.0
    for i in range(n):
        x1, y1 = xy[i]
        x2, y2 = xy[(i + 1) % n]
        area2 += x1 * y2 - x2 * y1
    shape = 'line' if width < 8.0 else 'poly'
    return {
        'count': n,
        'centroid': (round(clat, 6), round(clng, 6)),
        'long_axis_deg': round(_bearing_deg(ux, uy), 1),
        'length_m': round(length, 1),
        'width_m': round(width, 1),
        'perimeter_m': round(perimeter, 1),
        'winding': 'ccw' if area2 > 0 else 'cw',
        'shape': shape,
    }


def order_ring(points, direction='auto', start_deg=None):
    """按质心方位角排成环, 再按方向与起点旋转。

    direction='auto' 时用点位环向本身(ccw/cw); start_deg 是刻意选择的起步方位 ——
    同一块场地每次都从同一侧起步, 轨迹朝向才稳定, 不会今天顺时针明天逆时针。
    """
    pts = _points(points)
    if len(pts) < 3:
        return pts
    clat, clng = _centroid(pts)

    def angle(p):
        x, y = enu_of(p[0], p[1], clat, clng)
        return math.atan2(y, x)

    ring = sorted(pts, key=angle)                      # ENU 下升角 = 逆时针
    want = (direction or 'auto').lower()
    if want == 'cw':
        ring = [ring[0]] + ring[:0:-1]
    if start_deg is None:
        return ring

    # 起点: 方位最接近 start_deg 的那个点(同一侧起步, 场地朝向换了起点也跟着换)
    def bearing(p):
        x, y = enu_of(p[0], p[1], clat, clng)
        return _bearing_deg(x, y)

    def delta(p):
        d = abs(bearing(p) - float(start_deg) % 180.0)
        return min(d, 180.0 - d)

    first = min(ring, key=delta)
    i = ring.index(first)
    return ring[i:] + ring[:i]


def offset_ring(points, lane_m, direction='ccw'):
    """把点位环沿边法线外扩 lane_m 米, 得到一条等宽跑道。

    顶点用相邻两边法线的角平分线外推(miter), 并限制 miter 长度, 免得锐角顶点飞出天际。
    不足 3 点时原样返回 —— 两点场地没有「跑道」可言。
    """
    pts = _points(points)
    lane = float(lane_m or 0.0)
    if len(pts) < 3 or abs(lane) < 0.05:
        return pts
    clat, clng = _centroid(pts)
    xy = [enu_of(lat, lng, clat, clng) for lat, lng in pts]
    n = len(xy)
    ccw = (direction or 'ccw').lower() != 'cw'

    def unit(ax, ay, bx, by):
        dx, dy = bx - ax, by - ay
        d = math.hypot(dx, dy) or 1.0
        return dx / d, dy / d

    out = []
    for i in range(n):
        px, py = xy[i - 1]
        cx, cy = xy[i]
        nx_, ny_ = xy[(i + 1) % n]
        ax, ay = unit(px, py, cx, cy)
        bx, by = unit(cx, cy, nx_, ny_)
        # 逆时针环的行进方向左手是场内, 右手是场外: 法线取右侧
        na = (ay, -ax) if ccw else (-ay, ax)
        nb = (by, -bx) if ccw else (-by, bx)
        mx, my = na[0] + nb[0], na[1] + nb[1]
        norm = math.hypot(mx, my)
        if norm < 1e-6:                    # 折返边: 用单边法线
            mx, my, norm = na[0], na[1], 1.0
        cos_half = max(0.35, (mx / norm) * na[0] + (my / norm) * na[1])
        miter = _clamp(lane / cos_half, -2.5 * abs(lane), 2.5 * abs(lane))
        ox, oy = mx / norm * miter, my / norm * miter
        out.append(geo_of(cx + ox, cy + oy, clat, clng))
    return out


def _track_loop(xy, spacing_m=12.0):
    """四点打卡环铺成贴跑道的闭合环(ENU, 首点重复): 直道走直线, 弯道外凸圆弧。

    打卡点就在跑进线上, 环必须穿过每个点; 两条直道边保持直线, 两条弯道边用
    半径≈半宽 的圆弧向外凸, 避免拿弦切内场。四边等长的近正方形不是跑道, 返回 None。
    """
    n = len(xy)
    if n != 4:
        return None
    cx = sum(p[0] for p in xy) / n
    cy = sum(p[1] for p in xy) / n
    ring = sorted(xy, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))
    edges = [(ring[i], ring[(i + 1) % n]) for i in range(n)]
    lengths = [math.hypot(q[0] - p[0], q[1] - p[1]) for p, q in edges]
    if min(lengths) <= 0 or max(lengths) / min(lengths) < 1.1:
        return None
    dirs = [math.atan2(q[1] - p[1], q[0] - p[0]) % math.pi for p, q in edges]

    def pdiff(i, j):
        d = abs(dirs[i] - dirs[j]) % math.pi
        return min(d, math.pi - d)

    def width_of(pair):
        (a1, _a2), (b1, _b2) = edges[pair[0]], edges[pair[1]]
        th = dirs[pair[0]]
        nx, ny = -math.sin(th), math.cos(th)
        return abs((a1[0] * nx + a1[1] * ny) - (b1[0] * nx + b1[1] * ny)) / 2

    # 直道 = 更平行、且通常更长的一对对边; 两项加权打分取小者
    best = None
    for straight, caps in (((0, 2), (1, 3)), ((1, 3), (0, 2))):
        r = width_of(straight)
        if r < 8.0:
            continue
        score = math.degrees(pdiff(*straight)) \
            + 0.1 * ((lengths[caps[0]] + lengths[caps[1]])
                     - (lengths[straight[0]] + lengths[straight[1]]))
        if best is None or score < best[0]:
            best = (score, straight, caps, r)
    if best is None:
        return None
    _score, straight, _caps, r = best
    step = max(5.0, spacing_m)
    out = []
    for i, (p, q) in enumerate(edges):
        c = lengths[i]
        if i in straight:
            steps = max(1, int(round(c / step)))
            for k in range(steps):
                t = k / steps
                out.append((p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t))
            continue
        rr = max(r, c / 2)
        mx, my = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2
        h = math.sqrt(max(0.0, rr * rr - (c / 2) ** 2))
        ux, uy = (q[0] - p[0]) / c, (q[1] - p[1]) / c
        n1x, n1y = -uy, ux
        # 圆心靠质心一侧, 弧才向外凸
        if ((mx + n1x * h - cx) ** 2 + (my + n1y * h - cy) ** 2
                > (mx - n1x * h - cx) ** 2 + (my - n1y * h - cy) ** 2):
            n1x, n1y = -n1x, -n1y
        ccx, ccy = mx + n1x * h, my + n1y * h
        a0 = math.atan2(p[1] - ccy, p[0] - ccx)
        a1 = math.atan2(q[1] - ccy, q[0] - ccx)
        sweep = (a1 - a0) % (2 * math.pi)
        mid = a0 + sweep / 2
        if ((ccx + rr * math.cos(mid) - cx) ** 2 + (ccy + rr * math.sin(mid) - cy) ** 2
                < (mx - cx) ** 2 + (my - cy) ** 2):
            sweep -= 2 * math.pi
        steps = max(6, int(round(abs(sweep) * rr / step)))
        for k in range(steps):
            ang = a0 + sweep * k / steps
            out.append((ccx + rr * math.cos(ang), ccy + rr * math.sin(ang)))
    out.append(out[0])
    return out


def route_points(points, lane_m=0.0, direction='auto', start_deg=None,
                 spacing_m=12.0, rnd=None):
    """把「点位环 + 车道」铺成一条可跑的路线(闭合, 首点重复)。

    四点场地绕打卡点成环: 直道走直线、弯道外凸贴跑道, 不切内场; 其余场地保持多边形。
    直道按 spacing_m 采样并带一点横向游走(真人不会贴着白线跑)。
    """
    rnd = rnd or random.Random()
    geom = analyze(points)
    use_dir = direction if direction not in (None, 'auto') else geom['winding']
    ring = order_ring(points, direction=use_dir, start_deg=start_deg)
    if len(ring) < 2:
        return ring
    lane = float(lane_m or 0.0)
    drift = rnd.uniform(-1.4, 1.4)          # 整圈的内外漂移(同一条跑道不会圈圈不同)
    clat, clng = _centroid(ring)
    xy = [enu_of(p[0], p[1], clat, clng) for p in ring]
    loop = _track_loop(xy, spacing_m) if geom['shape'] == 'poly' else None
    if loop is not None:
        if use_dir == 'cw':
            loop = loop[::-1]
        dense = [geo_of(x, y, clat, clng) for x, y in loop]
        off = offset_ring(dense, lane + drift, direction=use_dir)
        oxy = [enu_of(p[0], p[1], clat, clng) for p in off]
        n = len(oxy)
        route = []
        wobble = 0.0
        for i in range(n - 1):              # 末点=首点, 游走不重复加
            px, py = oxy[i - 1]
            cx, cy = oxy[i]
            nx, ny = oxy[(i + 1) % n]
            dx, dy = nx - px, ny - py
            d = math.hypot(dx, dy) or 1.0
            wobble = _clamp(wobble * 0.7 + rnd.gauss(0, 0.5), -1.2, 1.2)
            route.append(geo_of(cx - dy / d * wobble, cy + dx / d * wobble,
                                clat, clng))
        route.append(route[0])
        return route
    ring = offset_ring(ring, lane + drift, direction=use_dir)
    clat, clng = _centroid(ring)
    route = []
    for i in range(len(ring)):
        a = ring[i]
        b = ring[(i + 1) % len(ring)]
        ax, ay = enu_of(a[0], a[1], clat, clng)
        bx, by = enu_of(b[0], b[1], clat, clng)
        dist = math.hypot(bx - ax, by - ay)
        steps = max(1, int(round(dist / max(5.0, float(spacing_m or 12.0)))))
        nx, ny = (-(by - ay), bx - ax)
        norm = math.hypot(nx, ny) or 1.0
        wobble = 0.0
        for k in range(steps):
            t = k / steps
            wobble = wobble * 0.7 + rnd.gauss(0, 0.5)
            wobble = _clamp(wobble, -1.2, 1.2)
            x = ax + (bx - ax) * t + nx / norm * wobble
            y = ay + (by - ay) * t + ny / norm * wobble
            route.append(geo_of(x, y, clat, clng))
    route.append(route[0])
    return route


def normalize_tuning(raw):
    """把界面/JSON 里的特调值收成受控词表: 方向 + 三个非负数字。"""
    raw = raw if isinstance(raw, dict) else {}
    direction = str(raw.get("direction") or "auto").lower()
    out = {"direction": direction if direction in DIRECTIONS else "auto"}
    for key in ("lane_m", "anchor_m", "start_deg"):
        try:
            value = float(raw.get(key) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        out[key] = round(max(0.0, value), 1)
    return out


def _coerce_lane(value, width):
    try:
        lane = float(value or 0.0)
    except (TypeError, ValueError):
        lane = 0.0
    if lane:
        return round(_clamp(lane, 0.0, 12.0), 1)
    return round(_clamp(0.18 * (width or 0.0), MIN_LANE_M, MAX_LANE_M), 1)


def _coerce_anchor(value, width):
    try:
        anchor = float(value or 0.0)
    except (TypeError, ValueError):
        anchor = 0.0
    if anchor:
        return round(_clamp(anchor, 5.0, 40.0), 1)
    # 窄场地贴点半径必须收紧: 20m 的容差在 60m 宽的场地上会跑到看台里
    base = 0.45 * (width or 0.0) if width else DEFAULT_ANCHOR_M
    return round(_clamp(base, MIN_ANCHOR_M, MAX_ANCHOR_M), 1)


def profile(points, tuning=None):
    """跑区特调: 几何 + 用户覆写 → 引擎参数。

    tuning: {'direction': auto|ccw|cw, 'lane_m': 0=自动, 'anchor_m': 0=自动,
             'start_deg': 0=自动}
    """
    tuning = tuning or {}
    geom = analyze(points)
    direction = str(tuning.get('direction') or 'auto').lower()
    if direction not in DIRECTIONS:
        direction = 'auto'
    resolved_direction = geom['winding'] if direction == 'auto' else direction
    try:
        start_deg = float(tuning.get('start_deg') or 0.0)
    except (TypeError, ValueError):
        start_deg = 0.0
    if not start_deg:
        start_deg = geom['long_axis_deg']
    lane = _coerce_lane(tuning.get('lane_m'), geom['width_m'])
    if not float(tuning.get('lane_m') or 0.0) and geom['count'] == 4 \
            and geom['shape'] == 'poly':
        pts = _points(points)
        clat, clng = _centroid(pts)
        if _track_loop([enu_of(lat, lng, clat, clng) for lat, lng in pts]) is not None:
            lane = 0.0                # 打卡点已在跑进线上: 不再自动外扩车道
    return {
        'zone_shape': geom['shape'],
        'direction': resolved_direction,
        'direction_choice': direction,
        'start_deg': round(start_deg % 180.0, 1),
        'lane_m': lane,
        'anchor_m': _coerce_anchor(tuning.get('anchor_m'), geom['width_m']),
        'long_axis_deg': geom['long_axis_deg'],
        'length_m': geom['length_m'],
        'width_m': geom['width_m'],
        'perimeter_m': geom['perimeter_m'],
        'winding': geom['winding'],
        'count': geom['count'],
    }


def summary(points, tuning=None):
    """给界面看的一句话几何 + 特调结果(不含任何坐标)。"""
    prof = profile(points, tuning)
    headings = {0: '南北', 90: '东西'}
    axis = min(headings, key=lambda k: abs(prof['long_axis_deg'] - k))
    axis_text = headings[axis] if abs(prof['long_axis_deg'] - axis) <= 22.5 \
        else f"{prof['long_axis_deg']:.0f}°"
    return {
        'shape': prof['zone_shape'],
        'points': prof['count'],
        'axis_deg': prof['long_axis_deg'],
        'axis_text': axis_text,
        'length_m': prof['length_m'],
        'width_m': prof['width_m'],
        'direction': prof['direction'],
        'direction_choice': prof['direction_choice'],
        'lane_m': prof['lane_m'],
        'anchor_m': prof['anchor_m'],
        'start_deg': prof['start_deg'],
        'text': (f"长轴{axis_text} · {prof['length_m']:.0f}m×{prof['width_m']:.0f}m · "
                 f"{'逆时针' if prof['direction'] == 'ccw' else '顺时针'} · "
                 f"跑道 {prof['lane_m']:.1f}m · 贴点 {prof['anchor_m']:.0f}m")
        if prof['count'] >= 3 else '点位不足, 先同步跑区',
    }
