"""Local geometry and totals checks; no network or protocol assumptions."""
import bisect
import math

EARTH_RADIUS_M = 6371000.0
# 轨迹相邻采样点的最小时间间隔（毫秒）。顶点时刻由累计里程换算而来，通常是
# 2999 这种非整秒值，会和 3 秒网格上的采样点只差几毫秒；两者空间上几乎重合，
# 留在一起会让瞬时速度算出几十上百 m/s。
MIN_SAMPLE_GAP_MS = 300


def coordinate(lat, lon):
    if isinstance(lat, bool) or isinstance(lon, bool):
        raise ValueError("coordinates must be finite numbers")
    lat, lon = float(lat), float(lon)
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("coordinates are out of range")
    return lat, lon


def distance(a, b):
    lat1, lon1 = map(math.radians, coordinate(*a))
    lat2, lon2 = map(math.radians, coordinate(*b))
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return EARTH_RADIUS_M * 2 * math.asin(min(1.0, math.sqrt(max(0.0, h))))


def interpolate(a, b, fraction):
    if fraction <= 0:
        return a
    if fraction >= 1:
        return b
    angle = distance(a, b) / EARTH_RADIUS_M
    if angle < 1e-12:
        return a
    if abs(math.sin(angle)) < 1e-12:
        raise ValueError("antipodal route points cannot be interpolated")
    xyz = []
    for lat, lon in (a, b):
        lat, lon = math.radians(lat), math.radians(lon)
        xyz.append((math.cos(lat)*math.cos(lon), math.cos(lat)*math.sin(lon), math.sin(lat)))
    wa, wb = math.sin((1-fraction)*angle)/math.sin(angle), math.sin(fraction*angle)/math.sin(angle)
    x, y, z = (wa*xyz[0][i] + wb*xyz[1][i] for i in range(3))
    return math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))


def validate_totals(seconds, steps, start, stop, meters):
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
        raise ValueError("duration must be a positive integer")
    if isinstance(steps, bool) or not isinstance(steps, int) or steps < 0:
        raise ValueError("steps must be a nonnegative integer")
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (start, stop)) or stop-start != seconds*1000:
        raise ValueError("timestamps do not match totalTime")
    if isinstance(meters, bool) or not isinstance(meters, (int, float)) or not math.isfinite(meters) or meters <= 0:
        raise ValueError("distance must be a finite positive number")


# --------------------------------------------------------------------------- #
# 阶段 0：输入整理
# --------------------------------------------------------------------------- #

def _dedupe_route(waypoints) -> list:
    """去掉重复点与首尾重合点，返回至少两个不同点的路线。

    顶点重复会让步行循环除零，所以在这里统一挡掉。
    """
    route = []
    for point in waypoints:
        point = coordinate(*point)
        if not route or distance(route[-1], point) > 1e-6:
            route.append(point)
    if len(route) > 1 and distance(route[0], route[-1]) <= 1e-6:
        route.pop()
    if len(route) < 2:
        raise ValueError("at least two distinct route points are required")
    return route


# --------------------------------------------------------------------------- #
# 阶段 1：采样（纯几何 —— 给定路线与里程，产出带时刻的采样点）
# --------------------------------------------------------------------------- #

def _walk_route(route, meters):
    """沿路线走满 meters 米，返回 (顶点序列, 累计里程)。

    闭环短于目标里程时会绕回起点继续走（取模前进），这正是
    「闭环不足靠自然绕圈补足」的实现位置 —— 不要在别处人为插环。
    """
    vertices = [route[0]]
    cumulative = [0.0]
    edge = 0
    while cumulative[-1] < meters - 1e-8:
        a, b = route[edge % len(route)], route[(edge+1) % len(route)]
        length = distance(a, b)
        remaining = meters-cumulative[-1]
        if length >= remaining:
            vertices.append(interpolate(a, b, remaining/length))
            cumulative.append(float(meters))
        else:
            vertices.append(b)
            cumulative.append(cumulative[-1]+length)
        edge += 1
        if edge > 100000:
            raise ValueError("route has too many segments")
    return vertices, cumulative


def _sample_by_time(vertices, cumulative, meters, seconds, pace):
    """按时间均匀（或按配速曲线）取样，并把顶点钉进采样表。

    顶点必须作为采样点存在，否则插值可能从拐角内侧切过去，
    打卡点就不在轨迹上了。
    """
    duration_ms = seconds*1000
    samples = {}
    for elapsed in range(0, duration_ms, 3000):
        if pace is None:
            traveled = meters*elapsed/duration_ms
        else:
            traveled = meters*pace.forward(elapsed/duration_ms)
        index = min(len(vertices)-2, max(0, bisect.bisect_right(cumulative, traveled)-1))
        fraction = (traveled-cumulative[index])/(cumulative[index+1]-cumulative[index])
        samples[elapsed] = interpolate(vertices[index], vertices[index+1], fraction)
    corner_times = set()
    for point, traveled in zip(vertices, cumulative):
        if pace is None:
            elapsed = round(duration_ms*traveled/meters)
        else:
            elapsed = round(duration_ms*pace.inverse(traveled/meters))
        if elapsed in corner_times:
            raise ValueError("route corners are too close for millisecond timestamps")
        corner_times.add(elapsed)
        samples[elapsed] = point
    return samples, corner_times


def _drop_crowded_grid_points(samples, corner_times):
    """顶点与近邻网格采样点只差几毫秒时，删掉网格点，只保留顶点。

    两者空间上几乎同一位置，留着不增加信息量，却会把相邻间隔压到 1ms，
    让瞬时速度算出几十上百 m/s。
    """
    if not corner_times:
        return samples
    ordered_corners = sorted(corner_times)

    def _crowded(elapsed):
        position = bisect.bisect_left(ordered_corners, elapsed)
        return any(0 <= j < len(ordered_corners)
                   and abs(ordered_corners[j] - elapsed) < MIN_SAMPLE_GAP_MS
                   for j in (position - 1, position))

    return {elapsed: coord for elapsed, coord in samples.items()
            if elapsed in corner_times or not _crowded(elapsed)}


# --------------------------------------------------------------------------- #
# 阶段 2：扰动（rng 为 None 时两步都原样跳过 —— 测试与复现走这条）
# --------------------------------------------------------------------------- #

def _jitter_timestamps(samples, corner_times, rng):
    """时间戳微扰：对非顶点的常规采样时刻施加小幅抖动，避免精确 3 秒倍数痕迹。

    关键约束：抖动幅度必须远小于采样间隔。采样间隔约 3 秒，若允许 ±1.4 秒
    的绝对抖动，相邻两点的相对位移可达 2.9 秒 —— 排序后会出现「1ms 间隔」
    的相邻点（瞬时速度算出来几千 m/s），严重时空间顺序被打乱、轨迹变「之」
    字形来回跳（实测 2150m 量成 3256m）。
    因此按**左右实际间隔**定抖动上限：最多抖掉各自间隔的 1/3，
    保证任意相邻点仍保留 ≥2/3 的原始间隔，顺序与间隔都稳定。
    """
    if rng is None:
        return samples
    ordered = sorted(samples.items())
    times = [ts for ts, _ in ordered]
    jittered = {}
    for index, (ts, coord) in enumerate(ordered):
        if ts in corner_times:
            jittered[ts] = coord
            continue
        left_gap = ts - times[index - 1] if index > 0 else 3000
        right_gap = times[index + 1] - ts if index + 1 < len(times) else 3000
        span = max(0, min(1000, left_gap // 3, right_gap // 3))
        new_ts = ts + (rng.randint(-span, span) if span else 0)
        # 兜底：极端输入（顶点与采样点几乎重合）仍不允许撞时刻
        while new_ts in jittered or new_ts in corner_times:
            new_ts += 1
        jittered[new_ts] = coord
    return jittered


def _apply_normal_drift(samples, corner_times, rng):
    """坐标微扰：沿轨迹法向施加低频漂移，模拟 GPS 定位漂移。

    三个约束共同决定了这里的形态：
      1. 长度是硬约束 —— 服务端拿 allLocJson 折线长度对比 totalDis，容差 0.1%；
      2. 顶点（路线拐点，也就是打卡点）不能动 —— 动了就得靠吸附补救，
         而吸附是几何改动，5 个点累计能把 2150m 顶高 3~4m；
      3. 归一化的整体缩放会按比例移动**所有**点，包括顶点，所以长度误差
         必须小到让归一化无事可做。
    逐点白噪声满足不了第三条：每段长度期望从 s 涨到 √(s²+2σ²)，σ=0.3m
    时 260 段累计虚增 2.8m，归一化随即把顶点推离 0.5m，链条全乱。

    改成**沿法向的低频漂移**：法向位移不改变沿路径的投影长度，只让折线
    微弯，段长增量是二阶量 (Δσ)²/(2s)。振幅再按步长自适应，保证总增量
    压在 0.05% 以内 —— 通勤距离下肉眼可见蜿蜒，长度几乎不动。
    """
    ordered = sorted(samples.items())
    if rng is None or len(ordered) <= 2:
        return ordered
    step_lengths = [distance((a[1][0], a[1][1]), (b[1][0], b[1][1]))
                    for a, b in zip(ordered, ordered[1:])]
    mean_step = (sum(step_lengths) / len(step_lengths)) if step_lengths else 3.0
    period = 25.0                                       # 约 75 秒一个起伏
    amplitude = min(0.6, 0.35 * math.sqrt(max(0.5, mean_step)))
    phase = rng.uniform(0.0, 2 * math.pi)
    fine_sigma = 0.03                                   # 叠一层极细法向噪声

    noisy = []
    for index, (elapsed, (lat, lon)) in enumerate(ordered):
        if elapsed in corner_times:
            noisy.append((elapsed, (lat, lon)))
            continue
        previous = ordered[max(0, index - 1)][1]
        following = ordered[min(len(ordered) - 1, index + 1)][1]
        cos_lat = max(0.1, abs(math.cos(math.radians(lat))))
        # 局部平面内的切线向量（米）
        tangent_lat = following[0] - previous[0]
        tangent_lon = (following[1] - previous[1]) * cos_lat
        norm = math.hypot(tangent_lat, tangent_lon)
        if norm < 1e-12:
            noisy.append((elapsed, (lat, lon)))
            continue
        normal_lat = -tangent_lon / norm                # 法向 = 切线旋转 90°
        normal_lon = tangent_lat / norm
        offset_m = (amplitude * math.sin(2 * math.pi * index / period + phase)
                    + rng.gauss(0.0, fine_sigma))
        noisy.append((elapsed, (
            round(lat + offset_m * normal_lat / 111320.0, 6),
            round(lon + offset_m * normal_lon / (111320.0 * cos_lat), 6))))
    return noisy


# --------------------------------------------------------------------------- #
# 阶段 3：归一（长度是硬约束，最后一步兜底）
# --------------------------------------------------------------------------- #

def _normalize_length(result, meters):
    """长度归一：服务端比对 totalDis 与 allLocJson 的实际折线长度。

    加了噪声之后相邻点位移被抬高，早期实现实测总长虚增 +7%。
    这里把每个点沿「相对首点的位移」方向等比缩放，使折线总长 = meters。
    法向漂移把这个增量压到了二阶量级，缩放系数通常只差小数点后四位，
    顶点几乎不会被推动 —— 这是扰动形态刻意换来的余量。
    """
    actual = sum(distance((a[0], a[1]), (b[0], b[1]))
                 for a, b in zip(result, result[1:]))
    if actual <= 0:
        return result
    factor = float(meters) / actual
    fixed_lat, fixed_lon = result[0][0], result[0][1]
    return [result[0]] + [
        (fixed_lat + (lat - fixed_lat) * factor,
         fixed_lon + (lon - fixed_lon) * factor,
         ts)
        for lat, lon, ts in result[1:]
    ]


def sample_route(waypoints, meters, seconds, start, require_all=True, pace=None, rng=None):
    """把 waypoints 串成一条恰好 meters 米的带时刻采样轨迹。

    分三段，各段只经手一种量，避免参数互相制约：

      采样  _dedupe_route → _walk_route → _sample_by_time → _drop_crowded_grid_points
            纯几何与时间换算，无随机性，同样输入永远得到同样输出；
      扰动  _jitter_timestamps → _apply_normal_drift
            只在给了 rng 时生效，顶点（打卡点）的时间与坐标都不动；
      归一  _normalize_length
            把扰动引入的长度增量缩放回目标值，只在扰动开启时执行，
            是最后一道兜底。

    require_all=True 时目标里程必须装得下所有途经点（计分跑语义）；
    闭环短于目标里程时沿路线自然绕圈补足，不做人为绕行环。
    """
    validate_totals(seconds, 0, start, start + seconds*1000, meters)
    route = _dedupe_route(waypoints)
    minimum = sum(distance(a, b) for a, b in zip(route, route[1:]))
    if require_all and meters + .001 < minimum:
        raise ValueError("target distance is too short to visit the supplied points")

    vertices, cumulative = _walk_route(route, meters)
    samples, corner_times = _sample_by_time(vertices, cumulative, meters, seconds, pace)
    samples = _drop_crowded_grid_points(samples, corner_times)

    samples = _jitter_timestamps(samples, corner_times, rng)
    ordered = _apply_normal_drift(samples, corner_times, rng)
    result = [(lat, lon, start + elapsed) for elapsed, (lat, lon) in ordered]

    if rng is not None:
        result = _normalize_length(result, meters)
    return result


def check_track(points, meters, seconds, start, stop, fixed_points=()):
    validate_totals(seconds, 0, start, stop, meters)
    if len(points) < 2 or points[0].time != start or points[-1].time != stop:
        raise ValueError("track endpoints do not match run timestamps")
    for point in points:
        coordinate(point.lat, point.lon)
        if not isinstance(point.time, int) or not math.isfinite(point.speed) or point.speed < 0:
            raise ValueError("invalid track timestamp or speed")
    if any(a.time >= b.time for a, b in zip(points, points[1:])):
        raise ValueError("track timestamps must strictly increase")
    actual = sum(distance((a.lat, a.lon), (b.lat, b.lon)) for a, b in zip(points, points[1:]))
    if abs(actual-meters) > max(.1, meters*.0001):
        raise ValueError(f"track length {actual:.3f}m differs from totalDis {meters}m")
    for fixed in fixed_points:
        if not any(distance((fixed.lat, fixed.lon), (point.lat, point.lon)) <= .1 for point in points):
            raise ValueError("a checkpoint is missing from the track")
    return actual


def time_buckets(seconds, steps, start, stop, meters):
    validate_totals(seconds, steps, start, stop, meters)
    duration_ms = seconds*1000
    cents = round(meters*100)
    for begin in range(0, duration_ms, 10000):
        end = min(begin+10000, duration_ms)
        count = steps*end//duration_ms - steps*begin//duration_ms
        length = (cents*end//duration_ms - cents*begin//duration_ms)/100
        yield start+begin, start+end, count, length
