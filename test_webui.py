# -*- coding: utf-8 -*-
"""Web 控制台的离线用例：设备指纹延续、参数校验、配置回退。

webui 依赖 Flask（唯一的第三方依赖），CI 的干净环境里没有，
因此整个模块 skip —— 与冒烟「跳过但做语法检查」的策略保持一致。
本地装了 Flask 才会真正执行，且所有用例都不出网。
"""
import configparser
import importlib.util
import unittest
from unittest.mock import patch

import config_loader

HAS_FLASK = importlib.util.find_spec("flask") is not None

if HAS_FLASK:
    import webui

SKIP_REASON = "需要 Flask（CI 干净环境跳过，与冒烟策略一致）"


class DeviceResolutionTests(unittest.TestCase):
    """设备指纹解析：纯函数，不构造客户端。"""

    def setUp(self):
        if not HAS_FLASK:
            self.skipTest(SKIP_REASON)
        self._saved = dict(webui._config)

    def tearDown(self):
        if hasattr(self, "_saved"):
            webui._config.clear()
            webui._config.update(self._saved)

    def test_explicit_id_wins_over_stored(self):
        webui._config["device_id"] = "STORED"
        self.assertEqual(webui._resolve_device_id("EXPLICIT"), "EXPLICIT")

    def test_stored_id_used_when_not_specified(self):
        webui._config["device_id"] = "STORED01"
        self.assertEqual(webui._resolve_device_id(), "STORED01")

    def test_hyphens_are_stripped(self):
        self.assertEqual(webui._resolve_device_id("AA-BB-CC"), "AABBCC")

    def test_blank_everything_returns_empty(self):
        # 空串表示「不指定」，交给上层回落到 config.ini 的默认指纹
        webui._config["device_id"] = ""
        self.assertEqual(webui._resolve_device_id(), "")

    def test_whitespace_is_trimmed(self):
        self.assertEqual(webui._resolve_device_id("  ABC  "), "ABC")


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class DeviceInheritanceTests(unittest.TestCase):
    """回归锚点：登录记住的指纹必须被跑步阶段沿用，否则会自触发 10121。"""

    def setUp(self):
        self._saved = dict(webui._config)

    def tearDown(self):
        webui._config.clear()
        webui._config.update(self._saved)

    def test_new_client_carries_stored_device(self):
        webui._config["device_id"] = "DEADBEEF"
        client = webui._new_client()
        self.assertEqual(client.device_config.get("DeviceId"), "DEADBEEF")


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class ApiValidationTests(unittest.TestCase):
    """参数校验分支：全部在 _run_job 之前返回，不会启动任务、不会出网。"""

    def setUp(self):
        webui.app.config["TESTING"] = True
        self.c = webui.app.test_client()

    def post(self, url, payload):
        return self.c.post(url, json=payload).get_json()

    def test_run_rejects_zero_distance(self):
        self.assertFalse(self.post("/api/run", {"mode": 1, "distance": 0, "time_sec": 600})["ok"])

    def test_run_rejects_negative_time(self):
        self.assertFalse(self.post("/api/run", {"mode": 1, "distance": 2100, "time_sec": -1})["ok"])

    def test_run_rejects_unknown_mode(self):
        self.assertFalse(self.post("/api/run", {"mode": 99, "distance": 2100, "time_sec": 600})["ok"])

    def test_run_rejects_oversized_distance(self):
        # 上限 50km：超限多半是误填，而 do_run 会按 time_sec 真等物理时间
        result = self.post("/api/run", {"mode": 1, "distance": 60000, "time_sec": 600})
        self.assertFalse(result["ok"])
        self.assertIn("50000", result["message"])

    def test_run_rejects_oversized_time(self):
        result = self.post("/api/run", {"mode": 1, "distance": 2100, "time_sec": 99999})
        self.assertFalse(result["ok"])
        self.assertIn("6 小时", result["message"])

    def test_run_rejects_non_numeric(self):
        self.assertFalse(self.post("/api/run", {"mode": "abc", "distance": 2100, "time_sec": 600})["ok"])

    def test_login_requires_credentials(self):
        self.assertFalse(self.post("/api/login", {"username": "", "password": "x"})["ok"])
        self.assertFalse(self.post("/api/login", {"username": "13800000000", "password": ""})["ok"])

    def test_unknown_action_rejected(self):
        self.assertFalse(self.post("/api/action", {"action": "rm -rf"})["ok"])


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class DeviceIdApiTests(unittest.TestCase):
    """页面上的「随机生成」：只返回候选值，绝不落盘。

    这条契约是刻意的 —— 生成即写入的话，用户点了按钮却没登录（或又改成
    别的值），指纹已经换掉，账号还绑在旧设备上，下次登录直接 10121。
    """

    def setUp(self):
        webui.app.config["TESTING"] = True
        self.c = webui.app.test_client()
        self._saved = dict(webui._config)

    def tearDown(self):
        webui._config.clear()
        webui._config.update(self._saved)

    def test_generate_returns_valid_fingerprint(self):
        r = self.c.post("/api/device-id/new", json={}).get_json()
        self.assertTrue(r["ok"])
        ok, _, reason = config_loader.validate_device_id(r["device_id"])
        self.assertTrue(ok, reason)
        self.assertEqual(len(r["device_id"]), 32)

    def test_generate_leaves_stored_value_untouched(self):
        webui._config["device_id"] = "0" * 32
        r = self.c.post("/api/device-id/new", json={}).get_json()
        self.assertNotEqual(r["device_id"], webui._config["device_id"])
        self.assertEqual(webui._config["device_id"], "0" * 32)

    def test_generate_never_writes_config(self):
        with patch.object(webui, "_save_config") as save:
            self.c.post("/api/device-id/new", json={})
        save.assert_not_called()

    def test_state_prefers_webui_value(self):
        webui._config["device_id"] = "0123456789abcdef0123456789abcdef"
        r = self.c.get("/api/device-id").get_json()
        self.assertEqual(r["source"], "网页控制台")
        self.assertEqual(r["current"], "0123456789abcdef0123456789abcdef")
        self.assertTrue(r["valid"])

    def test_state_flags_bad_format(self):
        webui._config["device_id"] = "XYZ"
        r = self.c.get("/api/device-id").get_json()
        self.assertFalse(r["valid"])
        self.assertIn("32", r["reason"])

    def test_state_falls_back_to_config_ini(self):
        webui._config["device_id"] = ""
        with patch.object(webui, "get_device_id_from_file", return_value="f" * 32):
            r = self.c.get("/api/device-id").get_json()
        self.assertEqual(r["source"], "config.ini")
        self.assertEqual(r["current"], "f" * 32)

    def test_state_reports_unset(self):
        webui._config["device_id"] = ""
        with patch.object(webui, "get_device_id_from_file", return_value=""):
            r = self.c.get("/api/device-id").get_json()
        self.assertEqual(r["current"], "")
        self.assertEqual(r["source"], "未设置")


class WebuiConfigTests(unittest.TestCase):
    """config.ini 缺 [webui] 段时不能让控制台启动即崩。"""

    def test_missing_section_falls_back_to_localhost(self):
        empty = configparser.ConfigParser()
        with patch.object(config_loader, "_load_config", return_value=empty):
            cfg = config_loader.get_webui_config()
        self.assertEqual(cfg["host"], "127.0.0.1")
        self.assertEqual(cfg["port"], 9999)

    def test_partial_section_keeps_present_key(self):
        cfg_parser = configparser.ConfigParser()
        cfg_parser.read_dict({"webui": {"port": "8080"}})
        with patch.object(config_loader, "_load_config", return_value=cfg_parser):
            cfg = config_loader.get_webui_config()
        self.assertEqual(cfg["host"], "127.0.0.1")
        self.assertEqual(cfg["port"], 8080)


class SessionImportTests(unittest.TestCase):
    """手机端的登录态导入：粘贴 .session.json 内容即可登录。

    存在理由：Android 上没有 Node + Playwright，过不了极验滑块。所以手机版
    不在本地解验证码，而是接收电脑上登录得到的 token/uid/unid。
    """

    def setUp(self):
        if not HAS_FLASK:
            self.skipTest(SKIP_REASON)
        import tempfile, os
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, ".session.json")
        self._patch = patch.object(webui, "SESSION_FILE", self.path)
        self._patch.start()
        self.c = webui.app.test_client()

    def tearDown(self):
        if hasattr(self, "_patch"):
            self._patch.stop()
            self.tmp.cleanup()

    def test_import_writes_only_token_uid_unid(self):
        import json
        r = self.c.post("/api/session/import", json={
            "text": json.dumps({"token": "T" * 24, "uid": 12752192, "unid": 61401,
                                "password": "不该落盘"})}).get_json()
        self.assertTrue(r["ok"])
        self.assertEqual(r["uid"], "12752192")
        with open(self.path, encoding="utf-8") as fp:
            saved = json.load(fp)
        self.assertEqual(saved["token"], "T" * 24)
        self.assertEqual(saved["unid"], "61401")
        self.assertNotIn("password", saved)

    def test_import_accepts_object_body(self):
        r = self.c.post("/api/session/import",
                        json={"token": "T" * 24, "uid": "42"}).get_json()
        self.assertTrue(r["ok"])
        self.assertEqual(r["unid"], "")

    def test_rejects_bad_payload(self):
        import json
        for body in ({"text": "{not json"}, {"text": "[1,2]"},
                     {"text": json.dumps({"uid": 1})},
                     {"text": json.dumps({"token": "T" * 24})}):
            with self.subTest(body=body):
                r = self.c.post("/api/session/import", json=body).get_json()
                self.assertFalse(r["ok"])
                self.assertTrue(r["message"])


class DataDirTests(unittest.TestCase):
    """ZJWH_DATA_DIR 让可写数据跟着环境走 —— Android 上源码目录是只读的。"""

    def test_override_moves_config_and_session(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ZJWH_DATA_DIR": tmp}):
                import importlib
                mod = importlib.reload(config_loader)
                try:
                    self.assertEqual(mod.get_data_dir().as_posix(), tmp.replace("\\", "/"))
                    self.assertTrue(mod.get_session_file_path().startswith(tmp))
                finally:
                    os.environ.pop("ZJWH_DATA_DIR", None)
                    importlib.reload(config_loader)

    def test_default_is_project_dir(self):
        self.assertEqual(config_loader.get_data_dir(), config_loader.get_base_dir())


if __name__ == "__main__":
    unittest.main()
