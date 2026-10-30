# -*- coding: utf-8 -*-
"""本地会话凭据持久化（token / uid / unid）— 仅缓存认证结果，不存储密码"""
import json
import os
from pathlib import Path


def load_session(path: str) -> dict:
    """从本地加载已保存的 token/uid/unid 凭据，不存在时返回空字典"""
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fp:
            data = json.load(fp)

        # 检查基本字段类型与合法性
        if not isinstance(data, dict):
            return {}
        if "token" in data and not isinstance(data["token"], str):
            return {}
        if "uid" in data and not isinstance(data["uid"], (str, int)):
            return {}
        if "unid" in data and not isinstance(data["unid"], (str, int)):
            return {}

        return data
    except (json.JSONDecodeError, IOError):
        return {}


def save_session(path: str, token: str, uid, unid=None):
    """将认证凭据保存到本地 JSON 文件（仅保存 token/uid/unid，不保存密码）"""
    if not isinstance(token, str) or not token.strip():
        raise ValueError("token must be a non-empty string")
    if not uid:
        raise ValueError("uid must be provided")

    data = {"token": token, "uid": str(uid)}
    if unid is not None:
        data["unid"] = str(unid)

    p = Path(path)
    # 创建父目录（如果不存在）
    p.parent.mkdir(parents=True, exist_ok=True)

    # 设置文件权限为仅当前用户可读写 (0o600)
    try:
        with open(p, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)

        # Windows 下设置文件为隐藏 + 只读（尽力而为）
        if os.name == "nt":
            try:
                import ctypes
                FILE_ATTRIBUTE_HIDDEN = 0x02
                ctypes.windll.kernel32.SetFileAttributesW(str(p), FILE_ATTRIBUTE_HIDDEN)
            except Exception:
                pass
        else:
            # Unix-like 系统设置权限为 0o600
            os.chmod(p, 0o600)
    except IOError as e:
        raise IOError(f"Failed to save session: {e}")
