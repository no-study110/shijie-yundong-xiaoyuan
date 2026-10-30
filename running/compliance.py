# -*- coding: utf-8 -*-
"""围栏与打卡点合规校验器。

对一条已合成的轨迹给出「是否合规」的离线判定，避免把明显出界、
未经过必经打卡点、里程/时长越界的轨迹提交到网关再被服务端驳回。

覆盖四类校验：
1. 围栏包含性 —— 轨迹是否整体落在电子围栏多边形内；
2. 打卡点命中 —— 是否逐个经过必经打卡点（含容差半径）；
3. 总量约束 —— 里程/时长/步数是否满足学校规则与协议下限；
4. 采样合法性 —— 时间戳严格递增、速度非负、区间覆盖完整。

所有判定均为纯本地计算，不依赖网络。
"""
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from core.pace import format_pace, pace_min_from_speed_mps

from .geometry import (
    closest_point_on_path,
    haversine,
    point_in_polygon,
    polygon_area_m2,
)
from .models import GpsPoint


@dataclass
class CheckpointHit:
    """单个必经打卡点的命中情况。"""

    name: str
    lat: float
    lng: float
    hit: bool
    distance_m: float
    index: Optional[int] = None
    timestamp: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "lat": self.lat,
            "lng": self.lng,
            "hit": self.hit,
            "distance_m": round(self.distance_m, 3),
            "index": self.index,
            "timestamp": self.timestamp,
        }


@dataclass
class ComplianceReport:
    """一次合规校验的完整结论。"""

    compliant: bool
    issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    inside_fence: bool = True
    fence_area_m2: float = 0.0
    checkpoint_hits: List[CheckpointHit] = field(default_factory=list)
    checkpoint_hit_count: int = 0
    checkpoint_total: int = 0
    measured_distance_m: float = 0.0
    measured_time_sec: int = 0
    max_speed_mps: float = 0.0
    avg_speed_mps: float = 0.0
    steps: Optional[int] = None
    avg_step_freq: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "compliant": self.compliant,
            "issues": list(self.issues),
            "warnings": list(self.warnings),
            "inside_fence": self.inside_fence,
            "fence_area_m2": round(self.fence_area_m2, 2),
            "checkpoint_hit_count": self.checkpoint_hit_count,
            "checkpoint_total": self.checkpoint_total,
            "checkpoint_hits": [h.to_dict() for h in self.checkpoint_hits],
            "measured_distance_m": round(self.measured_distance_m, 3),
            "measured_time_sec": self.measured_time_sec,
            "max_speed_mps": round(self.max_speed_mps, 3),
            "avg_speed_mps": round(self.avg_speed_mps, 3),
            "steps": self.steps,
            "avg_step_freq": self.avg_step_freq,
        }

    def summary(self) -> str:
        """一行式中文摘要，供 CLI 直接打印。"""
        if self.compliant:
            return (f"合规 围栏内=是 打卡点={self.checkpoint_hit_count}/{self.checkpoint_total} "
                    f"里程={self.measured_distance_m:.1f}m 时长={self.measured_time_sec}s")
        return "不合规 " + "; ".join(self.issues)


class ComplianceValidator:
    """轨迹合规校验器。"""

    def __init__(self,
                 checkpoint_tolerance_m: float = 15.0,
                 speed_limit_mps: Optional[float] = None,
                 min_speed_mps: Optional[float] = None,
                 min_distance_m: Optional[int] = None,
                 min_time_sec: Optional[int] = None,
                 step_freq_range: Optional[Sequence[int]] = None):
        self.checkpoint_tolerance_m = float(checkpoint_tolerance_m)
        self.speed_limit_mps = speed_limit_mps
        self.min_speed_mps = min_speed_mps
        self.min_distance_m = min_distance_m
        self.min_time_sec = min_time_sec
        self.step_freq_range = tuple(step_freq_range) if step_freq_range else None

    def validate(self,
                 points: Sequence[GpsPoint],
                 fence_polygon: Optional[Sequence[Sequence[float]]] = None,
                 checkpoints: Optional[Sequence[Sequence[float]]] = None,
                 steps: Optional[int] = None,
                 total_distance_m: Optional[int] = None,
                 total_time_sec: Optional[int] = None) -> ComplianceReport:
        """对轨迹执行全部校验并返回报告。"""
        issues: List[str] = []
        warnings: List[str] = []
        hits: List[CheckpointHit] = []

        if len(points) < 2:
            return ComplianceReport(compliant=False, issues=["轨迹点少于 2 个，无法构成有效跑步"])

        # 1. 时间戳与速度合法性
        for a, b in zip(points, points[1:]):
            if not isinstance(b.time, int) or b.time <= a.time:
                issues.append("轨迹时间戳未严格递增")
                break
            if not math.isfinite(b.speed) or b.speed < 0:
                issues.append("轨迹存在负速度或非法速度")
                break

        measured_distance = sum(haversine((a.lat, a.lon), (b.lat, b.lon))
                                for a, b in zip(points, points[1:]))
        measured_time_sec = (points[-1].time - points[0].time) // 1000

        # 2. 围栏包含性
        inside_fence = True
        fence_area = 0.0
        if fence_polygon and len(fence_polygon) >= 3:
            fence_area = polygon_area_m2(fence_polygon)
            outside = [p for p in points if not point_in_polygon((p.lat, p.lon), fence_polygon)]
            if outside:
                inside_fence = False
                issues.append(f"轨迹有 {len(outside)} 个采样点位于电子围栏之外")
        elif fence_polygon:
            warnings.append("围栏多边形点数不足 3，已跳过围栏包含性校验")

        # 3. 打卡点命中
        if checkpoints:
            for cp in checkpoints:
                # 轨迹跑在 GCJ-02 坐标系里，校验必须优先用 glat/glon；
                # 原始报文里的 lat/lon 是 WGS-84，直接比对会差 500~700m。
                lat = float(cp.get("glat", cp["lat"]))
                lng = float(cp.get("glon", cp.get("glng", cp["lng"])))
                name = str(cp.get("name") or cp.get("pointName") or "")
                dist, seg_index = closest_point_on_path((lat, lng), [(p.lat, p.lon) for p in points])
                hit = dist <= self.checkpoint_tolerance_m
                timestamp = None
                index = None
                if hit:
                    # 找到离打卡点最近的轨迹采样点下标
                    best = min(range(len(points)),
                               key=lambda i: haversine((lat, lng), (points[i].lat, points[i].lon)))
                    if haversine((lat, lng), (points[best].lat, points[best].lon)) <= self.checkpoint_tolerance_m:
                        index = best
                        timestamp = points[best].time
                    else:
                        # 落在两点之间的线段上：取线段两端时间的较早者
                        index = min(seg_index, len(points) - 1)
                        timestamp = points[index].time
                hits.append(CheckpointHit(name=name, lat=lat, lng=lng, hit=hit,
                                          distance_m=dist, index=index, timestamp=timestamp))
            missed = [h for h in hits if not h.hit]
            if missed:
                issues.append("未经过必经打卡点: "
                              + ", ".join(f"{h.name or '未命名'}(偏差{h.distance_m:.1f}m)" for h in missed))

        # 4. 总量约束
        if total_distance_m is not None:
            drift = abs(measured_distance - total_distance_m)
            if drift > max(0.5, total_distance_m * 0.001):
                issues.append(f"实测里程 {measured_distance:.2f}m 与上报 totalDis {total_distance_m}m 偏差过大")
        if self.min_distance_m and measured_distance < self.min_distance_m:
            issues.append(f"实测里程 {measured_distance:.1f}m 低于规则最低距离 {self.min_distance_m}m")
        if total_time_sec is not None:
            drift = abs(measured_time_sec - total_time_sec)
            if drift > 1:
                issues.append(f"实测时长 {measured_time_sec}s 与上报 totalTime {total_time_sec}s 不一致")
        if self.min_time_sec and measured_time_sec < self.min_time_sec:
            issues.append(f"实测时长 {measured_time_sec}s 低于规则最低时长 {self.min_time_sec}s")

        # 5. 速度与步频
        speeds = [p.speed for p in points if math.isfinite(p.speed)]
        max_speed = max(speeds) if speeds else 0.0
        avg_speed = measured_distance / measured_time_sec if measured_time_sec else 0.0
        if self.speed_limit_mps is not None and max_speed > self.speed_limit_mps:
            issues.append(f"峰值速度 {max_speed:.2f}m/s 超过规则上限 {self.speed_limit_mps}m/s")
        if self.min_speed_mps is not None and avg_speed > 0 and avg_speed < self.min_speed_mps:
            # 服务端判的是**平均速度**：实测 2.001m/s 达标、1.872m/s 未达标，
            # 分界线正好压在规则下界上。不能改成逐点判 —— 任何真实轨迹的点间速度
            # 都有 GPS 漂移带来的起伏（实测 p5≈1.3m/s），逐点判会把官方用户也拦掉。
            issues.append(f"平均速度 {avg_speed:.3f}m/s 低于规则下限 "
                          f"{self.min_speed_mps:.2f}m/s"
                          f"（配速 {format_pace(pace_min=pace_min_from_speed_mps(avg_speed))}）")

        avg_step_freq = None
        if steps is not None and measured_time_sec > 0:
            avg_step_freq = round(steps * 60.0 / measured_time_sec)
            if self.step_freq_range:
                lo, hi = self.step_freq_range
                if avg_step_freq < lo or avg_step_freq > hi:
                    issues.append(f"平均步频 {avg_step_freq} 步/分 超出规则区间 {lo}~{hi}")

        return ComplianceReport(
            compliant=not issues,
            issues=issues,
            warnings=warnings,
            inside_fence=inside_fence,
            fence_area_m2=fence_area,
            checkpoint_hits=hits,
            checkpoint_hit_count=sum(1 for h in hits if h.hit),
            checkpoint_total=len(hits),
            measured_distance_m=measured_distance,
            measured_time_sec=int(measured_time_sec),
            max_speed_mps=max_speed,
            avg_speed_mps=avg_speed,
            steps=steps,
            avg_step_freq=avg_step_freq,
        )
