# -*- coding: utf-8 -*-
import json
import ssl
import time
import urllib.error
import urllib.request
import uuid
from typing import Dict, Any, Optional, Union

from .config import load_customer_device
from .crypto import NetSecClient, compute_token_sign, decode_response
from .retry import (
    RetryPolicy,
    RateLimiter,
    classify_response,
)


class BaseNetSecClient:
    """运动世界校园 7.3.92 通用 NetSecKit 4.2.4 协议客户端基类"""

    def __init__(self,
                 uid: Optional[Union[int, str]] = None,
                 token: Optional[str] = None,
                 unid: Optional[Union[int, str]] = None,
                 device_config: Optional[Dict[str, Any]] = None,
                 retry_policy: Optional[RetryPolicy] = None,
                 proxy: Optional[str] = None):
        self.uid = str(uid) if uid is not None and str(uid) != "-1" else None
        self.token = str(token) if token else None
        self.unid = str(unid) if unid is not None else None

        # 初始化设备指纹
        self.device_config = load_customer_device()
        if device_config:
            self.device_config.update(device_config)

        self.crypto = NetSecClient()
        self.ssl_ctx = ssl.create_default_context()
        # 统一重试/退避/限速策略（各子客户端继承）
        self.retry_policy = retry_policy or RetryPolicy()
        self.rate_limiter = RateLimiter(self.retry_policy.min_interval)

        # 代理配置（支持 http/https/socks5 代理，格式: "http://host:port" 或 "socks5://host:port"）
        self.proxy = proxy
        self._proxy_handler = None
        if proxy:
            self._proxy_handler = urllib.request.ProxyHandler({
                'http': proxy,
                'https': proxy
            })

    def set_credentials(self, uid: Optional[Union[int, str]], token: Optional[str], unid: Optional[Union[int, str]] = None):
        """动态设置或更新鉴权凭据"""
        if uid is not None and str(uid) != "-1":
            self.uid = str(uid)
        if token:
            self.token = str(token)
        if unid is not None:
            self.unid = str(unid)

    def build_headers(self, custom_headers: Optional[Dict[str, str]] = None, sign_params: Optional[Dict[str, str]] = None) -> tuple:
        """组装标准请求头并计算 headerSign 与 tokenSign"""
        now_ms = int(time.time() * 1000)
        nonce = str(uuid.uuid4())

        headers = dict(self.device_config)
        headers["timeStamp"] = str(now_ms)
        headers["nonce"] = nonce

        if self.uid:
            headers["uid"] = str(self.uid)
        if self.token:
            headers["token"] = str(self.token)

        if custom_headers:
            headers.update(custom_headers)

        # 1. 计算 tokenSign (仅在已有 token 或显式要求时签名)
        sign_dict = {
            "timeStamp": str(now_ms),
            "token": str(self.token or ""),
            "uid": str(self.uid or "")
        }
        if sign_params:
            sign_dict.update(sign_params)
        headers["tokenSign"] = compute_token_sign(sign_dict)

        # 2. 生成 headerSign (包含本会话密钥种子 kd1..kd4 并派生 h_resp_key)
        h_wire, h_resp_key = self.crypto.encrypt_request(
            json.dumps(headers, separators=(",", ":"), ensure_ascii=False)
        )
        headers["headerSign"] = json.dumps(h_wire, separators=(",", ":"))

        return headers, h_resp_key

    def execute_request(self,
                        method: str,
                        url: str,
                        body_plain: Optional[Union[dict, list]] = None,
                        custom_headers: Optional[Dict[str, str]] = None,
                        sign_params: Optional[Dict[str, str]] = None,
                        timeout: int = 20,
                        retries: Optional[int] = None) -> Any:
        """发送加密网络请求并自动解密返回响应。

        重试语义由 core.retry.RetryPolicy 统一裁决：
        - 网络异常（连接失败/超时）与网关 5xx：指数退避后重试；
        - 业务错误（网关已返回明确业务码）：不重试，直接透传；
        - 鉴权失效（401）与设备保护（10121）：不重试，直接透传。
        """
        method = method.upper()
        policy = self.retry_policy
        max_retries = policy.max_retries if retries is None else max(0, int(retries))
        last_err: Optional[Exception] = None

        for attempt in range(max_retries + 1):
            headers, h_resp_key = self.build_headers(custom_headers, sign_params)

            req_data = None
            if method == "POST" and body_plain is not None:
                b_wire, _ = self.crypto.encrypt_request(body_plain)
                req_data = json.dumps(b_wire, separators=(",", ":")).encode("utf-8")

            req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
            try:
                self.rate_limiter.wait()
                # 使用代理（如果已配置）
                if self._proxy_handler:
                    opener = urllib.request.build_opener(self._proxy_handler, urllib.request.HTTPSHandler(context=self.ssl_ctx))
                    with opener.open(req, timeout=timeout) as resp:
                        raw = resp.read().decode("utf-8")
                        resp_obj = json.loads(raw)
                        return decode_response(resp_obj, h_resp_key)
                else:
                    with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=timeout) as resp:
                        raw = resp.read().decode("utf-8")
                        resp_obj = json.loads(raw)
                        return decode_response(resp_obj, h_resp_key)
            except urllib.error.HTTPError as e:
                # 网关已响应：4xx 属于已决业务错误，不重试；5xx 视为临时故障可退避重试。
                err_body = e.read().decode("utf-8", errors="ignore")
                if e.code >= 500:
                    kind, code = classify_response({"error": e.code})
                    if policy.should_retry(attempt, kind, code):
                        time.sleep(policy.delay_for(attempt))
                        continue
                try:
                    return json.loads(err_body)
                except Exception:
                    return {"error": e.code, "message": f"HTTP Error {e.code}: {e.reason}", "raw": err_body}
            except Exception as e:
                last_err = e
                kind, code = classify_response(None)
                if not policy.should_retry(attempt, kind, code):
                    break
                time.sleep(policy.delay_for(attempt))
        return {"error": -1, "message": str(last_err)}
