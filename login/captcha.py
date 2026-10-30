# -*- coding: utf-8 -*-
import json
import os
import shutil
import subprocess
from typing import Optional

from .config import LOGIN_CAPTCHA_ID
from .models import GeetestResult

# 集中维护的 Node 运行时路径候选。
# 这些是通用安装位置；个人机器上的私有路径请通过环境变量 NODE_EXECUTABLE / NODE_PATH 指定，
# 不要写死在此处，否则换机器即失效。
_NODE_EXECUTABLE_FALLBACKS = [
    r"D:/Program Files/nodejs/node.exe",
    r"C:/Program Files/nodejs/node.exe",
    "/usr/bin/node",
    "/usr/local/bin/node",
    "/usr/bin/nodejs",
]

_NODE_PATH_FALLBACKS = [
    r"D:/Program Files/nodejs/node_modules",
    r"C:/Program Files/nodejs/node_modules",
    "/usr/lib/node_modules",
    "/usr/local/lib/node_modules",
]


def _captcha_config() -> dict:
    """读 config.ini 的 [captcha] 段，读不到就返回空配置。

    login 包可能被单独导入（脚本、测试），那时项目根未必在 sys.path 上。
    这类场景不该因为读配置失败就把整条登录链路打断 —— 空配置等价于
    「全部自动探测」，与原行为一致。
    """
    empty = {"node_executable": "", "node_path": "", "playwright_path": ""}
    try:
        from config_loader import get_captcha_config
        return get_captcha_config()
    except Exception:  # noqa
        return empty


def _shared_module_dirs(node_executable: str = "") -> list:
    """从 node 可执行文件的位置反推「与版本目录平行的共享模块目录」。

    某些托管式 Node 布局长这样::

        <root>/versions/<version>/node.exe
        <root>/workspace/node_modules/      ← 全局包装在这里

    `npm root -g` 只会报 ``<root>/versions/<version>/node_modules``，查不到
    后面那一处，于是明明装了 playwright 也 require 不到。这里按目录结构
    推断而不写死绝对路径：换机器、换版本号照样成立，布局不符时自然返回空。
    """
    if not node_executable or not os.path.isfile(node_executable):
        return []
    version_dir = os.path.dirname(os.path.abspath(node_executable))
    root = os.path.dirname(os.path.dirname(version_dir))
    candidate = os.path.join(root, "workspace", "node_modules")
    return [candidate] if os.path.isdir(candidate) else []


def detect_node_executable() -> str:
    """探测 node 可执行文件：环境变量 → config.ini → PATH → 候选列表 → 兜底 'node'。"""
    env_node = os.environ.get("NODE_EXECUTABLE") or os.environ.get("NODE_BIN")
    if env_node:
        return env_node
    from_config = _captcha_config().get("node_executable")
    if from_config:
        return from_config
    for name in ("node", "nodejs"):
        found = shutil.which(name)
        if found:
            return found
    for path in _NODE_EXECUTABLE_FALLBACKS:
        if os.path.isfile(path):
            return path
    return "node"


def detect_node_paths() -> str:
    """返回存在的 NODE_PATH 候选目录（os.pathsep 连接），供 webui 注入环境变量。

    来源顺序：NODE_PATH 环境变量 → config.ini → `npm root -g` →
    node 布局推断 → 常见安装位置。
    """
    paths = []

    def add(directory: str) -> None:
        if directory and os.path.isdir(directory) and directory not in paths:
            paths.append(directory)

    env_path = os.environ.get("NODE_PATH")
    if env_path:
        for part in env_path.split(os.pathsep):
            add(part)

    for part in _captcha_config().get("node_path", "").split(os.pathsep):
        add(part)

    npm = shutil.which("npm")
    if npm:
        try:
            proc = subprocess.run([npm, "root", "-g"], capture_output=True, text=True,
                                  errors="replace", timeout=15)
            add((proc.stdout or "").strip())
        except (OSError, subprocess.SubprocessError):
            pass

    for directory in _shared_module_dirs(detect_node_executable()):
        add(directory)

    for path in _NODE_PATH_FALLBACKS:
        add(path)
    return os.pathsep.join(paths)


class GeetestV4Solver:
    """极验 4 代滑块求解封装器"""

    def __init__(self,
                 node_executable: Optional[str] = None,
                 solver_script: Optional[str] = None):
        if node_executable is None:
            node_executable = self._detect_node_executable()
        self.node_executable = node_executable
        if solver_script is None:
            cur_dir = os.path.dirname(os.path.abspath(__file__))
            solver_script = os.path.join(cur_dir, "geetest_solver.js")
        self.solver_script = solver_script

    @staticmethod
    def _detect_node_executable() -> str:
        return detect_node_executable()

    def solve(self, captcha_id: str = LOGIN_CAPTCHA_ID, timeout_sec: int = 60) -> GeetestResult:
        """调用无头 Chromium 求解极验滑块并解析结果"""
        if not os.path.isfile(self.solver_script):
            raise FileNotFoundError(f"极验求解脚本不存在: {self.solver_script}")

        executable = self.node_executable
        resolved = shutil.which(executable) or (executable if os.path.isfile(executable) else None)
        if not resolved:
            raise FileNotFoundError(
                f"未找到 Node.js 可执行文件 ({self.node_executable})，请确保已安装 Node.js 并将其加入 PATH，或通过环境变量 NODE_EXECUTABLE 指定完整路径。"
            )

        cmd = [resolved, self.solver_script, captcha_id]
        env = os.environ.copy()

        # 优化 Node.js 模块查找路径：若未指定 NODE_PATH，补充当前工作区与常见全局路径
        captcha_cfg = _captcha_config()
        extra_node_paths = []
        if "NODE_PATH" in env and env["NODE_PATH"]:
            extra_node_paths.append(env["NODE_PATH"])

        candidate_node_paths = (
            [os.path.join(os.path.dirname(os.path.abspath(__file__)), "node_modules")]
            + [p for p in captcha_cfg.get("node_path", "").split(os.pathsep) if p]
            + _shared_module_dirs(self.node_executable)
            + _NODE_PATH_FALLBACKS
        )
        for cnp in candidate_node_paths:
            if os.path.isdir(cnp) and cnp not in extra_node_paths:
                extra_node_paths.append(cnp)

        if extra_node_paths:
            env["NODE_PATH"] = os.pathsep.join(extra_node_paths)

        # 显式指定的 playwright 模块路径优先级最高，js 端会先试它再看 NODE_PATH
        playwright_path = captcha_cfg.get("playwright_path")
        if playwright_path and os.path.isdir(playwright_path):
            env["PLAYWRIGHT_PATH"] = playwright_path

        proc = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_sec
        )

        for line in proc.stdout.splitlines():
            if line.startswith("FINAL_RESULT:"):
                raw_json = line[len("FINAL_RESULT:"):].strip()
                res = json.loads(raw_json)
                return GeetestResult(
                    captcha_id=res.get("captcha_id", captcha_id),
                    lot_number=res.get("lot_number", ""),
                    pass_token=res.get("pass_token", ""),
                    gen_time=res.get("gen_time", ""),
                    captcha_output=res.get("captcha_output", "")
                )

        raise RuntimeError(f"极验滑块识别失败 (退出码 {proc.returncode}):\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}")

