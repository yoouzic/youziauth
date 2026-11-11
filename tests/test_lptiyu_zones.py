"""跑区几何: 朝向、长宽、环向、起步方位与选点特调。

这些参数决定「每块场地怎么跑」, 所以逐条钉死: 朝向算错 90°, 轨迹就会横穿球场。
"""
import math
import random
import unittest

from lptiyu import zones
from lptiyu.tracks import enu_of, geo_of

CENTER = (29.8200, 106.4300)


def rect(length_m=120.0, width_m=60.0, angle_deg=0.0, center=CENTER, order='ccw'):
    """以 center 为中心、按 angle_deg 旋转的矩形四个角点(ENU 逆时针)。"""
    half_l, half_w = length_m / 2.0, width_m / 2.0
    corners = [(-half_w, -half_l), (half_w, -half_l), (half_w, half_l), (-half_w, half_l)]
    rad = math.radians(angle_deg)
    cos_r, sin_r = math.cos(rad), math.sin(rad)
    points = []
    for x, y in corners:
        points.append(geo_of(x * cos_r - y * sin_r, x * sin_r + y * cos_r, *center))
    return points if order == 'ccw' else points[::-1]


def signed_area(points):
    clat = sum(p[0] for p in points) / len(points)
    clng = sum(p[1] for p in points) / len(points)
    xy = [((p[1] - clng) * 111320 * math.cos(math.radians(clat)),
           (p[0] - clat) * 110570) for p in points]
    total = 0.0
    for i in range(len(xy)):
        x1, y1 = xy[i]
        x2, y2 = xy[(i + 1) % len(xy)]
        total += x1 * y2 - x2 * y1
    return total / 2.0


class AnalyzeTests(unittest.TestCase):
    def test_long_axis_follows_the_field_orientation(self):
        north_south = zones.analyze(rect(angle_deg=0))
        east_west = zones.analyze(rect(angle_deg=90))
        self.assertAlmostEqual(north_south['long_axis_deg'], 0.0, delta=2.0)
        self.assertAlmostEqual(east_west['long_axis_deg'], 90.0, delta=2.0)

    def test_extents_are_measured_in_meters(self):
        geom = zones.analyze(rect(length_m=120, width_m=60))
        self.assertAlmostEqual(geom['length_m'], 120, delta=3)
        self.assertAlmostEqual(geom['width_m'], 60, delta=3)
        self.assertAlmostEqual(geom['perimeter_m'], 360, delta=8)
        self.assertEqual(geom['count'], 4)
        self.assertEqual(geom['shape'], 'poly')

    def test_winding_is_read_from_the_point_order(self):
        self.assertEqual(zones.analyze(rect(order='ccw'))['winding'], 'ccw')
        self.assertEqual(zones.analyze(rect(order='cw'))['winding'], 'cw')

    def test_degenerate_inputs_do_not_raise(self):
        self.assertEqual(zones.analyze([])['shape'], 'single')
        self.assertEqual(zones.analyze([CENTER])['shape'], 'single')
        line = [geo_of(0, 0, *CENTER), geo_of(80, 0, *CENTER), geo_of(160, 0, *CENTER)]
        self.assertEqual(zones.analyze(line)['shape'], 'line')


class RingOrderTests(unittest.TestCase):
    def test_ccw_and_cw_visit_the_same_corners_in_opposite_order(self):
        points = rect()
        ccw = zones.order_ring(points, direction='ccw')
        cw = zones.order_ring(points, direction='cw')
        self.assertGreater(signed_area(ccw), 0)
        self.assertLess(signed_area(cw), 0)
        self.assertEqual(sorted(ccw), sorted(cw))

    def test_start_bearing_picks_the_nearby_corner(self):
        points = rect()
        northeast = zones.order_ring(points, direction='ccw', start_deg=45)
        southwest = zones.order_ring(points, direction='ccw', start_deg=135)
        self.assertNotEqual(northeast[0], southwest[0], '不同起步方位要落在不同角上')

        def bearing(point):
            dx = (point[1] - CENTER[1]) * math.cos(math.radians(CENTER[0]))
            dy = point[0] - CENTER[0]
            return math.degrees(math.atan2(dx, dy)) % 180.0

        self.assertAlmostEqual(bearing(northeast[0]), 26.6, delta=6.0)
        self.assertAlmostEqual(bearing(southwest[0]), 153.4, delta=6.0)


class OffsetTests(unittest.TestCase):
    def test_lane_moves_the_ring_outward(self):
        points = rect(length_m=120, width_m=60)
        inner = zones.offset_ring(points, 0)
        lane = zones.offset_ring(points, 6, direction='ccw')
        self.assertEqual(inner, points, '零车道就是原点位环')
        # 每个角都离中心更远了(角上 miter ≈ 6/cos45°)
        for before, after in zip(inner, lane):
            d_before = math.hypot((before[1] - CENTER[1]) * 111320 * math.cos(math.radians(CENTER[0])),
                                  (before[0] - CENTER[0]) * 110570)
            d_after = math.hypot((after[1] - CENTER[1]) * 111320 * math.cos(math.radians(CENTER[0])),
                                 (after[0] - CENTER[0]) * 110570)
            self.assertGreater(d_after, d_before + 5.0)

    def test_two_point_field_has_no_lane(self):
        points = [geo_of(0, 0, *CENTER), geo_of(60, 0, *CENTER)]
        self.assertEqual(zones.offset_ring(points, 5), points)


class ProfileTests(unittest.TestCase):
    def test_narrow_field_tightens_the_anchor_radius(self):
        wide = zones.profile(rect(width_m=60))
        narrow = zones.profile(rect(width_m=30))
        self.assertLessEqual(narrow['anchor_m'], wide['anchor_m'])
        self.assertLess(narrow['anchor_m'], 20.0)
        self.assertLessEqual(narrow['lane_m'], wide['lane_m'])

    def test_tuning_overrides_the_automatic_values(self):
        prof = zones.profile(rect(), {'direction': 'cw', 'anchor_m': 9, 'lane_m': 3, 'start_deg': 45})
        self.assertEqual(prof['direction'], 'cw')
        self.assertEqual(prof['direction_choice'], 'cw')
        self.assertEqual(prof['anchor_m'], 9.0)
        self.assertEqual(prof['lane_m'], 3.0)
        self.assertEqual(prof['start_deg'], 45.0)

    def test_auto_direction_is_counter_clockwise(self):
        # 打卡点是「点」不是「路」: 服务器下发顺序的环绕方向不代表跑向。
        # 实测那次跑动是逆时针, 所以 auto 固定给 ccw, 顺时针只能由跑区特调覆盖。
        self.assertEqual(zones.profile(rect(order='cw'))['direction'], 'ccw')
        self.assertEqual(zones.profile(rect(order='ccw'))['direction'], 'ccw')
        self.assertEqual(zones.profile(rect(), {'direction': 'cw'})['direction'], 'cw')
        self.assertEqual(zones.profile(rect(), {'direction': 'cw'})['direction_choice'], 'cw')

    def test_normalize_tuning_drops_unknown_values(self):
        self.assertEqual(zones.normalize_tuning({'direction': 'sideways', 'lane_m': -2}),
                         {'direction': 'auto', 'lane_m': 0.0, 'anchor_m': 0.0, 'start_deg': 0.0})
        self.assertEqual(zones.normalize_tuning(None)['direction'], 'auto')

    def test_summary_speaks_in_plain_words(self):
        text = zones.summary(rect(angle_deg=0))['text']
        self.assertIn('长轴南北', text)
        self.assertIn('贴点', text)
        self.assertIn('跑道', text)
        self.assertEqual(zones.summary([])['text'], '点位不足, 先同步跑区')
        self.assertNotIn('29.82', text, '摘要里不该出现坐标')


class TrackLoopTests(unittest.TestCase):
    """四点场地绕打卡点成环: 直道走直线, 弯道外凸贴跑道, 不切内场。"""

    THETA = math.radians(20)
    A, R = 45.0, 30.0

    def boundary(self, u, v):
        x = u * math.cos(self.THETA) - v * math.sin(self.THETA)
        y = u * math.sin(self.THETA) + v * math.cos(self.THETA)
        return geo_of(x, y, *CENTER)

    def off_tangent_points(self):
        a, r = self.A, self.R
        return [self.boundary(-20, -r),
                self.boundary(a + r * math.cos(math.radians(-50)),
                              r * math.sin(math.radians(-50))),
                self.boundary(10, r),
                self.boundary(-a + r * math.cos(math.radians(140)),
                              r * math.sin(math.radians(140)))]

    def enu(self, p):
        return enu_of(p[0], p[1], *CENTER)

    def test_loop_passes_every_checkpoint(self):
        points = self.off_tangent_points()
        route = zones.route_points(points, lane_m=0, direction='ccw',
                                   rnd=random.Random(7))
        for corner in points:
            cx, cy = self.enu(corner)
            nearest = min(math.hypot(x - cx, y - cy)
                          for x, y in map(self.enu, route))
            self.assertLess(nearest, 3.0, '绕环路线仍要经过每个打卡点')

    def test_loop_bulges_outward_instead_of_cutting_the_infield(self):
        points = self.off_tangent_points()
        route = zones.route_points(points, lane_m=0, direction='ccw',
                                   rnd=random.Random(7))
        clat, clng = CENTER
        quad_r = max(math.hypot(*self.enu(p)) for p in points)
        route_r = max(math.hypot(*self.enu(p)) for p in route)
        self.assertGreater(route_r, quad_r + 2.0,
                           '弯道要外凸贴住跑道, 不能拿弦切内场')
        self.assertEqual(route[0], route[-1], '路线闭合')

    def test_auto_lane_is_zero_once_the_points_form_a_track_loop(self):
        prof = zones.profile(self.off_tangent_points())
        self.assertEqual(prof['lane_m'], 0.0, '打卡点已在跑进线上, 不再自动外扩车道')

    def test_user_lane_override_survives_the_track_loop(self):
        prof = zones.profile(self.off_tangent_points(), {'lane_m': 4})
        self.assertEqual(prof['lane_m'], 4.0)

    def test_square_is_not_a_track(self):
        xy = [enu_of(p[0], p[1], *CENTER) for p in rect(80, 80)]
        self.assertIsNone(zones._track_loop(xy), '正方形不该被铺成带弯道的跑道环')


class AxisTests(unittest.TestCase):
    """跑区朝向: 已知跑区用实测长轴, 不认识的才回退到外接矩形/PCA。"""

    def test_a_known_zone_uses_the_measured_axis(self):
        # 1873 的四个打卡点摆成菱形, PCA 会给出 ~111.9°, 实测是 160.1°
        geom = zones.analyze(rect(120, 60, angle_deg=111.9), zone_id='1873')
        self.assertEqual(geom['long_axis_deg'], 160.1)
        self.assertEqual(geom['axis_source'], 'prior')

    def test_an_unknown_zone_falls_back_to_the_rectangle(self):
        # rect() 的 angle_deg 是数学角(逆时针), 罗盘方位 = -angle_deg mod 180
        geom = zones.analyze(rect(220, 140, angle_deg=-37.0), zone_id='9999')
        self.assertEqual(geom['axis_source'], 'rect')
        self.assertAlmostEqual(geom['long_axis_deg'], 37.0, delta=4.0)

    def test_profile_carries_the_zone_and_the_axis_source(self):
        prof = zones.profile(rect(), {'direction': 'ccw'}, zone_id='1875')
        self.assertEqual(prof['zone_id'], '1875')
        self.assertEqual(prof['axis_source'], 'prior')
        self.assertEqual(prof['long_axis_deg'], 16.9)
        self.assertEqual(prof['start_deg'], 16.9, '默认从长轴方向起步')

    def test_the_route_follows_the_axis_of_a_rotated_field(self):
        # 1873 实测 160.1°: 生成的环长轴也该在这条线上, 而不是 PCA 的对角线
        points = rect(220, 140, angle_deg=-160.1)   # 数学角 -160.1° = 方位 160.1°
        route = zones.route_points(points, lane_m=0, direction='ccw',
                                   rnd=random.Random(3), zone_id='1873')
        clat = sum(p[0] for p in route) / len(route)
        clng = sum(p[1] for p in route) / len(route)
        xy = [((p[1] - clng) * 111320 * math.cos(math.radians(clat)),
               (p[0] - clat) * 110570) for p in route]
        sxx = sum(x * x for x, _ in xy) / len(xy)
        syy = sum(y * y for _, y in xy) / len(xy)
        sxy = sum(x * y for x, y in xy) / len(xy)
        angle = 0.5 * math.atan2(2 * sxy, sxx - syy)
        bearing = (90.0 - math.degrees(angle)) % 180.0
        delta = abs(bearing - 160.1)
        self.assertLess(min(delta, 180.0 - delta), 4.0,
                        f'环的长轴 {bearing:.1f}° 应该贴着实测的 160.1°')


class RoundedLoopTests(unittest.TestCase):
    """菱形点位(边长接近)不能走半圆弯兜底: 那样两个打卡点会漏掉。"""

    def diamond(self):
        return [(0.0, 1.0), (0.7, 0.0), (0.0, -1.0), (-0.7, 0.0)] if False else [
            p for p in rect(120, 60, angle_deg=45.0)]

    def test_every_checkpoint_stays_near_the_route(self):
        points = self.diamond()
        route = zones.route_points(points, lane_m=0, direction='ccw',
                                   rnd=random.Random(5), zone_id='9999')
        clat = sum(p[0] for p in points) / len(points)
        clng = sum(p[1] for p in points) / len(points)
        rxy = [((p[1] - clng) * 111320 * math.cos(math.radians(clat)),
                (p[0] - clat) * 110570) for p in route]
        for point in points:
            px = (point[1] - clng) * 111320 * math.cos(math.radians(clat))
            py = (point[0] - clat) * 110570
            nearest = min(math.hypot(x - px, y - py) for x, y in rxy)
            self.assertLess(nearest, 12.0, f'打卡点离环 {nearest:.1f}m, 太远')

    def test_the_loop_has_no_sharp_corners(self):
        route = zones.route_points(rect(200, 120, angle_deg=20.0), lane_m=0,
                                   direction='ccw', rnd=random.Random(2),
                                   zone_id='9999')
        clat = sum(p[0] for p in route) / len(route)
        clng = sum(p[1] for p in route) / len(route)
        xy = [((p[1] - clng) * 111320 * math.cos(math.radians(clat)),
               (p[0] - clat) * 110570) for p in route]
        worst = 0.0
        for a, b, c in zip(xy, xy[1:], xy[2:]):
            v1 = (b[0] - a[0], b[1] - a[1])
            v2 = (c[0] - b[0], c[1] - b[1])
            l1 = math.hypot(*v1)
            l2 = math.hypot(*v2)
            if l1 < 0.3 or l2 < 0.3:
                continue
            cosang = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2)))
            worst = max(worst, math.degrees(math.acos(cosang)))
        self.assertLess(worst, 35.0, f'最大转角 {worst:.1f}°, 还是折线')


class RouteTests(unittest.TestCase):
    def test_route_is_closed_and_starts_near_a_corner(self):
        points = rect()
        route = zones.route_points(points, lane_m=4, direction='ccw', start_deg=90)
        self.assertEqual(route[0], route[-1])
        self.assertGreater(len(route), 4)
        nearest = min(math.hypot((p[1] - route[0][1]) * 111320 * math.cos(math.radians(CENTER[0])),
                                 (p[0] - route[0][0]) * 110570) for p in points)
        self.assertLess(nearest, 20.0, '起步点应该贴着一个打卡点')

    def test_direction_flips_the_route(self):
        points = rect()
        ccw = zones.route_points(points, lane_m=4, direction='ccw', start_deg=0)
        cw = zones.route_points(points, lane_m=4, direction='cw', start_deg=0)
        self.assertGreater(signed_area(ccw[:-1]), 0)
        self.assertLess(signed_area(cw[:-1]), 0)


class OvalRouteTests(unittest.TestCase):
    """四点椭圆场地的路线必须是田径场形: 直道+半圆弯, 而不是切弯的多边形。"""

    def enu(self, p):
        return ((p[1] - CENTER[1]) * 111320 * math.cos(math.radians(CENTER[0])),
                (p[0] - CENTER[0]) * 110570)

    def test_curves_bulge_out_to_the_hand_computed_apex(self):
        # rect(120, 60): 角点(±30, ±60), 弯道弦=60 → 半径30 → 顶点(0, ±90)
        route = zones.route_points(rect(), lane_m=0, direction='ccw',
                                   rnd=random.Random(1))
        for apex in ((0.0, 90.0), (0.0, -90.0)):
            nearest = min(math.hypot(x - apex[0], y - apex[1])
                          for x, y in map(self.enu, route))
            self.assertLess(nearest, 4.0,
                            f'弯道要外凸到 {apex}, 多边形切弯会差 30m')
        self.assertGreater(max(abs(y) for _x, y in map(self.enu, route)), 85.0)

    def test_every_checkpoint_stays_on_the_oval_route(self):
        corners = rect()
        route = zones.route_points(corners, lane_m=0, direction='ccw',
                                   rnd=random.Random(1))
        for corner in corners:
            cx, cy = self.enu(corner)
            nearest = min(math.hypot(x - cx, y - cy) for x, y in map(self.enu, route))
            self.assertLess(nearest, 4.0, '改弯形后仍要经过每个打卡点')

    def test_square_field_keeps_the_polygon_route(self):
        route = zones.route_points(rect(80, 80), lane_m=0, direction='ccw',
                                   rnd=random.Random(1))
        self.assertLess(max(abs(y) for _x, y in map(self.enu, route)), 45.0,
                        '正方形不是跑道, 不该鼓出半圆弯')

    def test_six_point_campus_keeps_the_polygon_route(self):
        campus = [geo_of(x, y, *CENTER) for x, y in
                  ((-160, -40), (-40, -55), (120, -30), (160, 20), (40, 50), (-120, 35))]
        route = zones.route_points(campus, lane_m=0, direction='ccw',
                                   rnd=random.Random(1))
        corner_r = max(math.hypot(x, y) for x, y in map(self.enu, campus))
        route_r = max(math.hypot(x, y) for x, y in map(self.enu, route))
        self.assertLess(route_r, corner_r + 8.0,
                        '跨校区点位环不是田径场, 保持多边形')


if __name__ == '__main__':
    unittest.main()
