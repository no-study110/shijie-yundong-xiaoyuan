# -*- coding: utf-8 -*-
"""本地运行账本 —— 把「这个账号过去怎么跑的」落盘。

服务端判定风险靠的是**纵向比对**：信用分是否连续下滑、设备指纹是否频繁
更换、同一账号是否在高频提交。这三点都只有存在历史序列时才成立，
而本工具每次调用都是独立进程 —— 没有账本，每次运行都是「第一次」，
既看不见趋势，也无法在提交前发现异常。

账本只保存判定所需的摘要（时间、距离、时长、信用分、设备指纹），
不保存 Token、不保存轨迹、不保存任何凭据。

设计约束：
- 写盘原子化（临时文件 + os.replace），中途崩溃不会留下半个 JSON；
- 账本损坏**绝不静默删除**，改名留档后重建 —— 用户数据优先于程序便利；
- 损坏或缺失一律降级为空账本，不让风控辅助功能反过来阻断主流程。
"""
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# 账本默认落在数据目录的 state/ 下（桌面端=项目根，Android=应用私有目录），
# 与代码同目录便于随项目迁移
from config_loader import get_data_dir

DEFAULT_PATH = get_data_dir() / "state" / "run_ledger.json"

SCHEMA_VERSION = 1
MAX_RUNS = 200
MAX_CREDITS = 100
MAX_DEVICES = 20

# 设备指纹登记结果
DEVICE_NEW = "new"
DEVICE_SAME = "same"
DEVICE_CHANGED = "changed"


class RunLedger:
    """运行账本：读写本地 JSON，缺省路径 ``state/run_ledger.json``。"""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else DEFAULT_PATH
        self._data: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------ 读写
    @property
    def data(self) -> Dict[str, Any]:
        if self._data is None:
            self._data = self._load()
        return self._data

    @staticmethod
    def _empty() -> Dict[str, Any]:
        return {"version": SCHEMA_VERSION, "devices": [], "runs": [], "credits": []}

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                raw = json.load(fp)
            if not isinstance(raw, dict):
                raise ValueError("账本根节点不是 JSON 对象")
        except Exception:
            # 损坏时改名留档：用户可能想找回历史，直接覆盖等于丢数据
            stamp = int(time.time())
            try:
                os.replace(self.path, self.path.with_name("%s.corrupt-%d" % (self.path.name, stamp)))
            except OSError:
                pass
            return self._empty()

        for key in ("devices", "runs", "credits"):
            if not isinstance(raw.get(key), list):
                raw[key] = []
        raw["version"] = SCHEMA_VERSION
        return raw

    def save(self) -> None:
        """原子写：先写 .tmp 再替换，避免半截文件。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(self.data, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def _trim(self) -> None:
        data = self.data
        if len(data["runs"]) > MAX_RUNS:
            data["runs"] = data["runs"][-MAX_RUNS:]
        if len(data["credits"]) > MAX_CREDITS:
            data["credits"] = data["credits"][-MAX_CREDITS:]
        if len(data["devices"]) > MAX_DEVICES:
            data["devices"] = data["devices"][-MAX_DEVICES:]

    # ------------------------------------------------------------- 写入接口
    def touch_device(self, device_id: str, at_ms: Optional[int] = None) -> str:
        """登记设备指纹，返回 ``new`` / ``same`` / ``changed``。

        ``changed`` 是本账本最重要的信号：历史里有别的指纹、当前这个不在其中，
        说明换机或指纹被重新生成过 —— 服务端会按新设备登录处理（10121）。
        """
        now = int(at_ms if at_ms is not None else time.time() * 1000)
        devices = self.data["devices"]
        for item in devices:
            if item.get("id") == device_id:
                item["last_ms"] = now
                item["runs"] = int(item.get("runs", 0)) + 1
                return DEVICE_SAME
        status = DEVICE_CHANGED if devices else DEVICE_NEW
        devices.append({"id": device_id, "first_ms": now, "last_ms": now, "runs": 1})
        return status

    def record_run(self, *,
                   at_ms: int,
                   distance_m: int,
                   time_sec: int,
                   success: bool = True,
                   rrid: Optional[int] = None,
                   credit: Optional[int] = None,
                   device_id: str = "") -> None:
        """记录一次提交结果，并顺带登记设备指纹与信用分。"""
        entry: Dict[str, Any] = {
            "at_ms": int(at_ms),
            "distance_m": int(distance_m),
            "time_sec": int(time_sec),
            "ok": bool(success),
        }
        if rrid is not None:
            entry["rrid"] = rrid
        if credit is not None:
            entry["credit"] = int(credit)
        self.data["runs"].append(entry)
        if device_id:
            self.touch_device(device_id, at_ms=at_ms)
        if credit is not None:
            self.data["credits"].append({"at_ms": int(at_ms), "score": int(credit)})
        self._trim()
        self.save()

    def record_credit(self, score: int, at_ms: Optional[int] = None) -> None:
        now = int(at_ms if at_ms is not None else time.time() * 1000)
        self.data["credits"].append({"at_ms": now, "score": int(score)})
        self._trim()
        self.save()

    # ------------------------------------------------------------- 查询接口
    def recent_runs(self, count: int = 20) -> List[Dict[str, Any]]:
        return self.data["runs"][-max(1, int(count)):]

    def runs_since(self, since_ms: int, only_successful: bool = True) -> List[Dict[str, Any]]:
        picked = []
        for item in self.data["runs"]:
            if int(item.get("at_ms", 0)) < int(since_ms):
                continue
            if only_successful and not item.get("ok", False):
                continue
            picked.append(item)
        return picked

    def credits(self) -> List[Dict[str, Any]]:
        return self.data["credits"]

    def last_credit(self) -> Optional[int]:
        trail = self.data["credits"]
        if not trail:
            return None
        try:
            return int(trail[-1].get("score"))
        except (TypeError, ValueError):
            return None

    def credit_trail(self, count: int = 10) -> List[int]:
        picked = self.data["credits"][-max(1, int(count)):]
        scores = []
        for item in picked:
            try:
                scores.append(int(item.get("score")))
            except (TypeError, ValueError):
                continue
        return scores

    def known_devices(self) -> List[str]:
        return [str(item.get("id")) for item in self.data["devices"] if item.get("id")]

    def summary_lines(self) -> List[str]:
        """供 CLI 直接打印的账本摘要。"""
        runs = self.data["runs"]
        ok_runs = [item for item in runs if item.get("ok")]
        lines = []
        if not runs:
            return ["账本: 无历史记录（本次是首次运行）"]
        lines.append("账本: 累计 %d 次记录，其中成功 %d 次" % (len(runs), len(ok_runs)))
        trail = self.credit_trail(5)
        if trail:
            trend = " → ".join(str(score) for score in trail)
            lines.append("账本: 信用分近 %d 次 %s" % (len(trail), trend))
        devices = self.data["devices"]
        if devices:
            lines.append("账本: 登记设备指纹 %d 个（最近 %s）"
                         % (len(devices), str(devices[-1].get("id", ""))[:8] + "…"))
        return lines

    # ------------------------------------------------------------- 旁路产物
    def save_artifact(self, name: str, payload: Any) -> Path:
        """把服务端下发的风控规则原文落到 state/ 下，供离线自查。

        黑名单与无障碍策略是服务端**实际在扫描**的证据，把它们存下来
        比只在日志里看一眼有用得多 —— 排查时能直接对照特征条目。
        """
        target = self.path.parent / name
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
        return target
