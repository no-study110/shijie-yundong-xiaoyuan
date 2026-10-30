# -*- coding: utf-8 -*-
import json
import math
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Union

from core.pace import format_pace_range, speed_mps_from_pace_min


@dataclass
class FaceCheckConfig:
    """人脸抽检配置模型"""
    enable: bool = False
    check_point_min: int = 0
    check_point_max: int = 0
    check_run_valid_time: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api_data(cls, data: Dict[str, Any]) -> "FaceCheckConfig":
        if not data:
            return cls()
        return cls(
            enable=bool(data.get("enable", False)),
            check_point_min=int(data.get("checkPointMin", 0)),
            check_point_max=int(data.get("checkPointMax", 0)),
            check_run_valid_time=int(data.get("checkRunValidTime", 0)),
            raw=data
        )


@dataclass
class RunRule:
    """单次与单日跑步规则模型"""
    day_goal: int
    min_distance: int
    speed_top: str
    speed_bottom: str
    step_top: Union[int, str]
    step_bottom: Union[int, str]
    valid_times: List[str]
    face_check: FaceCheckConfig
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api_data(cls, data: Dict[str, Any]) -> "RunRule":
        rule = data.get("runRuleModel", {}) or {}
        face = data.get("runFaceCheckConfigDTO", {}) or {}
        
        v_times = []
        for vt in rule.get("validTime", []):
            st = vt.get("start", "")
            et = vt.get("end", "")
            if st and et:
                v_times.append(f"{st}~{et}")

        s_top = rule.get("stepTop", "-")
        s_bot = rule.get("stepBottom", "-")
        step_top_text = "不限" if s_top in (0, "0") else s_top
        step_bot_text = "不限" if s_bot in (0, "0") else s_bot

        return cls(
            day_goal=int(rule.get("dayGoal", 0)),
            min_distance=int(rule.get("minDistance", 0)),
            speed_top=str(rule.get("speedTop", "-")),
            speed_bottom=str(rule.get("speedBottom", "-")),
            step_top=step_top_text,
            step_bottom=step_bot_text,
            valid_times=v_times,
            face_check=FaceCheckConfig.from_api_data(face),
            raw=data
        )

    def speed_range_text(self) -> str:
        """配速区间文案：`2′00″~10′00″/km（速度 1.67~8.33 m/s）`。

        配速部分走 :func:`core.pace.format_pace_range`，与 App 记录页、服务端
        拒绝文案（「配速控制在2′00″-10′00″」）同一套写法，不再出现「2~10 分/km」
        这种只有区间才用的表述。

        speedTop / speedBottom 的数值就是**配速（分/公里）**，与服务端拒绝时的
        文案一一对应（speedTop=2.0、speedBottom=10.0）。实测依据：同账号 20 条
        记录里，服务端保存的 speed 字段与 `时长/(距离/1000)/60` 的差都小于 0.005。

        注：这两个字段的字面名与大小关系是反的（speedTop=2.0 反而最小），
        所以按数值取 min/max，不依赖字段名。
        """
        values = []
        for raw in (self.speed_top, self.speed_bottom):
            try:
                value = float(str(raw).strip())
            except (TypeError, ValueError):
                continue
            if value > 0:
                values.append(value)
        if not values:
            return "不限"
        fast, slow = min(values), max(values)
        pace = format_pace_range(fast, slow)
        if fast == slow:
            return f"{pace}（速度 {speed_mps_from_pace_min(fast):.2f} m/s）"
        return (f"{pace}（速度 {speed_mps_from_pace_min(slow):.2f}~"
                f"{speed_mps_from_pace_min(fast):.2f} m/s）")


@dataclass
class GeoPoint:
    """经纬度坐标点"""
    lat: float
    lng: float
    point_id: Optional[int] = None
    name: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api_data(cls, data):
        if not isinstance(data, dict):
            raise ValueError("point must be an object")

        # 优先读取 GCJ-02 坐标（glat/glon），如果不存在再读 WGS-84（lat/lon）
        lat = data.get("glat", data.get("lat", data.get("latitude")))
        lon = data.get("glon", data.get("lng", data.get("lon", data.get("longitude"))))

        # 检测经纬度是否被调换（纬度范围 -90~90，经度范围 -180~180）
        if lat is not None and lon is not None:
            try:
                lat_val, lon_val = float(lat), float(lon)
                # 如果 lat 超出 -90~90 但在 -180~180 内，且 lon 在 -90~90 内，说明调换了
                if not (-90 <= lat_val <= 90) and (-180 <= lat_val <= 180) and (-90 <= lon_val <= 90):
                    lat_val, lon_val = lon_val, lat_val  # 交换回来
                lat, lon = lat_val, lon_val
            except (TypeError, ValueError):
                pass

        if isinstance(lat, bool) or isinstance(lon, bool):
            raise ValueError("point coordinates must be numbers")
        lat, lon = float(lat), float(lon)
        if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("point coordinates are out of range")
        return cls(lat=lat, lng=lon, point_id=data.get("id"),
                   name=data.get("pointName", data.get("name")), raw=dict(data))

    def gcj_coordinates(self):
        lat = self.raw.get("glat", self.raw.get("gLat"))
        lon = self.raw.get("glon", self.raw.get("glng", self.raw.get("gLng")))
        if lat is None or lon is None or lat == -1 or lon == -1:
            raise ValueError("围栏未返回 GCJ 坐标，无法构造官方点位请求")
        point = GeoPoint.from_api_data({"lat": lat, "lon": lon})
        return point.lat, point.lng

    def as_checkpoint(self):
        # self.lat/self.lng 是 GCJ-02 值（from_api_data 优先读 glat/glon）。
        # 结算报文里的 lat/lon 必须回传官方下发的 WGS-84 原值，
        # 而 lng 是内部用的「规划坐标」，统一给 GCJ 值供轨迹规划/合规校验。
        values = dict(self.raw)
        values.update({"lat": self.lat, "lng": self.lng, "name": self.name})
        # 报文原值优先：lat/lon 若在原始报文里存在，一律以原值为准
        for key in ("lat", "lon"):
            if key in self.raw:
                values[key] = self.raw[key]
        return values


@dataclass
class GeoFenceArea:
    """学校电子围栏与必经打卡点模型"""
    fence_id: int
    fence_name: str
    polygon_points: List[GeoPoint]
    check_points: List[GeoPoint]
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api_data(cls, data: Dict[str, Any]) -> "GeoFenceArea":
        def parse(field_name):
            values = data.get(field_name) or []
            if isinstance(values, str):
                values = json.loads(values)
            if not isinstance(values, list):
                raise ValueError(f"{field_name} must be a list")

            return [GeoPoint.from_api_data(point) for point in values]

        poly = parse("points")
        # Legacy compatibility only: live checkpoints come from pointsResModels.
        checks = parse("passPoints")

        return cls(
            fence_id=int(data.get("id", 0)),
            fence_name=str(data.get("name", "未命名围栏")),
            polygon_points=poly,
            check_points=checks,
            raw=data
        )


@dataclass
class SemesterInfo:
    """学期跑步进度模型"""
    semester_name: str
    total_completed_distance: float
    valid_count: int
    target_distance: float
    remaining_distance: float
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api_data(cls, data: Dict[str, Any]) -> "SemesterInfo":
        tot = float(data.get("totalDistance", 0.0))
        tar = float(data.get("targetDistance", 0.0) or data.get("goalDistance", 0.0))
        rem = max(0.0, tar - tot)
        return cls(
            semester_name=str(data.get("semesterName", "")),
            total_completed_distance=tot,
            valid_count=int(data.get("validCount", 0)),
            target_distance=tar,
            remaining_distance=rem,
            raw=data
        )
