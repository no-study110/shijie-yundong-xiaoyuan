# -*- coding: utf-8 -*-
"""拟真跑步运动学与生理画像。

针对 7.3.92 纯协议套件合成轨迹与真实跑步的差异点,提供三类拟真增强:

1. PaceProfile —— 可逆、单调的分段线性配速曲线,让 GPS 采样沿路径
   非匀速推进(起跑热身慢、中段波动、末段冲刺),speed 字段随之自然起伏;
2. physiological_profile —— 依据配速与时长生成自洽的生理/传感器字段
   (心率、触地时间、垂直振幅、vo2max、累计爬升/下降);
3. jitter_* —— 对 10 秒步频/速度分桶做「总和守恒」的相邻桶抖动,
   消除均匀分桶的机器痕迹。

所有函数仅依赖 Python 标准库,且默认不影响既有调用方的确定性行为。
"""
import random
from collections import Counter
from typing import List, Optional, Dict, Any, Tuple


class PaceProfile:
    """分段线性、严格单调的配速曲线：时间比例 t∈[0,1] ↔ 距离比例 d∈[0,1]。

    前向 forward(t) 与逆向 inverse(d) 互为反函数，用于把「等间隔采样时刻」
    映射到「路径累计距离」，同时保证必经打卡点仍能精确定位到采样时刻。

    min_ratio / max_ratio 把「每段速度 ÷ 全程平均速度」压进规则允许的区间。
    学校对配速有硬约束时（如 2~10 分/km），起跑热身和末段疲劳都不能慢出下限 ——
    服务端是逐段判配速的，平均配速合规但某一公里慢到 12 分/km 一样判未达标。
    """

    def __init__(self, rng: Optional[random.Random] = None, n_segments: Optional[int] = None,
                 min_ratio: float = 0.0, max_ratio: Optional[float] = None):
        rng = rng or random.Random()
        n = n_segments or rng.randint(7, 11)  # 增加分段数，配速更自然

        # 等分时间轴为 n 段，段端点即控制点的 t 坐标
        xs = [i / n for i in range(n + 1)]

        # 每段一个速度乘数：起跑热身慢、末段可冲刺、中段小幅波动
        segs = []
        for i in range(n):
            t_mid = (xs[i] + xs[i + 1]) / 2.0
            if t_mid < 0.15:
                w = rng.uniform(0.78, 0.92)   # 热身：更明显的慢启动
            elif t_mid > 0.85:
                w = rng.uniform(0.98, 1.15)   # 末段：可加速但不过度
            else:
                w = rng.uniform(0.88, 1.12)   # 中段：增大波动范围，更真实
            segs.append(w)

        segs = self._clamp_ratios(segs, min_ratio, max_ratio)

        # 归一化：使整段累计距离比例恰好到 1.0
        total = sum(segs)
        self._xs = xs
        self._cum = [0.0]
        acc = 0.0
        for w in segs:
            acc += w / total
            self._cum.append(acc)
        # self._cum[0]=0.0, self._cum[-1]=1.0，严格递增

    @staticmethod
    def _clamp_ratios(segs: List[float], min_ratio: float, max_ratio: Optional[float]) -> List[float]:
        """把每段「相对平均速度的倍数」压进 [min_ratio, max_ratio]。

        约束的是比值而不是 w 本身：归一化后段的实际速度 = w/mean(w) × 平均速度。

        做法是**按比例压缩振幅**而不是逐轮抬升最慢段：把 w 归一化成「1 + 偏差」，
        偏差均值必然为 0，于是整体乘一个系数 k 就能同时保住平均值（仍为 1）和形状
        （快慢结构不变），k 取到让最慢段刚好落在下界为止。

        逐轮抬升的写法在这里会失效：抬高最慢段会让均值上移，其余段比值随之下降，
        下一轮又被判定越界 —— 迭代过程单调趋向「所有段相等」，把跑步压成匀速跑。
        """
        n = len(segs)
        mean = sum(segs) / n
        if mean <= 0:
            return segs
        dev = [w / mean - 1.0 for w in segs]      # 归一化偏差，均值恒为 0
        down = -min(dev)                           # 最慢段向下偏离的幅度
        up = max(dev)                              # 最快段向上偏离的幅度

        k = 1.0
        if min_ratio > 0 and down > 0:
            k = min(k, (1.0 - min_ratio) / down)
        if max_ratio and up > 0:
            k = min(k, (max_ratio - 1.0) / up)
        k = max(0.0, k)                            # 下界高于均值时只能退化为匀速
        return [1.0 + k * d for d in dev]

    def _locate(self, values: List[float], x: float) -> int:
        """返回区间下标 i，使 values[i] <= x <= values[i+1]；越界收敛到首尾。"""
        if x <= values[0]:
            return 0
        if x >= values[-1]:
            return len(values) - 2
        for i in range(len(values) - 1):
            if values[i] <= x <= values[i + 1]:
                return i
        return len(values) - 2

    def forward(self, t: float) -> float:
        """时间比例 -> 距离比例。"""
        t = min(1.0, max(0.0, t))
        i = self._locate(self._xs, t)
        span = self._xs[i + 1] - self._xs[i]
        if span <= 0:
            return self._cum[i]
        frac = (t - self._xs[i]) / span
        return self._cum[i] + (self._cum[i + 1] - self._cum[i]) * frac

    def inverse(self, d: float) -> float:
        """距离比例 -> 时间比例（forward 的反函数）。"""
        d = min(1.0, max(0.0, d))
        i = self._locate(self._cum, d)
        span = self._cum[i + 1] - self._cum[i]
        if span <= 0:
            return self._xs[i]
        frac = (d - self._cum[i]) / span
        return self._xs[i] + (self._xs[i + 1] - self._xs[i]) * frac


def terrain_climb(distance_m: float, pace_kmh: float,
                  rng: Optional[random.Random] = None) -> Tuple[int, int]:
    """按里程推算累计爬升/下降（米）。

    原先取 ``rng.randint(2, 9)``，有两个静态痕迹：2km 和 10km 的爬升分布一模一样，
    且与配速无关。服务端拿不到高程（allLocJson 里没有 elevation 字段），无法直接
    校验，但同一账号的历史序列会呈现「距离越长、起伏越大」的规律，恒定在个位数
    是最容易被横向比对出来的破绽。

    按里程线性给每公里起伏；配速越快越偏向平路（跑得动的前提是坡少）。
    终点不保证回到起点，所以下降与爬升允许 1~3m 差异。
    """
    rng = rng or random.Random()
    km = max(0.4, distance_m / 1000.0)
    low, high = (3.0, 7.0) if pace_kmh >= 11.0 else (5.0, 12.0)
    ascent = max(1, int(round(rng.uniform(low, high) * km)))
    descent = max(1, ascent + rng.randint(-2, 2))
    return ascent, descent


def physiological_profile(distance_m: float, time_sec: int,
                          rng: Optional[random.Random] = None) -> Dict[str, Any]:
    """依据配速与时长生成自洽的生理/传感器字段。

    返回键与 7.3.92 结算报文字段一一对应（camelCase）。心率/触地时间/垂直振幅
    均随配速联动，并叠加个体差异噪声，避免所有记录聚集在同一固定值上。
    """
    rng = rng or random.Random()
    pace_kmh = (distance_m / time_sec) * 3.6 if time_sec > 0 else 10.0
    total_ascent, total_descent = terrain_climb(distance_m, pace_kmh, rng)

    # 个体差异：静息心率与最大心率一次跑步内固定，增大个体差异范围
    rest_hr = rng.randint(52, 73)  # 扩大静息心率范围
    max_hr = rng.randint(182, 203)  # 扩大最大心率范围

    # 强度系数：慢走(6km/h)≈0.42，慢跑(10km/h)≈0.62，快跑(13km/h)≈0.82
    intensity = min(0.9, max(0.35, 0.42 + (pace_kmh - 6.0) / 7.0 * 0.40))
    # 累计爬升抬平均心率：每公里起伏 10m 约 +1.5 次/分，封顶 +8
    climb_boost = min(8, int(total_ascent / max(0.5, distance_m / 1000.0) * 1.5))
    avg_hr = int(rest_hr + (max_hr - rest_hr) * intensity * rng.uniform(0.92, 1.08)) + climb_boost
    avg_hr = min(max_hr - 5, avg_hr)  # 平均值不能贴到最大值，否则峰值无处可放
    peak_hr = min(max_hr, avg_hr + rng.randint(6, 22))  # 增大峰值心率范围

    # 触地时间随配速加快而缩短；垂直振幅随配速加快而略增（增大个体差异）
    ground_contact = int(round(305 - pace_kmh * 4.8 + rng.uniform(-12, 12)))
    ground_contact = max(205, min(310, ground_contact))
    vertical_osc = round(4.8 + pace_kmh * 0.38 + rng.uniform(-0.8, 0.8), 1)
    vertical_osc = max(5.2, min(11.0, vertical_osc))

    # vo2max 粗略随配速抬升，落在普通大学生区间，增大个体差异
    vo2max = round(min(60.0, max(38.0, 28.0 + pace_kmh * 1.5 + rng.uniform(-3.0, 3.0))), 1)

    return {
        "avgHeartRate": avg_hr,
        "maxHeartRate": peak_hr,
        "restHeartRate": rest_hr,
        "groundContactTime": ground_contact,
        "verticalOscillation": vertical_osc,
        "vo2max": vo2max,
        "totalAscent": total_ascent,
        "totalDescent": total_descent,
    }


def jitter_counts(counts: List[int], rng: Optional[random.Random] = None) -> List[int]:
    """在相邻桶间转移整数步数，制造自然波动，同时保持总和精确不变。"""
    rng = rng or random.Random()
    arr = list(counts)
    n = len(arr)
    if n < 2:
        return arr
    # 增加迭代次数，使波动更明显
    for _ in range(n * 6):
        i = rng.randrange(n)
        j = i - 1 if rng.random() < 0.5 else i + 1
        if not (0 <= j < n) or arr[i] <= 0:
            continue
        # 增大转移幅度范围
        cap = max(1, int(arr[i] * rng.uniform(0.08, 0.40)))
        delta = rng.randint(1, max(1, cap))
        arr[i] -= delta
        arr[j] += delta
    return arr


def jitter_distances(cents: List[int], rng: Optional[random.Random] = None) -> List[int]:
    """在相邻桶间转移厘米整数距离，制造自然波动，同时保持总和精确不变。"""
    rng = rng or random.Random()
    arr = list(cents)
    n = len(arr)
    if n < 2:
        return arr
    # 增加迭代次数，使波动更明显
    for _ in range(n * 6):
        i = rng.randrange(n)
        j = i - 1 if rng.random() < 0.5 else i + 1
        if not (0 <= j < n) or arr[i] <= 0:
            continue
        # 增大转移幅度范围
        cap = max(1, int(arr[i] * rng.uniform(0.08, 0.40)))
        delta = rng.randint(1, max(1, cap))
        arr[i] -= delta
        arr[j] += delta
    return arr


# --------------------------------------------------------------------------- #
# 个人画像：让新记录落在本人历史分布内
# --------------------------------------------------------------------------- #

MIN_HISTORY_SAMPLES = 3


class PersonalStyle:
    """从本人历史记录学到的跑步习惯。

    服务端对同一账号的历史序列做纵向比对时,「本人常年 2.3~2.6 m/s,
    新记录突然 2.9 m/s」比任何单条字段校验都显眼。这里只做描述统计,
    不修改调用方的默认行为 —— 样本不足时调用方应退回原逻辑。
    """

    def __init__(self, speeds: List[float], hours: List[int], sample_size: int):
        self.speeds = list(speeds)
        self.hours = list(hours)
        self.sample_size = sample_size
        mean = sum(self.speeds) / len(self.speeds)
        variance = sum((s - mean) ** 2 for s in self.speeds) / len(self.speeds)
        self.avg_speed_mps = mean
        # 标准差下限 0.05：历史高度一致时不能退化成「每次都跑同一速度」,
        # 那反而制造了一个新的恒定值,比有波动更假。
        self.speed_sigma = max(0.05, variance ** 0.5)
        self.usual_hour = Counter(self.hours).most_common(1)[0][0] if self.hours else None

    def suggest_speed(self, rng: Optional[random.Random] = None) -> float:
        """在个人均值附近取配速,截断到 ±2σ,避免抖出本人历史区间。"""
        rng = rng or random.Random()
        lo = max(0.5, self.avg_speed_mps - 2 * self.speed_sigma)
        hi = self.avg_speed_mps + 2 * self.speed_sigma
        return min(hi, max(lo, rng.gauss(self.avg_speed_mps, self.speed_sigma)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sample_size": self.sample_size,
            "avg_speed_mps": round(self.avg_speed_mps, 3),
            "speed_sigma": round(self.speed_sigma, 3),
            "usual_hour": self.usual_hour,
        }


def _record_field(record, name):
    """兼容 dict 与带属性的记录对象。"""
    if isinstance(record, dict):
        return record.get(name)
    return getattr(record, name, None)


def personal_style(records, timezone_offset_hours: int = 8) -> Optional[PersonalStyle]:
    """从历史记录归纳个人配速分布与常用起跑时段。

    records 接受任意可迭代的字典/对象,只要能取到 totalDis(米)、totalTime(秒)、
    startTime(毫秒)。无效记录直接跳过;有效样本少于 MIN_HISTORY_SAMPLES 时返回 None ——
    用两三条噪声样本「学」出来的画像比不学更糟。
    """
    speeds: List[float] = []
    hours: List[int] = []
    for record in records or []:
        try:
            distance = float(_record_field(record, "totalDis"))
            duration = float(_record_field(record, "totalTime"))
        except (TypeError, ValueError):
            continue
        if distance <= 0 or duration <= 0:
            continue
        speeds.append(distance / duration)
        try:
            utc_hour = int(_record_field(record, "startTime")) // 1000 // 3600 % 24
            hours.append((utc_hour + timezone_offset_hours) % 24)
        except (TypeError, ValueError):
            pass
    if len(speeds) < MIN_HISTORY_SAMPLES:
        return None
    return PersonalStyle(speeds, hours, len(speeds))
