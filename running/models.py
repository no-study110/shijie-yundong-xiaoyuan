# -*- coding: utf-8 -*-
from dataclasses import dataclass, field
from typing import Optional, Dict, Any


@dataclass
class GpsPoint:
    """单个 GPS 运动轨迹采样点"""
    lat: float
    lon: float
    time: int
    speed: float = 2.5
    accuracy: float = 3.0
    isValid: bool = True

    def to_dict(self) -> dict:
        return {
            "lat": self.lat,
            "lon": self.lon,
            "time": self.time,
            "speed": self.speed,
            "accuracy": self.accuracy,
            "isValid": self.isValid
        }


@dataclass
class FivePoint:
    """必经打卡目标点"""
    lat: float
    lon: float
    isFixed: int = 1
    isPass: bool = True
    pointName: str = ""
    position: int = 0
    raw: Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        result = dict(self.raw or {})
        result.update({
            "lat": self.lat,
            "lon": self.lon,
            "isFixed": self.isFixed,
            "isPass": self.isPass,
            "pointName": self.pointName,
            "position": self.position
        })
        return result


class StepBucket:
    """10秒步频分桶统计"""

    def __init__(self, stepsNum: int = 0, beginTime: int = 0, endTime: int = 0,
                 flag: int = 0, maxDiff: float = 0.0, minDiff: float = 0.0,
                 avgDiff: float = 0.0, state: int = 0, queueNum: int = 1):
        self.stepsNum = stepsNum
        self.beginTime = beginTime
        self.endTime = endTime
        self.flag = flag
        self.maxDiff = maxDiff
        self.minDiff = minDiff
        self.avgDiff = avgDiff
        self.state = state
        self.queueNum = queueNum

    def to_dict(self) -> dict:
        return {
            "stepsNum": self.stepsNum,
            "beginTime": self.beginTime,
            "endTime": self.endTime,
            "flag": self.flag,
            "maxDiff": self.maxDiff,
            "minDiff": self.minDiff,
            "avgDiff": self.avgDiff,
            "state": self.state,
            "queueNum": self.queueNum
        }


class SpeedBucket:
    """10秒速度分桶统计"""

    def __init__(self, distance: float = 0.0, beginTime: int = 0, endTime: int = 0,
                 flag: int = 0, state: int = 0, queueNum: int = 1):
        self.distance = distance
        self.beginTime = beginTime
        self.endTime = endTime
        self.flag = flag
        self.state = state
        self.queueNum = queueNum

    def to_dict(self) -> dict:
        return {
            "distance": self.distance,
            "beginTime": self.beginTime,
            "endTime": self.endTime,
            "flag": self.flag,
            "state": self.state,
            "queueNum": self.queueNum
        }


@dataclass
class RunningSubmissionResult:
    """跑步提交结果模型"""
    success: bool
    rrid: Optional[int] = None
    error_code: int = -1
    message: str = ""
    total_distance: float = 0.0
    total_time: int = 0
    is_valid: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)
    status_text: str = ""
    # 与 App 记录页一致的配速文案（如 8′54″/km）。取服务端回查到的 speed 字段，
    # 回查失败才用提交的里程/时长自算 —— 网页显示必须与手机记录同一个值。
    pace_text: str = ""

    def __post_init__(self):
        if not self.status_text:
            self.status_text = "达标" if self.is_valid else "未达标"
