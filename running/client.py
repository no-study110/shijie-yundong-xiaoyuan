# -*- coding: utf-8 -*-
import json
import re
import time
import urllib.error
import urllib.request
import random
import uuid
from typing import Optional, Dict, Any, Union, Callable, List, Tuple, NamedTuple

from core.client import BaseNetSecClient
from core.pace import (
    format_pace,
    format_pace_range,
    pace_min_from_speed_mps,
    speed_mps_from_pace_min,
)
from account.client import ProfileClient
from policy.client import PolicyClient
from history.client import HistoryClient
from history.models import parse_history_records
from telemetry.client import TelemetryClient
from .config import (
    URL_OBS_TEMPORARY_URL,
    URL_SAVE_RECORD,
    URL_RUN_DETAIL_CONFIG,
    OBS_BUCKET_NAME
)
from .models import RunningSubmissionResult
from .builder import RunningTrackBuilder
from .realism import PaceProfile, physiological_profile, personal_style, MIN_HISTORY_SAMPLES
from .compliance import ComplianceValidator
from .geometry import polygon_area_m2
from .kinematics import validate_kinematics
from . import riskguard
from core.ledger import RunLedger


def _parse_step_freq(value) -> Optional[int]:
    """把规则里的步频字段解析为每分钟步数；「不限」或无效值返回 None"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if int(value) > 0 else None
    s = str(value).strip()
    if s in ("", "-", "不限", "无", "0"):
        return None
    try:
        return int(float(s)) if float(s) > 0 else None
    except ValueError:
        return None


# 人类跑步的步频常识区间（步/分）。学校规则的 stepBottom~stepTop 常常宽得离谱
# （实测某校是 40~300），直接在整个区间均匀采样会得出 298 步/分这种值 ——
# 配合 2180s 时长就是 10842 步、步幅 0.376m，比走路还碎，虽是"合规"的异常。
_HUMAN_STEP_FREQ = (140.0, 180.0)


def _derive_total_steps(rule, distance_m: int, time_sec: int) -> int:
    """根据学校规则的步频上下限推导总步数，让 avgStepFreq 落在合规且合理的区间"""
    lo = _parse_step_freq(getattr(rule, "step_bottom", None))
    hi = _parse_step_freq(getattr(rule, "step_top", None))
    if lo and hi:
        if lo > hi:
            lo, hi = hi, lo
        # 与人类常识区间取交集；交集为空（规则本身很窄很怪）时才退回规则区间
        h_lo, h_hi = _HUMAN_STEP_FREQ
        overlap_lo, overlap_hi = max(lo, h_lo), min(hi, h_hi)
        if overlap_lo <= overlap_hi:
            lo, hi = overlap_lo, overlap_hi
        avg_freq = random.uniform(lo, hi)
        return int(avg_freq * time_sec / 60)
    # 规则未约束步频时回退到步幅法，并把平均步频约束在常见跑步区间
    steps = int(distance_m / 0.72 * random.uniform(0.96, 1.04))
    avg_freq = steps * 60 / time_sec
    if avg_freq < 120 or avg_freq > 200:
        steps = int(random.uniform(140, 180) * time_sec / 60)
    return steps


# 配速（分/公里）的合理区间。超出这个范围只可能是字段解析或单位读错了。
# 20 分/km 是 0.83m/s，比散步还慢；1.4 分/km 是 11.9m/s，已是世界纪录级别。
_IMPLAUSIBLE_PACE_MAX_MIN = 20.0
_IMPLAUSIBLE_PACE_MIN_MIN = 1.4
# 分段速度贴着规则边界走，服务端用自己的口径重算时容易因舍入踩线，留 2% 余量。
_PACE_SAFETY_MARGIN = 0.02
# 配速曲线最慢段相对平均速度的下限倍数。保留这 5% 起伏是为了不像匀速跑 ——
# 平均配速由 _check_target_pace 单独卡死，曲线不必逐段都压在规则边界上。
_PACE_RATIO_FLOOR_CAP = 0.95


def _parse_rule_number(value) -> Optional[float]:
    """把规则里的数值字段解析为 float；「不限」或无效值返回 None。

    只做解析，**不做任何单位换算** —— 单位判断属于调用方（见 _rule_speed_bounds_mps）。
    曾经这里顺手把「分/公里」换算成速度，还写成了 `pace / 60`，
    10 分/km 被算成 0.167m/s，一次把全量跑步拦死。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if float(value) > 0 else None
    s = str(value).strip()
    if s in ("", "-", "不限", "无", "0"):
        return None
    try:
        number = float(s)
    except ValueError:
        # 兼容「12.5分/km」「2.0m/s」这类带单位的写法：取开头的数字，忽略后缀
        match = re.match(r"\s*(\d+(?:\.\d+)?)", s)
        if not match:
            return None
        number = float(match.group(1))
    return number if number > 0 else None


# 兼容旧名：单测与调用方曾用这个名字，语义就是「解析规则里的数值」
_parse_pace_min_per_km = _parse_rule_number


def _speed_to_pace_min(speed_mps: Optional[float]) -> Optional[float]:
    """速度（m/s）→ 配速（分/公里）。换算本身在 core.pace，这里只是旧入口。"""
    return pace_min_from_speed_mps(speed_mps)


def _pace_to_speed_mps(pace_min_per_km: float) -> float:
    """配速（分/公里）→ 速度（m/s）。10 分/km 意味着 1000 米用 600 秒 = 1.667 m/s。"""
    return speed_mps_from_pace_min(pace_min_per_km)


def _rule_pace_bounds(rule, log=None) -> Tuple[Optional[float], Optional[float]]:
    """返回规则允许的 (最慢配速, 最快配速)，单位「分/公里」；未约束的一端为 None。

    **speedTop / speedBottom 的数值就是配速。** 服务端拒绝时的文案直接对应：
    「配速控制在2′00″-10′00″」⇔ speedTop=2.0、speedBottom=10.0。

    这是实测校准的结果，不是从字段名猜的：拉同一账号 20 条记录，服务端保存的
    ``speed`` 字段与 ``时长/(距离/1000)/60`` 的差全部小于 0.005（13 条真实 App
    记录无一例外）。所以 speed 既不是 m/s、也不是「平均速度」——
    它是配速本身，报文必须按配速填（见 builder.pace_thousandths）。

    两个字段的字面名与大小关系是反的（speedTop=2.0 反而最小），因此按数值
    取 min/max，不依赖字段名的字面含义。
    """
    values = [v for v in (_parse_rule_number(getattr(rule, "speed_top", None)),
                          _parse_rule_number(getattr(rule, "speed_bottom", None)))
              if v is not None]
    if not values:
        return None, None
    fast, slow = min(values), max(values)

    # 哨兵：数值落在人类不可能的位置，只可能是字段解析/单位读错了。此时放弃该端
    # 约束并记日志 —— 拿错值拦提交会让每一次正常跑步都失败。
    if slow > _IMPLAUSIBLE_PACE_MAX_MIN:
        if log:
            log(f"合规自检: 规则最慢配速 {slow:g} 分/km 超过 "
                f"{_IMPLAUSIBLE_PACE_MAX_MIN:g} 分/km，不合常理，本次不设配速下限")
        slow = None
    if fast < _IMPLAUSIBLE_PACE_MIN_MIN:
        if log:
            log(f"合规自检: 规则最快配速 {fast:g} 分/km 低于 "
                f"{_IMPLAUSIBLE_PACE_MIN_MIN:g} 分/km，不合常理，本次不设配速上限")
        fast = None
    return slow, fast


def _rule_speed_bounds_mps(rule, log=None) -> Tuple[Optional[float], Optional[float]]:
    """把配速区间换算成 (最低速度, 最高速度)，单位 m/s；未约束的一端为 None。"""
    slow_pace, fast_pace = _rule_pace_bounds(rule, log)
    floor = _pace_to_speed_mps(slow_pace) if slow_pace else None
    ceil = _pace_to_speed_mps(fast_pace) if fast_pace else None
    return floor, ceil


def _pace_to_speed_limit_mps(rule, log=None) -> Optional[float]:
    """规则允许的峰值速度上限（m/s）。只有下界时退回下界，宁可严不可松。"""
    floor, ceil = _rule_speed_bounds_mps(rule, log)
    if ceil is not None:
        return ceil
    return floor


def _pace_ratio_bounds(avg_speed_mps: float,
                       floor_mps: Optional[float],
                       ceil_mps: Optional[float]) -> Dict[str, float]:
    """把速度区间换算成「相对全程平均速度的倍数」，供 PaceProfile 约束分段曲线。

    留 2% 余量：服务端会用自己的口径重算速度，贴着阈值走容易因四舍五入踩线。
    """
    bounds: Dict[str, float] = {}
    if not avg_speed_mps or avg_speed_mps <= 0:
        return bounds
    if floor_mps:
        # 下界最多压到平均值的 95%：贴着规则下限跑时（如 2.04 对 2.00），
        # 严格按 floor 换算会得出 ≥1.0 的倍数，把整条曲线压成匀速 —— 那反而是
        # 真实跑步绝不会出现的机器特征。平均速度由 _check_target_pace 硬卡。
        bounds["min_ratio"] = min(floor_mps * (1.0 + _PACE_SAFETY_MARGIN) / avg_speed_mps,
                                  _PACE_RATIO_FLOOR_CAP)
    if ceil_mps:
        bounds["max_ratio"] = max(ceil_mps * (1.0 - _PACE_SAFETY_MARGIN) / avg_speed_mps, 1.0)
    return bounds


def _check_target_pace(avg_speed_mps: float,
                       floor_mps: Optional[float],
                       ceil_mps: Optional[float],
                       distance_m: float,
                       log) -> Optional["RunningSubmissionResult"]:
    """目标平均配速本身就在规则外时直接拒绝。

    这种情况下无论轨迹怎么合成都不可能达标，生成 + OBS 上传 + 结算一趟是纯浪费，
    而且会白白消耗当天的提交次数。
    """
    if avg_speed_mps <= 0 or (floor_mps is None and ceil_mps is None):
        return None
    too_slow = floor_mps is not None and avg_speed_mps < floor_mps
    too_fast = ceil_mps is not None and avg_speed_mps > ceil_mps
    if not (too_slow or too_fast):
        return None

    pace_text = _speed_to_pace_min(avg_speed_mps)
    lo = _speed_to_pace_min(ceil_mps)      # 最快 → 配速数值最小
    hi = _speed_to_pace_min(floor_mps)     # 最慢 → 配速数值最大
    range_text = format_pace_range(lo, hi)
    if floor_mps and ceil_mps and abs(floor_mps - ceil_mps) > 1e-9:
        range_text += f"（速度 {floor_mps:.2f}~{ceil_mps:.2f} m/s）"
    elif ceil_mps and not floor_mps:
        range_text += f"（{ceil_mps:.2f} m/s）"
    elif floor_mps and not ceil_mps:
        range_text += f"（{floor_mps:.2f} m/s）"
    hint = "太快" if too_fast else "太慢"
    message = (f"本地合规自检未通过: 目标平均速度 {avg_speed_mps:.3f}m/s"
               f"（配速 {format_pace(pace_min=pace_text)}）{hint}，"
               f"超出学校规则 {range_text}。")
    if floor_mps and ceil_mps and abs(floor_mps - ceil_mps) > 1e-9:
        message += (f"{distance_m:.0f}m 的时长应在 "
                    f"{distance_m / ceil_mps:.0f}~{distance_m / floor_mps:.0f} 秒之间。")
    log(f"合规自检: 失败 原因={message}")
    return RunningSubmissionResult(success=False, message=message)


def _step_freq_range(rule) -> Optional[tuple]:
    """从规则的步频上下限返回 (min, max) 步/分；未约束返回 None。"""
    lo = _parse_step_freq(getattr(rule, "step_bottom", None))
    hi = _parse_step_freq(getattr(rule, "step_top", None))
    if lo is None or hi is None:
        return None
    if lo > hi:
        lo, hi = hi, lo
    return (lo, hi)


def _valid_time_window(rule) -> Optional[List[Tuple[str, str]]]:
    """返回规则里的有效时段窗口列表，未设置返回 None。"""
    vt = getattr(rule, "valid_times", None)
    if not vt:
        return None
    windows = []
    for item in vt:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            windows.append((str(item[0]), str(item[1])))
        elif isinstance(item, str) and "~" in item:
            a, b = item.split("~", 1)
            windows.append((a.strip(), b.strip()))
    return windows or None


def _face_check_gate(rule, log) -> Optional[str]:
    """人脸抽检闸门：返回拒绝原因，放行时返回 None。

    三态必须分开，不能一律 ``getattr(..., False)``：

      · **确认关闭** —— 规则返回了 runFaceCheckConfigDTO 且 enable=False。
        此时结算报文里的 faceCheck=0 就是真实值，不是破绽 ——
        「本次没被抽检」本来就该填 0。
      · **确认开启** —— enable=True，合成报文无法满足，直接拒绝提交，
        而不是硬塞一个 faceCheck=0 上去赌服务端不看。
      · **未返回配置** —— 规则里根本没有该 DTO，属于**未知**而非「关闭」。
        仍放行（多数学校不返回该字段），但必须留痕，否则
        「确实没开启」和「压根没检查」在事后完全无法区分。
    """
    config = getattr(rule, "face_check", None)
    if config is None:
        log("合规自检: 规则未返回人脸抽检配置，按未开启处理（若学校实际已开启，报文会被驳回）")
        return None
    enable = getattr(config, "enable", False)
    if isinstance(enable, str):
        # 服务端偶尔把布尔下发成字符串。bool("false") 是 True ——
        # 直接转型会把「关闭」误判成「开启」而白白拒绝提交，必须显式解析。
        enable = enable.strip().lower() not in ("", "0", "false", "no", "off")
    if enable:
        log("合规自检: 失败 原因=学校已开启人脸抽检，当前合成报文 faceCheck=0，不满足合规要求")
        return "学校已开启人脸抽检，需在 App 内完成人脸核验后重试"
    low = getattr(config, "check_point_min", 0)
    high = getattr(config, "check_point_max", 0)
    log(f"合规自检: 人脸抽检未开启（抽检点 {low}~{high} 次），faceCheck=0 为真实值")
    return None


def _in_window(ts_ms: int, windows) -> bool:
    """判断毫秒时间戳是否落在任一有效时段窗口内（按本地时区的 HH:MM 比较）。"""
    import datetime as _dt
    local = _dt.datetime.fromtimestamp(ts_ms / 1000.0)
    hm = local.strftime("%H:%M")
    for start, end in windows:
        if start <= hm <= end:
            return True
    return False


class _RunSetup(NamedTuple):
    """一次跑步任务的服务端上下文（拉取阶段产出，后续阶段只读）。"""

    rule: Any
    fences: List[Any]
    fence: Any
    fence_polygon: Optional[List[Tuple[float, float]]]
    checkpoints: List[dict]
    center_lat: float
    center_lng: float
    run_uuid: str
    now_ms: int
    start_time_ms: int
    stop_time_ms: int
    total_steps: int
    minimum_distance: int
    minimum_time: int
    policy: int


class _PreparedTrack(NamedTuple):
    """本地预检全部通过后的待上传产物。"""

    gps_points: List[Any]
    five_points: List[Any]
    save_record: Dict[str, Any]
    obs_payload: Dict[str, Any]
    object_key: str


class RunningClient(BaseNetSecClient):
    """运动世界校园 7.3.92 跑步提交与 OBS 上传客户端"""

    def __init__(self,
                 uid: Optional[Union[int, str]] = None,
                 token: Optional[str] = None,
                 unid: Optional[Union[int, str]] = None,
                 device_config: Optional[Dict[str, Any]] = None,
                 proxy: Optional[str] = None,
                 checkpoint_tolerance_m: float = 15.0,
                 ledger: Optional[RunLedger] = None):
        super().__init__(uid=uid, token=token, unid=unid, device_config=device_config, proxy=proxy)
        # telemetry 必须与业务通道共用代理：否则埋点走直连、业务走代理，
        # 服务端会看到同一账号在同一时刻来自两个出口 IP —— 这是比任何字段
        # 校验都直白的异常信号，且完全免费地被采集。
        self.telemetry = TelemetryClient(device_config=self.device_config, proxy=proxy)
        self.profile = ProfileClient(uid=uid, token=token, unid=unid, device_config=device_config, proxy=proxy)
        self.policy = PolicyClient(uid=uid, token=token, unid=unid, device_config=device_config, proxy=proxy)
        self.history = HistoryClient(uid=uid, token=token, unid=unid, device_config=device_config, proxy=proxy)
        # 打卡点命中容差（米）：GPS 定位误差允许范围内即视为经过
        self.checkpoint_tolerance_m = float(checkpoint_tolerance_m)
        # 本地运行账本：设备指纹、提交频次、信用分趋势的纵向记录。
        # 允许注入是为了让测试用临时文件 —— 共用真实账本会让用例之间
        # 通过磁盘状态互相影响（跑到第 N 个用例时被自己的历史阻断）。
        self.ledger = ledger if ledger is not None else RunLedger()

    def set_credentials(self, uid: Optional[Union[int, str]], token: Optional[str], unid: Optional[Union[int, str]] = None):
        super().set_credentials(uid, token, unid)
        self.profile.set_credentials(uid, token, unid)
        self.policy.set_credentials(uid, token, unid)
        self.history.set_credentials(uid, token, unid)

    def request_obs_upload_url(self, object_key: str) -> dict:
        """向网关申请华为云 OBS 预签名上传地址 (/api/obs/temporary/url)"""
        body = {
            "bucketName": OBS_BUCKET_NAME,
            "objectKey": object_key,
            "method": "Put",
            "contentType": "text/plain"
        }
        res = self.execute_request("POST", URL_OBS_TEMPORARY_URL, body_plain=body)
        if isinstance(res, dict) and res.get("error") == 10000 and "data" in res:
            data = res["data"]
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    pass
            return {"error": 10000, "data": data}
        return res if isinstance(res, dict) else {"error": -1, "message": str(res)}

    def upload_to_obs(self, signed_url: str, signed_headers: dict, payload: dict) -> dict:
        """直接以 PUT 请求向华为云 OBS 存储桶异步推送轨迹与步频载荷。

        华为云 OBS 的预签名 URL 是服务端签发的，必须原样透传；
        但出站通道仍需走与主网关一致的代理，否则真实出口 IP 会与
        代理通道不一致，被服务端的出口一致性风控识别。
        """
        data_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers = dict(signed_headers or {"Content-Type": "text/plain"})
        req = urllib.request.Request(signed_url, data=data_bytes, headers=headers, method="PUT")
        try:
            if self._proxy_handler:
                opener = urllib.request.build_opener(
                    self._proxy_handler, urllib.request.HTTPSHandler(context=self.ssl_ctx))
                with opener.open(req, timeout=30) as resp:
                    if 200 <= resp.status < 300:
                        return {"error": 10000, "message": "OBS 上传成功"}
                    return {"error": resp.status, "message": f"OBS HTTP {resp.status}"}
            with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=30) as resp:
                if 200 <= resp.status < 300:
                    return {"error": 10000, "message": "OBS 上传成功"}
                return {"error": resp.status, "message": f"OBS HTTP {resp.status}"}
        except urllib.error.HTTPError as e:
            return {"error": e.code, "message": f"OBS 上传失败: {e.reason}"}
        except Exception as e:
            return {"error": -1, "message": f"OBS 网络异常: {e}"}

    def submit_record(self, save_body: dict) -> dict:
        """核心跑步记录结算上报 (/api/v70260/runnings/save/record)"""
        return self.execute_request("POST", URL_SAVE_RECORD, body_plain=save_body)

    def get_rundetail_config(self, sport_type: int = 1) -> dict:
        """拉取跑步详情页配置 (/api/v70340/activity/rundetail/config)"""
        return self.execute_request("GET", f"{URL_RUN_DETAIL_CONFIG}?sportType={sport_type}")

    def execute_full_run(self,
                         target_distance_m: int = 2100,
                         target_time_sec: int = 780,
                         sport_type: int = 1,
                         progress_callback: Optional[Callable[[str], None]] = None,
                         realism: bool = True,
                         simulate_physical_time: bool = True,
                         match_history: bool = False,
                         risk_check: bool = True) -> RunningSubmissionResult:
        """按照 7.3.92 实机抓包全时序规范执行拟人跑步结算全流程

        参数:
            target_distance_m: 目标距离(米)
            target_time_sec: 目标时长(秒)
            sport_type: 跑步类型 1=计分跑 6=自由跑
            progress_callback: 进度回调函数
            realism: 是否启用拟真增强(配速曲线/生理参数波动/GPS抖动)
            simulate_physical_time: 是否模拟真实物理时间等待(默认True)
                - True: 完整等待 跑前准备(1-3分钟) + 跑步时长(target_time_sec)
                - False: 仅等待必要的网络延迟,适用于批量测试
            match_history: 是否按本人历史配速修正目标时长(默认False)
                - 开启后先拉最近历史,把时长调整到个人配速分布内;
                - 历史不足 3 条、拉取失败或调整后低于学校最低时长时自动放弃
            risk_check: 是否在提交前做风控预检(默认True)
                - 查设备指纹变更、24h 提交频次、信用分趋势、控制信令、时段偏离;
                - 查不到一律降级为提示,只有确认有问题才中止

        本方法只做编排，五个阶段的实现分别在：

            1. ``_prepare_run``        规则 / 时钟 / 围栏 / 打卡点
            2. ``_risk_preflight``     设备保护 / 作弊扫描 / 信用分 / 频次时段
            3. ``_prepare_track``      轨迹生成 / 报文组装 / 本地自检
            4. ``_wait_physical_time`` 跑前准备 + 跑步过程 + 跑后停留
            5. ``_upload_and_settle``  OBS 上传 / 成绩结算 / 跑后回查

        阶段 2 与 3 必须早于阶段 4：预检与自检都是无副作用的前置判断，
        放在物理等待之前才能避免「白等 1~3 分钟才发现根本不该提交」。
        """
        def log(msg: str):
            if progress_callback:
                progress_callback(msg)

        if not self.uid or not self.token:
            return RunningSubmissionResult(success=False, message="缺少鉴权凭据 (uid/token)")

        setup, failure = self._prepare_run(sport_type, target_distance_m,
                                           target_time_sec, log)
        if failure is not None:
            return failure

        if risk_check:
            _, failure = self._risk_preflight(setup, log)
            if failure is not None:
                return failure

        if match_history:
            adjusted = self._match_history_pace(target_distance_m, target_time_sec,
                                                setup, log)
            if adjusted != target_time_sec:
                # startTime 的语义是「此刻往前推 totalTime」。改了时长却不重算起点，
                # stopTime-startTime 就与 totalTime 对不上，validate_totals 会当场抛错。
                setup = setup._replace(
                    start_time_ms=setup.stop_time_ms - adjusted * 1000)
                target_time_sec = adjusted

        track, failure = self._prepare_track(setup, sport_type, target_distance_m,
                                             target_time_sec, realism, log)
        if failure is not None:
            return failure

        if simulate_physical_time:
            self._wait_physical_time(target_time_sec, log)

        return self._upload_and_settle(setup, track, sport_type,
                                       target_distance_m, target_time_sec, log)

    # ---------------------------------------------------------------- 历史画像
    def _match_history_pace(self, target_distance_m: int, target_time_sec: int,
                            setup: "_RunSetup", log) -> int:
        """按本人历史配速修正目标时长，返回新的时长（秒）。

        服务端对同一账号做纵向比对时，「本人常年 2.3~2.6 m/s，这条突然 2.9 m/s」
        比任何单条字段校验都显眼 —— 字段自洽只保证「这次说得通」，
        历史序列才保证「这个人一直是这么跑」。

        三道闸门依次放弃，任何一道不过就原样返回，绝不硬改：
        拉取失败 / 有效样本不足 / 建议时长低于学校最低时长。
        """
        try:
            response = self.history.get_run_records(page_num=1, page_size=20)
            records = parse_history_records(response)
        except Exception as exc:
            log(f"历史画像: 跳过（拉取失败: {exc}）")
            return target_time_sec

        style = personal_style(records)
        if style is None:
            log(f"历史画像: 跳过（有效样本不足 {MIN_HISTORY_SAMPLES} 条）")
            return target_time_sec

        # 「要不要改」用**均速**判断，「改成多少」才用采样值 —— 顺序不能反。
        # 拿采样值去比 3% 阈值会让同一组输入时改时不改：sigma 下限 0.05 在
        # 2.8m/s 上就是 ±2σ≈3.5%，比 3% 阈值还宽，噪声自己就能把结果推出阈值，
        # 表现为「同一个 760s 有时保持、有时变成 780s」—— 正是这里想避免的抖动。
        average_sec = int(round(target_distance_m / max(0.5, style.avg_speed_mps)))
        floor_sec = max(1, int(getattr(setup, "minimum_time", 0) or 0))
        if average_sec < floor_sec:
            log(f"历史画像: 建议时长 {average_sec}s 低于学校最低 {floor_sec}s，保持原时长")
            return target_time_sec
        # 3% 以内不动：改也看不出区别，反而让同一距离的时长忽长忽短
        if abs(average_sec - target_time_sec) <= max(5, int(target_time_sec * 0.03)):
            log(f"历史画像: {style.sample_size} 条样本 均速={style.avg_speed_mps:.2f}m/s "
                f"→ 目标时长 {target_time_sec}s 本就在个人区间内")
            return target_time_sec

        suggested = int(round(target_distance_m / max(0.5, style.suggest_speed())))
        # 采样完可能又落回原值附近；这种改动毫无意义，保持原时长
        if suggested < floor_sec or abs(suggested - target_time_sec) <= 5:
            log(f"历史画像: 采样时长 {suggested}s 与本值接近或低于学校下限，保持 {target_time_sec}s")
            return target_time_sec

        log(f"历史画像: {style.sample_size} 条样本 均速={style.avg_speed_mps:.2f}±"
            f"{style.speed_sigma:.2f}m/s 常用时段={style.usual_hour}时 "
            f"→ 时长 {target_time_sec}s 调整为 {suggested}s")
        return suggested

    # ---------------------------------------------------------------- 阶段 1
    def _prepare_run(self, sport_type: int, target_distance_m: int, target_time_sec: int,
                     log) -> Tuple[Optional[_RunSetup], Optional[RunningSubmissionResult]]:
        """拉学校规则、校准时钟、取围栏与打卡点。

        返回 ``(上下文, None)`` 表示成功；``(None, 失败结果)`` 表示应当中止。
        """
        # Use the same school policy for point issuance and record construction.
        rule = self.policy.get_run_policy(run_mode=sport_type)
        if rule is None:
            return None, RunningSubmissionResult(success=False, message="未取得学校规则，停止提交")
        try:
            minimum_distance = rule.min_distance
            minimum_time = int((rule.raw.get("runRuleModel") or {}).get("minRunTime", 0))
            policy = int(rule.raw.get("policy", 1))
            if minimum_distance <= 0 or minimum_time < 0:
                raise ValueError("学校距离或时长规则无效")
        except (TypeError, ValueError, AttributeError) as exc:
            return None, RunningSubmissionResult(success=False, message=f"学校规则无法解析: {exc}")
        log(f"学校规则: 最低距离={minimum_distance}m 最低时长={minimum_time}s")

        # 1. 冷启动时间校准与行为埋点
        st1 = self.profile.get_server_time()
        now_ms = st1.server_timestamp_ms
        log(f"时钟校准: 成功 时间戳={now_ms} 偏斜={st1.offset_seconds:+.3f}s")
        if self.unid:
            self.telemetry.send_startup_pv(uid=int(self.uid), unid=int(self.unid))
            self.telemetry.send_page_exposure(unid=int(self.unid))

        # 2. 拉取电子围栏与打卡点
        fences = self.policy.get_geo_fence()
        run_uuid = str(uuid.uuid4())
        if not fences:
            return None, RunningSubmissionResult(success=False, message="未取得电子围栏锚点，无法生成合规轨迹")
        fence = fences[0]

        # 围栏多边形存在性先判空，再取首点（顺序颠倒时无顶点围栏会直接抛异常）
        if not fence.polygon_points:
            return None, RunningSubmissionResult(success=False, message="围栏缺少多边形锚点，无法生成合规轨迹")

        log(f"围栏原始数据: 名称={fence.fence_name} 多边形顶点数={len(fence.polygon_points)}")
        first_point = fence.polygon_points[0]
        log(f"围栏第一个顶点: lat={first_point.lat}, lng={first_point.lng}")
        first_point_raw = getattr(first_point, "raw", None)
        if isinstance(first_point_raw, dict):
            log(f"围栏第一个顶点原始数据: {json.dumps(first_point_raw, ensure_ascii=False)}")

        center_lat = first_point.lat
        center_lng = first_point.lng

        # 围栏多边形（GCJ-02 锚点）用于围栏内轨迹规划与合规自检
        fence_polygon = [(p.lat, p.lng) for p in fence.polygon_points]
        if len(fence_polygon) >= 3:
            area = polygon_area_m2(fence_polygon)
            log(f"围栏多边形: 顶点={len(fence_polygon)} 面积={area:.0f}m²")
            # 诊断：面积为 0 时输出前 3 个顶点
            if area < 1.0:
                log(f"  ⚠️ 围栏面积接近 0，前3个顶点: {fence_polygon[:3]}")
                log(f"  可能原因: 坐标重复或经纬度字段调换")
        else:
            fence_polygon = None

        checkpoints = [{"lat": cp.lat, "lng": cp.lng, "name": cp.name} for cp in fence.check_points]
        if sport_type == 1 and not checkpoints:
            live = self.policy.get_checkpoints_for_run(fence, uuid_str=run_uuid, rule=rule)
            if live:
                checkpoints = [point.as_checkpoint() for point in live]
                log(f"官方点位: 成功 来源=pointsResModels 点数={len(checkpoints)}")
            else:
                log(f"官方点位: 失败 原因={self.policy.last_checkpoint_error}")
        if sport_type == 1 and not checkpoints:
            return None, RunningSubmissionResult(success=False, message="计分跑未取得必经打卡点，无法生成合规轨迹")
        log(f"围栏关联: 成功 名称={fence.fence_name} 点数={len(checkpoints)}")

        # 时间基准与步数在这里定下来，后续阶段全部基于它，避免中途再取时钟
        stop_time_ms = now_ms
        start_time_ms = stop_time_ms - (target_time_sec * 1000)
        total_steps = _derive_total_steps(rule, target_distance_m, target_time_sec)

        return _RunSetup(
            rule=rule,
            fences=fences,
            fence=fence,
            fence_polygon=fence_polygon,
            checkpoints=checkpoints,
            center_lat=center_lat,
            center_lng=center_lng,
            run_uuid=run_uuid,
            now_ms=now_ms,
            start_time_ms=start_time_ms,
            stop_time_ms=stop_time_ms,
            total_steps=total_steps,
            minimum_distance=minimum_distance,
            minimum_time=minimum_time,
            policy=policy,
        ), None

    # ---------------------------------------------------------------- 阶段 2
    def _risk_preflight(self, setup: "_RunSetup", log
                        ) -> Tuple[Optional[riskguard.RiskReport], Optional[RunningSubmissionResult]]:
        """提交前的风控预检：设备保护 / 作弊扫描 / 信用分 / 频次 / 时段。

        这一步存在的理由：`faceCheck`、字段自洽、轨迹拟真解决的都是「这条记录
        单看是否说得通」，而服务端的风控主体是**纵向比对** —— 同一个账号
        今天和昨天比、这台设备和上次那台比。单条报文再完美，也挡不住
        「24 小时内第 3 次提交」「信用分连跌两次」这种序列级判定。

        预检自身绝不允许成为故障源：任何子项异常都被 riskguard 降级为提示。
        """
        device_id = str(self.device_config.get("DeviceId", "") or "")
        try:
            report = riskguard.preflight(
                self.telemetry, self.profile, self.ledger,
                now_ms=setup.now_ms, device_id=device_id, log=log)
        except Exception as exc:
            log(f"风控预检: 跳过（预检模块异常: {exc}）")
            return None, None

        log(f"风控预检: 结论 {report.level}"
            + (f" 触发 {len(report.warnings)} 项提示" if report.warnings else ""))

        if report.blocked:
            return report, RunningSubmissionResult(
                success=False, message=f"风控预检未通过: {report.blocking_reason()}")
        return report, None

    # ---------------------------------------------------------------- 阶段 3
    def _prepare_track(self, setup: _RunSetup, sport_type: int, target_distance_m: int,
                       target_time_sec: int, realism: bool,
                       log) -> Tuple[Optional[_PreparedTrack], Optional[RunningSubmissionResult]]:
        """生成轨迹、组装结算与 OBS 报文，并跑完全部本地自检。

        纯本地计算，不发任何请求 —— 放在物理等待之前，注定失败的任务就不会空等。
        """
        rng = random.Random() if realism else None

        # 配速区间必须**先于**轨迹生成算出来：服务端是逐段判配速的，
        # 平均配速合规但起跑热身段慢到 12 分/km，一样判「未达标」。
        speed_floor, speed_ceil = _rule_speed_bounds_mps(setup.rule, log)
        avg_speed_mps = (float(target_distance_m) / target_time_sec) if target_time_sec else 0.0
        rejected = _check_target_pace(avg_speed_mps, speed_floor, speed_ceil,
                                      target_distance_m, log)
        if rejected is not None:
            return None, rejected

        pace = PaceProfile(rng, **_pace_ratio_bounds(avg_speed_mps, speed_floor,
                                                     speed_ceil)) if realism else None
        physio = physiological_profile(target_distance_m, target_time_sec, rng) if realism else None
        total_ascent = int(physio.pop("totalAscent", 0)) if physio else 0

        builder = RunningTrackBuilder(uid=int(self.uid), unid=int(self.unid or 0),
                                      sport_type=sport_type, uuid_str=setup.run_uuid, rng=rng)

        # 3. 拟人运动学轨迹生成
        try:
            gps_points, five_points = builder.generate_track_around_points(
                center_lat=setup.center_lat,
                center_lng=setup.center_lng,
                checkpoints=setup.checkpoints,
                total_distance_m=target_distance_m,
                total_time_sec=target_time_sec,
                start_time_ms=setup.start_time_ms,
                pace=pace,
                fence_polygon=setup.fence_polygon
            )

            # 里程回正结果：超差时 builder 会发 TrackLengthWarning，
            # 这里把「修正前→修正后」打出来，避免只能从最终报错反推。
            length_report = builder.last_length_report
            if length_report is not None:
                log(f"里程回正: {length_report.original_m:.1f}m -> "
                    f"{length_report.final_m:.1f}m (目标 {length_report.target_m:.0f}m "
                    f"偏离 {length_report.deviation_ratio * 100:.3f}%)")

            save_record, obs_payload, object_key = builder.build_submission_payload(
                total_distance_m=target_distance_m,
                total_time_sec=target_time_sec,
                total_steps=setup.total_steps,
                start_time_ms=setup.start_time_ms,
                stop_time_ms=setup.stop_time_ms,
                gps_points=gps_points,
                five_points=five_points,
                sel_distance_m=setup.minimum_distance,
                sel_run_time_s=setup.minimum_time,
                policy=setup.policy,
                run_area_id=self.policy.last_checkpoint_area_id,
                geo_fences_json=json.dumps([f.raw for f in setup.fences], separators=(",", ":")),
                total_ascent=total_ascent,
                physio=physio,
                jitter_buckets=realism
            )
        except (TypeError, ValueError) as exc:
            return None, RunningSubmissionResult(success=False, message=f"本地一致性校验失败: {exc}")
        log(f"轨迹合成: 成功 距离={target_distance_m}m 耗时={target_time_sec}s "
            f"点数={len(gps_points)} 步数={setup.total_steps} "
            f"配速={format_pace(target_distance_m, target_time_sec)}"
            f"（报文 speed={save_record['speed']}）")

        # 3.5 提交前自检：围栏/打卡点合规 + 运动学分桶一致性
        #     目的：在本地拦截「能组装但会被服务端交叉校验驳回」的报文，
        #     避免无谓的 OBS 上传与结算请求。
        #
        #     服务端规则里有三类硬约束，此前只取了最低距离/时长，其余字段
        #     取到后没用上，等于把服务端会驳回的报文原样送上去。这里把它们
        #     一并接进 ComplianceValidator：
        #       - speedTop/speedBottom：配速上下限（分/km），转成 m/s 上限
        #       - stepTop/stepBottom：步频区间（步/分）
        #       - validTime：有效时段窗口，不在窗口内提交会被拒
        #       - faceCheck.enable：人脸抽检开启时 faceCheck 必须为 1
        rule = setup.rule
        speed_limit_mps = speed_ceil
        step_freq_range = _step_freq_range(rule)
        valid_window = _valid_time_window(rule)
        if speed_floor is not None or speed_ceil is not None:
            low = f"{speed_floor:.2f}" if speed_floor is not None else "不限"
            high = f"{speed_ceil:.2f}" if speed_ceil is not None else "不限"
            hint = ""
            if speed_floor and speed_ceil:
                hint = (f"（配速 {_speed_to_pace_min(speed_ceil):.2f}~"
                        f"{_speed_to_pace_min(speed_floor):.2f} 分/km）")
            log(f"合规自检: 规则速度区间 {low}~{high} m/s{hint}")

        if valid_window is not None and not _in_window(setup.now_ms, valid_window):
            log(f"合规自检: 失败 原因=当前时间不在学校跑步有效时段 {valid_window} 内，提交会被服务端驳回")
            return None, RunningSubmissionResult(
                success=False,
                message=f"当前时间不在学校跑步有效时段 {valid_window} 内")
        face_reason = _face_check_gate(rule, log)
        if face_reason is not None:
            return None, RunningSubmissionResult(success=False, message=face_reason)

        compliance = ComplianceValidator(
            checkpoint_tolerance_m=self.checkpoint_tolerance_m,
            min_distance_m=setup.minimum_distance,
            min_time_sec=setup.minimum_time,
            speed_limit_mps=speed_limit_mps,
            min_speed_mps=speed_floor,
            step_freq_range=step_freq_range,
        ).validate(gps_points, setup.fence_polygon, setup.checkpoints,
                   steps=setup.total_steps,
                   total_distance_m=target_distance_m,
                   total_time_sec=target_time_sec)
        if not compliance.compliant:
            log(f"合规自检: 失败 原因={'; '.join(compliance.issues)}")
            return None, RunningSubmissionResult(
                success=False,
                message="本地合规自检未通过: " + "; ".join(compliance.issues))
        log(f"合规自检: 通过 {compliance.summary()}")

        kinematics = validate_kinematics(save_record, gps_points)
        if not kinematics.passed:
            log(f"一致性自检: 失败 原因={kinematics.summary()}")
            return None, RunningSubmissionResult(
                success=False,
                message="本地一致性自检未通过: " + kinematics.summary())
        log(f"一致性自检: 通过 {kinematics.summary()}")

        return _PreparedTrack(gps_points=gps_points,
                              five_points=five_points,
                              save_record=save_record,
                              obs_payload=obs_payload,
                              object_key=object_key), None

    # ---------------------------------------------------------------- 阶段 4
    def _wait_physical_time(self, target_time_sec: int, log) -> None:
        """跑前准备 + 跑步过程 + 跑后停留的真实耗时。

        真实场景：用户打开跑步页面、查看规则、等 GPS 定位、调整起跑位置（1~3 分钟）；
        实际跑步（target_time_sec），期间手机在口袋/臂包里，没有任何 HTTP 请求；
        跑完停留几秒看数据再点结束。只应在本地校验全过之后调用。
        """
        prep_delay = random.randint(60, 180)
        log(f"⏱️  跑前准备: 开始等待 {prep_delay}秒 (GPS定位/调整位置/心理准备)")
        for elapsed in range(0, prep_delay, 10):
            remaining = prep_delay - elapsed
            if elapsed % 30 == 0:  # 每30秒更新一次
                log(f"  ⏳ 准备中... 剩余 {remaining}秒")
            time.sleep(10)
        log(f"✓ 跑前准备完成 实际等待={prep_delay}秒")

        log(f"🏃 跑步进行中: 开始等待 {target_time_sec}秒 "
            f"({target_time_sec // 60}分{target_time_sec % 60}秒)")
        # 每分钟输出一次进度
        for elapsed in range(0, target_time_sec, 60):
            remaining = target_time_sec - elapsed
            progress_pct = (elapsed / target_time_sec * 100) if target_time_sec > 0 else 0
            log(f"  🏃 跑步中... 进度 {progress_pct:.0f}% "
                f"剩余 {remaining // 60}分{remaining % 60}秒")
            time.sleep(60)
        # 最后不足1分钟的部分
        final_delay = target_time_sec % 60
        if final_delay > 0:
            time.sleep(final_delay)
        log(f"✓ 跑步完成 实际耗时={target_time_sec}秒")

        # 跑步结束后,用户通常会停留几秒查看数据/调整状态,再点击结束
        post_run_delay = random.randint(3, 8)
        log(f"  ⏱️  跑后停留 {post_run_delay}秒 (查看实时数据/调整状态)")
        time.sleep(post_run_delay)

    # ---------------------------------------------------------------- 阶段 5
    def _upload_and_settle(self, setup: _RunSetup, track: _PreparedTrack, sport_type: int,
                           target_distance_m: int, target_time_sec: int,
                           log) -> RunningSubmissionResult:
        """OBS 预签名上传 → 成绩结算 → 跑后回查。

        真实场景：用户点击「结束跑步」后 App 立即做时间校准、OBS 上传、成绩结算，
        耗时 10~30 秒（取决于网络）。
        """
        # 4. 华为云 OBS 预签名上传
        obs_res = self.request_obs_upload_url(object_key=track.object_key)
        if obs_res.get("error") != 10000 or not obs_res.get("data"):
            err = obs_res.get("message", "OBS凭证申请失败")
            log(f"OBS上传: 失败 原因={err}")
            return RunningSubmissionResult(success=False,
                                           message=f"申请 OBS 上传凭证失败: {err}",
                                           raw=obs_res)

        obs_info = obs_res["data"]
        signed_url = obs_info.get("signedUrl")
        signed_headers = obs_info.get("actualSignedRequestHeaders") or {"Content-Type": "text/plain"}
        put_res = self.upload_to_obs(signed_url, signed_headers, track.obs_payload)
        if put_res.get("error") != 10000:
            err = put_res.get("message", "推送失败")
            log(f"OBS上传: 失败 原因={err}")
            return RunningSubmissionResult(success=False,
                                           message=f"推送到 OBS 失败: {err}",
                                           raw=put_res)
        log("OBS上传: 成功 状态=已同步")

        # 4.1 上传成功后填入 recordUrl (服务端用于关联轨迹数据)
        track.save_record["recordUrl"] = track.object_key
        track.save_record["isUpload"] = True

        # 5. 核心跑步结算上报 (save/record)
        sub_res = self.submit_record(track.save_record)
        if not isinstance(sub_res, dict) or sub_res.get("error") != 10000:
            code = sub_res.get("error", -1) if isinstance(sub_res, dict) else -1
            msg = sub_res.get("message", "结算上报失败") if isinstance(sub_res, dict) else str(sub_res)
            log(f"成绩结算: 失败 code={code} 原因={msg}")
            return RunningSubmissionResult(success=False, error_code=code, message=msg, raw=sub_res)

        data = sub_res.get("data") or {}
        rrid = data.get("rrid") or data.get("id")
        log(f"成绩结算: 成功 rrid={rrid}")

        # 6. 跑后实时成绩与状态回查
        time.sleep(0.5)
        record_detail = self.history.get_one_record(rrid=rrid) if rrid else None
        is_valid = record_detail.is_valid if record_detail else True
        status_text = record_detail.status_text() if record_detail else "未知(未回查到记录)"
        # 配速以**服务端存下来的记录**为准（App 记录页显示的同一个值），
        # 回查不到才退回按提交的里程/时长自算，避免网页与手机两套数字。
        pace_text = getattr(record_detail, "pace_text", "") if record_detail else ""
        if not pace_text or pace_text == "-":
            pace_text = format_pace(target_distance_m, target_time_sec)

        # 跑后触发详情配置与看盘回刷
        self.get_rundetail_config(sport_type=sport_type)
        if self.unid:
            self.telemetry.send_page_exposure(unid=int(self.unid))

        log(f"判定结果: {status_text} · 配速 {pace_text}")
        self._settle_ledger(setup, target_distance_m, target_time_sec, rrid, log)
        return RunningSubmissionResult(
            success=True,
            rrid=rrid,
            error_code=10000,
            message="跑步提交与结算完成",
            total_distance=float(target_distance_m),
            total_time=int(target_time_sec),
            is_valid=is_valid,
            raw=sub_res,
            status_text=status_text,
            pace_text=pace_text,
        )

    # ------------------------------------------------------------ 账本落盘
    def _settle_ledger(self, setup: _RunSetup, distance_m: int, time_sec: int,
                       rrid, log) -> None:
        """把本次结果写进本地账本，并回读一次信用分。

        回读信用分是这里唯一有意义的动作：扣分制下「提交没报错」和「没被扣分」
        是两件事，服务端不会主动告知，只有把分数序列记下来才看得出趋势。
        回读或落盘失败都只记一行日志，不影响已经完成的提交。
        """
        credit = None
        try:
            payload = self.telemetry.get_credit_popup(self.profile, retries=0)
            credit = riskguard.extract_credit_score(payload)
        except Exception as exc:
            log(f"信用分: 回读失败（{exc}）")
        if credit is not None:
            log(f"信用分: 当前 {credit}")

        try:
            self.ledger.record_run(
                at_ms=setup.stop_time_ms,
                distance_m=distance_m,
                time_sec=time_sec,
                success=True,
                rrid=rrid,
                credit=credit,
                device_id=str(self.device_config.get("DeviceId", "") or ""),
            )
            log("账本: " + self.ledger.summary_lines()[0].split("账本: ", 1)[-1])
        except Exception as exc:
            log(f"账本: 写入失败（{exc}）")
