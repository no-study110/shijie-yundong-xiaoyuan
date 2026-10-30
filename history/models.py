# -*- coding: utf-8 -*-
from dataclasses import dataclass, field
from typing import Dict, Any

from core.pace import format_pace, record_pace_min, speed_mps_from_pace_min


@dataclass
class RunRecordDetail:
    """单次跑步详情模型 (/api/v70320/runnings/get_one_record)"""
    rrid: int
    sport_type: int
    total_distance: float
    valid_distance: float
    total_time: int
    valid_time: int
    calorie: int
    status: int
    is_valid: bool
    uncomplete_reason: int
    start_time: int
    stop_time: int
    avg_speed: float
    avg_step_freq: int
    raw: Dict[str, Any] = field(default_factory=dict)
    failed_reasons: list = field(default_factory=list)
    is_cheat: int = 0
    # 服务端记录里的配速（分/公里）——App 记录页显示的就是这个值，
    # 网页/CLI 必须用它而不是另算一份，否则两边会对不上。
    pace_min_per_km: float = 0.0

    @property
    def pace_text(self) -> str:
        """与 App 记录页一致的配速文案，例：``8′54″/km``。"""
        return format_pace(pace_min=self.pace_min_per_km)

    @property
    def verdict(self) -> str:
        """结论只有三种：达标 / 未达标 / 判定作弊。"""
        if self.is_valid:
            return "达标"
        if self.is_cheat:
            return "判定作弊"
        return "未达标"

    @property
    def reason_text(self) -> str:
        """未达标的具体原因；达标时为空串（表格里不该出现一行废话）。"""
        if self.is_valid or self.is_cheat:
            return ""
        if self.failed_reasons:
            return "；".join(str(r) for r in self.failed_reasons)
        if self.uncomplete_reason:
            return f"unCompleteReason={self.uncomplete_reason}"
        return ""

    def status_text(self) -> str:
        """人类可读的判定结论（结论 + 原因，一行看完）"""
        reason = self.reason_text
        return f"{self.verdict}: {reason}" if reason else self.verdict

    @classmethod
    def from_api_data(cls, data: Dict[str, Any]) -> "RunRecordDetail":
        status = int(data.get("status", 0))
        un_reason = int(data.get("unCompleteReason", 0))
        # 服务端实测：正常完成的记录 status=0，异常/未完成才会有非零 unCompleteReason。
        # 达标判定应看 complete / isCheat / unCompleteReason / reasonList 各项 completeStatus，
        # 不能依赖 status==1。
        reasons = data.get("reasonList") or []
        reasons_ok = all(bool(item.get("complete")) for item in reasons) if reasons else True
        is_cheat = int(data.get("isCheat", 0) or 0)
        is_valid = (
            data.get("complete") is True
            and un_reason == 0
            and is_cheat == 0
            and reasons_ok
        )
        failed = [item.get("reason") for item in reasons if not item.get("complete")]
        # speed 字段是**配速**（分/公里），不是 m/s：4007m/2002s 的记录存的是 8.33，
        # 而速度是 2.0m/s。这里统一解析成配速，再派生 avg_speed 供旧调用方使用。
        pace_min = record_pace_min(data) or 0.0
        return cls(
            rrid=int(data.get("rrid", 0) or data.get("id", 0)),
            sport_type=int(data.get("sportType", 1)),
            total_distance=float(data.get("totalDis", 0.0) or data.get("distance", 0.0)),
            valid_distance=float(data.get("validDis", 0.0)),
            total_time=int(data.get("totalTime", 0) or data.get("costTime", 0)),
            valid_time=int(data.get("validTime", 0)),
            calorie=int(data.get("calorie", 0)),
            status=status,
            is_valid=is_valid,
            uncomplete_reason=un_reason,
            start_time=int(data.get("startTime", 0)),
            stop_time=int(data.get("stopTime", 0)),
            avg_speed=speed_mps_from_pace_min(pace_min) or 0.0,
            avg_step_freq=int(data.get("avgStepFreq", 0)),
            raw=data,
            failed_reasons=failed,
            is_cheat=is_cheat,
            pace_min_per_km=pace_min,
        )

def parse_history_records(data):
    """Accept the current list response and the older recordList envelope."""
    if isinstance(data, str):
        import json
        data = json.loads(data)
    if data is None:
        return []
    if isinstance(data, dict):
        if "recordList" not in data:
            raise ValueError("历史响应对象缺少 recordList")
        data = data["recordList"]
        if data is None:
            return []
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise ValueError("历史记录应为对象列表")
    return data


def history_record_status(record):
    """列表接口用 complete/isCheat 判定，status 字段正常完成的值为 0，不能作为判据。"""
    if record.get("isCheat"):
        return "判定作弊"
    if record.get("isValid") is False:
        return "未达标"
    if record.get("complete") is True or record.get("isValid") is True:
        return "达标"
    if record.get("complete") is False:
        return "未达标"
    return f"状态码={record.get('status')}"


def detail_for(details, rrid):
    """从 ``{rrid: RunRecordDetail}`` 里取这条记录的详情。

    键非法（rrid 缺失/非数字）或没回查到都返回 ``None`` —— 调用方据此退回列表字段，
    不必每处都写一遍 ``try: int(rrid)``。
    """
    try:
        return details.get(int(rrid))
    except (TypeError, ValueError, AttributeError):
        return None


def record_verdict(record, detail=None):
    """列表的一行 → ``(状态, 原因)``。

    列表接口 ``/api/v70230/runnings/records`` **不返回 reasonList**，只有
    ``get_one_record`` 才给逐条判定。实测 10 条记录里列表的 ``complete`` 全是
    ``True``，其中 2 条真实判定是「未达标: 配速控制在2′00″-10′00″」——
    只看列表字段必然把未达标误报成达标。

    所以：有详情就以详情为准；详情回查失败（网络抖动/接口变更）才退回列表字段，
    此时原因列留空，不编造理由。
    """
    if detail is not None:
        return detail.verdict, detail.reason_text
    return history_record_status(record), ""
