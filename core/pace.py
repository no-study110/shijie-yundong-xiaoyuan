# -*- coding: utf-8 -*-
"""配速的唯一换算与展示口径。

项目里「配速」有三种数值形态，混用一次就会算错一个数量级：

=========================  =====================  ==============================
形态                       示例（4080m / 2180s）  出现位置
=========================  =====================  ==============================
配速（分/公里，小数）       8.905                 规则 speedTop / speedBottom
                                                  历史记录 ``speed`` 字段
配速千分位（整数）          8905                  上报报文 ``speed`` 字段
速度（m/s）                1.872                  合规自检、日志
=========================  =====================  ==============================

三者换算只允许走本模块。展示统一成 App 记录页的样子 —— ``8′54″/km``，
CLI、网页实时提示、历史记录表、跑步结果卡片全部引用 :func:`format_pace`，
这样手机里看到的和网页上看到的永远是同一个字符串。
"""
import math
from typing import Dict, Optional

# 与服务端拒绝文案「配速控制在2′00″-10′00″」保持同款分隔符
PACE_MINUTE_MARK = "′"
PACE_SECOND_MARK = "″"
PACE_UNIT = "/km"
# 记录里的 speed 字段超过这个数只可能是千分位整数（配速不可能 100 分/km）
_THOUSANDTHS_THRESHOLD = 100


def pace_sec_per_km(distance_m, time_sec) -> Optional[float]:
    """距离(米) + 时长(秒) → 每公里用时(秒)。参数不合法返回 None。"""
    try:
        dist, secs = float(distance_m), float(time_sec)
    except (TypeError, ValueError):
        return None
    if dist <= 0 or secs <= 0:
        return None
    return secs / (dist / 1000.0)


def pace_min_per_km(distance_m, time_sec) -> Optional[float]:
    """距离(米) + 时长(秒) → 配速(分/公里)。"""
    secs = pace_sec_per_km(distance_m, time_sec)
    return None if secs is None else secs / 60.0


def pace_thousandths(distance_m, time_sec) -> int:
    """距离 + 时长 → 上报报文 ``speed`` 字段（配速 分/公里 ×1000）。"""
    pace_min = pace_min_per_km(distance_m, time_sec)
    return 0 if pace_min is None else int(round(pace_min * 1000))


def speed_mps_from_pace_min(pace_min) -> Optional[float]:
    """配速(分/公里) → 速度(m/s)。10 分/km 表示 1000 米用 600 秒 = 1.667m/s。"""
    try:
        pace = float(pace_min)
    except (TypeError, ValueError):
        return None
    if pace <= 0:
        return None
    return 1000.0 / (pace * 60.0)


def pace_min_from_speed_mps(speed_mps) -> Optional[float]:
    """速度(m/s) → 配速(分/公里)。"""
    try:
        speed = float(speed_mps)
    except (TypeError, ValueError):
        return None
    if speed <= 0:
        return None
    return 1000.0 / (speed * 60.0)


def format_pace_sec(sec_per_km) -> str:
    """每公里秒数 → ``8′54″``。四舍五入到秒，59.6 秒会进位成下一分钟的 00″。

    舍入方式必须与前端 ``Math.round`` 一致：Python 内置 ``round`` 是银行家舍入
    （round(534.5) == 534），JS 是四舍五入（Math.round(534.5) == 535）。
    同一个 .5 秒会让网页实时提示和记录里差 1 秒，所以这里显式写成地板 + 0.5。
    """
    try:
        secs = float(sec_per_km)
    except (TypeError, ValueError):
        return "-"
    if secs <= 0:
        return "-"
    total = int(math.floor(secs + 0.5))
    minutes, seconds = divmod(total, 60)
    return f"{minutes}{PACE_MINUTE_MARK}{seconds:02d}{PACE_SECOND_MARK}"


def format_pace(distance_m=None, time_sec=None, *, pace_sec=None,
                pace_min=None, with_unit: bool = True) -> str:
    """统一的配速文案，例：``8′54″/km``。

    三种入参任选其一：距离+时长（最常用）、每公里秒数、配速分/公里。
    取不到值时返回 ``-``，调用方不用再判空。
    """
    if pace_sec is None and pace_min is not None:
        pace_sec = pace_min * 60.0
    if pace_sec is None:
        pace_sec = pace_sec_per_km(distance_m, time_sec)
    text = format_pace_sec(pace_sec)
    if text == "-" or not with_unit:
        return text
    return text + PACE_UNIT


def format_pace_range(fast_min: Optional[float], slow_min: Optional[float]) -> str:
    """配速区间 → ``2′00″~10′00″/km``；单边或固定值各有对应文案。

    ``fast_min`` 是数值更小的那一端（跑得更快），与规则字段 speedTop 对应。
    """
    if fast_min is None and slow_min is None:
        return "不限"
    if fast_min is None:
        return f"不慢于 {format_pace(pace_min=slow_min)}"
    if slow_min is None:
        return f"不快于 {format_pace(pace_min=fast_min)}"
    if abs(fast_min - slow_min) < 1e-9:
        return f"固定 {format_pace(pace_min=fast_min)}"
    return (f"{format_pace(pace_min=fast_min, with_unit=False)}~"
            f"{format_pace(pace_min=slow_min)}")


def format_pace_from_server_speed(value) -> str:
    """服务端 ``speed`` 字段 → ``8′54″/km``。

    兼容两种落库形态：小数（8.905，正常记录）与千分位整数（8905）。
    """
    pace_min = _server_speed_to_pace_min(value)
    if pace_min is None:
        return "-"
    return format_pace(pace_min=pace_min)


def _server_speed_to_pace_min(value) -> Optional[float]:
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None
    return raw / 1000.0 if raw > _THOUSANDTHS_THRESHOLD else raw


def record_pace_min(record: Dict) -> Optional[float]:
    """从历史记录（列表项或详情）取配速，单位 分/公里。

    **以 totalDis / totalTime 为准**，``speed`` 字段只作兜底。原因是实测：
    rrid=1350559546（本套件早期的提交）里 speed 存的是 1.872 —— 那是 m/s，
    被服务端当成 1.872 分/km 判了「配速太快」。如果网页照抄这个字段，
    就会显示 1′52″/km；而里程 4080m / 耗时 2180s 明明对应 8′54″/km。

    真实 App 记录两种口径是一致的（speed=7.81 对 2143/(4576/1000)/60=7.8052），
    所以用里程/耗时算，既和手机记录对得上，也不会被历史脏字段带偏。
    """
    if not isinstance(record, dict):
        return None
    dist = record.get("totalDis") or record.get("distance")
    secs = record.get("totalTime") or record.get("costTime")
    pace = pace_min_per_km(dist, secs)
    if pace is not None:
        return pace
    # 里程或耗时缺失（个别列表接口只回 rrid + speed）才退回服务端字段
    return _server_speed_to_pace_min(record.get("speed"))


def record_pace_text(record: Dict) -> str:
    """历史记录 → ``8′54″/km``；服务端没给 speed 时用里程/耗时自算。"""
    pace = record_pace_min(record)
    if pace is None:
        return "-"
    return format_pace(pace_min=pace)
