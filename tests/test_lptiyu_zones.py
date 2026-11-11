"""跑区几何: 朝向、长宽、环向、起步方位与选点特调。

这些参数决定「每块场地怎么跑」, 所以逐条钉死: 朝向算错 90°, 轨迹就会横穿球场。
"""
import math
import unittest

from lptiyu import zones
from lptiyu.tracks import geo_of

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

    def test_auto_direction_uses_the_winding(self):
        self.assertEqual(zones.profile(rect(order='cw'))['direction'], 'cw')
        self.assertEqual(zones.profile(rect(order='ccw'))['direction'], 'ccw')

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


if __name__ == '__main__':
    unittest.main()
