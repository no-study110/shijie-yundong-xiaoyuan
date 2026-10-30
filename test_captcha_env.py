# -*- coding: utf-8 -*-
"""验证码求解的 Node 运行环境探测。

这里守两条线：

1. **路径不能写死**。曾经 solver 的兜底路径是别人的开发机目录
   （``D:/Work/Tools/...``），在别的机器上必然不存在，等于没有兜底；
2. **配置不能是摆设**。``config.ini`` 的 ``[captcha]`` 段一度只有定义、
   没有调用方 —— 用户照着注释填 ``node_path`` 却毫无效果，而报错还在说
   「请配置 NODE_PATH」，把人指回了原地。

全部用例不出网、不起子进程。
"""
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import config_loader
from login import captcha

EMPTY_CFG = {"node_executable": "", "node_path": "", "playwright_path": ""}


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        fp.write("")


class SharedModuleDirTests(unittest.TestCase):
    """从 node 位置反推「与版本目录平行的共享模块目录」。"""

    def test_finds_sibling_workspace(self):
        # <root>/versions/<ver>/node.exe + <root>/workspace/node_modules
        with tempfile.TemporaryDirectory() as tmp:
            node = os.path.join(tmp, "versions", "22.0.0", "node.exe")
            _touch(node)
            workspace = os.path.join(tmp, "workspace", "node_modules")
            os.makedirs(workspace)
            self.assertEqual(captcha._shared_module_dirs(node), [workspace])

    def test_empty_when_workspace_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = os.path.join(tmp, "versions", "22.0.0", "node.exe")
            _touch(node)
            self.assertEqual(captcha._shared_module_dirs(node), [])

    def test_empty_for_missing_executable(self):
        self.assertEqual(captcha._shared_module_dirs(""), [])
        self.assertEqual(captcha._shared_module_dirs("/nonexistent/node"), [])


class NodeDetectionTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.pop(k, None)
                       for k in ("NODE_EXECUTABLE", "NODE_BIN", "NODE_PATH")}

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_config_value_used_when_env_absent(self):
        cfg = dict(EMPTY_CFG, node_executable="C:/x/node.exe")
        with patch.object(config_loader, "get_captcha_config", return_value=cfg):
            self.assertEqual(captcha.detect_node_executable(), "C:/x/node.exe")

    def test_env_overrides_config(self):
        os.environ["NODE_EXECUTABLE"] = "C:/env/node.exe"
        cfg = dict(EMPTY_CFG, node_executable="C:/x/node.exe")
        with patch.object(config_loader, "get_captcha_config", return_value=cfg):
            self.assertEqual(captcha.detect_node_executable(), "C:/env/node.exe")

    def test_config_node_path_is_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = dict(EMPTY_CFG, node_path=tmp)
            with patch.object(config_loader, "get_captcha_config", return_value=cfg), \
                 patch.object(captcha.shutil, "which", return_value=None):
                parts = captcha.detect_node_paths().split(os.pathsep)
        self.assertIn(tmp, parts)

    def test_unreadable_config_is_not_fatal(self):
        # login 包被单独导入时读不到 config.ini，不能因此中断登录
        with patch.object(config_loader, "get_captcha_config", side_effect=KeyError("captcha")):
            self.assertEqual(captcha._captcha_config(), EMPTY_CFG)


class SolveWiringTests(unittest.TestCase):
    """config.ini 的 playwright_path 必须真的传到子进程环境里。"""

    def test_playwright_path_reaches_child_env(self):
        with tempfile.TemporaryDirectory() as playwright_dir:
            cfg = dict(EMPTY_CFG, playwright_path=playwright_dir)
            captured = {}

            def fake_run(cmd, env=None, **kwargs):
                captured["env"] = dict(env or {})
                return subprocess.CompletedProcess(cmd, 1, "", "")

            with patch.object(config_loader, "get_captcha_config", return_value=cfg), \
                 patch.object(captcha.subprocess, "run", side_effect=fake_run):
                with self.assertRaises(RuntimeError):
                    captcha.GeetestV4Solver().solve(timeout_sec=5)

        self.assertEqual(captcha.os.path.normpath(captured["env"]["PLAYWRIGHT_PATH"]),
                         os.path.normpath(playwright_dir))

    def test_blank_playwright_path_is_not_exported(self):
        captured = {}

        def fake_run(cmd, env=None, **kwargs):
            captured["env"] = dict(env or {})
            return subprocess.CompletedProcess(cmd, 1, "", "")

        saved = os.environ.pop("PLAYWRIGHT_PATH", None)
        try:
            with patch.object(config_loader, "get_captcha_config", return_value=EMPTY_CFG), \
                 patch.object(captcha.subprocess, "run", side_effect=fake_run):
                with self.assertRaises(RuntimeError):
                    captcha.GeetestV4Solver().solve(timeout_sec=5)
        finally:
            if saved is not None:
                os.environ["PLAYWRIGHT_PATH"] = saved

        self.assertNotIn("PLAYWRIGHT_PATH", captured["env"])


if __name__ == "__main__":
    unittest.main()
