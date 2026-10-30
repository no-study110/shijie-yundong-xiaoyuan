# -*- coding: utf-8 -*-
"""跑前风控预检 —— 把三项真实存在的风控面在提交之前各查一遍。

这三个面都不是推测，`telemetry/config.py` 里都有对应的服务端接口：

===================  ==========================================  ================
风控面               服务端接口                                  本模块如何判定
===================  ==========================================  ================
设备保护 10121       （登录时返回 10121）                        账本比对指纹变更
作弊工具扫描         ``/api/v78/cheat/cheatlist``                拉取特征库并落盘
无障碍服务检测       ``/api/v1/barrierFree/getBarrierFree``      拉取策略并落盘
信用分（扣分制）     ``/api/creditscore/v70390/popup``           比对历史分数趋势
控制信令             ``control.gxapp.iydsj.com/getMessage``      扫描封禁/冻结关键词
===================  ==========================================  ================

**核心原则：查不到不是罪证。** 任何一个子项网络失败、响应结构不认识、
字段缺失，一律降级为 ``warn`` 而不是 ``block``。只有「已经确认当前
环境提交必被驳回」才中止 —— 否则风控辅助功能本身就成了故障源。
"""
import datetime
import ssl
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_BLOCK = "block"

_LEVEL_RANK = {LEVEL_OK: 0, LEVEL_WARN: 1, LEVEL_BLOCK: 2}
_LEVEL_LABEL = {LEVEL_OK: "通过", LEVEL_WARN: "注意", LEVEL_BLOCK: "阻断"}

# 控制信令里出现这些词说明账号已被处置，继续提交只是加深标记
_BAN_KEYWORDS = ("封禁", "封停", "冻结", "禁用", "停用", "违规", "作弊", "禁止")
# 单日提交频次阈值：按**自然日**而非滚动 24 小时统计 —— 学校规则是「每天一次」，
# 昨晚 22 点跑、今晚 21 点再跑，滚动窗口会误判成 2 次，自然日只算 1 次。
_FREQ_WARN = 1
_FREQ_BLOCK = 2
# 信用分单次下跌超过该值视为异常（扣分制下正常波动在个位数）
_CREDIT_DROP_WARN = 1
_CREDIT_DROP_BLOCK = 10
# 时段偏离阈值（小时）：样本足够时才判
_HOUR_SHIFT_LIMIT = 4
_HOUR_SAMPLE_MIN = 3


@dataclass
class RiskFinding:
    """单条预检结论。"""

    code: str
    level: str
    message: str
    detail: dict = field(default_factory=dict)

    @property
    def is_known_limitation(self) -> bool:
        return bool(self.detail.get("known_limitation"))

    def line(self) -> str:
        return "[%s] %s" % (_LEVEL_LABEL.get(self.level, self.level), self.message)


@dataclass
class RiskReport:
    """一次预检的全部结论。"""

    findings: List[RiskFinding] = field(default_factory=list)

    @property
    def level(self) -> str:
        if not self.findings:
            return LEVEL_OK
        return max((item.level for item in self.findings), key=lambda lv: _LEVEL_RANK[lv])

    @property
    def blocked(self) -> bool:
        return any(item.level == LEVEL_BLOCK for item in self.findings)

    @property
    def warnings(self) -> List[RiskFinding]:
        return [item for item in self.findings if item.level == LEVEL_WARN]

    def blocking_reason(self) -> str:
        for item in self.findings:
            if item.level == LEVEL_BLOCK:
                return item.message
        return ""

    def lines(self, include_ok: bool = True) -> List[str]:
        """逐条渲染。``known_limitation`` 的项归到末尾，避免每次刷屏打断主线。"""
        main, limits = [], []
        for item in self.findings:
            if not include_ok and item.level == LEVEL_OK:
                continue
            (limits if item.is_known_limitation else main).append(item.line())
        if limits:
            main.append("已知限制: " + "；".join(line for line in limits))
        return main


# --------------------------------------------------------------------- 解析
def extract_credit_score(payload: Any) -> Optional[int]:
    """从信用分响应里找出分数。

    服务端结构未公开，字段名可能是 ``creditScore`` / ``score`` / ``creditValue``，
    因此按「键名含 score 且值可转整数」递归搜索，取第一个命中项。
    认不出来就返回 None —— 猜一个值写进账本，比没有账本更糟。
    """
    hits: List[int] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if "score" in str(key).lower() and not isinstance(value, (dict, list, bool)):
                    try:
                        hits.append(int(float(value)))
                    except (TypeError, ValueError):
                        pass
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return hits[0] if hits else None


# --------------------------------------------------------------------- 检查项
def _check_control_message(telemetry) -> RiskFinding:
    """控制信令：服务端下发的封禁/维护公告。"""
    try:
        response = telemetry.check_control_message()
    except Exception as exc:
        return RiskFinding("control_message", LEVEL_WARN, "控制信令: 查询失败（%s）" % exc)

    if not isinstance(response, dict) or response.get("error") is not None:
        return RiskFinding("control_message", LEVEL_WARN, "控制信令: 查询失败，无法确认账号状态")
    body = str(response.get("body") or "")
    hits = [word for word in _BAN_KEYWORDS if word in body]
    if hits:
        return RiskFinding("control_message", LEVEL_BLOCK,
                           "控制信令: 下发内容含处置关键词 %s，账号可能已被限制" % "/".join(hits),
                           {"body": body[:500]})
    return RiskFinding("control_message", LEVEL_OK, "控制信令: 无封禁或维护公告")


def _check_credit(telemetry, profile_client, ledger) -> RiskFinding:
    """信用分：扣分制，只有分数序列才有意义。"""
    try:
        payload = telemetry.get_credit_popup(profile_client, retries=0)
    except Exception as exc:
        return RiskFinding("credit", LEVEL_WARN, "信用分: 查询失败（%s），无法确认扣分趋势" % exc)

    score = extract_credit_score(payload)
    if score is None:
        return RiskFinding("credit", LEVEL_WARN,
                           "信用分: 响应中未识别到分数字段，本次不参与趋势比对",
                           {"payload_keys": list(payload.keys()) if isinstance(payload, dict) else []})

    previous = ledger.last_credit()
    if previous is None:
        return RiskFinding("credit", LEVEL_OK, "信用分: 当前 %d（首次记录，作为基线）" % score,
                           {"score": score})

    drop = previous - score
    if drop >= _CREDIT_DROP_BLOCK:
        return RiskFinding("credit", LEVEL_BLOCK,
                           "信用分: %d → %d，单次下跌 %d 分，账号大概率已被判定异常"
                           % (previous, score, drop), {"score": score, "previous": previous})
    if drop >= _CREDIT_DROP_WARN:
        return RiskFinding("credit", LEVEL_WARN,
                           "信用分: %d → %d，下跌 %d 分（扣分制下每次提交都可能累积）"
                           % (previous, score, drop), {"score": score, "previous": previous})
    return RiskFinding("credit", LEVEL_OK, "信用分: 当前 %d（上次 %d，未下跌）" % (score, previous),
                       {"score": score, "previous": previous})


def _check_cheat_blacklist(telemetry, profile_client, ledger) -> RiskFinding:
    """作弊工具特征库：拉下来落盘，供部署时自查环境。"""
    try:
        payload = telemetry.get_cheat_blacklist(profile_client, retries=0)
    except Exception as exc:
        return RiskFinding("cheat_blacklist", LEVEL_WARN, "作弊特征库: 拉取失败（%s）" % exc)

    if not isinstance(payload, dict) or payload.get("error") not in (None, 0, 10000, 200):
        return RiskFinding("cheat_blacklist", LEVEL_WARN, "作弊特征库: 服务端未返回数据，无法确认扫描项")

    try:
        path = ledger.save_artifact("cheat_blacklist.json", payload)
    except Exception:
        path = None

    count = _count_items(payload)
    place = "已存 state/%s" % path.name if path is not None else "未能落盘"
    return RiskFinding("cheat_blacklist", LEVEL_OK,
                       "作弊特征库: 服务端在下发 %d 项特征（%s），本实现不加载任何注入模块"
                       % (count, place), {"count": count})


def _check_barrier_free(telemetry, profile_client, ledger) -> RiskFinding:
    """无障碍服务检测策略：本实现不注册无障碍服务，天然不触发。"""
    try:
        payload = telemetry.get_barrier_free_policy(profile_client, retries=0)
    except Exception as exc:
        return RiskFinding("barrier_free", LEVEL_WARN, "无障碍策略: 拉取失败（%s）" % exc)

    if not isinstance(payload, dict) or payload.get("error") not in (None, 0, 10000, 200):
        return RiskFinding("barrier_free", LEVEL_WARN, "无障碍策略: 服务端未返回数据")

    try:
        path = ledger.save_artifact("barrier_free_policy.json", payload)
    except Exception:
        path = None
    place = "已存 state/%s" % path.name if path is not None else "未能落盘"
    return RiskFinding("barrier_free", LEVEL_OK,
                       "无障碍策略: 已同步（%s），本实现不注册无障碍服务，该检测项天然不命中" % place)


def _check_device(ledger, device_id: str, now_ms: int) -> RiskFinding:
    """设备指纹：变更即触发 10121。"""
    try:
        status = ledger.touch_device(device_id, at_ms=now_ms)
        ledger.save()
    except Exception as exc:
        return RiskFinding("device", LEVEL_WARN, "设备指纹: 账本读写失败（%s）" % exc)

    short = device_id[:8] + "…" if len(device_id) > 8 else device_id
    if status == "changed":
        known = ledger.known_devices()
        return RiskFinding("device", LEVEL_WARN,
                           "设备指纹: %s 不在历史记录中（账本已有 %d 个指纹），"
                           "服务端会按新设备登录处理，可能返回 10121" % (short, len(known)),
                           {"device_id": device_id, "known": known})
    if status == "new":
        return RiskFinding("device", LEVEL_OK, "设备指纹: %s（首次登记为本机设备）" % short,
                           {"device_id": device_id})
    return RiskFinding("device", LEVEL_OK, "设备指纹: %s（与历史一致）" % short,
                       {"device_id": device_id})


def _check_frequency(ledger, now_ms: int) -> List[RiskFinding]:
    """提交频次与时段：服务端最廉价、也最有效的异常发现手段。"""
    findings: List[RiskFinding] = []
    today = local_date_of(now_ms)
    count = 0
    try:
        for item in ledger.recent_runs(50):
            if not item.get("ok"):
                continue
            if today and local_date_of(item.get("at_ms", 0)) == today:
                count += 1
    except Exception:
        count = 0

    if count >= _FREQ_BLOCK:
        findings.append(RiskFinding("frequency", LEVEL_BLOCK,
                                    "提交频次: 今日已成功提交 %d 次，"
                                    "远超学校「每天一次」的常规限制，继续提交会进入人工复核" % count,
                                    {"count": count, "date": today}))
    elif count >= _FREQ_WARN:
        findings.append(RiskFinding("frequency", LEVEL_WARN,
                                    "提交频次: 今日已成功提交 %d 次，"
                                    "确认学校允许单日多次再继续" % count, {"count": count, "date": today}))
    else:
        findings.append(RiskFinding("frequency", LEVEL_OK, "提交频次: 今日无成功提交记录",
                                    {"date": today}))

    findings.append(_check_hour(ledger, now_ms))
    return findings


def _check_hour(ledger, now_ms: int) -> RiskFinding:
    """时段一致性：本人常年晚 7 点跑，突然凌晨 3 点跑，比任何字段都显眼。"""
    try:
        runs = ledger.recent_runs(30)
    except Exception:
        runs = []
    if len(runs) < _HOUR_SAMPLE_MIN:
        return RiskFinding("hour", LEVEL_OK,
                           "时段: 历史样本 %d 条，不足以判断常用时段" % len(runs))

    # 常用时段取历史提交小时的中位数，比众数稳（众数在样本少时会跳）
    hours = []
    for item in runs:
        hour = time_hour_of(item.get("at_ms", 0))
        if hour is not None:
            hours.append(hour)
    if len(hours) < _HOUR_SAMPLE_MIN:
        return RiskFinding("hour", LEVEL_OK, "时段: 有效样本不足，跳过判断")

    hours.sort()
    median = hours[len(hours) // 2]
    current = time_hour_of(now_ms)
    if current is None:
        return RiskFinding("hour", LEVEL_OK, "时段: 当前时间戳异常，跳过时段判断")
    # 跨零点取环形距离：23 点与 1 点相差 2 小时而不是 22 小时
    shift = min((current - median) % 24, (median - current) % 24)
    if shift >= _HOUR_SHIFT_LIMIT:
        return RiskFinding("hour", LEVEL_WARN,
                           "时段: 本次 %d 时，历史多在 %d 时前后（偏差 %d 小时），"
                           "作息突变是异常行为模型的常见触发条件" % (current, median, shift),
                           {"current_hour": current, "usual_hour": median, "shift": shift})
    return RiskFinding("hour", LEVEL_OK,
                       "时段: 本次 %d 时，与历史常用 %d 时一致" % (current, median),
                       {"current_hour": current, "usual_hour": median})


def _check_tls() -> RiskFinding:
    """TLS 指纹：能如实说清楚的就不用含糊过去。"""
    return RiskFinding(
        "tls_fingerprint",
        LEVEL_WARN,
        "网络栈为 urllib + %s，无 HTTP/2、无 OkHttp JA3 指纹；"
        "服务端若做 TLS 指纹比对，此项无法自证" % ssl.OPENSSL_VERSION,
        {"openssl": ssl.OPENSSL_VERSION, "known_limitation": True},
    )


def time_hour_of(ms: int) -> Optional[int]:
    """毫秒时间戳 → 本地小时（0~23）；非法时间戳返回 None。

    账本里的时间戳来自磁盘，可能被手工改坏或由早期版本写错。一个坏值
    不应该让整个预检抛异常，所以这里显式返回 None 由调用方跳过。
    """
    try:
        return datetime.datetime.fromtimestamp(int(ms) / 1000.0).hour
    except (OverflowError, OSError, ValueError, TypeError):
        return None


def local_date_of(ms: int) -> Optional[str]:
    """毫秒时间戳 → 本地自然日 ``YYYY-MM-DD``；非法时间戳返回 None。"""
    try:
        return datetime.datetime.fromtimestamp(int(ms) / 1000.0).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError, TypeError):
        return None


def _count_items(payload: Any) -> int:
    """粗算特征条目数：优先看 data 下的列表长度，退化为递归计数叶子。"""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            for value in data.values():
                if isinstance(value, list):
                    return len(value)
    return 0


# --------------------------------------------------------------------- 入口
def preflight(telemetry,
              profile_client,
              ledger,
              *,
              now_ms: int,
              device_id: str = "",
              log: Optional[Callable[[str], None]] = None) -> RiskReport:
    """执行全部预检，返回汇总结论。单项异常不会中断其余检查。"""
    findings: List[RiskFinding] = []
    checks = (
        lambda: _check_control_message(telemetry),
        lambda: _check_credit(telemetry, profile_client, ledger),
        lambda: _check_cheat_blacklist(telemetry, profile_client, ledger),
        lambda: _check_barrier_free(telemetry, profile_client, ledger),
    )
    for check in checks:
        try:
            finding = check()
        except Exception as exc:  # 兜底：任何未预料的异常都不得阻断主流程
            finding = RiskFinding("internal", LEVEL_WARN, "预检子项异常: %s" % exc)
        findings.append(finding)
        if log:
            log(finding.line())

    if device_id:
        findings.append(_check_device(ledger, device_id, now_ms))
        if log:
            log(findings[-1].line())

    try:
        findings.extend(_check_frequency(ledger, now_ms))
    except Exception as exc:
        findings.append(RiskFinding("frequency", LEVEL_WARN, "提交频次: 判断失败（%s）" % exc))
    if log:
        for item in findings[-2:]:
            log(item.line())

    findings.append(_check_tls())
    if log:
        log(findings[-1].line())

    return RiskReport(findings)
