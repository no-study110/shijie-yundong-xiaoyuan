# -*- coding: utf-8 -*-
"""规则配速口径与 `speed` 字段单位的回归测试。

同一个字段上先后踩了三个坑，全部以真实记录为准绳：

1. **换算式写错**：把「分/公里」换算成速度时写成 `pace / 60`，
   10 分/km 变成 0.167m/s，每一次正常跑步都被「峰值速度超限」拦死。
2. **字段语义读错**：把 speedTop/speedBottom 读成 m/s，于是把
   4080m/2180s（8.91 分/km，本来合规）当成「跑太快/太慢」在本地拒绝。
3. **报文单位填错**：`speed` 字段填了速度（4080/2180 → 1872 = 1.872m/s），
   而服务端按**配速**解读（1.872 分/km），于是判
   「未达标: 配速控制在2′00″-10′00″」。这才是 rrid=1350559546 的真因。

标尺是真实 App 的记录：服务端保存的 `speed` 与 `时长/(距离/1000)/60`
相差全部小于 0.005。
"""
import types
import unittest

from running.builder import pace_thousandths
from running.client import (
    _IMPLAUSIBLE_PACE_MAX_MIN,
    _IMPLAUSIBLE_PACE_MIN_MIN,
    _check_target_pace,
    _pace_ratio_bounds,
    _pace_to_speed_limit_mps,
    _parse_rule_number,
    _rule_pace_bounds,
    _rule_speed_bounds_mps,
    _speed_to_pace_min,
)

# 真实 App 提交的记录：(rrid, totalDis, totalTime, 服务端保存的 speed)
REAL_APP_RECORDS = [
    (1254181493, 4007, 2002, 8.33),
    (1281444278, 4576, 2143, 7.81),
    (1280042147, 3104, 983, 5.28),
    (1275073478, 4201, 1797, 7.13),
    (1269372753, 4012, 1704, 7.08),
    (1252194677, 4355, 1161, 4.44),
    (1274352959, 4027, 1504, 6.22),
    (1283755834, 1000, 495, 8.24),
]


def _rule(speed_top=None, speed_bottom=None):
    return types.SimpleNamespace(speed_top=speed_top, speed_bottom=speed_bottom)


class RuleNumberParsingTests(unittest.TestCase):
    """规则字段可能给数字，也可能给带单位的字符串。"""

    def test_accepts_plain_numbers(self):
        self.assertEqual(_parse_rule_number(2.0), 2.0)
        self.assertEqual(_parse_rule_number("10"), 10.0)

    def test_accepts_number_with_unit_suffix(self):
        # 后缀只被丢弃，不做任何换算 —— 换算属于调用方的职责
        self.assertEqual(_parse_rule_number("2.0分/km"), 2.0)
        self.assertEqual(_parse_rule_number("12.5分/km"), 12.5)

    def test_placeholder_values_mean_unconstrained(self):
        for raw in (None, "", "-", "不限", "无", "0"):
            with self.subTest(raw=raw):
                self.assertIsNone(_parse_rule_number(raw))

    def test_bool_is_not_a_number(self):
        # True 在 Python 里等于 1，会被当成配速 1.0，必须排除
        self.assertIsNone(_parse_rule_number(True))
        self.assertIsNone(_parse_rule_number(False))


class PayloadSpeedFieldTests(unittest.TestCase):
    """`speed` 字段是配速（分/公里）×1000 —— 报文侧最容易错的地方。"""

    def test_matches_real_app_records(self):
        for rrid, dist, secs, server_speed in REAL_APP_RECORDS:
            with self.subTest(rrid=rrid):
                ours = pace_thousandths(dist, secs) / 1000.0
                self.assertAlmostEqual(ours, server_speed, delta=0.015)

    def test_mps_interpretation_is_ruled_out(self):
        # 若按速度填，4007m/2002s 会填 2001，而服务端存的是 8330
        dist, secs, server_speed = REAL_APP_RECORDS[0][1:]
        self.assertAlmostEqual(dist / secs, 2.0015, places=3)
        self.assertGreater(abs(dist / secs - server_speed), 6.0)

    def test_rejected_run_value(self):
        # 我们这次实际报出去的是 1872（= 1.872m/s）；正确值应是 8905
        self.assertEqual(pace_thousandths(4080, 2180), 8905)
        self.assertEqual(round(4080 / 2180 * 1000), 1872)

    def test_handles_zero_inputs(self):
        self.assertEqual(pace_thousandths(0, 2180), 0)
        self.assertEqual(pace_thousandths(4080, 0), 0)

    def test_payload_carries_pace_not_speed(self):
        """端到端锁一次：真实组装的报文里 speed 必须是配速量级。"""
        from running.builder import RunningTrackBuilder

        builder = RunningTrackBuilder(1, 2, 1, uuid_str='pace-field-test')
        points, fixed = builder.generate_track_around_points(
            30, 120, None, 2150, 760, 1700000000000)
        body, _, _ = builder.build_submission_payload(
            2150, 760, 2900, 1700000000000, 1700000000000 + 760 * 1000, points, fixed)

        pace_value = body["speed"] / 1000.0
        self.assertAlmostEqual(pace_value, 760 / 2.15 / 60, delta=0.005)
        # 落在学校规则 2~10 分/km 内；若误填 m/s（2.83）同样在区间内，
        # 所以再看一眼绝不可能相等的那个值
        self.assertNotAlmostEqual(pace_value, 2150 / 760, delta=0.5)


class SpeedPaceConversionTests(unittest.TestCase):
    """速度与配速的换算：用可独立验证的物理锚点对表。"""

    def test_real_world_reference_points(self):
        cases = {
            2.0: 8.3333,    # 2.0 m/s = 7.2 km/h = 8'20"/km
            1.872: 8.9031,  # 本次被拒的那条：4080m / 2180s
            2.778: 5.9999,  # 2000m / 720s
            3.333: 5.0005,  # 3.333 m/s = 12 km/h = 5'00"/km
            5.0: 3.3333,
        }
        for speed, pace in cases.items():
            with self.subTest(speed=speed):
                self.assertAlmostEqual(_speed_to_pace_min(speed), pace, places=3)

    def test_ten_min_per_km_is_six_kmh(self):
        # 最容易记错的锚点：10 分/km 是 1.667m/s，不是 0.167m/s
        self.assertAlmostEqual(_speed_to_pace_min(1.6666667), 10.0, places=5)

    def test_rejects_zero_and_negative(self):
        self.assertIsNone(_speed_to_pace_min(0))
        self.assertIsNone(_speed_to_pace_min(-1))
        self.assertIsNone(_speed_to_pace_min(None))


class RulePaceBoundsTests(unittest.TestCase):
    """规则区间：数值就是配速，按 min/max 取，不依赖字段名的字面含义。"""

    def test_reads_fields_as_pace(self):
        # 实测样本：speedTop=2.0 / speedBottom=10.0 → 配速 2′00″~10′00″
        slow, fast = _rule_pace_bounds(_rule("2", "10"))
        self.assertAlmostEqual(slow, 10.0)
        self.assertAlmostEqual(fast, 2.0)

    def test_field_order_does_not_matter(self):
        # 两个字段的字面名和大小关系是反的，取数值 min/max 才稳
        self.assertEqual(_rule_pace_bounds(_rule("10", "2")),
                         _rule_pace_bounds(_rule("2", "10")))

    def test_single_end(self):
        self.assertEqual(_rule_pace_bounds(_rule("2.5", None)), (2.5, 2.5))
        self.assertEqual(_rule_pace_bounds(_rule(None, "3.0")), (3.0, 3.0))

    def test_unconstrained_returns_nones(self):
        self.assertEqual(_rule_pace_bounds(_rule("-", "-")), (None, None))
        self.assertEqual(_rule_pace_bounds(types.SimpleNamespace()), (None, None))

    def test_absurd_pace_is_skipped_with_log(self):
        # 30 分/km 比散步还慢，只可能来自解析错误
        logs = []
        slow, _ = _rule_pace_bounds(_rule("2", "30"), logs.append)
        self.assertIsNone(slow)
        self.assertTrue(any("不设配速下限" in line for line in logs))

    def test_sentinels_are_documented(self):
        self.assertEqual(_IMPLAUSIBLE_PACE_MAX_MIN, 20.0)
        self.assertEqual(_IMPLAUSIBLE_PACE_MIN_MIN, 1.4)

    def test_converts_to_speed_bounds(self):
        # 2~10 分/km → 1.667~8.333 m/s
        floor, ceil = _rule_speed_bounds_mps(_rule("2", "10"))
        self.assertAlmostEqual(floor, 1000 / 600, places=6)
        self.assertAlmostEqual(ceil, 1000 / 120, places=6)

    def test_peak_limit_uses_faster_end(self):
        # 峰值上限由最快的那一端（2 分/km = 8.33m/s）决定
        self.assertAlmostEqual(_pace_to_speed_limit_mps(_rule("2", "10")), 1000 / 120, places=6)


class HistoricalVerdictTests(unittest.TestCase):
    """用真实账号的判定结果当标尺：这些数字是服务端给出的结论，不是推测。"""

    def test_compliant_run_is_not_rejected(self):
        # 4080m/2180s = 8.91 分/km，在 2′00″-10′00″ 内 —— 上一版正是在这里误拒
        floor, ceil = _rule_speed_bounds_mps(_rule("2", "10"))
        self.assertIsNone(_check_target_pace(4080 / 2180, floor, ceil, 4080, lambda _: None))

    def test_previous_failure_would_pass_on_pace(self):
        # 4000m/2148s = 8.95 分/km 同样合规
        floor, ceil = _rule_speed_bounds_mps(_rule("2", "10"))
        self.assertIsNone(_check_target_pace(4000 / 2148, floor, ceil, 4000, lambda _: None))

    def test_too_slow_is_rejected_with_window(self):
        # 4000m 用 3000s = 12.5 分/km，慢于 10′00″
        floor, ceil = _rule_speed_bounds_mps(_rule("2", "10"))
        logs = []
        result = _check_target_pace(4000 / 3000, floor, ceil, 4000, logs.append)
        self.assertIsNotNone(result)
        self.assertIn("太慢", result.message)
        # 提示里的配速必须与 App 记录页同款（分′秒″），不再用 2.00 分/km 这种小数
        self.assertIn("2′00″~10′00″/km", result.message)
        self.assertIn("12′30″/km", result.message)
        # 4000m 要达标得跑进 2400 秒
        self.assertIn("2400", result.message)
        self.assertTrue(logs)

    def test_too_fast_is_rejected(self):
        # 4080m 用 400s = 1.63 分/km，快于 2′00″
        floor, ceil = _rule_speed_bounds_mps(_rule("2", "10"))
        result = _check_target_pace(4080 / 400, floor, ceil, 4080, lambda _: None)
        self.assertIsNotNone(result)
        self.assertIn("太快", result.message)


class PaceRatioBoundsTests(unittest.TestCase):
    """速度区间换算成分段曲线的相对倍数。"""

    def test_bounds_leave_safety_margin(self):
        # 平均速度远高于下界时，按 2% 余量精确换算
        bounds = _pace_ratio_bounds(4.0, 1000 / 600, 1000 / 120)
        self.assertAlmostEqual(bounds["min_ratio"], (1000 / 600) * 1.02 / 4.0, places=6)
        self.assertAlmostEqual(bounds["max_ratio"], (1000 / 120) * 0.98 / 4.0, places=6)

    def test_lower_bound_is_capped_to_keep_curve_alive(self):
        # 贴着规则下限跑时，严格换算会得到 ≥1.0 的倍数把曲线压成匀速跑；
        # 上限截到 0.95，留一点起伏
        bounds = _pace_ratio_bounds(1.72, 1000 / 600, 1000 / 120)
        self.assertEqual(bounds["min_ratio"], 0.95)
        self.assertGreater(bounds["max_ratio"], 1.0)

    def test_ceiling_never_below_one(self):
        # 上界比平均值还低属于「目标本身超速」，由预检负责拒绝，
        # 这里不能给出 <1 的 max_ratio，否则曲线会反向
        bounds = _pace_ratio_bounds(15.0, 1000 / 600, 1000 / 120)
        self.assertEqual(bounds["max_ratio"], 1.0)

    def test_zero_average_returns_empty(self):
        self.assertEqual(_pace_ratio_bounds(0, 1.667, 8.333), {})

    def test_missing_bounds_are_omitted(self):
        self.assertEqual(list(_pace_ratio_bounds(2.0, None, None).keys()), [])
        self.assertEqual(list(_pace_ratio_bounds(2.0, None, 8.333).keys()), ["max_ratio"])


class SpeedRangeTextTests(unittest.TestCase):
    """CLI / 网页展示的区间文案。"""

    def _rule(self, top, bottom):
        from policy.models import RunRule
        return RunRule.from_api_data(
            {"runRuleModel": {"speedTop": top, "speedBottom": bottom}})

    def test_formats_pace_with_speed_hint(self):
        self.assertEqual(self._rule("2.0", "10.0").speed_range_text(),
                         "2′00″~10′00″/km（速度 1.67~8.33 m/s）")

    def test_dash_means_unconstrained(self):
        self.assertEqual(self._rule("-", "-").speed_range_text(), "不限")

    def test_single_value(self):
        self.assertEqual(self._rule("2.0", "-").speed_range_text(),
                         "固定 2′00″/km（速度 8.33 m/s）")


if __name__ == "__main__":
    unittest.main()
