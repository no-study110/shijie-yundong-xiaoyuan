# -*- coding: utf-8 -*-
"""离线回归测试:围栏/打卡点合规校验、围栏内轨迹规划、运动学分桶一致性。"""
import unittest
from types import SimpleNamespace

from running.builder import RunningTrackBuilder
from running.client import _face_check_gate
from running.compliance import ComplianceValidator
from running.geometry import (
    haversine,
    inset_polygon,
    point_in_polygon,
    polygon_area_m2,
)
from running.kinematics import validate_kinematics
from running.models import GpsPoint
from running.planner import FenceRoutePlanner


def square_fence(lat=30.0, lng=120.0, half=0.0009):
    """构造一个约 200m x 200m 的正方形围栏。"""
    return [
        (lat - half, lng - half),
        (lat - half, lng + half),
        (lat + half, lng + half),
        (lat + half, lng - half),
    ]


def inner_checkpoints():
    """围栏内部的一圈打卡点。"""
    return [
        {"lat": 30.0 + 0.0006, "lng": 120.0 - 0.0006, "name": "CP1"},
        {"lat": 30.0 + 0.0006, "lng": 120.0 + 0.0006, "name": "CP2"},
        {"lat": 30.0 - 0.0006, "lng": 120.0 + 0.0006, "name": "CP3"},
        {"lat": 30.0 - 0.0006, "lng": 120.0 - 0.0006, "name": "CP4"},
    ]


class GeometryTests(unittest.TestCase):
    def test_point_in_polygon_interior_and_exterior(self):
        fence = square_fence()
        self.assertTrue(point_in_polygon((30.0, 120.0), fence))
        self.assertFalse(point_in_polygon((30.05, 120.0), fence))
        # 边界点算作内部
        self.assertTrue(point_in_polygon((30.0 + 0.0009, 120.0), fence))

    def test_polygon_area_is_plausible(self):
        area = polygon_area_m2(square_fence())
        # 0.0009 度纬度 ≈ 100m，围栏边长约 200m，面积约 40000 m²
        self.assertTrue(30000 < area < 50000, area)

    def test_inset_polygon_stays_inside_and_shrinks(self):
        fence = square_fence()
        inset = inset_polygon(fence, 10.0)
        for point in inset:
            self.assertTrue(point_in_polygon(point, fence), point)
        self.assertLess(polygon_area_m2(inset), polygon_area_m2(fence))

    def test_invalid_coordinates_rejected(self):
        for point in [(float('nan'), 120.0), (30.0, 181.0), (30.0, float('inf'))]:
            with self.subTest(point=point), self.assertRaises(ValueError):
                point_in_polygon(point, square_fence())


class PlannerTests(unittest.TestCase):
    def test_route_stays_inside_fence(self):
        planner = FenceRoutePlanner(boundary_margin_m=8.0)
        fence = square_fence()
        plan = planner.plan(fence, inner_checkpoints(), start_point=(30.0, 120.0))
        self.assertGreater(len(plan.waypoints), 2)
        for point in plan.waypoints:
            self.assertTrue(point_in_polygon(point, fence), point)
        for a, b in zip(plan.waypoints, plan.waypoints[1:]):
            # 相邻途经点连线也应在围栏内（凸围栏）
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            self.assertTrue(point_in_polygon(mid, fence), (a, b))

    def test_all_checkpoints_are_kept_in_route(self):
        planner = FenceRoutePlanner()
        plan = planner.plan(square_fence(), inner_checkpoints(), start_point=(30.0, 120.0))
        for cp in inner_checkpoints():
            self.assertTrue(any(haversine((cp["lat"], cp["lng"]), wp) <= 1.0
                                for wp in plan.waypoints), cp)

    def test_boundary_lap_extends_route_length(self):
        planner = FenceRoutePlanner(boundary_margin_m=5.0)
        fence = square_fence()
        plain = planner.plan(fence, inner_checkpoints(), start_point=(30.0, 120.0))
        lap = planner.plan(fence, inner_checkpoints(), start_point=(30.0, 120.0),
                           boundary_lap=True)
        self.assertGreater(lap.route_length_m, plain.route_length_m)
        for point in lap.waypoints:
            self.assertTrue(point_in_polygon(point, fence), point)

    def test_out_of_fence_start_falls_back_to_centroid(self):
        planner = FenceRoutePlanner()
        plan = planner.plan(square_fence(), [], start_point=(30.05, 120.05))
        self.assertTrue(any("起跑点不在围栏内" in w for w in plan.warnings))
        self.assertTrue(point_in_polygon(plan.waypoints[0], square_fence()))

    def test_degenerate_polygon_rejected(self):
        with self.assertRaises(ValueError):
            FenceRoutePlanner().plan([(30.0, 120.0), (30.001, 120.001)], [])


class FenceBuilderTests(unittest.TestCase):
    """builder 在传入围栏多边形时的行为。"""

    def build(self, checkpoints=None, meters=2150, seconds=760, fence=None):
        builder = RunningTrackBuilder(1, 2, 1, uuid_str='fence-builder-test')
        points, fixed = builder.generate_track_around_points(
            30.0, 120.0, checkpoints, meters, seconds, 1700000000000,
            fence_polygon=fence)
        return points, fixed, builder

    def test_track_stays_inside_fence(self):
        fence = square_fence()
        points, _, _ = self.build(checkpoints=inner_checkpoints(), fence=fence)
        for point in points:
            self.assertTrue(point_in_polygon((point.lat, point.lon), fence), point)

    def test_all_checkpoints_are_visited_inside_fence(self):
        fence = square_fence()
        checkpoints = inner_checkpoints()
        points, fixed, _ = self.build(checkpoints=checkpoints, fence=fence)
        for cp in checkpoints:
            self.assertTrue(
                any(haversine((cp["lat"], cp["lng"]), (p.lat, p.lon)) <= 0.5 for p in points), cp)
        # 打卡点官方坐标未被篡改
        for cp, fp in zip(checkpoints, fixed):
            self.assertAlmostEqual(cp["lat"], fp.lat, places=9)
            self.assertAlmostEqual(cp["lng"], fp.lon, places=9)

    def test_fence_plan_is_exposed_for_diagnostics(self):
        fence = square_fence()
        _, _, builder = self.build(checkpoints=inner_checkpoints(), fence=fence)
        plan = getattr(builder, "last_plan", None)
        self.assertIsNotNone(plan)
        self.assertGreater(plan.route_length_m, 0)
        for point in plan.waypoints:
            self.assertTrue(point_in_polygon(point, fence), point)

    def test_route_longer_than_target_is_rejected(self):
        # 围栏很小但目标里程很短：环线装不进里程 -> 必须显式失败而不是静默漏点
        tiny = [(30.0, 120.0), (30.0002, 120.0), (30.0002, 120.0002), (30.0, 120.0002)]
        with self.assertRaises(ValueError):
            self.build(checkpoints=inner_checkpoints(), meters=30, fence=tiny)


class ComplianceTests(unittest.TestCase):
    def build(self, mode=1, checkpoints=None, seconds=760, meters=2150, fence=True):
        builder = RunningTrackBuilder(1, 2, mode, uuid_str='compliance-test')
        points, fixed = builder.generate_track_around_points(
            30.0, 120.0, checkpoints, meters, seconds, 1700000000000)
        return points, fixed

    def test_compliant_track_passes_validation(self):
        fence = square_fence()
        checkpoints = inner_checkpoints()
        points, _ = self.build(checkpoints=checkpoints)
        validator = ComplianceValidator(checkpoint_tolerance_m=15.0)
        report = validator.validate(points, fence, checkpoints,
                                    steps=2900, total_distance_m=2150, total_time_sec=760)
        self.assertTrue(report.compliant, report.issues)
        self.assertTrue(report.inside_fence)
        self.assertEqual(report.checkpoint_hit_count, 4)
        self.assertEqual(report.checkpoint_total, 4)

    def test_missing_checkpoint_is_reported(self):
        fence = square_fence()
        checkpoints = inner_checkpoints()
        points, _ = self.build(checkpoints=checkpoints)
        # 追加一个根本不在轨迹上的打卡点
        far = checkpoints + [{"lat": 30.05, "lng": 120.05, "name": "FAR"}]
        validator = ComplianceValidator(checkpoint_tolerance_m=15.0)
        report = validator.validate(points, fence, far)
        self.assertFalse(report.compliant)
        self.assertTrue(any("未经过必经打卡点" in i for i in report.issues))

    def test_out_of_fence_point_is_reported(self):
        fence = square_fence()
        points = [GpsPoint(lat=30.05, lon=120.05, time=1700000000000),
                  GpsPoint(lat=30.05, lon=120.05, time=1700000010000)]
        validator = ComplianceValidator()
        report = validator.validate(points, fence)
        self.assertFalse(report.compliant)
        self.assertFalse(report.inside_fence)

    def test_distance_and_time_drift_is_reported(self):
        points, _ = self.build(checkpoints=inner_checkpoints())
        validator = ComplianceValidator()
        report = validator.validate(points, square_fence(), inner_checkpoints(),
                                    total_distance_m=9999, total_time_sec=760)
        self.assertFalse(report.compliant)
        self.assertTrue(any("totalDis" in i for i in report.issues))

    def test_too_short_track_is_rejected(self):
        validator = ComplianceValidator()
        report = validator.validate([], square_fence())
        self.assertFalse(report.compliant)


class KinematicsTests(unittest.TestCase):
    def build_payload(self, meters=2150, seconds=760, steps=2900, jitter=True):
        builder = RunningTrackBuilder(1, 2, 1, uuid_str='kinematics-test')
        points, fixed = builder.generate_track_around_points(
            30.0, 120.0, inner_checkpoints(), meters, seconds, 1700000000000)
        body, obs, _ = builder.build_submission_payload(
            meters, seconds, steps, 1700000000000, 1700000000000 + seconds * 1000,
            points, fixed, jitter_buckets=jitter)
        return body, points

    def test_consistent_payload_passes(self):
        body, points = self.build_payload()
        report = validate_kinematics(body, points)
        self.assertTrue(report.passed, report.summary())
        self.assertEqual(report.stats["avg_step_freq"], body["avgStepFreq"])

    def test_bucket_sum_mismatch_is_caught(self):
        body, points = self.build_payload()
        body["stepsPerTenSec"][0]["stepsNum"] += 17
        report = validate_kinematics(body, points)
        self.assertFalse(report.passed)
        self.assertTrue(any(i.field == "steps" for i in report.issues))

    def test_avg_step_freq_mismatch_is_caught(self):
        body, points = self.build_payload()
        body["avgStepFreq"] = 999
        report = validate_kinematics(body, points)
        self.assertFalse(report.passed)
        self.assertTrue(any(i.field == "avgStepFreq" for i in report.issues))

    def test_speed_unit_mismatch_is_caught(self):
        body, points = self.build_payload()
        body["speed"] = 1234
        report = validate_kinematics(body, points)
        self.assertFalse(report.passed)
        self.assertTrue(any(i.field == "speed" for i in report.issues))

    def test_timestamp_drift_is_caught(self):
        body, points = self.build_payload()
        body["stopTime"] = body["stopTime"] - 1000
        report = validate_kinematics(body, points)
        self.assertFalse(report.passed)
        self.assertTrue(any(i.field == "timestamps" for i in report.issues))

    def test_gps_distance_mismatch_is_caught(self):
        body, points = self.build_payload()
        body["totalDis"] = 3000
        report = validate_kinematics(body, points)
        self.assertFalse(report.passed)
        self.assertTrue(any(i.field == "allLocJson" for i in report.issues))

    def test_jitter_keeps_bucket_totals_consistent(self):
        body, points = self.build_payload(jitter=True)
        report = validate_kinematics(body, points)
        self.assertTrue(report.passed, report.summary())
        self.assertEqual(sum(r["stepsNum"] for r in body["stepsPerTenSec"]), 2900)


class FaceCheckGateTest(unittest.TestCase):
    """人脸抽检闸门必须是三态，而不是一律按「关闭」处理。

    「确认关闭」和「规则里没返回配置」在行为上都是放行，但在事实层面完全不同：
    后者意味着我们并不知道学校开没开。两者在日志上必须可区分，
    否则事后无法判断这次提交是「验过了」还是「压根没看」。
    """

    @staticmethod
    def _rule(face_config=None):
        rule = SimpleNamespace()
        if face_config is not None:
            rule.face_check = face_config
        return rule

    @staticmethod
    def _face(**kwargs):
        return SimpleNamespace(**kwargs)

    def test_confirmed_off_is_allowed_and_logged(self):
        rule = self._rule(self._face(enable=False, check_point_min=1, check_point_max=2))
        logs = []
        self.assertIsNone(_face_check_gate(rule, logs.append))
        self.assertTrue(any("未开启" in line for line in logs), logs)

    def test_enabled_is_rejected(self):
        rule = self._rule(self._face(enable=True, check_point_min=1, check_point_max=2))
        logs = []
        reason = _face_check_gate(rule, logs.append)
        self.assertIsNotNone(reason)
        self.assertIn("人脸", reason)
        self.assertTrue(any("失败" in line for line in logs), logs)

    def test_missing_config_is_logged_not_silent(self):
        rule = self._rule()
        logs = []
        self.assertIsNone(_face_check_gate(rule, logs.append))
        self.assertTrue(any("未返回人脸抽检配置" in line for line in logs), logs)

    def test_missing_config_is_distinguishable_from_confirmed_off(self):
        off_logs, missing_logs = [], []
        _face_check_gate(self._rule(self._face(enable=False)), off_logs.append)
        _face_check_gate(self._rule(), missing_logs.append)
        self.assertNotEqual(off_logs, missing_logs, "两种放行必须留下不同痕迹")

    def test_string_false_is_not_treated_as_on(self):
        # bool("false") == True，直接转型会把「关闭」误判成「开启」
        rule = self._rule(self._face(enable="false"))
        self.assertIsNone(_face_check_gate(rule, lambda _: None))

    def test_string_true_is_treated_as_on(self):
        rule = self._rule(self._face(enable="true"))
        self.assertIsNotNone(_face_check_gate(rule, lambda _: None))


if __name__ == '__main__':
    unittest.main()
