# -*- coding: utf-8 -*-
"""统一配置加载器 - 支持 config.ini 自定义配置或回退到硬编码默认值"""
import os
import re
import uuid
import configparser
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

_BASE_DIR = Path(__file__).parent.absolute()
_CONFIG_PATH = _BASE_DIR / "config.ini"
_config: Optional[configparser.ConfigParser] = None


def _load_config() -> configparser.ConfigParser:
    """延迟加载配置文件（优先 config.ini，回退到硬编码默认值）"""
    global _config
    if _config is not None:
        return _config

    cfg = configparser.ConfigParser()

    # 硬编码默认值（config.ini 不存在时回退）
    cfg["server"] = {
        "host_run": "https://run.gxapp.iydsj.com",
        "host_discovery": "https://discovery.gxapp.iydsj.com",
        "host_datapoint": "https://datapoint.gxapp.iydsj.com",
        "host_partner": "https://partner.iydsj.com",
    }
    cfg["signature"] = {
        "sign_salt": "2slhe02lsfiwowlcixisla_sls-_slaor",
    }
    cfg["device"] = {
        "app_version": "7.3.92",
        "physic_pixel": "1080x2400",
        "logic_pixel": "393x873",
        "is_root": "0",
        "os_type": "0",
        "cpu_model": "arm64-v8a",
        "device_name": "Android Device",
        "device_id": "",
        "os_version": "13",
        "user_agent": "okhttp/4.12.0",
    }
    cfg["session"] = {
        "session_file": ".session.json",
    }
    cfg["webui"] = {
        "host": "127.0.0.1",
        # 与 config.ini、README/USAGE 里的 9999 保持一致。这里曾残留 8765，
        # 导致「有配置文件」和「没有配置文件」两种情况下端口不同。
        "port": "9999",
    }
    cfg["proxy"] = {
        "proxy_url": "",
    }
    cfg["captcha"] = {
        "node_executable": "",
        "node_path": "",
    }
    cfg["realism"] = {
        "enabled": "True",
        "simulate_physical_time": "True",
    }
    cfg["compliance"] = {
        "checkpoint_tolerance_m": "15.0",
    }

    # 覆盖加载 config.ini（如果存在）
    if _CONFIG_PATH.exists():
        try:
            cfg.read(_CONFIG_PATH, encoding="utf-8")
        except Exception as exc:
            print(f"警告: 加载 config.ini 失败，回退到默认配置: {exc}", flush=True)

    _config = cfg
    return _config


def get_base_dir() -> Path:
    """返回项目根目录（7.3.92 文件夹）"""
    return _BASE_DIR


def get_session_file_path() -> str:
    """返回会话文件绝对路径"""
    cfg = _load_config()
    relative = cfg.get("session", "session_file", fallback=".session.json")
    return str(_BASE_DIR / relative)


def get_server_config() -> Dict[str, str]:
    """返回服务端配置"""
    cfg = _load_config()
    return {
        "host_run": cfg.get("server", "host_run"),
        "host_discovery": cfg.get("server", "host_discovery"),
        "host_datapoint": cfg.get("server", "host_datapoint"),
        "host_partner": cfg.get("server", "host_partner"),
    }


def get_signature_salt() -> str:
    """返回签名加盐"""
    cfg = _load_config()
    return cfg.get("signature", "sign_salt")


def get_device_defaults() -> Dict[str, Any]:
    """返回设备默认配置"""
    cfg = _load_config()
    return {
        "AppVersion": cfg.get("device", "app_version"),
        "PhysicPixel": cfg.get("device", "physic_pixel"),
        "LogicPixel": cfg.get("device", "logic_pixel"),
        "IsRoot": cfg.get("device", "is_root"),
        "OsType": cfg.get("device", "os_type"),
        "CpuModel": cfg.get("device", "cpu_model"),
        "DeviceName": cfg.get("device", "device_name"),
        "DeviceId": get_or_create_device_id(),
        "OsVersion": cfg.get("device", "os_version"),
        "UserAgent": cfg.get("device", "user_agent"),
    }


def _write_device_id(new_id: str) -> None:
    """把 device_id 写进 config.ini 的 [device] 段，并让配置缓存失效。

    可能抛 ``OSError``（只读目录、磁盘满），由调用方决定是否吞掉。
    """
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fp:
            content = fp.read()
    else:
        content = ""

    if "[device]" in content:
        # 就地回填已有的 device_id 行；只有在段内完全没有该键时才插入新行。
        # 插入位置必须是 [device] 段内（下一个段头之前），不能 append 到文件
        # 末尾 —— 否则键会落进最后一个段，configparser 读不到，等于没写；
        # 而每次运行都追加又会累积重复键，把 config.ini 读坏。
        lines = content.splitlines(keepends=True)
        section_start = section_end = None
        for index, line in enumerate(lines):
            if line.strip().lower() != "[device]":
                continue
            section_start = index
            section_end = len(lines)
            for follow in range(index + 1, len(lines)):
                probe = lines[follow].strip()
                if probe.startswith("[") and probe.endswith("]"):
                    section_end = follow
                    break
            break

        if section_start is not None:
            filled = False
            for index in range(section_start + 1, section_end):
                stripped = lines[index].strip().replace(" ", "").lower()
                if not stripped.startswith("device_id="):
                    continue
                current = lines[index]
                indent = current[: len(current) - len(current.lstrip())]
                ending = "\n" if current.endswith("\n") else ""
                lines[index] = f"{indent}device_id = {new_id}{ending}"
                filled = True
                break
            if not filled:
                insert_at = section_end
                # 段尾通常是空行，插在最后一个非空行之后更接近手写格式
                while insert_at > section_start + 1 and not lines[insert_at - 1].strip():
                    insert_at -= 1
                if insert_at > 0 and not lines[insert_at - 1].endswith("\n"):
                    lines[insert_at - 1] += "\n"
                lines.insert(insert_at, f"device_id = {new_id}\n")
            content = "".join(lines)
        else:
            content = content.rstrip("\n") + "\n\n[device]\ndevice_id = " + new_id + "\n"
    else:
        content = content.rstrip("\n") + "\n\n[device]\ndevice_id = " + new_id + "\n"

    with open(_CONFIG_PATH, "w", encoding="utf-8") as fp:
        fp.write(content)
    # 重置缓存，使本次调用后续读取到新值
    global _config
    _config = None


def get_device_id_from_file() -> str:
    """只读取 config.ini 里写的指纹，**不生成、不写盘**。

    与 ``get_or_create_device_id`` 的区别：那个在为空时会造一个新值并写回，
    用于首次运行；而「查看当前配置」「设备指纹工具 --show」这类只读场景
    绝不能触发写入 —— 否则只是看一眼配置就换了设备。
    """
    return normalize_device_id(_load_config().get("device", "device_id", fallback=""))


def get_or_create_device_id() -> str:
    """返回本机稳定设备指纹。

    config.ini 中 [device] device_id 为空时，自动生成一个随机 32 位十六进制指纹
    并**写回 config.ini**，保证同一台机器多次运行复用同一指纹，避免每次重启
    都被服务端判定为新设备登录（10121）。

    生成规则：32 位小写十六进制，与手机 App 的 DeviceId 格式一致。
    """
    cfg = _load_config()
    existing = (cfg.get("device", "device_id", fallback="") or "").strip()
    if existing:
        return existing

    new_id = uuid.uuid4().hex
    try:
        _write_device_id(new_id)
    except OSError:
        # 只读目录：回退到内存中的随机值，不阻塞主流程
        pass
    return new_id


_DEVICE_ID_LOWER = re.compile(r"^[0-9a-f]{32}$")
_DEVICE_ID_ANY_CASE = re.compile(r"^[0-9a-fA-F]{32}$")


def normalize_device_id(raw: Any) -> str:
    """去掉空白与连字符。

    **刻意不转小写**：服务端按字符串比对指纹，大小写不同就是两台设备。
    悄悄改大小写，等于把「抄对了」变成「抄错了」。
    """
    return str(raw or "").replace("-", "").strip()


def validate_device_id(raw: Any) -> Tuple[bool, str, str]:
    """校验指纹格式，返回 ``(是否合法, 规范化后的值, 不合法原因)``。

    判据按本项目的生成规则：32 位小写十六进制。真实 App 是否也守这个规则
    未经实机验证，所以工具层保留了显式跳过校验的入口（``--force``）。
    """
    value = normalize_device_id(raw)
    if not value:
        return False, "", "值为空"
    if len(value) != 32:
        return False, value, "长度为 %d，应为 32" % len(value)
    if _DEVICE_ID_LOWER.match(value):
        return True, value, ""
    if _DEVICE_ID_ANY_CASE.match(value):
        return False, value, "含大写字母，服务端按字符比对，大小写不同会被当成两台设备"
    return False, value, "含非十六进制字符"


def set_device_id(raw: Any) -> Tuple[str, str]:
    """校验并写入指定指纹，返回 ``(旧值, 新值)``。

    格式不合法抛 ``ValueError``；写入失败抛 ``OSError`` —— 用户明确要求保存时，
    「以为存了其实没存」比直接报错更糟，所以这里不吞异常。
    """
    ok, value, reason = validate_device_id(raw)
    if not ok:
        raise ValueError("DeviceId 不合法（%s）：%r" % (reason, raw))
    old = (_load_config().get("device", "device_id", fallback="") or "").strip()
    _write_device_id(value)
    return old, value


def get_webui_config() -> Dict[str, Any]:
    """返回 Web UI 配置。

    缺少 [webui] 段或单个键时回退到本机默认值，而不是让 configparser 抛
    NoSectionError —— 用户手改 config.ini 删掉一段是很常见的，不该因此
    连控制台都启动不了。
    """
    cfg = _load_config()
    return {
        "host": cfg.get("webui", "host", fallback="127.0.0.1"),
        "port": cfg.getint("webui", "port", fallback=9999),
    }


def get_proxy_url() -> Optional[str]:
    """返回代理 URL（留空则直连）"""
    cfg = _load_config()
    url = cfg.get("proxy", "proxy_url", fallback="").strip()
    return url if url else None


def get_captcha_config() -> Dict[str, str]:
    """返回验证码求解依赖的配置。

    三个键的语义都是「留空则自动探测」：

    - ``node_executable``：node 可执行文件绝对路径
    - ``node_path``：NODE_PATH（多个目录用 os.pathsep 分隔）
    - ``playwright_path``：装了 playwright 的 node_modules 目录

    这段配置曾经只有定义没有调用方 —— 用户照着注释填了 node_path 却一点
    效果都没有，报错还说「请配置 NODE_PATH」，等于把人指回了原地。
    """
    cfg = _load_config()
    return {
        "node_executable": cfg.get("captcha", "node_executable", fallback="").strip(),
        "node_path": cfg.get("captcha", "node_path", fallback="").strip(),
        "playwright_path": cfg.get("captcha", "playwright_path", fallback="").strip(),
    }


def get_realism_defaults() -> Dict[str, bool]:
    """返回拟真增强默认开关"""
    cfg = _load_config()
    return {
        "enabled": cfg.getboolean("realism", "enabled", fallback=True),
        "simulate_physical_time": cfg.getboolean("realism", "simulate_physical_time", fallback=True),
    }


def get_checkpoint_tolerance() -> float:
    """返回打卡点容差（米）"""
    cfg = _load_config()
    return cfg.getfloat("compliance", "checkpoint_tolerance_m", fallback=15.0)
