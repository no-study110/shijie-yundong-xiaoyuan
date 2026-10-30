#!/usr/bin/env python3
"""运动轨迹组装工具（7.3.92）

职责：把「围栏内规划好的途经点」合成为可直接提交的两份载荷
—— 结算报文 save_record 与华为云 OBS 轨迹对象 obs_payload。

设计约束（修改前请先看对应测试）：

1. **里程严格对齐**：轨迹采样后按测地线长度重算，与 totalDis 的偏差
   控制在 0.1m 以内，否则服务端交叉校验会直接驳回。
2. **打卡点原样保留**：官方点位坐标不允许篡改，轨迹必须真的经过它，
   只是把时间轴对齐到最近采样点。
3. **goalId 双形态**：签名用字符串 ``"null"``，body 用 JSON ``null``。
4. **OBS 载荷与 body 同源**：同一份 JSON 字符串再做 gzip+base64，
   不得各自序列化一次，否则浮点/顺序漂移会导致两侧不一致。

本模块只做本地计算，不发起任何网络请求。
"""

import base64
import gzip
import hashlib
import json
import math
import random
import uuid
import warnings
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from core.pace import pace_thousandths as _pace_thousandths
from .config import SPORT_STATIC_SALT
from .consistency import distance, sample_route, time_buckets, validate_totals
from .models import FivePoint, GpsPoint, SpeedBucket, StepBucket
from .planner import FenceRoutePlanner
from .realism import jitter_counts, jitter_distances

DEFAULT_MAX_RUN_TIME = 7200
DEFAULT_MIN_STEPS = 2000
DEFAULT_CALORIE_PER_KM = 62.0
LENGTH_TOLERANCE_M = 0.1


def pace_thousandths(distance_m: float, time_sec: float) -> int:
    """配速（分/公里）×1000 —— 这就是结算报文 ``speed`` 字段的取值。

    单位是**配速**，不是速度。判定口径由服务端 reasonList 直接给出：
    「配速控制在 2′00″-10′00″」，对应规则字段 speedTop=2.0 / speedBottom=10.0。

    实测校准：13 条真实 App 记录的服务端 ``speed`` 与本函数结果相差 <0.005
    （只有舍入差）。此前这里写的是 ``distance / time * 1000``（即 m/s×1000），
    4080m/2180s 报送 1872 → 服务端读到 1.872 配速 → 直接判「配速」未达标。

    换算式只保留在 :func:`core.pace.pace_thousandths` 一处，本函数是兼容入口。
    """
    return _pace_thousandths(distance_m, time_sec)


# 服务端对 allLocJson 折线长度与 totalDis 的容差比例（0.1%）
LENGTH_TOLERANCE_RATIO = 0.001
# 吸附打卡点时，连带平移的邻域半径（采样点个数）
ADJUST_SPREAD = 2
# 偏离目标里程超过这个比例，说明轨迹几何形态本身有问题（例如闭环远短于目标
# 里程），继续缩放只会把点摊开、视觉上露出破绽，因此不再硬凑。
MAX_LENGTH_DEVIATION_RATIO = 0.30


class TrackLengthError(ValueError):
    """轨迹里程超出可修正范围，无法对齐 totalDis。"""


class TrackLengthWarning(UserWarning):
    """轨迹里程未对齐目标值。

    非 strict 模式下只发告警、不打断流程（下游 ``_assert_track`` 仍会拦截），
    strict 模式下同样情形会抛 :class:`TrackLengthError`。
    """


class LengthReport(NamedTuple):
    """``_restore_length`` 的结果快照。

    调用方据此判断轨迹到底有没有真的对齐目标里程 —— 之前这个函数超差时
    直接 return，上层只能从最终 ``_assert_track`` 的报错反推，排查成本很高。
    """

    target_m: float
    original_m: float
    final_m: float
    adjusted: bool
    deviation_ratio: float

    @property
    def aligned(self) -> bool:
        """是否落在服务端可接受的容差内（max(0.1m, totalDis 的 0.1%)）。"""
        tolerance = max(LENGTH_TOLERANCE_M, self.target_m * LENGTH_TOLERANCE_RATIO)
        return abs(self.final_m - self.target_m) <= tolerance



def gzip_base64_encode(data) -> str:
    """OBS 载荷字段级编码：gzip 后 base64。"""
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    return base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")


def compute_sport_signature(fields: Dict[str, Any], salt: str = SPORT_STATIC_SALT) -> str:
    """运动结算签名：按小写字段名排序，None 跳过，bool 转小写，尾部接盐做 MD5。"""
    items = []
    for key, value in fields.items():
        if value is None:
            continue
        if key.lower() == "signature":
            continue
        items.append((key, str(value).lower() if isinstance(value, bool) else str(value)))
    items.sort(key=lambda kv: (kv[0].lower(), kv[0]))
    canonical = "&".join(f"{k}={v}" for k, v in items)
    return hashlib.md5((canonical + salt).encode("utf-8")).hexdigest().lower()


class RunningTrackBuilder:
    """GPS 轨迹合成 + 结算报文组装。"""

    def __init__(self,
                 uid: int,
                 unid: int,
                 sport_type: int = 1,
                 uuid_str: Optional[str] = None,
                 rng: Optional[random.Random] = None):
        if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
            raise ValueError("uid 必须是正整数")
        if isinstance(unid, bool) or not isinstance(unid, int) or unid < 0:
            raise ValueError("unid 必须是非负整数")
        if sport_type not in (1, 3, 6):
            raise ValueError("sportType 只支持 1(计分跑)/3(室内)/6(自由跑)")
        self.uid = uid
        self.unid = unid
        self.sport_type = sport_type
        self.uuid = uuid_str or str(uuid.uuid4())
        self.rng = rng
        # 供调用方做诊断展示，规划失败时为 None
        self.last_plan = None
        # 里程回正结果快照（LengthReport），未生成轨迹时为 None
        self.last_length_report: Optional[LengthReport] = None

    # ------------------------------------------------------------------ 轨迹
    def generate_track_around_points(self,
                                     center_lat: float,
                                     center_lng: float,
                                     checkpoints: Optional[Sequence[Dict[str, Any]]],
                                     total_distance_m: int,
                                     total_time_sec: int,
                                     start_time_ms: int,
                                     pace=None,
                                     fence_polygon: Optional[Sequence[Sequence[float]]] = None,
                                     strict_length: bool = False,
                                     ) -> Tuple[List[GpsPoint], List[FivePoint]]:
        """在围栏内规划环线，采样成 GPS 点并输出必经打卡点列表。

        计分跑（sport_type=1）必须命中所有打卡点；自由跑（6）不强制。
        围栏缺失时退化为「中心点折返」的简易路线。

        strict_length=True 时，里程无法对齐会抛 TrackLengthError 而不是仅告警。
        """
        validate_totals(int(total_time_sec), 0, int(start_time_ms),
                        int(start_time_ms) + int(total_time_sec) * 1000, float(total_distance_m))
        require_all = self.sport_type == 1

        cp_coords = self._checkpoint_coords(checkpoints)
        start = (float(center_lat), float(center_lng))
        invalid_center = (start == (0.0, 0.0)) or not (
            math.isfinite(start[0]) and math.isfinite(start[1]) and
            -90 <= start[0] <= 90 and -180 <= start[1] <= 180)

        if not cp_coords and self.sport_type == 1:
            # 计分跑拿不到官方点位、又没有可用中心坐标/围栏时，报文必被驳回
            if invalid_center:
                raise ValueError("sportType=1 无有效中心坐标且无官方点位，无法生成合规轨迹")
            if fence_polygon and len(fence_polygon) >= 3:
                raise ValueError("sportType=1 缺少必经打卡点，无法生成合规轨迹")

        if not cp_coords:
            if self.sport_type == 6:
                # 自由跑没有官方点位，fivePointJson 留空即可（服务端不校验）；
                # 硬塞合成点位反而会让「固定点必须全部经过」的断言把短距离跑拦掉。
                checkpoints = []
            else:
                # 计分跑（有围栏但官方点位未拉到）按 7.3.40 语义补等分五固定点，
                # 保证 fivePointJson envelope 结构完整而不是留空。
                cp_coords = self._synthetic_checkpoints(center_lat, center_lng, total_distance_m)
                checkpoints = [{"lat": lat, "lng": lng, "name": f"FixedPoint_{i + 1}"}
                               for i, (lat, lng) in enumerate(cp_coords)]

        # 自由跑不强制打卡点：距离短到装不下时允许丢掉五个点位，只保留轨迹
        if self.sport_type == 6 and self._loop_route_too_long(start, cp_coords, total_distance_m):
            cp_coords, checkpoints = [], []

        self._validate_start(start, cp_coords, require_all)

        if fence_polygon and len(fence_polygon) >= 3:
            planned = FenceRoutePlanner().plan(
                fence_polygon=fence_polygon,
                checkpoints=[{"lat": lat, "lng": lng} for lat, lng in cp_coords] or None,
                start_point=None if start == (0.0, 0.0) else start,
                extra_lap=True,
                boundary_lap=self.sport_type == 6,
                target_distance_m=total_distance_m if (cp_coords and require_all) else None,
            )
            self.last_plan = planned
            waypoints = list(planned.waypoints)
        else:
            self.last_plan = None
            chain = self._loop_waypoints(start, cp_coords)
            # 里程装不下打卡点间的最短连线时，改用以打卡点为锚的折返线
            if cp_coords and self._route_length(chain) > float(total_distance_m):
                chain = self._fallback_waypoints(start, cp_coords, total_distance_m)
                if self._route_length(chain) > float(total_distance_m):
                    if self.sport_type == 1:
                        raise ValueError(
                            f"target distance too short to visit the supplied points "
                            f"（目标里程 {total_distance_m}m 装不下 {len(cp_coords)} 个打卡点）")
                    # 自由跑不强制打卡点，退回小环线
                    chain = _square_ring(start[0], start[1], max(20.0, total_distance_m / 8.0))
            elif cp_coords:
                # 闭环短于目标里程时，交给 sample_route 沿闭环自然绕圈补足。
                # 不人为插入绕行环：环的半径由缺口反推，在围栏较小的学校里
                # 会把轨迹甩到围栏之外，反而不合规。
                pass
            waypoints = chain
            if self.sport_type == 6:
                # 自由跑没有必过点位，fivePointJson 保持空列表
                cp_coords, checkpoints = [], []

        samples = sample_route(
            waypoints,
            meters=total_distance_m,
            seconds=total_time_sec,
            start=start_time_ms,
            require_all=False,          # 打卡点是否必须命中由调用方判定
            pace=pace,
            rng=self.rng,
        )

        gps_points = [GpsPoint(lat=lat, lon=lon, time=int(ts), speed=0.0, accuracy=3.0)
                      for lat, lon, ts in samples]

        # 顺序：对齐里程 → 吸附打卡点 → 再次对齐 → 只归位打卡点。
        #
        # 为什么绕这么一圈：
        #   · 吸附必须靠平移整段邻域来完成，否则单点位移会在轨迹上留下尖刺；
        #   · 平移会带来 Δ²/(2·步长) 的二阶长度误差，累积几百段后仍可能超容差，
        #     所以要再对齐一次；
        #   · 但再对齐是整体缩放，会把官方打卡点推离 0.1m 量级，最后单独归位。
        # 三步之后长度误差只剩「归位那一 0.1m 位移」带来的二阶项（<0.05m）。
        self.last_length_report = self._restore_length(
            gps_points, float(total_distance_m), [], strict=strict_length)
        self._align_fixed_points(gps_points, cp_coords)
        self._restore_length(gps_points, float(total_distance_m), [])
        self._align_fixed_points(gps_points, cp_coords, spread=0)
        self.last_length_report = self.last_length_report._replace(
            final_m=self._measure_length(gps_points))
        self._fill_speed(gps_points)
        self._fill_accuracy(gps_points, self.rng)

        five_points = self._build_five_points(gps_points, checkpoints or [], cp_coords)
        return gps_points, five_points

    def generate_test_track(self,
                            center_lat: float,
                            center_lng: float,
                            total_distance_m: int,
                            total_time_sec: int,
                            start_time_ms: Optional[int] = None
                            ) -> Tuple[List[GpsPoint], List[FivePoint]]:
        """无围栏、无打卡点的最小轨迹（自测 / 与 7.3.40 对齐用）。"""
        import time as _time
        start = start_time_ms if start_time_ms is not None else int(_time.time() * 1000)
        side = max(40.0, total_distance_m / 8.0)
        ring = _square_ring(center_lat, center_lng, side)
        samples = sample_route(ring, total_distance_m, total_time_sec, start,
                              require_all=False, rng=self.rng)
        points = [GpsPoint(lat=lat, lon=lon, time=int(ts), speed=0.0) for lat, lon, ts in samples]
        self._fill_speed(points)
        self._fill_accuracy(points, self.rng)
        return points, []

    # ---------------------------------------------------------------- 报文
    def build_submission_payload(self,
                                 total_distance_m: int,
                                 total_time_sec: int,
                                 total_steps: int,
                                 start_time_ms: int,
                                 stop_time_ms: int,
                                 gps_points: Sequence[GpsPoint],
                                 five_points: Sequence[FivePoint],
                                 sel_distance_m: int = 0,
                                 sel_run_time_s: int = 0,
                                 policy: int = 1,
                                 run_area_id: int = -1,
                                 geo_fences_json: str = "[]",
                                 total_ascent: int = 0,
                                 physio: Optional[Dict[str, Any]] = None,
                                 jitter_buckets: bool = True,
                                 ) -> Tuple[Dict[str, Any], Dict[str, str], str]:
        """组装 (save_record, obs_payload, object_key)。

        入参自检：距离/时长/步数与轨迹必须互相自洽，否则直接抛 ValueError，
        不允许把必然被驳回的报文组装出来。
        """
        if not gps_points:
            raise ValueError("轨迹为空，无法组装报文")
        points = list(gps_points)
        stop_time_ms = int(stop_time_ms)
        start_time_ms = int(start_time_ms)
        validate_totals(int(total_time_sec), int(total_steps), start_time_ms,
                        stop_time_ms, float(total_distance_m))
        if int(total_distance_m) <= 0 or int(total_steps) < 0:
            raise ValueError("距离必须为正，步数不能为负")

        physio = dict(physio or {})
        complete = (int(total_distance_m) >= int(sel_distance_m)) and (int(total_time_sec) >= int(sel_run_time_s))
        un_complete_reason = 0 if complete else 10
        avg_step_freq = round(total_steps * 60.0 / total_time_sec) if total_time_sec else 0
        speed_val = pace_thousandths(total_distance_m, total_time_sec)
        calorie = int(total_distance_m / 1000.0 * DEFAULT_CALORIE_PER_KM)

        speed_buckets, step_buckets = self._build_buckets(
            total_distance_m, total_time_sec, total_steps, start_time_ms, stop_time_ms,
            jitter=jitter_buckets)

        all_loc_json = json.dumps([_point_dict(p) for p in points], separators=(",", ":"))
        # 7.3.92 的 fivePointJson 是一层 envelope，不是裸数组：
        # 服务端要同时拿到点位列表、围栏回显、runAreaId 才能校验打卡合规。
        five_pt_envelope = {
            "fivePointJson": json.dumps([_point_dict(p) for p in five_points],
                                        separators=(",", ":")),
            "useZip": False,
            "freedomShowFence": False,
            "runAreaId": run_area_id,
            "geoFencesJson": geo_fences_json,
        }
        five_pt_json = json.dumps(five_pt_envelope, separators=(",", ":"))
        speed_list = [b.to_dict() for b in speed_buckets]
        step_list = [b.to_dict() for b in step_buckets]
        speed_json_str = json.dumps(speed_list, separators=(",", ":"))
        step_json_str = json.dumps(step_list, separators=(",", ":"))

        self._assert_track(points, total_distance_m, total_time_sec,
                           start_time_ms, stop_time_ms, five_points)

        sign_fields = {
            "address": "",
            "avgPower": int(physio.get("avgPower", 0)),
            "avgStepFreq": avg_step_freq,
            "calorie": calorie,
            "complete": complete,
            "errorCode": 0,
            "faceCheck": 0,
            "geeToken": "",
            "getPrize": False,
            # 签名侧固定为字符串 "null"，body 侧才是 JSON null
            "goalId": "null",
            "policy": policy,
            "selDistance": sel_distance_m,
            "selRunTime": sel_run_time_s,
            "selectedUnid": self.unid,
            "speed": speed_val,
            "sportType": self.sport_type,
            "startTime": start_time_ms,
            "status": 1,
            "stopTime": stop_time_ms,
            "themeId": 0,
            "totalAscent": total_ascent,
            "totalDis": total_distance_m,
            "totalSteps": total_steps,
            "totalTime": total_time_sec,
            "uid": self.uid,
            "unCompleteReason": un_complete_reason,
            "unauthorized": 0,
            "useMobilityTools": 0,
            "uuid": self.uuid,
            "validDis": total_distance_m,
            "validTime": total_time_sec,
        }
        signature = compute_sport_signature(sign_fields)

        save_record: Dict[str, Any] = {
            "address": "",
            "allLocJson": all_loc_json,
            "avgPower": int(physio.get("avgPower", 0)),
            "avgStepFreq": avg_step_freq,
            "calorie": calorie,
            "complete": complete,
            "errorCode": 0,
            "faceCheck": 0,
            "fivePointJson": five_pt_json,
            "geeToken": "",
            "getPrize": False,
            "goalId": None,
            "isUpload": False,
            "latitude": points[0].lat,
            "longitude": points[0].lon,
            "maxRunTime": DEFAULT_MAX_RUN_TIME,
            "minSteps": DEFAULT_MIN_STEPS,
            "policy": policy,
            "recordUrl": "",
            "roomId": 0,
            "runAreaId": run_area_id,
            "segmentJson": "[]",
            "selDistance": sel_distance_m,
            "selRunTime": sel_run_time_s,
            "selectedUnid": self.unid,
            "signature": signature,
            "speed": speed_val,
            "speedPerTenSec": speed_list,
            "sportType": self.sport_type,
            "startTime": start_time_ms,
            "status": 1,
            "stepsPerTenSec": step_list,
            "stopTime": stop_time_ms,
            "themeId": 0,
            "totalAscent": total_ascent,
            "totalDis": total_distance_m,
            "totalSteps": total_steps,
            "totalTime": total_time_sec,
            "uid": self.uid,
            "unCompleteReason": un_complete_reason,
            "unauthorized": 0,
            "useMobilityTools": 0,
            "uuid": self.uuid,
            "validDis": total_distance_m,
            "validTime": total_time_sec,
        }
        if geo_fences_json and geo_fences_json != "[]":
            save_record["geoFenceJson"] = geo_fences_json

        obs_payload = {
            "rrid": gzip_base64_encode(""),
            "uid": gzip_base64_encode(str(self.uid)),
            "uuid": gzip_base64_encode(self.uuid),
            "run_data": gzip_base64_encode(all_loc_json),
            "step_freq_json": gzip_base64_encode(step_json_str),
            "speed_json": gzip_base64_encode(speed_json_str),
            "segment_json": gzip_base64_encode("[]"),
            "fixed_point_json": gzip_base64_encode(five_pt_json),
            "runFaceCheck": gzip_base64_encode(""),
            "extension_json": gzip_base64_encode(""),
            "laps_json": gzip_base64_encode(""),
        }

        return save_record, obs_payload, self._object_key(start_time_ms)

    # -------------------------------------------------------------- 内部工具
    @staticmethod
    def _checkpoint_coords(checkpoints) -> List[Tuple[float, float]]:
        """取打卡点用于规划轨迹的坐标 —— 必须是 GCJ-02。

        官方点位同时下发 lat/lon（WGS-84）与 glat/glon（GCJ-02）。轨迹跑在
        GCJ 坐标系里，规划与吸附都必须用 glat/glon，否则整条轨迹会偏移约 700m。
        """
        coords: List[Tuple[float, float]] = []
        for cp in checkpoints or []:
            if isinstance(cp, dict):
                try:
                    lat = float(cp.get("glat", cp["lat"]))
                    lng = float(cp.get("glon", cp.get("glng",
                               cp["lng"] if "lng" in cp else cp["lon"])))
                except (KeyError, TypeError, ValueError):
                    continue
            else:
                try:
                    lat = float(getattr(cp, "lat", getattr(cp, "latitude")))
                    lng = float(getattr(cp, "lng", getattr(cp, "lon", getattr(cp, "longitude", None))))
                except (AttributeError, TypeError, ValueError):
                    continue
            if math.isfinite(lat) and math.isfinite(lng):
                coords.append((lat, lng))
        return coords

    def _validate_start(self, start, cp_coords, require_all: bool) -> None:
        lat, lng = start
        if not (math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError("起跑点坐标越界")
        return

    def _fallback_waypoints(self, start, cp_coords,
                            total_distance_m: Optional[float] = None) -> List[Tuple[float, float]]:
        """折返线：锚点 -> 各打卡点 -> 锚点。

        锚点默认取起跑点；但若「起跑点 -> 首个点位」往返已超过目标里程，
        折返线根本装不下，此时改以首个点位为锚（轨迹从该点位出发并跑完整个里程），
        否则剩下的里程无处安放，只能被 sample_route 截断成半截轨迹。
        """
        if cp_coords:
            anchor = start
            if start == (0.0, 0.0):
                anchor = cp_coords[0]
            elif total_distance_m is not None:
                round_trip = 2 * distance(tuple(start), tuple(cp_coords[0]))
                if round_trip > float(total_distance_m):
                    anchor = cp_coords[0]
            return [anchor] + cp_coords + [anchor]
        ring = _square_ring(start[0], start[1], 120.0)
        return ring

    def _loop_waypoints(self, start, cp_coords) -> List[Tuple[float, float]]:
        """按最近邻串成闭环：起跑点 -> 各打卡点 -> 回起跑点。"""
        if not cp_coords:
            return _square_ring(start[0], start[1], 120.0)
        anchor = cp_coords[0] if start == (0.0, 0.0) else start
        remaining = list(cp_coords)
        route = [anchor]
        while remaining:
            nxt = min(remaining, key=lambda p: distance(tuple(route[-1]), tuple(p)))
            remaining.remove(nxt)
            route.append(nxt)
        if distance(tuple(route[-1]), tuple(anchor)) > 1e-6:
            route.append(anchor)
        return route

    def _loop_route_too_long(self, start, cp_coords, total_distance_m) -> bool:
        if not cp_coords:
            return False
        return self._route_length(self._loop_waypoints(start, cp_coords)) > float(total_distance_m)

    @staticmethod
    def _synthetic_checkpoints(center_lat: float, center_lng: float,
                               total_distance_m: int) -> List[Tuple[float, float]]:
        """自由跑无官方点位时，按目标里程生成 5 个等距环点（与 7.3.40 语义一致）。

        半径按目标里程反推，确保第一圈回到起点恰好用完整里程，短距离跑也装得下。
        """
        radius_m = max(8.0, total_distance_m / (2 * math.pi))
        d_lat = radius_m / 111320.0
        d_lng = radius_m / (111320.0 * max(0.1, abs(math.cos(math.radians(center_lat)))))
        points: List[Tuple[float, float]] = []
        for index in range(5):
            angle = 2 * math.pi * index / 5
            points.append((round(center_lat + d_lat * math.sin(angle), 6),
                           round(center_lng + d_lng * (1 - math.cos(angle)), 6)))
        return points

    @staticmethod
    def _route_length(waypoints: Sequence[Sequence[float]]) -> float:
        return sum(distance(tuple(a), tuple(b)) for a, b in zip(waypoints, waypoints[1:]))

    @staticmethod
    def _measure_length(points: Sequence[GpsPoint]) -> float:
        """轨迹折线的实测长度（米）。"""
        return sum(distance((a.lat, a.lon), (b.lat, b.lon))
                   for a, b in zip(points, points[1:]))

    @staticmethod
    def _restore_length(points: List[GpsPoint], target_m: float,
                        anchors: Sequence[float],
                        strict: bool = False) -> LengthReport:
        """把采样/吸附造成的里程偏差修正到目标值，并返回修正结果。

        锚点（首尾 + 每个被吸附的打卡点）坐标必须精确命中，不能动；
        其余自由点整体绕质心缩放即可改变总长。缩放会同时拉动锚点位置，
        所以每轮先把锚点复位，再按当前误差重新缩放，迭代到 0.1m 内。

        前提：本方法只对**首尾重合的闭环**有效。生产轨迹（环线、折返线）
        首尾都是同一起跑点，锁住它不影响整体伸缩；若传入开放折线，缩放量
        会被首尾复位抵消掉，长度几乎不动 —— 这种输入应在上游修正形态。

        若当前长度与目标差距超过 ``MAX_LENGTH_DEVIATION_RATIO``，说明轨迹
        形态本身有问题（例如闭环远短于目标里程），缩放只会把点摊开、
        视觉上露出破绽，此时保持原样不硬凑 —— 但**不再静默**：
        非 strict 模式发 :class:`TrackLengthWarning`，strict 模式抛
        :class:`TrackLengthError`。下游 ``_assert_track`` 无论如何都会拦截
        超差轨迹，这里提前把「为什么超差」讲清楚。

        参数:
            points: 待修正的采样点，原地修改。
            target_m: 目标里程（米），即报文里的 totalDis。
            anchors: 需要锁定坐标的下标（首尾与打卡点吸附位）。
            strict: True 时超差直接抛异常，适用于「宁可失败也不交脏数据」的调用方。

        返回:
            :class:`LengthReport`，含原始长度、最终长度与偏离比例。
        """
        if len(points) < 3 or target_m <= 0:
            return LengthReport(float(target_m), 0.0, 0.0, False, 0.0)

        def total() -> float:
            return sum(distance((a.lat, a.lon), (b.lat, b.lon))
                       for a, b in zip(points, points[1:]))

        original = total()
        if original <= 0:
            return LengthReport(float(target_m), 0.0, 0.0, False, 0.0)

        def _report(value: float, adjusted: bool) -> LengthReport:
            return LengthReport(float(target_m), original, value, adjusted,
                                abs(value - target_m) / target_m)

        def _reject(current: float, reason: str) -> LengthReport:
            ratio = abs(current - target_m) / target_m
            message = (f"轨迹里程 {current:.1f}m 与目标 {target_m:.1f}m 偏离 "
                       f"{ratio * 100:.1f}%（超过 {MAX_LENGTH_DEVIATION_RATIO * 100:.0f}% "
                       f"可修正上限）：{reason}")
            if strict:
                raise TrackLengthError(message)
            warnings.warn(message, TrackLengthWarning, stacklevel=3)
            return _report(current, False)

        if abs(original - target_m) / target_m > MAX_LENGTH_DEVIATION_RATIO:
            return _reject(original, "轨迹几何形态与目标里程不匹配，缩放无法修正")

        # 只锁首点与打卡点吸附位。
        #
        # 尾点坐标不参与任何服务端校验（只校验 stopTime 时间戳），锁住它反而
        # 会让缩放失效：轨迹跑不满整圈时首尾并不重合，尾点被钉死，绕质心
        # 缩放只改变内点分布，长度几乎不动甚至反向增长 —— 实测 2168m 的目标
        # 轨迹迭代 20 轮后涨到 3248m。放开尾点后缩放对闭环与开放折线都成立。
        fixed_indices = {0} | {int(i) for i in anchors}
        snapshot = {i: (points[i].lat, points[i].lon) for i in fixed_indices}

        # 缩放中心：首点。
        #
        # 以首点为原点的缩放是严格的相似变换 —— 每段向量 (p[i]-p[i-1]) 同比缩放，
        # 于是**每一段**长度都乘以 factor，总长精确等于 factor 倍原长。一轮即收敛。
        # 以质心为基准时，首尾锚点复位会破坏相似性，迭代可能反向增长
        # （实测 2168m 涨到 3248m）；以首点为基准时首点天然不动，无需复位。
        base_lat, base_lon = points[0].lat, points[0].lon

        for _ in range(20):
            current = total()
            if abs(current - target_m) <= LENGTH_TOLERANCE_M or current <= 0:
                break
            factor = target_m / current
            for point in points:
                point.lat = base_lat + (point.lat - base_lat) * factor
                point.lon = base_lon + (point.lon - base_lon) * factor
            for index, (lat, lon) in snapshot.items():
                points[index].lat = lat
                points[index].lon = lon

        final = total()
        if abs(final - target_m) / target_m > MAX_LENGTH_DEVIATION_RATIO:
            # 迭代 20 轮仍未收敛：锚点锁得太死，缩放改不动总长。
            return _reject(final, "锚点约束过强，缩放迭代未收敛")
        # adjusted 只表示「确实改动了坐标」，不是「调用了修正流程」
        return _report(final, abs(final - original) > LENGTH_TOLERANCE_M)

    @staticmethod
    def _fill_speed(points: List[GpsPoint]) -> None:
        """填入每个采样点的瞬时速度（m/s）。

        用**中心差分**而非单侧差分：``(p[i+1] - p[i-1]) / (dt_prev + dt_next)``。
        单侧差分对时间戳抖动过于敏感 —— 相邻两采样点只要被挤到 1ms 间隔，
        2m 的位移就会算出 2000m/s 的瞬时速度，写进 allLocJson 后既是明显
        的机器痕迹，也会被本地合规自检误判成超速。中心差分天然把该点分摊
        到前后两段正常间隔上，再叠一层 3 点滑动平均，曲线接近真实 GPS 输出。
        """
        count = len(points)
        if count == 0:
            return
        if count == 1:
            points[0].speed = 0.0
            return

        raw: List[float] = []
        for index, current in enumerate(points):
            previous = points[index - 1] if index > 0 else current
            following = points[index + 1] if index + 1 < count else current
            span = (following.time - previous.time) / 1000.0
            if span <= 0:
                raw.append(0.0)
                continue
            raw.append(distance((previous.lat, previous.lon),
                                (following.lat, following.lon)) / span)

        for index, point in enumerate(points):
            window = raw[max(0, index - 1): min(count, index + 2)]
            point.speed = round(sum(window) / len(window), 2)

    @staticmethod
    def _fill_accuracy(points: List[GpsPoint], rng: Optional[random.Random] = None) -> None:
        """填入随时间相关的定位精度（米）。

        原先每个采样点都写死 ``accuracy=3.0``。真实 Android 的 accuracy 是
        **一阶惯性**的：开阔地常年 3~8m，走进楼边或树荫会连续几十秒抬到
        10~25m，再缓慢回落 —— 不会逐点独立跳变，更不会所有点相等。
        恒定值是最容易被静态比对识别的痕迹之一。

        这里用「环境基线 + 惯性跟随 + 偶发劣化窗」模拟，劣化窗持续 4~18 个
        采样点（12~54 秒）才恢复，对应真实的建筑/树冠遮蔽时长。

        **刻意不动 isValid**：isValid=False 的点可能被服务端剔出折线长度统计，
        那样 totalDis 校验会直接失败，而 kinematics 的里程还原校验对此没有兜底。
        用「看起来更真」换「必定被驳回」不划算。
        """
        if not points:
            return
        if rng is None:
            for point in points:
                point.accuracy = 3.0
            return

        baseline = rng.uniform(3.2, 7.0)      # 本次跑步所处环境的精度基线
        level = baseline
        degrade_left = 0
        for point in points:
            if degrade_left > 0:
                degrade_left -= 1
                target = baseline + rng.uniform(6.0, 16.0)
            elif rng.random() < 0.02:         # 约每 50 个采样点进入一次劣化
                degrade_left = rng.randint(4, 18)
                target = baseline + rng.uniform(6.0, 16.0)
            else:
                target = baseline
            level += (target - level) * 0.35 + rng.gauss(0.0, 0.35)
            point.accuracy = round(max(2.5, min(32.0, level)), 1)

    def _align_fixed_points(self, points: List[GpsPoint],
                            cp_coords: Sequence[Tuple[float, float]],
                            tolerance_m: float = 3.0,
                            spread: Optional[int] = None) -> List[float]:
        """把打卡点吸附到轨迹采样点上（不改官方坐标，只让轨迹真的经过它）。

        直线段恰好穿过打卡点时，最近采样点可能落在几米外，服务端按误差带
        判定会认为没经过。这里把采样点对齐到官方坐标；首尾点承担起止时间戳，
        不挪动。

        **整段邻域平移**，而不是只挪一个点。

        单个点被拉到打卡点上，它前后两段的长度都会变，轨迹上留下一根来回的
        「尖刺」。5 个打卡点各贡献约 2 倍位移，实测总长被顶高 1~4m，把
        _assert_track 那 0.1% 的容差全吃掉。改成把该点前后各 ADJUST_SPREAD
        个采样点**一起平移同样的向量**，段内长度严格不变，只有平移区与非
        平移区的交界两段有二阶误差（O(Δ²/步长)，Δ≈0.3m 时不足 1cm）。

        容差下限：采样间距的一半。轨迹是一段段折线，打卡点落在两采样点之间时，
        最近采样点的距离最大可达半个步长。固定 3m 容差在这些位置会漏吸附。

        返回被吸附点的下标。

        参数 ``spread`` 控制连带平移的邻域半径，默认 ADJUST_SPREAD；
        传 0 表示只把打卡点本身归位（用于长度重新对齐之后，把缩放
        推离的几厘米拉回来，代价最小）。
        """
        if not cp_coords or len(points) < 3:
            return []
        spread = ADJUST_SPREAD if spread is None else max(0, int(spread))
        step = sum(distance((a.lat, a.lon), (b.lat, b.lon))
                   for a, b in zip(points, points[1:])) / max(1, len(points) - 1)
        effective = max(tolerance_m, step * 0.75 + 2.0)

        shifts: List[Tuple[int, float, float, float, float]] = []
        for lat, lng in cp_coords:
            nearest, best = min(
                (distance((points[i].lat, points[i].lon), (lat, lng)), i)
                for i in range(1, len(points) - 1))
            if nearest > effective:
                continue
            shifts.append((best, lat - points[best].lat, lng - points[best].lon, lat, lng))

        for best, dlat, dlng, _, _ in shifts:
            for index in range(best - spread, best + spread + 1):
                if 0 < index < len(points) - 1:
                    points[index].lat += dlat
                    points[index].lon += dlng
        # 平移区重叠时可能被推过一点，最后精确归位打卡点本身
        for best, _, _, lat, lng in shifts:
            points[best].lat = lat
            points[best].lon = lng
        return [float(best) for best, _, _, _, _ in shifts]

    def _build_five_points(self,
                           points: Sequence[GpsPoint],
                           raw_checkpoints: Sequence[Dict[str, Any]],
                           cp_coords: Sequence[Tuple[float, float]]) -> List[FivePoint]:
        """官方点位坐标原样保留，只把时间轴对齐到最接近的采样点。"""
        if not cp_coords:
            return []
        five: List[FivePoint] = []
        meta_by_index = [cp for cp in raw_checkpoints if isinstance(cp, dict)]
        for index, (lat, lng) in enumerate(cp_coords):
            extra: Dict[str, Any] = {}
            name = ""
            out_lat, out_lon = lat, lng
            if index < len(meta_by_index):
                raw = meta_by_index[index]
                extra = {k: v for k, v in raw.items()
                         if k not in ("lat", "lng", "lon", "name", "pointName")}
                name = str(raw.get("name") or raw.get("pointName") or "")
                # 原始报文里的 lat/lon 是官方下发的 WGS 值，glat/glon 才是 GCJ 值。
                # 两条都要按原样回传，不能拿 GCJ 值去覆盖 lat，否则点位会偏移约 700m。
                if isinstance(raw.get("lat"), (int, float)) and not isinstance(raw.get("lat"), bool):
                    out_lat = float(raw["lat"])
                for lon_key in ("lon", "lng"):
                    value = raw.get(lon_key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        out_lon = float(value)
                        break
            five.append(FivePoint(lat=out_lat, lon=out_lon,
                                  isFixed=int(extra.get("isFixed", 1)),
                                  isPass=True,
                                  pointName=name,
                                  position=index + 1,
                                  raw=extra))
        return five

    def _build_buckets(self, meters: int, seconds: int, steps: int,
                       start: int, stop: int, jitter: bool
                       ) -> Tuple[List[SpeedBucket], List[StepBucket]]:
        rows = list(time_buckets(seconds, steps, start, stop, meters))
        counts = [row[2] for row in rows]
        lengths = [row[3] for row in rows]
        if jitter:
            rng = self.rng if self.rng is not None else random.Random()
            counts = jitter_counts(counts, rng)
            lengths = jitter_distances(lengths, rng)

        speed_buckets: List[SpeedBucket] = []
        step_buckets: List[StepBucket] = []
        for index, (begin, end, _, _) in enumerate(rows, start=1):
            dist = lengths[index - 1] if index - 1 < len(lengths) else 0.0
            step = counts[index - 1] if index - 1 < len(counts) else 0
            speed_buckets.append(SpeedBucket(distance=round(float(dist), 2),
                                             beginTime=begin, endTime=end, queueNum=index))
            step_buckets.append(StepBucket(stepsNum=int(step), beginTime=begin,
                                           endTime=end, queueNum=index))
        return speed_buckets, step_buckets

    @staticmethod
    def _assert_track(points: Sequence[GpsPoint], meters, seconds, start, stop, five_points) -> None:
        if len(points) < 2:
            raise ValueError("轨迹点数不足")
        if points[0].time != start or points[-1].time != stop:
            raise ValueError("轨迹首尾时间戳与 startTime/stopTime 不一致")
        previous = points[0].time
        for point in points[1:]:
            if point.time <= previous:
                raise ValueError("轨迹时间戳必须严格递增")
            previous = point.time
        actual = sum(distance((a.lat, a.lon), (b.lat, b.lon)) for a, b in zip(points, points[1:]))
        tolerance = max(LENGTH_TOLERANCE_M, meters * 0.001)
        if abs(actual - meters) > tolerance:
            raise ValueError(f"轨迹实测长度 {actual:.3f}m 与 totalDis {meters}m 不一致")
        for fixed in five_points:
            # 轨迹跑在 GCJ-02 坐标系里，校验也必须用 glat/glon；
            # lat/lon 是官方下发的 WGS 原值，直接比对会差 700m 左右。
            extra = getattr(fixed, "raw", None) or {}
            check_lat = extra.get("glat", fixed.lat)
            check_lon = extra.get("glon", extra.get("glng", fixed.lon))
            try:
                check_lat, check_lon = float(check_lat), float(check_lon)
            except (TypeError, ValueError):
                check_lat, check_lon = fixed.lat, fixed.lon
            nearest = min(distance((p.lat, p.lon), (check_lat, check_lon)) for p in points)
            if nearest > 1.0:
                raise ValueError("必经打卡点未出现在轨迹上")

    def _object_key(self, start_time_ms: int) -> str:
        return (f"runData/{self.uid}/{self.sport_type}/{start_time_ms // 1000}"
                f"_{self.uuid}.txt")


def _point_dict(point) -> Dict[str, Any]:
    """采样点序列化；兼容带 to_dict() 的模型与轻量对象。"""
    to_dict = getattr(point, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return {"lat": point.lat, "lon": getattr(point, "lon", getattr(point, "lng", None)),
            "time": getattr(point, "time", 0), "speed": getattr(point, "speed", 0.0),
            "accuracy": getattr(point, "accuracy", 3.0), "isValid": getattr(point, "isValid", True)}


def _square_ring(lat: float, lng: float, side_m: float) -> List[Tuple[float, float]]:
    d_lat = side_m / 2 / 111320.0
    d_lng = side_m / 2 / (111320.0 * max(0.1, abs(math.cos(math.radians(lat)))))
    return [(lat - d_lat, lng - d_lng), (lat - d_lat, lng + d_lng),
            (lat + d_lat, lng + d_lng), (lat + d_lat, lng - d_lng)]
