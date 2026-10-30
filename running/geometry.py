# -*- coding: utf-8 -*-
"""校园尺度的平面几何工具。

校园围栏与轨迹点都集中在几百米量级，因此采用以围栏中心为原点的
等距圆柱（equirectangular）局部平面投影：在该尺度下米制误差远小于
GPS 本身数米的定位误差，足够做「点是否在围栏内」「轨迹是否出界」
这类判定，且完全不依赖第三方库。
"""
import math
from typing import List, Sequence, Tuple

EARTH_RADIUS_M = 6371000.0

Point = Tuple[float, float]


def _validate_point(point: Sequence[float]) -> Point:
    if len(point) < 2:
        raise ValueError("point must contain latitude and longitude")
    lat, lon = float(point[0]), float(point[1])
    if not (math.isfinite(lat) and math.isfinite(lon)):
        raise ValueError("point coordinates are invalid")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("point coordinates are out of range")
    return lat, lon


def local_origin(points: Sequence[Sequence[float]]) -> Point:
    """取点集的算术中心作为局部投影原点。"""
    if not points:
        raise ValueError("point set is empty")
    lats = [float(p[0]) for p in points]
    lons = [float(p[1]) for p in points]
    return sum(lats) / len(lats), sum(lons) / len(lons)


def to_local(point: Sequence[float], origin: Point) -> Point:
    """经纬度 -> 以 origin 为原点的米制局部坐标 (x 东, y 北)。"""
    lat, lon = _validate_point(point)
    x = math.radians(lon - origin[1]) * EARTH_RADIUS_M * math.cos(math.radians(origin[0]))
    y = math.radians(lat - origin[0]) * EARTH_RADIUS_M
    return x, y


def to_geo(local: Point, origin: Point) -> Point:
    """米制局部坐标 -> 经纬度。"""
    x, y = float(local[0]), float(local[1])
    lon = origin[1] + math.degrees(x / (EARTH_RADIUS_M * math.cos(math.radians(origin[0]))))
    lat = origin[0] + math.degrees(y / EARTH_RADIUS_M)
    return lat, lon


def haversine(a: Sequence[float], b: Sequence[float]) -> float:
    """两点大圆距离（米）。"""
    lat1, lon1 = _validate_point(a)
    lat2, lon2 = _validate_point(b)
    h = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(max(0.0, h))))


def polygon_area_m2(polygon: Sequence[Sequence[float]]) -> float:
    """多边形面积（平方米），使用局部平面投影的鞋带公式。"""
    if len(polygon) < 3:
        return 0.0
    origin = local_origin(polygon)
    pts = [to_local(p, origin) for p in polygon]
    total = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def polygon_centroid(polygon: Sequence[Sequence[float]]) -> Point:
    """多边形质心（退化时回退到点集算术中心）。"""
    if len(polygon) < 3:
        return local_origin(polygon)
    origin = local_origin(polygon)
    pts = [to_local(p, origin) for p in polygon]
    area2 = 0.0
    cx = 0.0
    cy = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        cross = x1 * y2 - x2 * y1
        area2 += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if abs(area2) < 1e-9:
        return origin
    lx = cx / (3.0 * area2)
    ly = cy / (3.0 * area2)
    return to_geo((lx, ly), origin)


def point_in_polygon(point: Sequence[float], polygon: Sequence[Sequence[float]]) -> bool:
    """射线交叉法判断点是否落在多边形内部（含边界）。"""
    if len(polygon) < 3:
        return False
    origin = local_origin(polygon)
    x, y = to_local(point, origin)
    pts = [to_local(p, origin) for p in polygon]
    inside = False
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        # 边界判定（容差 1 厘米）
        if _point_segment_distance((x, y), (x1, y1), (x2, y2)) <= 0.01:
            return True
        if (y1 > y) != (y2 > y):
            x_cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x_cross > x:
                inside = not inside
    return inside


def _point_segment_distance(p: Point, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def distance_point_to_segment(point: Sequence[float],
                              a: Sequence[float],
                              b: Sequence[float]) -> float:
    """点到线段的最小距离（米）。"""
    origin = local_origin([point, a, b])
    p = to_local(point, origin)
    pa = to_local(a, origin)
    pb = to_local(b, origin)
    return _point_segment_distance(p, pa, pb)


def segment_points(a: Sequence[float], b: Sequence[float], step_m: float = 2.0) -> List[Point]:
    """在两点间按固定步长采样（含端点），用于出界检测。

    采用球面线性插值，保证采样点落在大圆弧上而非直线。
    """
    pa = _validate_point(a)
    pb = _validate_point(b)
    length = haversine(pa, pb)
    if length <= 0:
        return [pa]
    count = max(1, int(math.ceil(length / max(0.1, step_m))))
    angle = length / EARTH_RADIUS_M
    if abs(math.sin(angle)) < 1e-12:
        return [pa, pb]
    lat1, lon1 = map(math.radians, pa)
    lat2, lon2 = map(math.radians, pb)
    xyz = [
        (math.cos(lat1) * math.cos(lon1), math.cos(lat1) * math.sin(lon1), math.sin(lat1)),
        (math.cos(lat2) * math.cos(lon2), math.cos(lat2) * math.sin(lon2), math.sin(lat2)),
    ]
    samples: List[Point] = []
    for i in range(count + 1):
        fraction = i / count
        wa = math.sin((1 - fraction) * angle) / math.sin(angle)
        wb = math.sin(fraction * angle) / math.sin(angle)
        x, y, z = (wa * xyz[0][k] + wb * xyz[1][k] for k in range(3))
        samples.append((math.degrees(math.atan2(z, math.hypot(x, y))),
                        math.degrees(math.atan2(y, x))))
    return samples


def convex_hull(points: Sequence[Sequence[float]]) -> List[Point]:
    """Andrew 单调链凸包，返回逆时针顺序的经纬度点集。"""
    pts = sorted({(round(float(p[0]), 9), round(float(p[1]), 9)) for p in points})
    if len(pts) <= 2:
        return list(pts)

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: List[Point] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: List[Point] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def inset_polygon(polygon: Sequence[Sequence[float]],
                  margin_m: float = 5.0) -> List[Point]:
    """把多边形沿质心方向内缩 margin_m，得到安全内环。

    实现方式：把每个顶点沿「顶点 -> 质心」方向平移 margin_m，
    再按原顶点顺序重排。对凸围栏效果良好；对凹多边形结果仍是
    围栏的子集，可安全用于「不出界」规划。
    """
    if len(polygon) < 3:
        return list(polygon)
    centroid = polygon_centroid(polygon)
    origin = local_origin(list(polygon) + [centroid])
    cx, cy = to_local(centroid, origin)
    result: List[Point] = []
    for point in polygon:
        px, py = to_local(point, origin)
        dx, dy = cx - px, cy - py
        norm = math.hypot(dx, dy)
        if norm < 1e-6:
            result.append(_validate_point(point))
            continue
        scale = max(0.0, (norm - margin_m)) / norm
        result.append(to_geo((px + dx * scale, py + dy * scale), origin))
    return result


def nearest_neighbor_order(anchor: Sequence[float],
                           points: Sequence[Sequence[float]]) -> List[int]:
    """最近邻排序，返回相对 anchor 的访问顺序下标。"""
    if not points:
        return []
    remaining = set(range(len(points)))
    order: List[int] = []
    current = _validate_point(anchor)
    while remaining:
        nxt = min(remaining, key=lambda i: haversine(current, points[i]))
        order.append(nxt)
        remaining.discard(nxt)
        current = _validate_point(points[nxt])
    return order


def path_length(points: Sequence[Sequence[float]], closed: bool = False) -> float:
    """折线路径总长（米）。"""
    if len(points) < 2:
        return 0.0
    total = sum(haversine(a, b) for a, b in zip(points, points[1:]))
    if closed:
        total += haversine(points[-1], points[0])
    return total


def closest_point_on_path(point: Sequence[float],
                          path: Sequence[Sequence[float]]) -> Tuple[float, int]:
    """点到折线的最近距离（米）与最近线段下标。"""
    if len(path) < 2:
        if not path:
            raise ValueError("path is empty")
        return haversine(point, path[0]), 0
    best = float("inf")
    best_index = 0
    for i, (a, b) in enumerate(zip(path, path[1:])):
        d = distance_point_to_segment(point, a, b)
        if d < best:
            best = d
            best_index = i
    return best, best_index
