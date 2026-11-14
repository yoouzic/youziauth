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

# 实测量出来的长轴罗盘方位(0=正北, 顺时针), 来源见 docs 里的跑区分析:
#   1873 第二运动场 围栏最小外接矩形 160.1°, 与 OSM 操场轮廓(164.7°) 差 4.6°
#   1874 第一运动场 校园长轴推断     42.3°,  OSM 41.3°
#   1875 第四运动场 真人轨迹直道     16.9°,  周边球场群 14.9~16.1°
#   1876 荣昌校区   田径场南北点对   152.0°, OSM 148.8°
# 为什么不信 PCA: 4 个打卡点做 PCA 常落在矩形的对角线上(1873 会得到 111.9°,
# 与真实 164.7° 差 53°), 按那个方向铺环就会斜着切过跑道。
AXIS_PRIORS = {
    '1873': 160.1, '1874': 42.3, '1875': 16.9, '1876': 152.0,
}

# 拟合出来的真跑道中心线(参数化体育场: 两段直道 + 两个半圆)。
# 有这条线就沿着它跑 —— 这才是真正的「贴着跑道」, 而不是围着打卡点绕一圈。
#   1875 真人轨迹拟合: 226 点、残差 RMS 5.1m、单圈 460m。
#   1874 卫星影像拟合: 1874 的打卡点在东北端挤成一团(两个点只隔 33m), 围着打卡点
#        铺环时那段弯道弧被压成贴弦的浅弧, 轨迹从内场凹进去。没有真人轨迹, 就从
#        高德卫星影像(z18) 按塑胶跑道颜色提取跑道外边界, 再拟合体育场参数:
#        外边界半径 37.6m、残差 RMS 2.2m, 内收半带宽 4.5m 得中心线, 单圈 320m。
#        东北弯道比对称模型瘦一圈(弧顶偏外 3~5m, 分道线/边界样本都指向同一侧),
#        所以 NE 弯心单独西移: cap_u_ne=25.0(对称值是 half_s=31.2), SW 弯不动。
#        校准: 服务端实测打卡点距中心线 1.7~13.1m, 我们这侧打卡点 3.4~14.7m,
#        全在 40m 判定半径内; 打卡点环形状变了护栏(_checkpoints_near)会兜回旧路径。
STADIUM_PRIORS = {
    '1874': {'center': (29.823774, 106.430968), 'axis_deg': 42.3,
             'half_s': 31.2, 'radius': 33.1, 'cap_u_ne': 25.0,
             'source': 'satellite_trace'},
    '1875': {'center': (29.816262, 106.421014), 'axis_deg': 15.5,
             'half_s': 48.8, 'radius': 42.2, 'source': 'crowd_track'},
}


def axis_prior(zone_id):
    """已知跑区的实测长轴(度); 不认识的跑区返回 None。"""
    key = str(zone_id or '').strip()
    return AXIS_PRIORS.get(key)



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


def _min_rect(xy):
    """最小面积外接矩形 → (方位角, 贴合度, 长, 宽)。

    逐条边方向当候选轴, 取面积最小者; 贴合度 = 矩形面积 / 点集自身面积比值的近似,
    用「点在最简多边形内的填充度」不好算, 这里用 点集凸包面积/矩形面积 代替:
    近矩形排布接近 0.5(凸包是半个矩形), 排成一条线时趋近 0。
    """
    n = len(xy)
    if n < 3:
        return 0.0, 0.0, 0.0, 0.0
    best = None
    for i in range(n):
        x1, y1 = xy[i]
        x2, y2 = xy[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            continue
        bearing = _bearing_deg(dx, dy)
        theta = math.radians(bearing)
        ux, uy = math.sin(theta), math.cos(theta)
        vx, vy = math.cos(theta), -math.sin(theta)
        us = [x * ux + y * uy for x, y in xy]
        vs = [x * vx + y * vy for x, y in xy]
        length = max(us) - min(us)
        width = max(vs) - min(vs)
        area = length * width
        # 矩形两条对边给出的面积相同, 取更长的方向当长轴(否则 200x120 的场地会
        # 随机报成 0° 或 90°, 铺出来的环就横着穿过跑道)
        if (best is None or area < best[0] - 1e-6
                or (abs(area - best[0]) <= 1e-6 and length > best[2])):
            best = (area, bearing, length, width)
    if not best:
        return 0.0, 0.0, 0.0, 0.0
    _area, bearing, length, width = best
    # 凸包面积(用于贴合度): 排布越接近矩形, 凸包/矩形 越接近 0.5
    hull = 0.0
    for i in range(n):
        x1, y1 = xy[i]
        x2, y2 = xy[(i + 1) % n]
        hull += x1 * y2 - x2 * y1
    hull_area = abs(hull) / 2.0
    box = max(length * width, 1e-9)
    fit = _clamp(hull_area / box / 0.5, 0.0, 1.0)
    return bearing, fit, length, width


def _axis_frame(xy, bearing_deg):
    """ENU → (沿长轴 u, 跨长轴 v) 坐标; 旋转不改变距离与面积符号。"""
    theta = math.radians(bearing_deg)
    ux, uy = math.sin(theta), math.cos(theta)
    vx, vy = math.cos(theta), -math.sin(theta)
    return [(x * ux + y * uy, x * vx + y * vy) for x, y in xy]


def _axis_unframe(uv, bearing_deg):
    theta = math.radians(bearing_deg)
    ux, uy = math.sin(theta), math.cos(theta)
    vx, vy = math.cos(theta), -math.sin(theta)
    return [(u * ux + v * vx, u * uy + v * vy) for u, v in uv]


def analyze(points, zone_id=None, axis_deg=None):
    """跑区几何画像。点数不足时退化为 'single'/'line', 不抛错。

    zone_id 已知时优先用实测长轴(AXIS_PRIORS), 否则用最小外接矩形, 最后才退回 PCA。
    """
    pts = _points(points)
    if not pts:
        return {'count': 0, 'centroid': (0.0, 0.0), 'long_axis_deg': 0.0,
                'axis_source': 'none', 'length_m': 0.0, 'width_m': 0.0,
                'perimeter_m': 0.0, 'winding': 'ccw', 'shape': 'single'}
    clat, clng = _centroid(pts)
    xy = [enu_of(lat, lng, clat, clng) for lat, lng in pts]
    if len(xy) == 1:
        return {'count': 1, 'centroid': (clat, clng), 'long_axis_deg': 0.0,
                'axis_source': 'none', 'length_m': 0.0, 'width_m': 0.0,
                'perimeter_m': 0.0, 'winding': 'ccw', 'shape': 'single'}

    n = len(xy)
    # PCA: 只当兜底 —— 四个点常常落在矩形对角线上
    sxx = sum(x * x for x, _ in xy) / n
    syy = sum(y * y for _, y in xy) / n
    sxy = sum(x * y for x, y in xy) / n
    angle = 0.5 * math.atan2(2 * sxy, sxx - syy)
    pca_deg = _bearing_deg(math.cos(angle), math.sin(angle))

    rect_deg, rect_fit, _rect_len, _rect_w = _min_rect(xy)
    prior = axis_prior(zone_id)
    try:
        learned = float(axis_deg or 0.0) % 180.0
    except (TypeError, ValueError):
        learned = 0.0
    if learned:
        axis_deg, source = learned, 'learned'
    elif prior is not None:
        axis_deg, source = float(prior), 'prior'
    elif rect_fit >= 0.75 and n >= 3:
        axis_deg, source = rect_deg, 'rect'
    else:
        axis_deg, source = pca_deg, 'pca'

    theta = math.radians(axis_deg)
    ux, uy = math.sin(theta), math.cos(theta)
    vx, vy = math.cos(theta), -math.sin(theta)
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
    shape = 'line' if min(width, length) < 8.0 else 'poly'
    return {
        'count': n,
        'centroid': (round(clat, 6), round(clng, 6)),
        'long_axis_deg': round(axis_deg % 180.0, 1),
        'axis_source': source,
        'length_m': round(length, 1),
        'width_m': round(width, 1),
        'perimeter_m': round(perimeter, 1),
        'winding': 'ccw' if area2 > 0 else 'cw',
        'shape': shape,
    }


def _segments_cross(a, b, c, d):
    """两条线段是否真相交(共端点不算)。经纬度小范围里当平面用足够。"""
    def ccw(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    d1 = ccw(c, d, a)
    d2 = ccw(c, d, b)
    d3 = ccw(a, b, c)
    d4 = ccw(a, b, d)
    return d1 * d2 < 0 and d3 * d4 < 0


def _untangle(ring):
    """2-opt 消交叉: 六个打卡点按方位角排会排成五角星, 铺出来自己穿自己。"""
    pts = list(ring)
    n = len(pts)
    if n < 4:
        return pts
    for _ in range(n * n):
        changed = False
        for i in range(n):
            for j in range(i + 2, n):
                if i == 0 and j == n - 1:
                    continue
                if _segments_cross(pts[i], pts[(i + 1) % n],
                                   pts[j], pts[(j + 1) % n]):
                    pts[i + 1:j + 1] = reversed(pts[i + 1:j + 1])
                    changed = True
        if not changed:
            break
    return pts


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
    ring = _untangle(ring)                             # 多点场地先消除自交
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


def _track_loop(xy, spacing_m=12.0, axis_deg=None):
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

    # 直道 = 方向最贴近场地长轴的那一对对边。轴已知(跑区实测或外接矩形)时,
    # 这一条比「谁更平行」可靠得多: 斜放的矩形两对对边都平行, 靠平行度会挑错。
    best = None
    for straight, caps in (((0, 2), (1, 3)), ((1, 3), (0, 2))):
        r = width_of(straight)
        if r < 8.0:
            continue
        if axis_deg is None:
            score = math.degrees(pdiff(*straight)) \
                + 0.1 * ((lengths[caps[0]] + lengths[caps[1]])
                         - (lengths[straight[0]] + lengths[straight[1]]))
        else:
            # 罗盘方位 → 数学角(从 +x 逆时针), 否则整整差 90°, 直道会挑成横边
            want = (90.0 - float(axis_deg)) % 180.0

            def axis_dist(index):
                d = abs(math.degrees(dirs[index]) - want) % 180.0
                return min(d, 180.0 - d)

            score = axis_dist(straight[0]) + axis_dist(straight[1])
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


def _rounded_loop(xy, radius_m=10.0, spacing_m=12.0):
    """沿打卡点连线走直线, 拐角用一段贝塞尔圆弧过渡(闭合, 首点重复)。

    为什么不用「有向包围盒 + 半圆弯」兜底: 四个点如果摆成菱形(边长接近), 半圆弯的
    两端会离两个打卡点 20 多米, 打卡就漏了。这里路一定经过每个点, 只是把尖角磨圆,
    跑步看着才像人而不是折线。
    """
    n = len(xy)
    if n < 3:
        return None
    step = max(3.0, float(spacing_m or 12.0))
    radius = max(3.0, float(radius_m or 10.0))
    # 圆角半径再大一倍也不能超过最短边的一小段: 校区级点位(边长几百米、圆心角又尖)
    # 用几十米的圆弧会在凹角处互相穿插, 画出来是一团乱线。
    shortest = min(math.hypot(xy[(i + 1) % n][0] - xy[i][0], xy[(i + 1) % n][1] - xy[i][1])
                   for i in range(n))
    radius = min(radius, 0.2 * shortest)
    corners = []
    for i in range(n):
        prev, cur, nxt = xy[i - 1], xy[i], xy[(i + 1) % n]
        v1 = (prev[0] - cur[0], prev[1] - cur[1])
        v2 = (nxt[0] - cur[0], nxt[1] - cur[1])
        l1 = math.hypot(*v1)
        l2 = math.hypot(*v2)
        if l1 < 1e-6 or l2 < 1e-6:
            return None
        u1 = (v1[0] / l1, v1[1] / l1)
        u2 = (v2[0] / l2, v2[1] / l2)
        cosang = _clamp(u1[0] * u2[0] + u1[1] * u2[1], -1.0, 1.0)
        theta = math.acos(cosang)
        if theta > math.pi - 0.02:                 # 近似共线: 直接过点
            corners.append((cur, cur, cur))
            continue
        # 尖角(内角小)只磨一点点: 大半径的圆弧会在凹处自己穿自己
        tight = 0.25 if theta > 0.55 else 0.12
        t = min(radius, tight * min(l1, l2))
        corners.append(((cur[0] + u1[0] * t, cur[1] + u1[1] * t),
                        cur,
                        (cur[0] + u2[0] * t, cur[1] + u2[1] * t)))
    path = []
    for i in range(n):
        p1, ctrl, p2 = corners[i]
        _a, _b, nxt_start = corners[(i + 1) % n]
        # 拐角弧: 二次贝塞尔, 半径小就几乎是尖角
        corner_steps = max(2, int(round(2.2 * radius / step)))
        for k in range(corner_steps + 1):
            s = k / corner_steps
            inv = 1 - s
            path.append((inv * inv * p1[0] + 2 * inv * s * ctrl[0] + s * s * p2[0],
                         inv * inv * p1[1] + 2 * inv * s * ctrl[1] + s * s * p2[1]))
        # 直道: p2 → 下一角的切入点
        target = nxt_start
        seg = math.hypot(target[0] - p2[0], target[1] - p2[1])
        steps = max(1, int(round(seg / step)))
        for k in range(1, steps):
            t = k / steps
            path.append((p2[0] + (target[0] - p2[0]) * t,
                         p2[1] + (target[1] - p2[1]) * t))
    path.append(path[0])
    return path


def _stadium_loop(xy, bearing_deg, spacing_m=12.0, pad_m=0.0):
    """把打卡点在有向包围盒里铺成「直道 + 半圆弯」的跑道环(ENU, 首点重复)。

    打卡点多半落在场地四角, 有向包围盒的边长就是这块场地能用的长宽; 直道沿长轴,
    两端用半径=半宽的半圆接起来 —— 人就是这么跑的, 也不会用弦切角穿过内场。
    尺寸不足时返回 None(交给调用方的退化路径)。
    """
    if len(xy) < 3:
        return None
    uv = _axis_frame(xy, bearing_deg)
    us = [u for u, _ in uv]
    vs = [v for _, v in uv]
    pad = max(0.0, float(pad_m or 0.0))
    half_u = (max(us) - min(us)) / 2.0 + pad
    half_v = (max(vs) - min(vs)) / 2.0 + pad
    cu = (max(us) + min(us)) / 2.0
    cv = (max(vs) + min(vs)) / 2.0
    if half_v < 8.0 or half_u < 8.0:
        return None
    step = max(3.0, float(spacing_m or 12.0))
    path = []
    if half_u - half_v < 1.0:              # 近圆形场地: 直接跑圈
        radius = half_v
        count = max(24, int(round(2 * math.pi * radius / step)))
        for k in range(count):
            ang = 2 * math.pi * k / count
            path.append((cu + radius * math.cos(ang), cv + radius * math.sin(ang)))
    else:
        straight = half_u - half_v
        n_straight = max(2, int(round(2 * straight / step)))
        for k in range(n_straight):        # 下直道: 左 → 右
            path.append((cu - straight + 2 * straight * k / n_straight, cv - half_v))
        arc = max(8, int(round(math.pi * half_v / step)))
        for k in range(arc + 1):           # 右侧半圆: -90° → +90°
            ang = -math.pi / 2 + math.pi * k / arc
            path.append((cu + straight + half_v * math.cos(ang),
                         cv + half_v * math.sin(ang)))
        for k in range(n_straight):        # 上直道: 右 → 左
            path.append((cu + straight - 2 * straight * k / n_straight, cv + half_v))
        for k in range(arc + 1):           # 左侧半圆: +90° → +270°
            ang = math.pi / 2 + math.pi * k / arc
            path.append((cu - straight + half_v * math.cos(ang),
                         cv + half_v * math.sin(ang)))
    path.append(path[0])
    return _axis_unframe(path, bearing_deg)


def _rotate_to_start(loop, xy, start_deg):
    """从「方位最接近 start_deg 的那个打卡点旁边」起步: 同一块场地每次起点一致。

    起点必须落在打卡点附近 —— 直接从环的某个方位角切进去会切在半圆弯的顶点上,
    那地方离任何打卡点都有 20~40m, 看起来像是凭空从跑道中段开始跑。
    """
    if not loop or start_deg is None or not xy:
        return loop
    cxs = sum(p[0] for p in xy) / len(xy)
    cys = sum(p[1] for p in xy) / len(xy)
    want = float(start_deg) % 180.0

    def delta(point):
        d = abs(_bearing_deg(point[0] - cxs, point[1] - cys) - want)
        return min(d, 180.0 - d)

    anchor = min(xy, key=delta)
    first = min(range(len(loop) - 1),
                key=lambda i: math.hypot(loop[i][0] - anchor[0], loop[i][1] - anchor[1]))
    body = loop[:-1]
    return body[first:] + body[:first] + [body[first]]


def stadium_line(prior, lane_m=0.0, spacing_m=12.0, direction='ccw', start_deg=None):
    """把拟合的体育场中心线铺成闭合点列(ENU 相对中心), 车道沿外法线偏移。

    弯道圆心默认在 ±half_s; prior 可用 cap_u_ne/cap_u_sw 单独指定 —— 真跑道两端
    弯道不总是一样大(1874 的东北弯就比对称模型瘦一圈), 直道端点跟弯心走, 半径
    共用 radius 才能和两条直道保持相切。lane 只扩 radius: 圆心不动, 圆变大仍然
    与外扩后的直道相切(旧实现连 half_s 一起加, 会让弯道顶端多飘出一个 lane)。
    """
    axis = float(prior['axis_deg'])
    half_s = float(prior['half_s'])
    radius = float(prior['radius']) + abs(lane_m)
    cap_ne = prior.get('cap_u_ne')
    cap_sw = prior.get('cap_u_sw')
    u_ne = half_s if cap_ne is None else float(cap_ne)
    u_sw = -half_s if cap_sw is None else float(cap_sw)
    step = max(3.0, float(spacing_m or 12.0))
    path = []
    n_straight = max(2, int(round((u_ne - u_sw) / step)))
    for k in range(n_straight):
        path.append((u_sw + (u_ne - u_sw) * k / n_straight, -radius))
    arc = max(10, int(round(math.pi * radius / step)))
    for k in range(arc + 1):
        ang = -math.pi / 2 + math.pi * k / arc
        path.append((u_ne + radius * math.cos(ang), radius * math.sin(ang)))
    for k in range(n_straight):
        path.append((u_ne - (u_ne - u_sw) * k / n_straight, radius))
    for k in range(arc + 1):
        ang = math.pi / 2 + math.pi * k / arc
        path.append((u_sw + radius * math.cos(ang), radius * math.sin(ang)))
    path.append(path[0])
    loop = _axis_unframe(path, axis)
    if str(direction or 'ccw').lower() == 'cw':
        loop = loop[::-1]
    return loop


def _checkpoints_near(line, xy, limit_m):
    """每个打卡点到这条线的最近距离都在 limit_m 以内才敢用这条线。"""
    worst = 0.0
    for point in xy:
        nearest = min(math.hypot(point[0] - q[0], point[1] - q[1]) for q in line)
        worst = max(worst, nearest)
        if worst > limit_m:
            return False
    return True


def route_points(points, lane_m=0.0, direction='auto', start_deg=None,
                 spacing_m=12.0, rnd=None, zone_id=None, anchor_m=None):
    """把「点位环 + 车道」铺成一条可跑的路线(闭合, 首点重复)。

    跑道形状 = 有向包围盒上的直道 + 半圆弯: 方向由跑区实测长轴决定(见 AXIS_PRIORS),
    车道向外偏移, 再叠一点横向游走 —— 真人不会贴着白线跑。
    """
    rnd = rnd or random.Random()
    geom = analyze(points, zone_id=zone_id)
    use_dir = direction if direction not in (None, 'auto') else geom['winding']
    ring = order_ring(points, direction=use_dir, start_deg=start_deg)
    if len(ring) < 2:
        return ring
    lane = float(lane_m or 0.0)
    drift = rnd.uniform(-1.4, 1.4)          # 整圈的内外漂移(同一条跑道不会圈圈不同)
    clat, clng = _centroid(ring)
    xy = [enu_of(p[0], p[1], clat, clng) for p in ring]
    # 有拟合跑道且每个打卡点都在判定半径内 → 直接沿真跑道跑
    prior = STADIUM_PRIORS.get(str(zone_id or '').strip())
    if prior is not None:
        line = stadium_line(prior, lane_m=lane, spacing_m=spacing_m,
                            direction=use_dir, start_deg=start_deg)
        # 判定半径是服务端的 40m, 不是我们给轨迹留的自定义贴点半径 ——
        # 真跑道离打卡点 23m 也是合法的, 不能用 20m 的门槛把它挡掉。
        if _checkpoints_near(line, xy, max(40.0, float(anchor_m or 0.0))):
            base = enu_of(prior['center'][0], prior['center'][1], clat, clng)
            shifted = [(x + base[0], y + base[1]) for x, y in line]
            shifted = _rotate_to_start(shifted, xy, start_deg)
            return [geo_of(x, y, clat, clng) for x, y in shifted]
    # 环一律先在「逆时针」的标准朝向上铺, 再按方向整体翻转 —— 否则 cw 会被翻两次,
    # 生成出来的轨迹跑到反方向去。
    xy_ccw = [enu_of(p[0], p[1], clat, clng)
              for p in order_ring(points, direction='ccw')]
    loop = _track_loop(xy_ccw, spacing_m, axis_deg=geom['long_axis_deg'])
    if loop is None:
        loop = _rounded_loop(xy_ccw, radius_m=max(8.0, 0.18 * geom['width_m']),
                             spacing_m=spacing_m)
    if loop is None:
        loop = _stadium_loop(xy_ccw, geom['long_axis_deg'], spacing_m,
                             pad_m=max(lane + drift, 2.0))
    if loop is not None:
        if use_dir == 'cw':
            loop = loop[::-1]
        loop = _rotate_to_start(loop, xy, start_deg)
        dense = [geo_of(x, y, clat, clng) for x, y in loop]
        oxy = [enu_of(p[0], p[1], clat, clng) for p in dense]
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
    for key in ("lane_m", "anchor_m", "start_deg", "axis_deg"):
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


def profile(points, tuning=None, zone_id=None):
    """跑区特调: 几何 + 用户覆写 → 引擎参数。

    tuning: {'direction': auto|ccw|cw, 'lane_m': 0=自动, 'anchor_m': 0=自动,
             'start_deg': 0=自动}
    zone_id: 已知跑区时用实测长轴(见 AXIS_PRIORS), 不再让 PCA 猜对角线。
    """
    tuning = tuning or {}
    try:
        axis_override = float(tuning.get('axis_deg') or 0.0)
    except (TypeError, ValueError):
        axis_override = 0.0
    geom = analyze(points, zone_id=zone_id, axis_deg=axis_override)
    direction = str(tuning.get('direction') or 'auto').lower()
    if direction not in DIRECTIONS:
        direction = 'auto'
    # 打卡点是「点」不是「路」, 它们的环绕方向随服务器下发顺序而变; 实测跑动是
    # 逆时针, 所以 auto 直接给 ccw, 要顺时针由跑区特调覆盖。
    resolved_direction = 'ccw' if direction == 'auto' else direction
    try:
        start_deg = float(tuning.get('start_deg') or 0.0)
    except (TypeError, ValueError):
        start_deg = 0.0
    if not start_deg:
        start_deg = geom['long_axis_deg']
    lane = _coerce_lane(tuning.get('lane_m'), geom['width_m'])
    if not float(tuning.get('lane_m') or 0.0) and geom['count'] == 4:
        pts = _points(points)
        clat, clng = _centroid(pts)
        if _track_loop([enu_of(lat, lng, clat, clng) for lat, lng in pts],
                       axis_deg=geom['long_axis_deg']) is not None:
            lane = 0.0                # 打卡点已在跑进线上: 不再自动外扩车道
    return {
        'zone_id': str(zone_id or ''),
        'axis_source': geom['axis_source'],
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
        'axis_source': prof['axis_source'],
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
