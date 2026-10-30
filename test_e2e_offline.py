# -*- coding: utf-8 -*-
"""离线端到端用例：一条完整成功的跑步提交链路，全程不发任何真实请求。

与 ``test_checkpoints`` 的分工：那边验证单点契约（请求体结构、错误分支），
这里验证**整条时序链路**与阶段顺序 ——

    时钟校准 → 冷启动埋点 → 围栏 → 官方点位 → 本地轨迹生成
    → 本地合规自检 → 运动学校验 → OBS 凭证 → OBS PUT
    → 结算上报 → 成绩回查 → 详情配置回刷 → 页面曝光

全部网络出口替换为桩，并用 ``simulate_physical_time=False`` 跳过物理等待，
因此单条用例在毫秒级完成，可安全放进每次回归。
"""
import base64
import gzip
import hashlib
import json
import math
import socket
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from policy.models import GeoFenceArea, GeoPoint
from running.client import RunningClient
from running.config import SPORT_STATIC_SALT

SERVER_NOW_MS = 1700000000000
DISTANCE_M = 2100
DURATION_S = 780


def official_points():
    """模拟服务端 pointsResModels：lat/lon 是 WGS-84，glat/glon 是 GCJ-02。"""
    return [{'lat': 30 + i * 0.0003, 'lon': 120 + i * 0.0002,
             'glat': 30 + i * 0.0003 - 0.006, 'glon': 120 + i * 0.0002 - 0.006,
             'pointName': f'Campus {i}', 'isFixed': 1} for i in range(5)]


def school_fence():
    return GeoFenceArea.from_api_data({
        'id': 3, 'name': '测试校区',
        'points': [{'lat': 30.0004, 'lon': 120.0004,
                    'glat': 29.9944, 'glon': 119.9944}]})


def measured_length(points):
    """与生产实现独立的球面余弦公式测距，避免用被测代码验被测代码。"""
    total = 0.0
    for first, last in zip(points, points[1:]):
        la1, la2 = math.radians(first['lat']), math.radians(last['lat'])
        delta = math.radians(last['lon'] - first['lon'])
        cosine = math.sin(la1) * math.sin(la2) + math.cos(la1) * math.cos(la2) * math.cos(delta)
        total += 6371000 * math.acos(max(-1.0, min(1.0, cosine)))
    return total


class OfflineEndToEndTests(unittest.TestCase):
    """完整链路：所有外部依赖为桩，断言顺序、副作用与报文自洽性。"""

    def setUp(self):
        # 任何 socket 连接都直接失败，确保用例真的没有触网
        guard = patch.object(socket.socket, 'connect',
                             side_effect=AssertionError('network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)

        # 跑后回查有 0.5s 固定等待，去掉以保持用例毫秒级
        sleeper = patch('running.client.time.sleep', return_value=None)
        sleeper.start()
        self.addCleanup(sleeper.stop)

        # 账本隔离：跑后落账会写盘，共用真实账本会让用例互相影响
        self._state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._state_dir.cleanup)
        ledger_patch = patch('core.ledger.DEFAULT_PATH',
                             Path(self._state_dir.name) / 'run_ledger.json')
        ledger_patch.start()
        self.addCleanup(ledger_patch.stop)

        self.order = []
        self.client = RunningClient(uid=1, token='DUMMY', unid=2,
                                    device_config={'DeviceId': '1' * 32})
        self._wire_success_path()

    # ------------------------------------------------------------- 桩装配
    def _record(self, name, value=None):
        def _side_effect(*args, **kwargs):
            self.order.append(name)
            return value() if callable(value) else value
        return Mock(side_effect=_side_effect)

    def _wire_success_path(self):
        client = self.client
        client.profile.get_server_time = self._record(
            'server_time',
            types.SimpleNamespace(server_timestamp_ms=SERVER_NOW_MS, offset_seconds=0.0))
        client.telemetry.send_startup_pv = self._record('startup_pv')
        client.telemetry.send_page_exposure = self._record('page_exposure')
        client.policy.get_run_policy = self._record(
            'policy',
            types.SimpleNamespace(min_distance=2000,
                                  raw={'runRuleModel': {'minRunTime': 300}, 'policy': 1}))
        client.policy.get_geo_fence = self._record('fence', [school_fence()])
        client.policy.get_checkpoints_for_run = self._record(
            'checkpoints', [GeoPoint.from_api_data(p) for p in official_points()])
        client.request_obs_upload_url = self._record('obs_url', {
            'error': 10000,
            'data': {'signedUrl': 'https://obs.invalid/run.txt',
                     'actualSignedRequestHeaders': {'Content-Type': 'text/plain'}}})
        client.upload_to_obs = self._record('obs_put', {'error': 10000})
        client.submit_record = self._record('submit', {'error': 10000, 'data': {'rrid': 98765}})
        client.history.get_one_record = self._record(
            'record_detail',
            types.SimpleNamespace(is_valid=True, status_text=lambda: '有效(已通过)'))
        client.get_rundetail_config = self._record('rundetail_config', {'error': 10000})

    def run_client(self, risk_check=False):
        messages = []
        result = self.client.execute_full_run(
            DISTANCE_M, DURATION_S, 1, messages.append,
            realism=True, simulate_physical_time=False,
            risk_check=risk_check)
        return result, messages

    # ------------------------------------------------------------- 成功路径
    def test_full_chain_succeeds_and_returns_rrid(self):
        result, messages = self.run_client()
        self.assertTrue(result.success, msg=f"失败原因: {result.message} | {messages}")
        self.assertEqual(result.rrid, 98765)
        self.assertEqual(result.error_code, 10000)
        self.assertEqual(result.total_distance, float(DISTANCE_M))
        self.assertEqual(result.total_time, DURATION_S)
        self.assertTrue(result.is_valid)
        self.assertIn('有效', result.status_text)

    def test_stage_order_matches_capture(self):
        self.run_client()
        self.assertEqual(self.order, [
            'policy',                                              # 先取学校规则
            'server_time', 'startup_pv', 'page_exposure',          # 冷启动
            'fence', 'checkpoints',                                # 围栏与点位
            'obs_url', 'obs_put',                                  # 轨迹上传
            'submit',                                              # 结算
            'record_detail', 'rundetail_config', 'page_exposure',   # 跑后回查
        ])

    def test_risk_check_degrades_gracefully_when_offline(self):
        """预检全部网络失败时只能降级成提示，绝不能把本来能过的提交挡掉。

        这是预检最危险的失效方式：辅助功能反过来成了故障源 —— 用户网络
        差一点，程序就以「风控未通过」为由拒绝提交，而实际上什么都没查到。
        """
        for name in ('check_control_message', 'get_credit_popup',
                     'get_cheat_blacklist', 'get_barrier_free_policy'):
            setattr(self.client.telemetry, name, Mock(side_effect=RuntimeError('offline')))

        result, messages = self.run_client(risk_check=True)
        self.assertTrue(result.success, msg=f"{result.message} | {messages}")
        joined = '\n'.join(messages)
        self.assertIn('风控预检', joined)
        self.assertIn('OBS上传: 成功', joined, '预检降级后仍必须走完上传与结算')

    def test_local_checks_pass_before_any_upload(self):
        """本地双自检必须在 OBS 之前通过 —— 否则等于把必被驳回的报文送上去。"""
        _, messages = self.run_client()
        joined = '\n'.join(messages)
        for expected in ('合规自检: 通过', '一致性自检: 通过', '里程回正', '官方点位: 成功'):
            with self.subTest(expected=expected):
                self.assertIn(expected, joined)

    def test_submission_body_is_self_consistent(self):
        self.run_client()
        body = self.client.submit_record.call_args.args[0]

        self.assertEqual(body['totalDis'], DISTANCE_M)
        self.assertEqual(body['totalTime'], DURATION_S)
        self.assertEqual(body['sportType'], 1)
        self.assertEqual(body['uid'], 1)
        self.assertEqual(body['selectedUnid'], 2)
        self.assertEqual(body['selDistance'], 2000)
        self.assertEqual(body['selRunTime'], 300)
        self.assertTrue(body['complete'])
        self.assertEqual(body['unCompleteReason'], 0)
        # 上传成功后才回填，服务端靠这两个字段关联 OBS 对象
        self.assertTrue(body['isUpload'])
        self.assertEqual(body['recordUrl'],
                         self.client.request_obs_upload_url.call_args.kwargs['object_key'])

    def test_uuid_is_shared_between_point_request_and_record(self):
        self.run_client()
        request_uuid = self.client.policy.get_checkpoints_for_run.call_args.kwargs['uuid_str']
        body = self.client.submit_record.call_args.args[0]
        self.assertEqual(body['uuid'], request_uuid)
        self.assertEqual(len(request_uuid), 36)

    def test_track_json_is_measurably_correct(self):
        self.run_client()
        body = self.client.submit_record.call_args.args[0]
        locations = json.loads(body['allLocJson'])

        # 首尾时间戳必须与 startTime/stopTime 对齐，否则服务端判定轨迹缺失
        self.assertEqual(locations[0]['time'], body['startTime'])
        self.assertEqual(locations[-1]['time'], body['stopTime'])
        self.assertEqual(body['stopTime'] - body['startTime'], DURATION_S * 1000)
        self.assertTrue(all(a['time'] < b['time'] for a, b in zip(locations, locations[1:])))

        # 里程必须对得上 totalDis（这是服务端最强的交叉校验项）
        self.assertAlmostEqual(measured_length(locations), DISTANCE_M,
                               delta=max(0.1, DISTANCE_M * 0.001))

    def test_five_point_envelope_and_official_coordinates(self):
        self.run_client()
        body = self.client.submit_record.call_args.args[0]
        envelope = json.loads(body['fivePointJson'])
        self.assertEqual(set(envelope),
                         {'fivePointJson', 'useZip', 'freedomShowFence',
                          'runAreaId', 'geoFencesJson'})
        self.assertFalse(envelope['useZip'])
        self.assertFalse(envelope['freedomShowFence'])
        self.assertEqual(envelope['runAreaId'], -1)

        points = json.loads(envelope['fivePointJson'])
        self.assertEqual(len(points), 5)
        for actual, source in zip(points, official_points()):
            # 官方下发的 WGS 原值必须原样回传，不能被 GCJ 规划坐标覆盖
            self.assertEqual(actual['lat'], source['lat'])
            self.assertEqual(actual['lon'], source['lon'])
            self.assertEqual(actual['glat'], source['glat'])
            self.assertEqual(actual['pointName'], source['pointName'])

        self.assertEqual(json.loads(envelope['geoFencesJson'])[0]['name'], '测试校区')

    def test_obs_payload_matches_body_and_is_gzip_base64(self):
        self.run_client()
        body = self.client.submit_record.call_args.args[0]
        payload = self.client.upload_to_obs.call_args.args[2]

        self.assertEqual(set(payload), {
            'rrid', 'uid', 'uuid', 'run_data', 'step_freq_json', 'speed_json',
            'segment_json', 'fixed_point_json', 'runFaceCheck',
            'extension_json', 'laps_json'})
        for field, encoded in payload.items():
            with self.subTest(field=field):
                raw = gzip.decompress(base64.b64decode(encoded)).decode('utf-8')
                self.assertIsInstance(raw, str)
        decoded = lambda name: gzip.decompress(base64.b64decode(payload[name])).decode()
        # OBS 与 body 必须同源：各自序列化一次会让浮点/键序漂移
        self.assertEqual(decoded('run_data'), body['allLocJson'])
        self.assertEqual(decoded('fixed_point_json'), body['fivePointJson'])
        self.assertEqual(json.loads(decoded('step_freq_json')), body['stepsPerTenSec'])

    def test_signature_can_be_recomputed_from_body(self):
        self.run_client()
        body = self.client.submit_record.call_args.args[0]

        # 签名只覆盖这 31 个业务字段（7.3.40 抓包反推的白名单），
        # 轨迹 JSON、recordUrl、isUpload 等都不参与。
        signed_keys = [
            'address', 'avgPower', 'avgStepFreq', 'calorie', 'complete',
            'errorCode', 'faceCheck', 'geeToken', 'getPrize', 'goalId',
            'policy', 'selDistance', 'selRunTime', 'selectedUnid', 'speed',
            'sportType', 'startTime', 'status', 'stopTime', 'themeId',
            'totalAscent', 'totalDis', 'totalSteps', 'totalTime', 'uid',
            'unCompleteReason', 'unauthorized', 'useMobilityTools', 'uuid',
            'validDis', 'validTime',
        ]
        self.assertEqual(len(signed_keys), 31)
        for key in signed_keys:
            self.assertIn(key, body, msg=f'报文体缺少参与签名的字段 {key}')

        fields = {key: body[key] for key in signed_keys}
        # goalId 双形态：body 里是 JSON null，签名侧必须是字符串 "null"
        fields['goalId'] = 'null'
        items = [(k, str(v).lower() if isinstance(v, bool) else str(v))
                 for k, v in fields.items() if v is not None]
        items.sort(key=lambda kv: (kv[0].lower(), kv[0]))
        canonical = '&'.join(f'{k}={v}' for k, v in items)
        expected = hashlib.md5((canonical + SPORT_STATIC_SALT).encode()).hexdigest()
        self.assertEqual(body['signature'], expected)

    # ------------------------------------------------------------- 失败分支
    def test_obs_credential_failure_stops_before_upload_and_settlement(self):
        self.client.request_obs_upload_url = self._record(
            'obs_url', {'error': -1, 'message': '签名服务不可用'})
        result, _ = self.run_client()
        self.assertFalse(result.success)
        self.assertIn('签名服务不可用', result.message)
        self.client.upload_to_obs.assert_not_called()
        self.client.submit_record.assert_not_called()

    def test_obs_put_failure_stops_before_settlement(self):
        self.client.upload_to_obs = self._record(
            'obs_put', {'error': 403, 'message': 'OBS HTTP 403'})
        result, _ = self.run_client()
        self.assertFalse(result.success)
        self.assertIn('403', result.message)
        self.client.submit_record.assert_not_called()
        self.client.history.get_one_record.assert_not_called()

    def test_settlement_rejection_skips_record_lookup(self):
        self.client.submit_record = self._record(
            'submit', {'error': -10000, 'message': '成绩无效'})
        result, _ = self.run_client()
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, -10000)
        self.assertEqual(result.message, '成绩无效')
        self.client.history.get_one_record.assert_not_called()

    def test_face_check_requirement_blocks_before_upload(self):
        self.client.policy.get_run_policy = self._record(
            'policy',
            types.SimpleNamespace(
                min_distance=2000,
                raw={'runRuleModel': {'minRunTime': 300}, 'policy': 1},
                face_check=types.SimpleNamespace(enable=True)))
        result, _ = self.run_client()
        self.assertFalse(result.success)
        self.assertIn('人脸', result.message)
        self.client.request_obs_upload_url.assert_not_called()
        self.client.submit_record.assert_not_called()

    def test_local_compliance_failure_returns_result_not_exception(self):
        """本地合规自检不通过时必须返回失败结果，不能把异常抛给调用方。"""
        self.client.policy.get_run_policy = self._record(
            'policy',
            types.SimpleNamespace(
                min_distance=2000,
                raw={'runRuleModel': {'minRunTime': 300}, 'policy': 1},
                # 允许速度 3.0m/s 起，而 2100m/780s ≈ 2.69m/s，必然低于下界被拒
                speed_top=3.0))
        result, messages = self.run_client()
        self.assertFalse(result.success)
        self.assertIn('合规自检', result.message)
        self.assertIn('速度', result.message)
        self.client.request_obs_upload_url.assert_not_called()
        self.client.submit_record.assert_not_called()


if __name__ == '__main__':
    unittest.main()
