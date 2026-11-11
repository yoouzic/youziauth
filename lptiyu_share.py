"""把本机的跑区几何与跑步结果整理成一份可以发回来的 Markdown 样本。

设计原则:
  * **只带几何与结果**: 打卡点坐标、当时用的朝向、服务器记录的实际通过点、
    成绩与判定结果。token、账号、姓名、学号、uid 一律不出现。
  * **人能读、机器也能用**: 每个跑区一段, 表格列固定; 坐标用 `lat, lng` 六位小数,
    正好是判定半径(40m)需要的精度, 再细就是噪音。
  * **失败也要留**: 被拒的那次同样有价值 —— 它说明「这条线在真机上被判失败」,
    是修正朝向/形状最直接的证据。
"""
from __future__ import annotations

import datetime as dt
import math

MAX_ROWS_PER_ZONE = 20


def _fmt(value, digits=2):
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _coord(point):
    """接受 {'lat','lng'} / {'latitude','longtitude'} / 'lat,lng' / (lat,lng)。"""
    if point is None:
        return None
    if isinstance(point, str):
        pieces = [p.strip() for p in point.replace("，", ",").split(",")]
        if len(pieces) != 2:
            return None
        try:
            return float(pieces[0]), float(pieces[1])
        except ValueError:
            return None
    if isinstance(point, dict):
        lat = point.get("lat", point.get("latitude"))
        lng = point.get("lng", point.get("longtitude", point.get("longitude")))
        if lat is None or lng is None:
            return None
        try:
            return float(lat), float(lng)
        except (TypeError, ValueError):
            return None
    if isinstance(point, (list, tuple)) and len(point) >= 2:
        try:
            return float(point[0]), float(point[1])
        except (TypeError, ValueError):
            return None
    return None


def _dist_m(a, b):
    lat = math.radians((a[0] + b[0]) / 2.0)
    return math.hypot((a[1] - b[1]) * 111320.0 * math.cos(lat), (a[0] - b[0]) * 110570.0)


def track_axis(points, min_points=8, min_share=0.25):
    """从真实通过点估跑道长轴(罗盘方位), 返回 (deg, 置信占比) 或 (None, 0.0)。

    做法与实验一致: 逐段方位按段长加权分 1° 箱, 取峰值箱, 再在 ±6° 内做圆均值精化。
    只有 4 个打卡点时不靠谱(那只是矩形的四个角), 所以默认要求至少 8 个点 ——
    也就是同一跑区积累过两次以上跑步记录之后才启用。
    """
    pts = [p for p in (_coord(x) for x in (points or [])) if p]
    if len(pts) < min_points:
        return None, 0.0
    lat = math.radians(sum(p[0] for p in pts) / len(pts))
    bins = {}
    total = 0.0
    for a, b in zip(pts, pts[1:]):
        dx = (b[1] - a[1]) * 111320.0 * math.cos(lat)
        dy = (b[0] - a[0]) * 110570.0
        length = math.hypot(dx, dy)
        if length < 3.0:
            continue
        bearing = math.degrees(math.atan2(dx, dy)) % 180.0
        key = int(bearing) % 180
        bins[key] = bins.get(key, 0.0) + length
        total += length
    if total <= 0:
        return None, 0.0
    peak = max(bins, key=bins.get)
    near = [key for key in bins if min(abs(key - peak), 180 - abs(key - peak)) <= 6]
    # 占比按 ±6° 的邻域算: 单看一个 1° 箱会把真实轨迹的抖动算成「不集中」
    share = sum(bins[key] for key in near) / total
    if share < min_share:
        return None, round(share, 2)
    sx = sy = sw = 0.0
    for key in near:
        weight = bins[key]
        angle = math.radians(2 * key)
        sx += math.cos(angle) * weight
        sy += math.sin(angle) * weight
        sw += weight
    if sw <= 0:
        return None, round(share, 2)
    refined = (math.degrees(math.atan2(sy, sx)) / 2) % 180.0
    return round(refined, 1), round(share, 2)


def zone_passes(samples, zone_id):
    """把某跑区所有样本里的实际通过点汇到一起(去重)。"""
    seen, out = set(), []
    for sample in samples or []:
        if str(sample.get('zone_id')) != str(zone_id):
            continue
        for point in sample.get('passed_points') or []:
            coord = _coord(point)
            if not coord:
                continue
            key = (round(coord[0], 6), round(coord[1], 6))
            if key in seen:
                continue
            seen.add(key)
            out.append(coord)
    return out


def sample_line(sample):
    """一行样本的表格单元格。"""
    passed = [p for p in (_coord(x) for x in (sample.get("passed_points") or [])) if p]
    points = [p for p in (_coord(x) for x in (sample.get("zone_points") or [])) if p]
    worst = "—"
    if passed and points:
        worst = f"{max(min(_dist_m(pt, q) for q in passed) for pt in points):.0f} m"
    return {
        "date": str(sample.get("at") or "")[:10],
        "distance": _fmt(sample.get("distance"), 2),
        "time": _fmt(sample.get("used_time"), 0),
        "status": str(sample.get("record_status") if sample.get("record_status") is not None
                       else "—"),
        "worst": worst,
        "style": str(sample.get("style") or "—"),
        "reason": (str(sample.get("record_failed_reason") or "")[:40] or "—"),
    }


def capture_log_tail(store, lines=40):
    """采集日志的最后几行: 「没流量 / 握手失败 / 没登录」一看就知道是哪种。"""
    root = getattr(store, "root", None)
    if root is None:
        return []
    path = root / "capture" / "capture.log"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [line for line in text.splitlines() if line.strip()][-lines:]


def build_diagnose(report, version="", now=None):
    """采集自检结果写成一份能直接发回来的小报告。"""
    now = now or dt.datetime.now()
    report = report or {}
    cert = report.get("cert") or {}
    proxy = report.get("proxy") or {}
    lines = [
        "# 乐跑采集自检（youziauth）",
        "",
        f"- 时间：{now.isoformat(timespec='seconds')}",
        f"- 版本：{version or '—'}",
        "",
        "## 结论",
        "",
        f"**{report.get('verdict') or '—'}**",
        "",
        "## 逐项",
        "",
        "| 检查 | 结果 |",
        "|---|---|",
        f"| weixin:// 协议 | {'已注册' if report.get('wechat_protocol') else '未注册'} |",
        f"| 微信安装 | {'找到' if report.get('wechat_installed') else '没找到'} |",
        f"| 微信进程 | {'在运行' if report.get('wechat_running') else '没运行'} |",
        f"| 采集证书 | {'已安装 ' + str(cert.get('fingerprint') or '')[:16] if cert.get('installed') else '未安装'} |",
        f"| 系统代理 | {proxy.get('server') or '—'}（{'开' if proxy.get('enable') else '关'}） |",
        f"| 代理链自测 | {report.get('proxy_detail') or '—'} |",
        f"| 上次采集 | {report.get('capture_state') or '—'}，收到 {report.get('connects', 0)} 个连接 |",
        "",
    ]
    tail = [str(line) for line in (report.get("log_tail") or [])]
    if tail:
        lines.append("## capture.log 末尾")
        lines.append("")
        lines.append("```")
        lines.extend(tail)
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def build_markdown(store, version="", now=None):
    """整份报告。store 需要 samples() / zones() / settings()。"""
    now = now or dt.datetime.now()
    samples = store.samples() if hasattr(store, "samples") else []
    zones = store.zones() if hasattr(store, "zones") else {}

    lines = [
        "# 乐跑轨迹样本（youziauth）",
        "",
        f"- 导出时间：{now.isoformat(timespec='seconds')}",
        f"- 程序版本：{version or '—'}",
        f"- 样本条数：{len(samples)}",
        "",
        "> 这份文件只有跑区几何和跑步结果：打卡点坐标、当时用的场地朝向、"
        "服务器记录的实际通过点、距离/用时/判定结果。",
        "> **不含** token、账号、姓名、学号、uid。确认无误后直接发回给我即可。",
        "",
        "## 怎么用",
        "",
        "1. 在乐跑页点「导出轨迹样本」，桌面会生成这份同名文件；",
        "2. 原样发回（微信/邮件都行），不需要改任何内容；",
        "3. 样本越多越准：同一跑区跑过几次就有几行，被拒的那次同样有用。",
        "",
        "## 跑区几何",
        "",
    ]

    by_zone = {}
    for sample in samples:
        by_zone.setdefault(str(sample.get("zone_id") or sample.get("game_id") or "—"), []).append(sample)

    zone_ids = sorted(by_zone) or sorted(str(z) for z in zones)
    if not zone_ids:
        lines.append("_本机还没有跑区数据：先在乐跑页同步一次跑区。_")
        lines.append("")

    for zone_id in zone_ids:
        rows = by_zone.get(zone_id, [])
        entry = (zones or {}).get(zone_id) or {}
        name = str(entry.get("name") or (rows[0].get("zone_name") if rows else "") or "—")
        lines.append(f"### {zone_id} {name}")
        lines.append("")
        points = [p for p in (_coord(x) for x in (entry.get("points") or [])) if p]
        if not points and rows:
            points = [p for p in (_coord(x) for x in (rows[0].get("zone_points") or [])) if p]
        if points:
            lines.append("打卡点：" + "；".join(f"{lat:.6f}, {lng:.6f}" for lat, lng in points))
        else:
            lines.append("打卡点：_未记录_")
        axis = entry.get("axis_deg")
        source = entry.get("axis_source")
        if rows and rows[0].get("zone_axis_deg") is not None:
            axis = rows[0].get("zone_axis_deg")
            source = rows[0].get("zone_axis_source") or source
        if axis is not None:
            lines.append(f"本机当时的朝向：{_fmt(axis, 1)}°"
                         + (f"（{source}）" if source else ""))
        lines.append("")
        if not rows:
            lines.append("_这个跑区还没有跑步样本。_")
            lines.append("")
            continue
        lines.append("| 日期 | 距离 km | 用时 s | 判定 | 与打卡点最大偏差 | 轨迹风格 | 备注 |")
        lines.append("|---|---|---|---|---|---|---|")
        for sample in rows[:MAX_ROWS_PER_ZONE]:
            cell = sample_line(sample)
            lines.append("| {date} | {distance} | {time} | {status} | {worst} | {style} | {reason} |"
                         .format(**cell))
        lines.append("")
        latest = rows[0]
        passed = [p for p in (_coord(x) for x in (latest.get("passed_points") or [])) if p]
        if passed:
            lines.append("最近一次服务器记录的实际通过点：")
            lines.append("")
            lines.append("```")
            for lat, lng in passed:
                lines.append(f"{lat:.6f}, {lng:.6f}")
            lines.append("```")
            lines.append("")

    tail = capture_log_tail(store)
    if tail:
        lines.append("## 采集诊断（本机最近一次采集）")
        lines.append("")
        lines.append("```")
        lines.extend(tail)
        lines.append("```")
        lines.append("")

    lines.append("## 想让我优先看什么")
    lines.append("")
    lines.append("- 哪块场地跑起来「不像在跑道上」（比如明显切进内场、绕过看台）；")
    lines.append("- 哪次被判失败、当时界面上的提示是什么；")
    lines.append("- 你观察到的真实跑道方向（东南—西北之类）也可以直接写一句。")
    lines.append("")
    return "\n".join(lines)
