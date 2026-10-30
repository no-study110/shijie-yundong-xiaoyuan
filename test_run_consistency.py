"""Offline regression tests for measured distance, timestamps and bucket totals."""
import base64
import copy
import gzip
import json
import math
import random
import socket
import tempfile
import types
import unittest
import warnings
from pathlib import Path
from unittest.mock import Mock, patch

from running.builder import (LENGTH_TOLERANCE_M, RunningTrackBuilder,
                             TrackLengthError, TrackLengthWarning)
from running.client import RunningClient
from running.consistency import distance, sample_route, time_buckets
from running.models import GpsPoint


def measured_length(points):
    # Independent spherical-law-of-cosines measurement, not the production helper.
    result = 0.0
    for first, last in zip(points, points[1:]):
        la1, la2 = math.radians(first.lat), math.radians(last.lat)
        delta = math.radians(last.lon-first.lon)
        cosine = math.sin(la1)*math.sin(la2) + math.cos(la1)*math.cos(la2)*math.cos(delta)
        result += 6371000*math.acos(max(-1.0, min(1.0, cosine)))
    return result


class RunConsistencyTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)
        # 账本隔离：共用真实账本会让用例通过磁盘状态互相影响
        self._state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._state_dir.cleanup)
        ledger_patch = patch('core.ledger.DEFAULT_PATH',
                             Path(self._state_dir.name) / 'run_ledger.json')
        ledger_patch.start()
        self.addCleanup(ledger_patch.stop)

    def build(self, seconds=760, meters=2150, steps=2900, mode=1, checkpoints=None):
        builder = RunningTrackBuilder(1, 2, mode, uuid_str='offline-consistency-test')
        points, fixed = builder.generate_track_around_points(
            30, 120, checkpoints, meters, seconds, 1700000000000)
        body, obs, key = builder.build_submission_payload(
            meters, seconds, steps, 1700000000000, 1700000000000+seconds*1000,
            points, fixed)
        return builder, points, fixed, body, obs

    def test_reported_2150m_760s_2900step_case(self):
        _, points, fixed, body, obs = self.build()
        self.assertAlmostEqual(measured_length(points), 2150, delta=.1)
        self.assertEqual(len(fixed), 5)
        self.assertEqual(sum(row['stepsNum'] for row in body['stepsPerTenSec']), 2900)
        self.assertAlmostEqual(sum(row['distance'] for row in body['speedPerTenSec']), 2150)
        self.assertEqual(points[-1].time-points[0].time, 760000)
        self.assertTrue(all(a.time < b.time for a,b in zip(points,points[1:])))
        # OBS and settlement must carry the same data, including bucket remainders.
        for field, obs_field in [('allLocJson','run_data'), ('fivePointJson','fixed_point_json')]:
            self.assertEqual(body[field], gzip.decompress(base64.b64decode(obs[obs_field])).decode())
        for field, obs_field in [('stepsPerTenSec','step_freq_json'), ('speedPerTenSec','speed_json')]:
            decoded=json.loads(gzip.decompress(base64.b64decode(obs[obs_field])))
            self.assertEqual(body[field], decoded)

    def test_partial_last_bucket_is_contiguous_and_lossless(self):
        for seconds in (1, 7, 10, 11, 31, 761):
            with self.subTest(seconds=seconds):
                rows=list(time_buckets(seconds, 2903, 1000, 1000+seconds*1000, 2153))
                self.assertEqual(rows[0][0],1000)
                self.assertEqual(rows[-1][1],1000+seconds*1000)
                self.assertTrue(all(a[1]==b[0] for a,b in zip(rows,rows[1:])))
                self.assertTrue(all(0 < end-begin <= 10000 for begin,end,_,_ in rows))
                self.assertEqual(sum(row[2] for row in rows),2903)
                self.assertAlmostEqual(sum(row[3] for row in rows),2153)

    def test_real_checkpoint_coordinates_preserved_and_visited(self):
        targets=[{'lat':30.0+i*.0002,'lng':120.0+i*.0003,'name':str(i)} for i in range(5)]
        _,points,fixed,_,_=self.build(checkpoints=targets)
        self.assertEqual([(p.lat,p.lon) for p in fixed],[(p['lat'],p['lng']) for p in targets])
        for point in fixed:
            self.assertTrue(any(p.lat==point.lat and p.lon==point.lon for p in points))
        self.assertAlmostEqual(measured_length(points),2150,delta=.1)

    def test_zero_checkpoint_coordinate_is_not_replaced_by_center(self):
        targets=[{'lat':0.0,'lng':120.0}, {'lat':.0005,'lng':120.0005}]
        _,points,fixed,_,_=self.build(checkpoints=targets)
        self.assertEqual(fixed[0].lat,0.0)
        self.assertEqual(points[0].lat,0.0)
        self.assertAlmostEqual(measured_length(points),2150,delta=.1)

    def test_route_too_short_for_supplied_points_is_rejected(self):
        targets=[{'lat':30.0,'lng':120.0},{'lat':31.0,'lng':121.0}]
        with self.assertRaisesRegex(ValueError,'too short'):
            self.build(checkpoints=targets)

    def test_scoring_run_zero_center_without_checkpoints_is_rejected(self):
        builder=RunningTrackBuilder(1,2,1,uuid_str='offline-zero-center-test')
        with self.assertRaisesRegex(ValueError,'无有效中心坐标'):
            builder.generate_track_around_points(0.0,0.0,None,2150,760,1700000000000)

    def test_partial_duration_and_shorter_template_repeated_laps(self):
        _,points,_,body,_=self.build(seconds=761,meters=5037,steps=6103)
        self.assertAlmostEqual(measured_length(points),5037,delta=.1)
        self.assertEqual(sum(x['stepsNum'] for x in body['stepsPerTenSec']),6103)
        self.assertEqual(body['stepsPerTenSec'][-1]['endTime']-body['stepsPerTenSec'][-1]['beginTime'],1000)

    def test_short_free_run_does_not_require_full_template_loop(self):
        _,points,fixed,_,_=self.build(meters=100,seconds=45,steps=120,mode=6)
        self.assertEqual(fixed,[])
        self.assertAlmostEqual(measured_length(points),100,delta=.1)

    def test_invalid_input_rejected(self):
        for seconds,steps,meters in [(0,1,100),(-1,1,100),(760,-1,2150),(760,1,float('nan'))]:
            with self.subTest(seconds=seconds,steps=steps,meters=meters),self.assertRaises(ValueError):
                self.build(seconds,meters,steps)

    def test_inconsistent_track_cannot_be_packaged(self):
        builder,points,fixed,_,_=self.build()
        for mutate in ('distance','timestamp','checkpoint'):
            bad=copy.deepcopy(points)
            if mutate=='distance': bad[1].lat += .01
            elif mutate=='timestamp': bad[-1].time-=1
            else: fixed=[types.SimpleNamespace(lat=31,lon=121)]
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):
                builder.build_submission_payload(2150,760,2900,1700000000000,
                                                 1700000760000,bad,fixed)

    def _base_client(self):
        client=RunningClient(uid=1,token='DUMMY',unid=2,device_config={'DeviceId':'1'*32})
        client.profile.get_server_time=Mock(return_value=types.SimpleNamespace(
            server_timestamp_ms=1700000000000,offset_seconds=0))
        client.telemetry.send_startup_pv=Mock()
        client.telemetry.send_page_exposure=Mock()
        client.policy.get_run_policy=Mock(return_value=types.SimpleNamespace(
            min_distance=2000,raw={'runRuleModel':{'minRunTime':300},'policy':1}))
        client.request_obs_upload_url=Mock()
        client.submit_record=Mock()
        return client

    def test_validation_failure_prevents_obs_and_settlement(self):
        # 轨迹生成抛错时，必须中断且不得触发 OBS 上传与结算
        client=self._base_client()
        fence=types.SimpleNamespace(
            polygon_points=[types.SimpleNamespace(lat=30.0,lng=120.0)],
            check_points=[types.SimpleNamespace(lat=30.0,lng=120.0,name='cp0')],
            fence_name='test',raw={})
        client.policy.get_geo_fence=Mock(return_value=[fence])
        with patch('running.client.RunningTrackBuilder.generate_track_around_points',side_effect=ValueError('mismatch')):
            # 本用例只验证「轨迹生成失败必须中断」，预检不在范围内，关掉避免跑真实加密链路
            result=client.execute_full_run(risk_check=False)
        self.assertFalse(result.success)
        self.assertIn('mismatch',result.message)
        client.request_obs_upload_url.assert_not_called()
        client.submit_record.assert_not_called()

    def test_empty_geofence_fails_fast_without_obs_or_settlement(self):
        # 围栏为空时应快速失败，而非用零坐标兜底生成必死数据
        client=self._base_client()
        client.policy.get_geo_fence=Mock(return_value=[])
        result=client.execute_full_run()
        self.assertFalse(result.success)
        self.assertIn('围栏',result.message)
        client.request_obs_upload_url.assert_not_called()
        client.submit_record.assert_not_called()


class LengthRestorationTests(unittest.TestCase):
    """_restore_length 必须报告修正结果，而不是超差时静默返回。

    此前超差直接 return，超差轨迹要等到下游 _assert_track 才炸，
    报错只说「长度与 totalDis 不一致」，看不出是「几何形态不对」
    还是「锚点锁死改不动」，排查成本很高。
    """

    @staticmethod
    def ring(radius_m, count=121):
        """铺一圈首尾重合的闭环采样点（生产轨迹都是闭环，首尾锚定才成立）。"""
        points = []
        for i in range(count):
            angle = 2*math.pi*i/(count-1)
            points.append(GpsPoint(
                lat=30.0 + radius_m*math.cos(angle)/111320.0,
                lon=120.0 + radius_m*math.sin(angle)/(111320.0*math.cos(math.radians(30))),
                time=1700000000000 + i*3000, speed=0.0))
        return points

    def test_aligned_track_reports_small_deviation(self):
        points = self.ring(48)          # 周长约 301m
        report = RunningTrackBuilder._restore_length(points, 301, [])
        self.assertTrue(report.aligned)
        self.assertAlmostEqual(report.final_m, 301, delta=LENGTH_TOLERANCE_M)
        self.assertLess(report.deviation_ratio, 0.01)

    def test_off_scale_track_is_rescaled_and_reported(self):
        points = self.ring(48)
        report = RunningTrackBuilder._restore_length(points, 360, [])
        self.assertTrue(report.aligned)
        self.assertTrue(report.adjusted)
        self.assertAlmostEqual(report.final_m, 360, delta=LENGTH_TOLERANCE_M)
        self.assertAlmostEqual(report.original_m, 301.2, delta=1.0)

    def test_unfixable_deviation_warns_instead_of_silent_return(self):
        points = self.ring(48)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            report = RunningTrackBuilder._restore_length(points, 1000, [])
        self.assertTrue(any(issubclass(w.category, TrackLengthWarning) for w in caught),
                        "超差必须发 TrackLengthWarning，不能静默返回")
        self.assertFalse(report.aligned)
        self.assertFalse(report.adjusted)
        self.assertAlmostEqual(report.final_m, report.original_m, delta=1e-6)
        self.assertGreater(report.deviation_ratio, 0.3)
        # 轨迹未被改动，交给上层判定
        self.assertAlmostEqual(measured_length(points), 301.2, delta=1.0)

    def test_strict_mode_raises_track_length_error(self):
        points = self.ring(48)
        with self.assertRaises(TrackLengthError) as ctx:
            RunningTrackBuilder._restore_length(points, 1000, [], strict=True)
        self.assertIn("偏离", str(ctx.exception))
        self.assertAlmostEqual(measured_length(points), 301.2, delta=1.0)

    def test_degenerate_input_returns_empty_report(self):
        for points, target in (([], 1000), ([GpsPoint(30, 120, 0)], 1000),
                               (self.ring(48), 0)):
            with self.subTest(points=len(points), target=target):
                report = RunningTrackBuilder._restore_length(points, target, [])
                self.assertFalse(report.adjusted)
                self.assertEqual(report.final_m, 0.0)

    def test_builder_exposes_report_after_track_generation(self):
        builder = RunningTrackBuilder(1, 2, 1, uuid_str='length-report-test')
        points, _ = builder.generate_track_around_points(30, 120, None, 2150, 760, 1700000000000)
        report = builder.last_length_report
        self.assertIsNotNone(report)
        self.assertTrue(report.aligned)
        self.assertAlmostEqual(report.final_m, measured_length(points), delta=0.5)

    def test_report_is_none_before_any_track(self):
        self.assertIsNone(RunningTrackBuilder(1, 2, 1).last_length_report)


class SampleRouteRealismTests(unittest.TestCase):
    """开启拟真（rng 非 None）后不能出现物理不可能的速度或时间挤压。

    这三个缺陷都是「噪声/抖动参数与采样密度不匹配」造成的，肉眼看不出来，
    只有量化才暴露：2150m 轨迹曾算出 311029 m/s 的瞬时速度，同一份报文里
    长度还被顶到 3256m。
    """

    ROUTE = [(30.0, 120.0), (30.0004, 120.0), (30.0004, 120.0004), (30.0, 120.0004)]

    def samples(self, seed):
        raw = sample_route(self.ROUTE, 2150, 760, 1700000000000,
                           require_all=False, rng=random.Random(seed))
        return [GpsPoint(lat=lat, lon=lon, time=ts, speed=0.0) for lat, lon, ts in raw]

    def test_sample_gaps_never_collapse(self):
        """时间抖动不能把相邻采样点挤成 1ms —— 顺序仍需严格递增。"""
        for seed in range(1, 11):
            samples = self.samples(seed)
            gaps = [b.time-a.time for a, b in zip(samples, samples[1:])]
            with self.subTest(seed=seed):
                self.assertGreater(min(gaps), 150, "相邻采样点间隔被抖动挤没了")
                self.assertTrue(all(g > 0 for g in gaps))

    def test_sample_length_tracks_target(self):
        for seed in range(1, 11):
            with self.subTest(seed=seed):
                self.assertAlmostEqual(measured_length(self.samples(seed)), 2150, delta=.1)

    def test_instantaneous_speed_stays_physical(self):
        for seed in range(1, 11):
            builder = RunningTrackBuilder(1, 2, 1, uuid_str=f'speed-{seed}',
                                          rng=random.Random(seed))
            points, _ = builder.generate_track_around_points(
                30, 120, None, 2150, 760, 1700000000000)
            speeds = [p.speed for p in points]
            with self.subTest(seed=seed, fastest=max(speeds)):
                # 慢跑峰值不该超过 8m/s；31 万 m/s 是时间差被压到 1ms 的产物
                self.assertGreaterEqual(min(speeds), 0.0)
                self.assertLess(max(speeds), 8.0)

    def test_fixed_points_land_on_track_without_absorption_drift(self):
        """官方打卡点必须精确落在轨迹上 —— 不能靠「大位移吸附」硬拽过去。"""
        targets = [{'lat': 30.0+i*.0003, 'lng': 120.0+i*.0002} for i in range(5)]
        for seed in range(1, 6):
            builder = RunningTrackBuilder(1, 2, 1, uuid_str=f'cp-{seed}',
                                          rng=random.Random(seed))
            points, fixed = builder.generate_track_around_points(
                30, 120, targets, 2150, 760, 1700000000000)
            for target in targets:
                nearest = min(distance((p.lat, p.lon), (target['lat'], target['lng']))
                              for p in points)
                with self.subTest(seed=seed, target=target):
                    self.assertLess(nearest, .01)
            self.assertAlmostEqual(measured_length(points), 2150, delta=2.15)


if __name__=='__main__':
    unittest.main()
