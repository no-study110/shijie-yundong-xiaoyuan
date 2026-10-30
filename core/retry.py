# -*- coding: utf-8 -*-
"""统一重试、退避与错误分类策略。

为 7.3.92 网关交互提供三层能力：

1. RetryPolicy —— 指数退避（带抖动上限）与「什么情况才值得重试」的判定；
2. classify_response —— 把网关响应/网络异常归类为
   ok / auth / device_protected / server / business / network，
   便于上层针对 10121 设备保护、401 鉴权失效给出明确提示；
3. RateLimiter —— 统一的最小请求间隔，避免短时间高频打网关触发风控。

设计原则：
- 业务错误（网关已明确返回业务码）默认不重试，重试只会放大无效请求；
- 网络异常与网关 5xx 才退避重试；
- 鉴权失效（401）与设备保护（10121）绝不重试，重试无意义且可能加深风控标记。
"""
import random
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple

# 错误类别
OK = "ok"
AUTH_ERROR = "auth"
DEVICE_PROTECTED = "device_protected"
SERVER_ERROR = "server"
BUSINESS_ERROR = "business"
NETWORK_ERROR = "network"

# 设备保护：账号在其他设备登录，需要在 App 内退出登录或复用原设备指纹
DEVICE_PROTECT_CODES = frozenset({10121})
# 鉴权失效：token/uid/unid 头信息不全或已过期
AUTH_CODES = frozenset({401, 10101})
# 网关侧临时故障，值得退避重试
SERVER_CODES = frozenset({500, 502, 503, 504, -500})


def classify_response(response) -> Tuple[str, Optional[int]]:
    """把网关响应字典归类为 (类别, 错误码)。"""
    if not isinstance(response, dict):
        return NETWORK_ERROR, None
    code = response.get("error", response.get("status"))
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = None
    if code in (10000, 200, 0):
        return OK, code
    if code in DEVICE_PROTECT_CODES:
        return DEVICE_PROTECTED, code
    if code in AUTH_CODES:
        return AUTH_ERROR, code
    if code in SERVER_CODES:
        return SERVER_ERROR, code
    return BUSINESS_ERROR, code


@dataclass
class RetryPolicy:
    """指数退避重试策略。"""

    max_retries: int = 2
    base_delay: float = 0.5
    max_delay: float = 8.0
    multiplier: float = 2.0
    jitter: float = 0.1
    # 网络异常（连接失败/超时）是否重试
    retry_network_errors: bool = True
    # 网关 5xx 是否重试
    retry_server_errors: bool = True
    # 业务错误是否重试（默认关闭）
    retry_business_errors: bool = False
    # 额外的可重试业务码
    extra_retry_codes: frozenset = field(default_factory=frozenset)
    # 最小请求间隔（秒），用于限速；0 表示不限速
    min_interval: float = 0.2

    def delay_for(self, attempt: int, rng: Optional[random.Random] = None) -> float:
        """第 attempt 次失败（0 起算）后应等待的秒数。"""
        delay = self.base_delay * (self.multiplier ** max(0, attempt))
        delay = min(delay, self.max_delay)
        if self.jitter:
            rng = rng or random.Random()
            delay += delay * self.jitter * rng.uniform(-1.0, 1.0)
        return max(0.0, delay)

    def should_retry(self, attempt: int, kind: str, code: Optional[int] = None) -> bool:
        """判断第 attempt 次失败后是否应当再次尝试。"""
        if attempt >= self.max_retries:
            return False
        if kind == NETWORK_ERROR:
            return self.retry_network_errors
        if kind == SERVER_ERROR:
            return self.retry_server_errors
        if kind == BUSINESS_ERROR:
            if not self.retry_business_errors:
                return False
            return code is not None and code in self.extra_retry_codes
        # ok / auth / device_protected 均不重试
        return False

    def describe(self, kind: str, code: Optional[int] = None) -> str:
        """把错误类别翻译成面向使用者的中文提示。"""
        if kind == DEVICE_PROTECTED:
            return (f"code={code} 账号已在其他设备登录（设备保护拦截）。"
                    "请在手机 App 内「退出登录」后重试，或用 --device-id 指定原设备指纹。")
        if kind == AUTH_ERROR:
            return (f"code={code} 鉴权头信息无效或已过期。"
                    "请重新登录获取 Token，并确认 uid/unid 均为真实值。")
        if kind == SERVER_ERROR:
            return f"code={code} 网关临时异常，可稍后重试。"
        if kind == NETWORK_ERROR:
            return "网络异常：连接失败或超时。"
        if kind == BUSINESS_ERROR:
            return f"code={code} 业务错误。"
        return "成功。"


class RateLimiter:
    """最小请求间隔限速器。"""

    def __init__(self, min_interval: float = 0.2):
        self.min_interval = max(0.0, float(min_interval))
        self._last_sent_at: Optional[float] = None

    def wait(self, now: Optional[float] = None) -> float:
        """阻塞到允许发送的时刻，返回实际等待秒数。"""
        if self.min_interval <= 0:
            return 0.0
        now = time.monotonic() if now is None else now
        if self._last_sent_at is None:
            self._last_sent_at = now
            return 0.0
        elapsed = now - self._last_sent_at
        remaining = self.min_interval - elapsed
        if remaining > 0:
            time.sleep(remaining)
            self._last_sent_at = time.monotonic()
            return remaining
        self._last_sent_at = now
        return 0.0

    def reset(self) -> None:
        self._last_sent_at = None
