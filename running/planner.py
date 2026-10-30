# -*- coding: utf-8 -*-
"""围栏内轨迹规划器。

在官方打卡点的基础上,把途经点扩展成一条「整体留在电子围栏内」的
安全环线:在围栏内缩安全环与必经打卡点之间做最近邻串联,并保证

- 所有途经点都落在围栏内(打卡点若本身在围栏外则原样保留并告警,
  因为官方坐标不可篡改);
- 相邻途经点之间的连线也在围栏内(对凸围栏成立;对轻微凹多边形,
  通过采样检查并对越界线段做绕行修正)。

配合 running/realism.PaceProfile 即可生成「围栏内合规 + 非匀速」的轨迹。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .geometry import (
    haversine,
    inset_polygon,
    nearest_neighbor_order,
    point_in_polygon,
    polygon_area_m2,
    polygon_centroid,
    segment_points,
)


@dataclass
class PlannedRoute:
    """规划结果。"""

    waypoints: List[Tuple[float, float]] = field(default_factory=list)
    checkpoint_waypoints: List[Tuple[float, float]] = field(default_factory=list)
    fence_area_m2: float = 0.0
    route_length_m: float = 0.0
    warnings: List[str] = field(default_factory=list)
    adjusted_segments: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "waypoint_count": len(self.waypoints),
            "checkpoint_count": len(self.checkpoint_waypoints),
            "fence_area_m2": round(self.fence_area_m2, 2),
            "route_length_m": round(self.route_length_m, 2),
            "adjusted_segments": self.adjusted_segments,
            "warnings": list(self.warnings),
        }


class FenceRoutePlanner:
    """围栏内安全环线规划器。"""

    def __init__(self,
                 boundary_margin_m: float = 8.0,
                 detour_step_m: float = 5.0,
                 max_detour_attempts: int = 6):
        self.boundary_margin_m = float(boundary_margin_m)
        self.detour_step_m = float(detour_step_m)
        self.max_detour_attempts = int(max_detour_attempts)

    def plan(self,
             fence_polygon: Sequence[Sequence[float]],
             checkpoints: Optional[Sequence[Sequence[float]]] = None,
             start_point: Optional[Sequence[float]] = None,
             extra_lap: bool = True,
             boundary_lap: bool = False,
             boundary_margin_m: Optional[float] = None,
             target_distance_m: Optional[float] = None) -> PlannedRoute:
        """规划途经点序列。

        参数：
        - fence_polygon: 围栏多边形经纬度点；
        - checkpoints: 官方必经打卡点；
        - start_point: 起跑点，缺省用围栏质心；
        - extra_lap: 是否在打卡点串联后再回到起点形成闭环；
        - boundary_lap: 是否沿围栏内缩边界补充途经点，让长距离跑有足够里程；
        - boundary_margin_m: 边界内缩量（米），缺省用构造参数；
        - target_distance_m: 目标总里程，用于校验环线长度是否可达全部打卡点。
        """
        if not fence_polygon or len(fence_polygon) < 3:
            raise ValueError("围栏多边形至少需要 3 个顶点")

        polygon = [(float(p[0]), float(p[1])) for p in fence_polygon]
        area = polygon_area_m2(polygon)
        warnings: List[str] = []

        # 起点：缺省用围栏质心，保证在围栏内部
        if start_point is None:
            anchor = polygon_centroid(polygon)
        else:
            anchor = (float(start_point[0]), float(start_point[1]))
        if not point_in_polygon(anchor, polygon):
            warnings.append("起跑点不在围栏内，已回退到围栏质心")
            anchor = polygon_centroid(polygon)

        # 必经打卡点：官方坐标不可篡改，原样保留
        cp_points: List[Tuple[float, float]] = []
        for cp in checkpoints or []:
            cp_points.append((float(cp["lat"]), float(cp["lng"])))

        # 途经点 = 起点 + 打卡点（按最近邻串联）
        # 多个候选起点分别串联，取总长最短的一组，避免环线过长导致
        # 尾部打卡点落在目标里程之外而无法命中。
        route: List[Tuple[float, float]] = []
        if cp_points:
            best: Optional[List[Tuple[float, float]]] = None
            best_len = float("inf")
            for candidate in cp_points:
                ordered = self._nearest_neighbor_chain(candidate, cp_points)
                chain = [anchor] + ordered
                chain_len = self._chain_length(chain)
                if chain_len < best_len:
                    best_len = chain_len
                    best = chain
            route = best or [anchor]
        else:
            route = [anchor]

        # 边界环线：沿围栏内缩边界补充途经点，扩展路线总长
        if boundary_lap:
            margin = self.boundary_margin_m if boundary_margin_m is None else float(boundary_margin_m)
            boundary = inset_polygon(polygon, margin)
            # 起点附近的边界点先入路线，随后按最近邻串联其余边界点
            boundary = [p for p in boundary if haversine(p, anchor) > 1.0]
            for point in boundary:
                route.append(point)

        # 闭合：回到起点形成环线（自由跑/长距离需要）
        if extra_lap and len(route) > 1:
            route.append(anchor)

        # 围栏内修正：逐段检查，越界则尝试绕行
        adjusted = 0
        fixed_route: List[Tuple[float, float]] = [route[0]]
        for a, b in zip(route, route[1:]):
            detour, changed = self._fix_segment(a, b, polygon)
            if changed:
                adjusted += 1
            fixed_route.extend(detour)
        route = fixed_route

        # 打卡点仍必须出现在最终途经点中（绕行不得吞掉打卡点）
        for cp in cp_points:
            if not any(haversine(cp, wp) <= 1.0 for wp in route):
                warnings.append(f"绕行后打卡点 ({cp[0]:.6f},{cp[1]:.6f}) 偏离途经点超过 1m")

        # 环线闭合校验：绕行后首尾可能不再重合，需补一条回到起点的段
        if extra_lap and len(route) > 1 and haversine(route[0], route[-1]) > 1.0:
            closing, changed = self._fix_segment(route[-1], route[0], polygon)
            if changed:
                adjusted += 1
            route.extend(closing[1:])

        length = 0.0
        for a, b in zip(route, route[1:]):
            length += haversine(a, b)

        # 里程可行性：环线总长必须不超过目标里程，否则尾部打卡点无法命中
        if target_distance_m and cp_points:
            if length > float(target_distance_m):
                raise ValueError(
                    f"围栏内环线长度 {length:.1f}m 超过目标里程 {target_distance_m}m，"
                    f"无法在里程内覆盖 {len(cp_points)} 个必经打卡点")

        return PlannedRoute(
            waypoints=route,
            checkpoint_waypoints=cp_points,
            fence_area_m2=area,
            route_length_m=length,
            warnings=warnings,
            adjusted_segments=adjusted,
        )

    def _nearest_neighbor_chain(self, start: Tuple[float, float],
                                points: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
        """从 start 出发对 points 做最近邻串联。"""
        chain = [start]
        remaining = [p for p in points if p != start]
        while remaining:
            order = nearest_neighbor_order(chain[-1], remaining)
            chain.append(remaining.pop(order[0]))
        return chain

    @staticmethod
    def _chain_length(chain: Sequence[Tuple[float, float]]) -> float:
        total = 0.0
        for a, b in zip(chain, chain[1:]):
            total += haversine(a, b)
        return total

    def _fix_segment(self, a: Sequence[float], b: Sequence[float],
                     polygon: Sequence[Sequence[float]]) -> Tuple[List[Tuple[float, float]], bool]:
        """检查线段 a->b 是否越界；越界则尝试经围栏内部中点绕行。

        返回 (途经点片段, 是否发生了绕行)。绕行时插入一个位于围栏内部的
        中点，把原本越界的直线段拆成两段都在围栏内的线段。
        """
        if self._segment_inside(a, b, polygon):
            return [(float(a[0]), float(a[1]))], False

        # 尝试绕行：取线段中点法向两侧的候选点，找一个使两段都在围栏内的
        mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        for attempt in range(1, self.max_detour_attempts + 1):
            # 逐步加大的法向偏移距离，覆盖轻微到明显的越界情形
            offset_m = attempt * self.detour_step_m
            for sign in (1.0, -1.0):
                candidate = self._normal_offset(mid, a, b, offset_m * sign)
                if candidate is None:
                    continue
                if not point_in_polygon(candidate, polygon):
                    continue
                if (self._segment_inside(a, candidate, polygon)
                        and self._segment_inside(candidate, b, polygon)):
                    return [(float(a[0]), float(a[1])), candidate], True
        # 无法绕行：保留原始线段，由调用方与合规自检兜底
        return [(float(a[0]), float(a[1]))], False

    @staticmethod
    def _normal_offset(mid: Tuple[float, float], a: Sequence[float], b: Sequence[float],
                       distance_m: float) -> Optional[Tuple[float, float]]:
        """把线段中点沿法向平移 distance_m（米）。"""
        import math as _math
        # 转成局部米制后做法向偏移
        scale_lat = 111320.0
        scale_lon = 111320.0 * _math.cos(_math.radians(mid[0]))
        dx = (b[0] - a[0]) * scale_lat
        dy = (b[1] - a[1]) * scale_lon
        norm = _math.hypot(dx, dy)
        if norm < 1e-9:
            return None
        nx, ny = -dy / norm, dx / norm
        new_lat = mid[0] + (ny * distance_m) / scale_lat
        new_lon = mid[1] + (nx * distance_m) / scale_lon
        if not (-90 <= new_lat <= 90 and -180 <= new_lon <= 180):
            return None
        return (new_lat, new_lon)

    def _segment_inside(self, a: Sequence[float], b: Sequence[float],
                        polygon: Sequence[Sequence[float]]) -> bool:
        """采样检查整条线段是否都落在围栏内。"""
        for point in segment_points(a, b, self.detour_step_m):
            if not point_in_polygon(point, polygon):
                return False
        return True
