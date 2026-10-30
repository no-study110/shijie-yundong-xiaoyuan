# -*- coding: utf-8 -*-
"""运动学分桶与信号一致性校验。

服务器会对结算报文做多维交叉校验:10 秒步频桶之和必须等于 totalSteps,
10 秒速度桶之和必须等于 totalDis,平均步频必须等于 totalSteps×60/totalTime,
上报 speed 必须等于**配速**（分/公里）×1000（不是速度，见 check 第 5 项）,
且 allLocJson 中的 GPS 点位必须能还原出同样的总里程与总时长。

本模块在提交前离线复现这些交叉校验,把「能组装但会被服务端驳回」的
报文提前拦下来,并输出结构化自检报告。
"""
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .geometry import haversine
from .models import GpsPoint


@dataclass
class ConsistencyIssue:
    """一条一致性问题的描述。"""

    field: str
    message: str
    expected: Optional[Any] = None
    actual: Optional[Any] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "field": self.field,
            "message": self.message,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass
class KinematicsReport:
    """运动学一致性自检报告。"""

    passed: bool = True
    issues: List[ConsistencyIssue] = field(default_factory=list)
    warnings: List[ConsistencyIssue] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    def add_issue(self, field: str, message: str, expected: Any = None, actual: Any = None) -> None:
        self.issues.append(ConsistencyIssue(field, message, expected, actual))
        self.passed = False

    def add_warning(self, field: str, message: str, expected: Any = None, actual: Any = None) -> None:
        self.warnings.append(ConsistencyIssue(field, message, expected, actual))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "issues": [i.to_dict() for i in self.issues],
            "warnings": [i.to_dict() for i in self.warnings],
            "stats": dict(self.stats),
        }

    def summary(self) -> str:
        if self.passed:
            return f"一致性自检通过 警告={len(self.warnings)}"
        return "一致性自检失败: " + "; ".join(f"[{i.field}] {i.message}" for i in self.issues)


def check_buckets(buckets: Sequence[Dict[str, Any]], expected_sum: float,
                  label: str, tolerance: float = 0.05) -> Optional[ConsistencyIssue]:
    """校验分桶总和是否等于总量。"""
    if not buckets:
        return None
    total = 0.0
    for row in buckets:
        value = row.get("stepsNum" if label == "steps" else "distance")
        if value is None:
            return ConsistencyIssue(label, f"分桶缺少 {'stepsNum' if label == 'steps' else 'distance'} 字段")
        try:
            total += float(value)
        except (TypeError, ValueError):
            return ConsistencyIssue(label, f"分桶数值非法: {value!r}")
    if abs(total - expected_sum) > max(tolerance, abs(expected_sum) * 0.0005):
        return ConsistencyIssue(label, f"{label} 分桶之和 {total:.3f} 与总量 {expected_sum:.3f} 不一致",
                                expected=expected_sum, actual=total)
    return None


def check_bucket_continuity(buckets: Sequence[Dict[str, Any]], start_time_ms: int,
                            stop_time_ms: int) -> Optional[ConsistencyIssue]:
    """校验分桶时间区间连续、无缝覆盖整段跑步。"""
    if not buckets:
        return ConsistencyIssue("buckets", "分桶为空")
    first = buckets[0]
    last = buckets[-1]
    if first.get("beginTime") != start_time_ms:
        return ConsistencyIssue("buckets", f"首个分桶起点 {first.get('beginTime')} 与 startTime {start_time_ms} 不符",
                                expected=start_time_ms, actual=first.get("beginTime"))
    if last.get("endTime") != stop_time_ms:
        return ConsistencyIssue("buckets", f"末个分桶终点 {last.get('endTime')} 与 stopTime {stop_time_ms} 不符",
                                expected=stop_time_ms, actual=last.get("endTime"))
    for a, b in zip(buckets, buckets[1:]):
        if a.get("endTime") != b.get("beginTime"):
            return ConsistencyIssue("buckets", f"分桶区间不连续: {a.get('endTime')} != {b.get('beginTime')}")
        if b.get("endTime", 0) - b.get("beginTime", 0) <= 0:
            return ConsistencyIssue("buckets", "存在非正长度的分桶区间")
    return None


def validate_kinematics(save_record: Dict[str, Any],
                         gps_points: Optional[Sequence[GpsPoint]] = None,
                         total_distance_m: Optional[int] = None,
                         total_time_sec: Optional[int] = None,
                         total_steps: Optional[int] = None) -> KinematicsReport:
    """对结算报文执行完整的一致性交叉校验。"""
    report = KinematicsReport()

    distance_m = total_distance_m if total_distance_m is not None else save_record.get("totalDis")
    time_sec = total_time_sec if total_time_sec is not None else save_record.get("totalTime")
    steps = total_steps if total_steps is not None else save_record.get("totalSteps")
    start_ms = save_record.get("startTime")
    stop_ms = save_record.get("stopTime")

    if not isinstance(distance_m, (int, float)) or distance_m <= 0:
        report.add_issue("totalDis", "总里程非法", expected=">0", actual=distance_m)
        return report
    if not isinstance(time_sec, int) or time_sec <= 0:
        report.add_issue("totalTime", "总时长非法", expected=">0", actual=time_sec)
        return report
    if not isinstance(steps, int) or steps < 0:
        report.add_issue("totalSteps", "总步数非法", expected=">=0", actual=steps)
        return report

    # 1. 时间戳与 totalTime 自洽
    if isinstance(start_ms, int) and isinstance(stop_ms, int):
        if stop_ms - start_ms != time_sec * 1000:
            report.add_issue("timestamps", f"stopTime-startTime={stop_ms - start_ms}ms 与 totalTime={time_sec}s 不符",
                             expected=time_sec * 1000, actual=stop_ms - start_ms)

    # 2. 步频桶
    step_buckets = save_record.get("stepsPerTenSec") or []
    issue = check_buckets(step_buckets, steps, "steps", tolerance=0.5)
    if issue:
        report.add_issue(issue.field, issue.message, issue.expected, issue.actual)
    if isinstance(start_ms, int) and isinstance(stop_ms, int):
        issue = check_bucket_continuity(step_buckets, start_ms, stop_ms)
        if issue:
            report.add_issue(issue.field, issue.message, issue.expected, issue.actual)

    # 3. 速度桶
    speed_buckets = save_record.get("speedPerTenSec") or []
    issue = check_buckets(speed_buckets, distance_m, "distance", tolerance=0.5)
    if issue:
        report.add_issue(issue.field, issue.message, issue.expected, issue.actual)
    if isinstance(start_ms, int) and isinstance(stop_ms, int):
        issue = check_bucket_continuity(speed_buckets, start_ms, stop_ms)
        if issue:
            report.add_issue(issue.field, issue.message, issue.expected, issue.actual)

    # 4. 平均步频
    expected_freq = round(steps * 60.0 / time_sec)
    actual_freq = save_record.get("avgStepFreq")
    if actual_freq is not None and int(actual_freq) != expected_freq:
        report.add_issue("avgStepFreq", f"平均步频 {actual_freq} 与 totalSteps×60/totalTime={expected_freq} 不符",
                         expected=expected_freq, actual=actual_freq)

    # 5. speed 字段是**配速**（分/公里）×1000，不是速度。
    #    实测校准：13 条真实 App 记录里，服务端保存的 speed 与
    #    时长/(距离/1000)/60 的差全部小于 0.005。若按 totalDis/totalTime×1000
    #    填（即 m/s），4080m/2180s 会变成 1872 → 服务端读作 1.872 分/km，
    #    判「配速控制在2′00″-10′00″」未达标（rrid=1350559546 的真因）。
    expected_speed = (round(time_sec / (distance_m / 1000.0) / 60.0 * 1000)
                      if distance_m > 0 else 0)
    actual_speed = save_record.get("speed")
    if actual_speed is not None and int(actual_speed) != expected_speed:
        report.add_issue("speed",
                         f"speed={actual_speed} 与 配速×1000 = {expected_speed} 不符"
                         f"（配速 = totalTime/(totalDis/1000)/60，单位分/公里）",
                         expected=expected_speed, actual=actual_speed)

    # 6. GPS 点位还原的里程/时长
    points = gps_points
    if points is None:
        points = _parse_all_loc(save_record.get("allLocJson"))
    if points:
        if len(points) < 2:
            report.add_issue("allLocJson", "GPS 点位少于 2 个")
        else:
            measured = sum(haversine((a.lat, a.lon), (b.lat, b.lon)) for a, b in zip(points, points[1:]))
            if abs(measured - distance_m) > max(0.5, distance_m * 0.001):
                report.add_issue("allLocJson", f"GPS 点位还原里程 {measured:.2f}m 与 totalDis={distance_m}m 偏差过大",
                                 expected=distance_m, actual=round(measured, 3))
            point_span = (points[-1].time - points[0].time) // 1000
            if abs(point_span - time_sec) > 1:
                report.add_issue("allLocJson", f"GPS 点位时间跨度 {point_span}s 与 totalTime={time_sec}s 不符",
                                 expected=time_sec, actual=point_span)
            for a, b in zip(points, points[1:]):
                if b.time <= a.time:
                    report.add_issue("allLocJson", "GPS 时间戳未严格递增")
                    break
            report.stats["gps_point_count"] = len(points)
    else:
        report.add_warning("allLocJson", "未提供 GPS 点位，无法校验里程还原")

    # 7. 步频与步长的自洽性（常识区间告警）
    if steps and distance_m:
        stride_m = distance_m / steps
        if not (0.4 <= stride_m <= 2.2):
            report.add_warning("totalSteps", f"平均步长 {stride_m:.2f}m 偏离常见区间 0.4~2.2m")

    # 8. 速度分桶的 flag/state 标记一致性
    for label, buckets in (("steps", step_buckets), ("distance", speed_buckets)):
        flags = {row.get("flag") for row in buckets if isinstance(row, dict)}
        states = {row.get("state") for row in buckets if isinstance(row, dict)}
        if flags and len(flags) > 1:
            report.add_warning(label, f"{label} 分桶 flag 取值不统一: {sorted(flags)}")
        if states and len(states) > 1:
            report.add_warning(label, f"{label} 分桶 state 取值不统一: {sorted(states)}")

    report.stats.update({
        "total_distance_m": distance_m,
        "total_time_sec": time_sec,
        "total_steps": steps,
        "avg_step_freq": expected_freq,
        "speed_mm_s": expected_speed,
        "step_bucket_count": len(step_buckets),
        "speed_bucket_count": len(speed_buckets),
    })
    return report


def _parse_all_loc(raw: Any) -> List[GpsPoint]:
    """解析 allLocJson 字符串为 GPS 点位列表。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        data = list(raw)
    else:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(data, list):
        return []
    points: List[GpsPoint] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            points.append(GpsPoint(
                lat=float(item["lat"]),
                lon=float(item["lon"]),
                time=int(item["time"]),
                speed=float(item.get("speed", 0.0)),
                accuracy=float(item.get("accuracy", 3.0)),
                isValid=bool(item.get("isValid", True)),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return points
