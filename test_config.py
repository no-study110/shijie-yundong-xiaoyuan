# -*- coding: utf-8 -*-
"""配置系统单元测试。

覆盖三级回退（config.ini → 内置默认值）与 ``get_or_create_device_id``
的持久化契约。后者此前零覆盖，导致「每次运行往 config.ini 里追加一行
device_id」的幂等性缺陷一直没被发现 —— 累积到第二行后 configparser 抛
DuplicateOptionError，整个配置静默回退到默认值。

所有用例都在临时目录里跑，不会碰到真实的 config.ini。
"""
import builtins
import configparser
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import config_loader
from config_loader import (
    get_base_dir,
    get_captcha_config,
    get_checkpoint_tolerance,
    get_device_defaults,
    get_or_create_device_id,
    get_proxy_url,
    get_realism_defaults,
    get_server_config,
    get_session_file_path,
    get_signature_salt,
    get_webui_config,
)

DEFAULT_HOST_RUN = "https://run.gxapp.iydsj.com"


class _TempConfigCase(unittest.TestCase):
    """把 config_loader 的路径与缓存整体指向临时目录。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config_path = Path(self._tmp.name) / "config.ini"

        path_patch = patch.object(config_loader, "_CONFIG_PATH", self.config_path)
        cache_patch = patch.object(config_loader, "_config", None)
        path_patch.start()
        cache_patch.start()
        self.addCleanup(patch.stopall)

    def write_config(self, text):
        self.config_path.write_text(text, encoding="utf-8")

    def read_device_id_lines(self):
        """返回 config.ini 里 device_id 键出现的次数。"""
        if not self.config_path.exists():
            return 0
        content = self.config_path.read_text(encoding="utf-8")
        return sum(1 for line in content.splitlines()
                   if line.strip().replace(" ", "").lower().startswith("device_id="))

    def assert_parses_cleanly(self):
        """config.ini 必须能被标准解析器读回来（不重复键、语法完好）。"""
        parser = configparser.ConfigParser()
        parser.read(self.config_path, encoding="utf-8")
        return parser


class FallbackTests(_TempConfigCase):
    """三级回退：无 config.ini → 内置默认值；有则覆盖；坏则回退。"""

    def test_missing_file_uses_builtin_defaults(self):
        self.assertFalse(self.config_path.exists())
        self.assertEqual(get_server_config()["host_run"], DEFAULT_HOST_RUN)
        self.assertEqual(get_signature_salt(), "2slhe02lsfiwowlcixisla_sls-_slaor")
        # 内置默认值曾残留 8765，与 config.ini / 文档里的 9999 不一致：
        # 有配置文件时端口是 9999，删掉配置文件就变回 8765，控制台打不开。
        self.assertEqual(get_webui_config()["port"], 9999)
        self.assertEqual(get_checkpoint_tolerance(), 15.0)
        self.assertTrue(get_realism_defaults()["simulate_physical_time"])

    def test_file_overrides_defaults(self):
        self.write_config(
            "[server]\nhost_run = https://example.invalid\n\n"
            "[webui]\nport = 9999\n\n"
            "[realism]\nsimulate_physical_time = False\n\n"
            "[compliance]\ncheckpoint_tolerance_m = 8.5\n"
        )
        self.assertEqual(get_server_config()["host_run"], "https://example.invalid")
        # 未覆盖的键仍取默认值，说明是「覆盖加载」而不是「整份替换」
        self.assertEqual(get_server_config()["host_datapoint"],
                         "https://datapoint.gxapp.iydsj.com")
        self.assertEqual(get_webui_config()["port"], 9999)
        self.assertFalse(get_realism_defaults()["simulate_physical_time"])
        self.assertEqual(get_checkpoint_tolerance(), 8.5)

    def test_malformed_file_falls_back_to_defaults(self):
        # 缺段头的坏文件会让 configparser 抛 MissingSectionHeaderError
        self.write_config("host_run = https://broken.invalid\n")
        with patch.object(builtins, "print"):
            self.assertEqual(get_server_config()["host_run"], DEFAULT_HOST_RUN)

    def test_duplicate_key_falls_back_instead_of_crashing(self):
        # 这正是 get_or_create_device_id 老实现写坏 config.ini 之后的现场
        self.write_config("[device]\ndevice_id = aaaa\ndevice_id = bbbb\n")
        with patch.object(builtins, "print"):
            cfg = config_loader._load_config()
        self.assertEqual(cfg.get("device", "app_version"), "7.3.92")


class DeviceIdPersistenceTests(_TempConfigCase):
    """device_id 必须稳定、幂等、可写回，且不能把文件写坏。"""

    def test_generated_id_is_stable_across_calls(self):
        first = get_or_create_device_id()
        second = get_or_create_device_id()
        # 第三次清缓存重读，验证是从文件读回来的而不是内存缓存
        config_loader._config = None
        third = get_or_create_device_id()
        self.assertEqual(first, second)
        self.assertEqual(first, third)
        self.assertEqual(len(first), 32)
        self.assertTrue(all(c in "0123456789abcdef" for c in first))

    def test_config_file_keeps_single_device_id_line(self):
        """回归用例：老实现每次调用都追加一行，第 N 次运行后配置整体失效。"""
        for _ in range(5):
            config_loader._config = None
            get_or_create_device_id()
        self.assertEqual(self.read_device_id_lines(), 1)
        parser = self.assert_parses_cleanly()
        self.assertEqual(len(parser.get("device", "device_id")), 32)

    def test_existing_id_is_reused_without_rewriting(self):
        self.write_config("[device]\ndevice_id = 0123456789abcdef0123456789abcdef\n")
        before = self.config_path.read_text(encoding="utf-8")
        self.assertEqual(get_or_create_device_id(), "0123456789abcdef0123456789abcdef")
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)

    def test_missing_key_inside_existing_section_is_appended(self):
        self.write_config("[device]\napp_version = 7.3.92\n\n[webui]\nport = 8765\n")
        device_id = get_or_create_device_id()
        parser = self.assert_parses_cleanly()
        self.assertEqual(parser.get("device", "device_id"), device_id)
        # 新增的键必须落在 [device] 段内，不能跑到 [webui] 里去
        self.assertEqual(parser.get("device", "app_version"), "7.3.92")
        self.assertEqual(parser.get("webui", "port"), "8765")

    def test_missing_section_is_created(self):
        self.write_config("[server]\nhost_run = https://example.invalid\n")
        device_id = get_or_create_device_id()
        parser = self.assert_parses_cleanly()
        self.assertEqual(parser.get("device", "device_id"), device_id)
        self.assertEqual(parser.get("server", "host_run"), "https://example.invalid")

    def test_missing_file_is_created(self):
        device_id = get_or_create_device_id()
        self.assertTrue(self.config_path.exists())
        self.assertEqual(self.assert_parses_cleanly().get("device", "device_id"), device_id)

    def test_write_failure_still_returns_id(self):
        """只读目录（打包成 exe 后常见）不能阻塞主流程。"""
        self.write_config("[device]\ndevice_id = \n")
        real_open = builtins.open

        def guarded_open(file, mode="r", *args, **kwargs):
            if "w" in mode or "a" in mode:
                raise OSError("read-only file system")
            return real_open(file, mode, *args, **kwargs)

        with patch.object(builtins, "open", guarded_open):
            device_id = get_or_create_device_id()
        self.assertEqual(len(device_id), 32)
        # 文件保持原样，没有被写坏
        self.assertEqual(self.read_device_id_lines(), 1)

    def test_device_defaults_exposes_generated_id(self):
        device_id = get_device_defaults()["DeviceId"]
        self.assertEqual(len(device_id), 32)
        self.assertEqual(get_device_defaults()["AppVersion"], "7.3.92")


class GetterShapeTests(_TempConfigCase):
    """各 getter 的返回类型必须稳定 —— 调用方依赖 int/float/bool 直接参与运算。"""

    def test_typed_values(self):
        webui = get_webui_config()
        self.assertIsInstance(webui["port"], int)
        self.assertEqual(webui["host"], "127.0.0.1")

        realism = get_realism_defaults()
        self.assertIsInstance(realism["enabled"], bool)
        self.assertIsInstance(realism["simulate_physical_time"], bool)

        self.assertIsInstance(get_checkpoint_tolerance(), float)
        self.assertIsInstance(get_server_config(), dict)
        self.assertEqual(set(get_server_config()),
                         {"host_run", "host_discovery", "host_datapoint", "host_partner"})

    def test_proxy_empty_means_direct_connection(self):
        self.assertIsNone(get_proxy_url())
        self.write_config("[proxy]\nproxy_url = \n")
        config_loader._config = None
        self.assertIsNone(get_proxy_url())
        self.write_config("[proxy]\nproxy_url = http://127.0.0.1:7890\n")
        config_loader._config = None
        self.assertEqual(get_proxy_url(), "http://127.0.0.1:7890")

    def test_session_file_path_is_absolute(self):
        path = Path(get_session_file_path())
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.name, ".session.json")

    def test_captcha_defaults_to_empty(self):
        self.assertEqual(get_captcha_config(),
                         {"node_executable": "", "node_path": "", "playwright_path": ""})

    def test_base_dir_points_at_package_root(self):
        base = get_base_dir()
        self.assertTrue((base / "config_loader.py").exists())
        self.assertEqual(base.name, "7.3.92")


class CoreConfigConsistencyTests(unittest.TestCase):
    """config_loader 与 core.config 的默认值必须一致，否则回退路径会漂移。"""

    def test_signature_salt_matches_core(self):
        from core.config import SIGN_SALT
        self.assertEqual(get_signature_salt(), SIGN_SALT)

    def test_app_version_matches_core(self):
        from core.config import DEFAULT_DEVICE_CONFIG
        self.assertEqual(get_device_defaults()["AppVersion"],
                         DEFAULT_DEVICE_CONFIG["appVersion"])


if __name__ == "__main__":
    unittest.main()
