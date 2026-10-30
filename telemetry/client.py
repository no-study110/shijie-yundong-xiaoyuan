# -*- coding: utf-8 -*-
import hashlib
import json
import ssl
import time
import urllib.request
from typing import Optional, Dict, Any, List

from core.config import load_customer_device
from .config import (
    URL_STARTUP_PV,
    URL_DATA_COLLECTION_PV,
    URL_DEVICE_VERIFY,
    URL_CONTROL_MESSAGE,
    URL_BARRIER_FREE,
    URL_CHEAT_LIST,
    URL_RANK_LIST,
    URL_CREDIT_POPUP
)


def _derive_android_id(device_id: str) -> str:
    """由 DeviceId 确定性派生 16 位十六进制 androidId。

    必须确定性派生而不是随机：同一台设备每次上报的 androidId 必须一致，
    随机值反而制造了「同一设备短时间换了 Android ID」这种更严重的矛盾。
    空串同样是痕迹 —— 真实设备的这个字段永远有值。
    """
    seed = "androidId:%s" % (device_id or "unknown")
    return hashlib.md5(seed.encode("utf-8")).hexdigest()[:16]


class TelemetryClient:
    """运动世界校园 7.3.92 客户端行为特征、反作弊规则同步与数据埋点客户端"""

    def __init__(self, device_config: Optional[Dict[str, Any]] = None, proxy: Optional[str] = None):
        self.device = load_customer_device()
        if device_config:
            self.device.update(device_config)
        self.android_id = _derive_android_id(self.device.get("DeviceId", ""))
        # Android 10+ 起普通应用读不到真实 MAC，系统统一返回全零（本地管理位已置 1）。
        # 保留这个标准占位值比填随机串更贴近现实 —— 随机 MAC 才是伪造痕迹。
        self.android_mac = "02:00:00:00:00:00"
        self.ssl_ctx = ssl.create_default_context()
        # 代理配置：与主网关通道保持一致，避免埋点/设备验证/排行榜走直连泄露真实出口 IP
        self.proxy = proxy
        self._proxy_handler = None
        if proxy:
            self._proxy_handler = urllib.request.ProxyHandler({
                'http': proxy,
                'https': proxy
            })

    def _open(self, req, timeout: float):
        """按代理配置打开请求：有代理则走 opener，否则走默认 urlopen。"""
        if self._proxy_handler:
            opener = urllib.request.build_opener(
                self._proxy_handler, urllib.request.HTTPSHandler(context=self.ssl_ctx))
            return opener.open(req, timeout=timeout)
        return urllib.request.urlopen(req, context=self.ssl_ctx, timeout=timeout)

    def _post_json(self, url: str, payload: dict, custom_headers: Optional[dict] = None) -> dict:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "okhttp/4.12.0",
        }
        if custom_headers:
            headers.update(custom_headers)
        data_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")
        try:
            with self._open(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return {"error": -1, "message": str(e)}

    def send_startup_pv(self, uid: int, unid: int, gender: int = 0) -> dict:
        """冷启动初始化埋点 (对齐 7.3.92 抓包格式)"""
        body = {
            "uid": int(uid),
            "unid": int(unid),
            "appVersion": self.device.get("appVersion", "7.3.92"),
            "gender": int(gender),
            "caid": 2023,
            "osType": 0,
            "deviceId": self.device.get("DeviceId", "")
        }
        return self._post_json(URL_STARTUP_PV, body)

    def send_page_exposure(self, unid: int, items: Optional[List[str]] = None) -> dict:
        """页面曝光行为打点 (My_page_exposure 等)"""
        now_ms = int(time.time() * 1000)
        target_items = items or ["My_page_exposure", "Exposure_of_sports_page_banner/2064"]
        events = [{"createTime": now_ms, "itemId": it, "module": 10018} for it in target_items]
        body = {
            "androidId": self.android_id,
            "androidMac": self.android_mac,
            "data": events,
            "sn": "",
            "unid": int(unid)
        }
        return self._post_json(URL_DATA_COLLECTION_PV, body)

    def send_device_verify(self, uid: int, unid: int, unid_override: Optional[int] = None) -> dict:
        """极验设备验证特征打点 (/api/v620/datacollection/geetest/deviceVerify)

        参数：
        - uid：用户ID
        - unid：学校UNID
        - unid_override：可选，重写性UNID，用于API请求时的临时身份（例如：对同一账户同时请求不同学校的数据）

        返回：服务器响应JSON
        """
        target_unid = unid_override if unid_override is not None else unid
        body = {
            "uid": int(uid),
            "unid": int(target_unid),
            "deviceId": self.device.get("DeviceId", ""),
            "timestamp": int(time.time() * 1000),
        }
        return self._post_json(URL_DEVICE_VERIFY, body)

    def check_control_message(self) -> dict:
        """心跳与控制信令检查 (control.gxapp.iydsj.com/getMessage)"""
        req = urllib.request.Request(URL_CONTROL_MESSAGE, headers={"User-Agent": "okhttp/4.12.0"}, method="GET")
        try:
            with self._open(req, timeout=5) as resp:
                return {"status": resp.status, "body": resp.read().decode("utf-8", errors="ignore")}
        except Exception as e:
            return {"error": -1, "message": str(e)}

    def get_barrier_free_policy(self, netsec_client, retries=None) -> dict:
        """拉取服务端无障碍服务检测与反辅助策略 (/api/v1/barrierFree/getBarrierFree)

        ``retries`` 供风控预检传入 0：预检失败即降级，重试只会白等而且是
        无谓的请求量。
        """
        return netsec_client.execute_request("GET", URL_BARRIER_FREE, retries=retries)

    def get_cheat_blacklist(self, netsec_client, retries=None) -> dict:
        """拉取客户端黑名单应用与作弊工具包名特征库 (/api/v78/cheat/cheatlist)"""
        return netsec_client.execute_request("POST", URL_CHEAT_LIST, body_plain={}, retries=retries)

    def get_rank_list(self, netsec_client, rank_type: int = 1) -> dict:
        """跑后排行榜查询 (/api/v41/rank)"""
        return netsec_client.execute_request("POST", URL_RANK_LIST, body_plain={"type": int(rank_type)})

    def get_credit_popup(self, netsec_client, retries=None) -> dict:
        """信用分弹窗与风险审查 (/api/creditscore/v70390/popup)"""
        return netsec_client.execute_request("GET", URL_CREDIT_POPUP, retries=retries)
