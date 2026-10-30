# -*- coding: utf-8 -*-
"""运动世界校园 7.3.92 协议配置 - 统一从 config_loader 加载"""
import os
import sys
import json
import uuid
from pathlib import Path

# 确保项目根目录在 sys.path（支持跨模块导入）
_BASE_DIR = Path(__file__).parent.parent.absolute()
if str(_BASE_DIR) not in sys.path:
    sys.path.insert(0, str(_BASE_DIR))

from config_loader import get_server_config, get_signature_salt, get_device_defaults

# 服务端网关主机（优先环境变量，回退到 config.ini 或硬编码默认值）
_server = get_server_config()
HOST_RUN = os.getenv("HOST_RUN", _server["host_run"])
HOST_DISCOVERY = os.getenv("HOST_DISCOVERY", _server["host_discovery"])
HOST_DATAPOINT = os.getenv("HOST_DATAPOINT", _server["host_datapoint"])
HOST_PARTNER = os.getenv("HOST_PARTNER", _server["host_partner"])

# 请求签名加盐（官方值，跨版本通用）
SIGN_SALT = get_signature_salt()

# 7.3.92 通用设备占位配置（从 config.ini 或硬编码默认值加载）
_device_defaults = get_device_defaults()
DEFAULT_DEVICE_CONFIG = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "appVersion": _device_defaults["AppVersion"],
    "physicPixel": _device_defaults["PhysicPixel"],
    "logicPixel": _device_defaults["LogicPixel"],
    "isRoot": _device_defaults["IsRoot"],
    "osType": _device_defaults["OsType"],
    "cpuModel": _device_defaults["CpuModel"],
    "deviceName": _device_defaults["DeviceName"],
    "DeviceId": _device_defaults["DeviceId"] or "00000000000000000000000000000000",
    "osVersion": _device_defaults["OsVersion"],
    "User-Agent": _device_defaults["UserAgent"]
}

def load_customer_device(config_path: str = None) -> dict:
    """从自定义配置文件加载设备指纹（兼容旧版 JSON 格式），否则生成随机 DeviceId"""
    dev = dict(DEFAULT_DEVICE_CONFIG)
    # 优先从 config.ini 读取 DeviceId；留空则生成随机
    if not _device_defaults["DeviceId"]:
        dev["DeviceId"] = uuid.uuid4().hex
    else:
        dev["DeviceId"] = _device_defaults["DeviceId"].replace("-", "")

    # 兼容旧版 JSON 配置文件（可选）
    if config_path and os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                phone = cfg.get("setting", {}).get("phone", {})
                if phone:
                    if "brand" in phone or "model" in phone:
                        dev["deviceName"] = f"{phone.get('brand', 'Android')} {phone.get('model', 'Device')}"
                    if "vdid" in phone:
                        dev["DeviceId"] = phone["vdid"].replace("-", "")
        except Exception:
            pass
    return dev
