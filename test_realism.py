# -*- coding: utf-8 -*-
"""拟真增强与设备标识的单元测试。

对应「风控对抗」四项改动：地形爬升、定位精度惯性、历史画像、Android ID 派生。
这些改动的共同风险是**看起来更真但破坏既有约束**，所以每条都要有用例钉住：
爬升不能压垮心率上界，accuracy 不能逐点跳变，画像样本不足时不能硬改时长。
"""
import random
import unittest
from types import SimpleNamespace

from running.builder import RunningTrackBuilder
from running.client import RunningClient
from running.realism import (
    MIN_HISTORY_SAMPLES,
    personal_style,
    physiological_profile,
    terrain_climb,
)
from telemetry.client import TelemetryClient, _derive_android_id


class TerrainClimbTest(unittest.TestCase):
    """累计爬升必须随里程增长，且配速快的人偏向平路。"""

    def test_ascent_grows_with_distance(self):
        previous = 0
        for distance in (2000, 3000, 5000, 8000, 10000):
            ascent, _ = terrain_climb(distance, 8.6, random.Random(7))
            self.assertGreater(ascent, previous, f"{distance}m 的爬升未超过更短距离")
            previous = ascent

    def test_descent_close_to_ascent(self):
        for seed in range(20):
            ascent, descent = terrain_climb(5000, 8.6, random.Random(seed))
            self.assertLessEqual(abs(descent - ascent), 2, "终点未回起点，落差不应超过 2m")

    def test_fast_pace_prefers_flatter_ground(self):
        slow = sum(terrain_climb(5000, 8.0, random.Random(s))[0] for s in range(50)) / 50
        fast = sum(terrain_climb(5000, 12.5, random.Random(s))[0] for s in range(50)) / 50
        self.assertLess(fast, slow, "配速快时不应比慢跑爬得更多")

    def test_never_zero(self):
        for distance in (10, 100, 400):
            ascent, descent = terrain_climb(distance, 8.6, random.Random(1))
            self.assertGreaterEqual(ascent, 1)
            self.assertGreaterEqual(descent, 1)


class PhysiologicalProfileTest(unittest.TestCase):
    """爬升会抬心率，但平均值不能顶到最大值。"""

    def test_avg_hr_below_max(self):
        for seed in range(200):
            phys = physiological_profile(3000, 1200, random.Random(seed))
            self.assertLess(phys["avgHeartRate"], phys["maxHeartRate"],
                            "平均心率不能等于或高于最大心率")
            self.assertGreaterEqual(phys["maxHeartRate"], phys["avgHeartRate"] + 6)

    def test_ascent_matches_terrain(self):
        rng = random.Random(3)
        phys = physiological_profile(10000, 3600, rng)
        self.assertGreater(phys["totalAscent"], 30, "10km 的爬升不该还是个位数")

    def test_short_and_long_are_distinguishable(self):
        short = physiological_profile(2000, 800, random.Random(11))["totalAscent"]
        long = physiological_profile(10000, 4000, random.Random(11))["totalAscent"]
        self.assertGreater(long, short * 2, "长距离与短距离的爬升应明显不同")


class PersonalStyleTest(unittest.TestCase):
    """历史画像：样本不足不介入，介入时不越界。"""

    @staticmethod
    def _record(distance, duration, started=None):
        row = {"totalDis": distance, "totalTime": duration}
        if started is not None:
            row["startTime"] = started
        return row

    def test_none_below_min_samples(self):
        records = [self._record(2000, 800) for _ in range(MIN_HISTORY_SAMPLES - 1)]
        self.assertIsNone(personal_style(records))

    def test_none_for_empty(self):
        self.assertIsNone(personal_style([]))
        self.assertIsNone(personal_style(None))

    def test_skips_invalid_records(self):
        records = [self._record(0, 800), self._record(2000, 0), self._record("x", 1)]
        self.assertIsNone(personal_style(records))

    def test_statistics(self):
        records = [self._record(2000, 800), self._record(2000, 820), self._record(2000, 810)]
        style = personal_style(records)
        self.assertIsNotNone(style)
        self.assertEqual(style.sample_size, 3)
        self.assertAlmostEqual(style.avg_speed_mps, 2000 / 810, places=2)

    def test_sigma_floor_keeps_variation(self):
        records = [self._record(2000, 800) for _ in range(5)]
        style = personal_style(records)
        self.assertGreaterEqual(style.speed_sigma, 0.05, "历史完全一致时不能退化成恒定配速")

    def test_suggest_speed_within_two_sigma(self):
        records = [self._record(2000, 800), self._record(2100, 900), self._record(1900, 780)]
        style = personal_style(records)
        rng = random.Random(5)
        lower = style.avg_speed_mps - 2 * style.speed_sigma
        upper = style.avg_speed_mps + 2 * style.speed_sigma
        for _ in range(200):
            speed = style.suggest_speed(rng)
            self.assertGreaterEqual(speed, max(0.5, lower))
            self.assertLessEqual(speed, upper)

    def test_usual_hour_uses_timezone(self):
        # 00:30 UTC → 东八区 08:30。用 datetime 现算，不手写 epoch
        from datetime import datetime, timezone
        started = int(datetime(2026, 10, 30, 0, 30, tzinfo=timezone.utc).timestamp() * 1000)
        records = [self._record(2000, 800, started) for _ in range(3)]
        style = personal_style(records, timezone_offset_hours=8)
        self.assertEqual(style.usual_hour, 8)


class GpsAccuracyTest(unittest.TestCase):
    """定位精度：无 rng 时保底恒定，有 rng 时呈惯性波动且在合理区间。"""

    @staticmethod
    def _points(count=200):
        from running.models import GpsPoint
        return [GpsPoint(lat=30.0 + i * 1e-5, lon=120.0, time=1000 + i * 3000) for i in range(count)]

    def test_constant_without_rng(self):
        points = self._points()
        RunningTrackBuilder._fill_accuracy(points, None)
        self.assertEqual({p.accuracy for p in points}, {3.0})

    def test_varies_with_rng(self):
        points = self._points()
        RunningTrackBuilder._fill_accuracy(points, random.Random(1))
        self.assertGreater(len({p.accuracy for p in points}), 20, "精度不应是少数几个固定值")

    def test_within_bounds(self):
        points = self._points(500)
        RunningTrackBuilder._fill_accuracy(points, random.Random(2))
        values = [p.accuracy for p in points]
        self.assertGreaterEqual(min(values), 2.5)
        self.assertLessEqual(max(values), 32.0)

    def test_is_inertial_not_white_noise(self):
        points = self._points(800)
        RunningTrackBuilder._fill_accuracy(points, random.Random(3))
        values = [p.accuracy for p in points]
        mean_step = sum(abs(b - a) for a, b in zip(values, values[1:])) / (len(values) - 1)
        # 白噪声（2.5~32 均匀）相邻差均值约 9.8，惯性过程应远小于此
        self.assertLess(mean_step, 2.0, "相邻采样点的精度应平滑过渡，而不是独立跳变")

    def test_empty_is_safe(self):
        RunningTrackBuilder._fill_accuracy([], random.Random(1))


class AndroidIdTest(unittest.TestCase):
    """androidId 必须由 DeviceId 确定性派生，不能每次上报都变。"""

    def test_deterministic(self):
        self.assertEqual(_derive_android_id("abc"), _derive_android_id("abc"))

    def test_is_16_hex(self):
        value = _derive_android_id("0123456789abcdef0123456789abcdef")
        self.assertEqual(len(value), 16)
        int(value, 16)  # 非法十六进制会抛 ValueError

    def test_differs_by_device(self):
        self.assertNotEqual(_derive_android_id("device-a"), _derive_android_id("device-b"))

    def test_telemetry_uses_derived_values(self):
        client = TelemetryClient(device_config={"DeviceId": "f" * 32})
        self.assertEqual(client.android_id, _derive_android_id("f" * 32))
        self.assertEqual(client.android_mac, "02:00:00:00:00:00")


class MatchHistoryPaceTest(unittest.TestCase):
    """历史画像接入的三道闸门：拉取失败 / 样本不足 / 低于最低时长。"""

    @staticmethod
    def _client(records):
        client = object.__new__(RunningClient)

        class _Stub:
            def get_run_records(self, **kwargs):
                if isinstance(records, Exception):
                    raise records
                return {"recordList": records}

        client.history = _Stub()
        return client

    @staticmethod
    def _setup(minimum_time=600):
        return SimpleNamespace(minimum_time=minimum_time)

    @staticmethod
    def _records(speed_mps, count=5, distance=2000):
        return [{"totalDis": distance, "totalTime": int(distance / speed_mps)}
                for _ in range(count)]

    def test_returns_original_when_fetch_fails(self):
        client = self._client(RuntimeError("网络错误"))
        logs = []
        result = client._match_history_pace(2150, 760, self._setup(), logs.append)
        self.assertEqual(result, 760)
        self.assertTrue(any("跳过" in line for line in logs))

    def test_returns_original_when_samples_insufficient(self):
        client = self._client(self._records(2.4, count=2))
        result = client._match_history_pace(2150, 760, self._setup(), lambda _: None)
        self.assertEqual(result, 760)

    def test_returns_original_when_below_minimum_time(self):
        # 历史配速 3.5 m/s → 2150m 约 614s，学校最低 900s 时不得采纳
        client = self._client(self._records(3.5))
        result = client._match_history_pace(2150, 760, self._setup(900), lambda _: None)
        self.assertEqual(result, 760)

    def test_keeps_original_within_three_percent(self):
        # 历史 2.83 m/s → 2150m 约 760s，与原时长几乎一致
        client = self._client(self._records(2150 / 760))
        result = client._match_history_pace(2150, 760, self._setup(0), lambda _: None)
        self.assertEqual(result, 760)

    def test_result_is_stable_across_samples(self):
        """同一组输入多跑几次，结论必须完全一致。

        回归锚点：曾用**采样值**去比 3% 阈值，而 sigma 下限 0.05 在 2.8m/s
        上等于 ±2σ≈3.5%，比阈值还宽 —— 噪声自己就能把结果推出阈值，同一个
        760s 有约 5% 概率变成 784s。判「要不要改」必须用均速，采样值只能
        决定「改成多少」。
        """
        client = self._client(self._records(2150 / 760))
        results = {client._match_history_pace(2150, 760, self._setup(0), lambda _: None)
                   for _ in range(200)}
        self.assertEqual(results, {760})

    def test_adjusts_when_out_of_personal_range(self):
        # 历史配速 2.0 m/s → 2150m 约 1075s，与 760s 相差 41%
        client = self._client(self._records(2.0))
        logs = []
        result = client._match_history_pace(2150, 760, self._setup(0), logs.append)
        self.assertGreater(result, 900)
        self.assertLess(result, 1300)
        self.assertTrue(any("调整" in line for line in logs))

    def test_adjusted_values_stay_inside_personal_band(self):
        # 该改的时候要真改，但每次都要落在本人历史的 ±2σ 区间内（2.0±0.1 m/s
        # → 2150m 落在 1024~1132s），不能抖出性格圈。
        client = self._client(self._records(2.0))
        values = {client._match_history_pace(2150, 760, self._setup(0), lambda _: None)
                  for _ in range(200)}
        self.assertTrue(all(1020 <= v <= 1140 for v in values), sorted(values))


if __name__ == "__main__":
    unittest.main()
