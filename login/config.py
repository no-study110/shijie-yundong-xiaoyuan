# -*- coding: utf-8 -*-
"""运动世界校园登录模块配置 - 支持环境变量和配置覆盖"""

import os

# 从环境变量或config_loader加载配置
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("PROJECT_ROOT", _BASE_DIR)

try:
    from config_loader import get_server_config, get_device_defaults
    SERVER_CONFIG = get_server_config()
    DEVICE_DEFAULTS = get_device_defaults()
except ImportError:
    SERVER_CONFIG = {"host_run": "https://run.gxapp.iydsj.com"}
    DEVICE_DEFAULTS = {"AppVersion": "7.3.92"}

# 从环境变量中读取配置，优先级高于默认配置
ENV_HOST_RUN = os.getenv("HOST_RUN") or os.getenv("RUN_HOST")
ENV_HOST_DISCOVERY = os.getenv("HOST_DISCOVERY") or os.getenv("DISCOVERY_HOST")

# 接口端点 - 优先使用环境变量配置，否则使用config_loader配置，最后回退到默认配置
DEFAULT_HOST_RUN = ENV_HOST_RUN or SERVER_CONFIG.get("host_run", "https://run.gxapp.iydsj.com")
DEFAULT_HOST_DISCOVERY = ENV_HOST_DISCOVERY or SERVER_CONFIG.get("host_discovery", "https://discovery.gxapp.iydsj.com")

# 接口端点
URL_CHECK_GEE_USE = f"{DEFAULT_HOST_RUN}/api/v65/security/checkGeeUse"
URL_GEE_VALIDATE = f"{DEFAULT_HOST_RUN}/api/v70270/security/geevalidate"
URL_LOGIN = f"{DEFAULT_HOST_RUN}/api/v70100/login"

# 极验登录 Captcha ID
LOGIN_CAPTCHA_ID = "3b02ad39bd099fd3a8336d9347a189ab"

# 通用设备与系统占位指纹 (通用占位参数) - 从配置加载
DEFAULT_DEVICE_CONFIG = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "appVersion": DEVICE_DEFAULTS.get("AppVersion", "7.3.92"),
    "physicPixel": DEVICE_DEFAULTS.get("PhysicPixel", "1080x2400"),
    "logicPixel": DEVICE_DEFAULTS.get("LogicPixel", "393x873"),
    "isRoot": DEVICE_DEFAULTS.get("IsRoot", "0"),
    "osType": DEVICE_DEFAULTS.get("OsType", "0"),
    "uid": "-1",
    "IMEI": "",
    "blMac": "",
    "token": "",
    "tokenSign": "",
    "cpuModel": DEVICE_DEFAULTS.get("CpuModel", "arm64-v8a"),
    "deviceName": DEVICE_DEFAULTS.get("DeviceName", "Android Device"),
    "appInstallTime": "0",
    "androidId": "",
    "DeviceId": DEVICE_DEFAULTS.get("DeviceId", "00000000000000000000000000000000"),
    "appUpdateTime": "0",
    "wifiMac": "",
    "CustomDeviceId": "",
    "osVersion": DEVICE_DEFAULTS.get("OsVersion", "13"),
    "User-Agent": DEVICE_DEFAULTS.get("UserAgent", "okhttp/4.12.0")
}
