# -*- coding: utf-8 -*-
"""device_id_tool 与指纹校验规则的离线测试。

覆盖两类东西：格式判据本身（长度 / 大小写 / 字符集），以及工具各个模式的
退出码与副作用 —— 「拒绝写入」如果退出码是 0，脚本里就看不出来。
"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import config_loader
import device_id_tool

VALID = "3de7c0707556404f8d50b78247debc90"


class TempConfigCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmpdir = Path(tmp.name)
        self.config_path = self.tmpdir / "config.ini"
        self.webui_path = self.tmpdir / ".webui_config.json"

        self.addCleanup(patch.stopall)
        patch.object(config_loader, "_CONFIG_PATH", self.config_path).start()
        patch.object(config_loader, "_config", None).start()
        patch.object(device_id_tool, "WEBUI_CONFIG", self.webui_path).start()

    def write_config(self, text):
        self.config_path.write_text(text, encoding="utf-8")
        config_loader._config = None

    def read_config(self):
        return self.config_path.read_text(encoding="utf-8")

    def run_tool(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = device_id_tool.main(list(argv))
        return code, buf.getvalue()


class ValidateTests(unittest.TestCase):
    def test_valid_lowercase_hex(self):
        ok, value, reason = config_loader.validate_device_id(VALID)
        self.assertTrue(ok)
        self.assertEqual(value, VALID)
        self.assertEqual(reason, "")

    def test_hyphenated_uuid_is_normalized(self):
        ok, value, _ = config_loader.validate_device_id(
            "3de7c070-7556-404f-8d50-b78247debc90")
        self.assertTrue(ok, "带连字符的 UUID 应被接受（去横线后合法）")
        self.assertEqual(value, VALID)

    def test_surrounding_whitespace_is_stripped(self):
        ok, value, _ = config_loader.validate_device_id("  " + VALID + "\n")
        self.assertTrue(ok)
        self.assertEqual(value, VALID)

    def test_short_value_is_rejected(self):
        ok, value, reason = config_loader.validate_device_id(VALID[:-1])
        self.assertFalse(ok)
        self.assertIn("31", reason)

    def test_long_value_is_rejected(self):
        ok, _, reason = config_loader.validate_device_id(VALID + "0")
        self.assertFalse(ok)
        self.assertIn("33", reason)

    def test_uppercase_is_rejected_not_silently_lowered(self):
        ok, value, reason = config_loader.validate_device_id(VALID.upper())
        self.assertFalse(ok)
        self.assertIn("大写", reason)
        # 关键：不能悄悄转小写。用户抄的是真机大写值时，改了等于换了设备
        self.assertEqual(value, VALID.upper())

    def test_non_hex_is_rejected(self):
        ok, _, reason = config_loader.validate_device_id("z" * 32)
        self.assertFalse(ok)
        self.assertIn("十六进制", reason)

    def test_empty_is_rejected(self):
        ok, _, reason = config_loader.validate_device_id("   ")
        self.assertFalse(ok)
        self.assertIn("空", reason)

    def test_normalize_keeps_case(self):
        self.assertEqual(config_loader.normalize_device_id(" ABC-def "), "ABCdef")


class SetTests(TempConfigCase):
    def test_set_reports_old_and_new(self):
        self.write_config("[device]\ndevice_id = " + "a" * 32 + "\n")
        old, new = config_loader.set_device_id(VALID)
        self.assertEqual(old, "a" * 32)
        self.assertEqual(new, VALID)
        self.assertIn("device_id = " + VALID, self.read_config())

    def test_set_leaves_other_sections_intact(self):
        self.write_config(
            "[server]\nhost_run = https://keep.invalid\n\n"
            "[device]\ndevice_id = " + "a" * 32 + "\n\n[session]\nuid = 7\n")
        config_loader.set_device_id(VALID)
        text = self.read_config()
        self.assertIn("https://keep.invalid", text)
        self.assertIn("uid = 7", text)

    def test_set_writes_single_line(self):
        self.write_config("[device]\ndevice_id = " + "a" * 32 + "\n")
        config_loader.set_device_id(VALID)
        config_loader.set_device_id(VALID)
        self.assertEqual(self.read_config().count("device_id"), 1,
                         "重复写入不能累积出多行 device_id")

    def test_set_rejects_invalid(self):
        with self.assertRaises(ValueError):
            config_loader.set_device_id("abc")

    def test_invalid_set_does_not_touch_file(self):
        self.write_config("[device]\ndevice_id = " + "a" * 32 + "\n")
        before = self.read_config()
        with self.assertRaises(ValueError):
            config_loader.set_device_id("too-short")
        self.assertEqual(self.read_config(), before)

    def test_creates_section_when_missing(self):
        self.write_config("[server]\nhost_run = https://x.invalid\n")
        config_loader.set_device_id(VALID)
        text = self.read_config()
        self.assertIn("[device]", text)
        self.assertIn("device_id = " + VALID, text)
        # 新段必须真的能被解析出来，而不是只是文本上看起来对
        config_loader._config = None
        self.assertEqual(config_loader.get_or_create_device_id(), VALID)


class ToolModeTests(TempConfigCase):
    def test_default_mode_only_prints(self):
        self.write_config("[device]\ndevice_id = " + "a" * 32 + "\n")
        before = self.read_config()
        code, out = self.run_tool()
        self.assertEqual(code, 0)
        self.assertIn("已生成", out)
        self.assertEqual(self.read_config(), before, "默认模式绝不能写文件")

    def test_count_generates_distinct_values(self):
        _, out = self.run_tool("-n", "5")
        found = [line.strip() for line in out.splitlines() if len(line.strip()) == 32]
        self.assertEqual(len(found), 5)
        self.assertEqual(len(set(found)), 5)

    def test_generated_values_pass_validation(self):
        for value in device_id_tool.generate(20):
            ok, _, reason = config_loader.validate_device_id(value)
            self.assertTrue(ok, "%s 应合法：%s" % (value, reason))

    def test_show_reports_source(self):
        self.write_config("[device]\ndevice_id = " + VALID + "\n")
        code, out = self.run_tool("--show")
        self.assertEqual(code, 0)
        self.assertIn(VALID, out)
        self.assertIn("config.ini", out)

    def test_show_empty_config(self):
        code, out = self.run_tool("--show")
        self.assertEqual(code, 0)
        self.assertIn("尚未生成", out)

    def test_check_valid_exits_zero(self):
        self.write_config("[device]\ndevice_id = " + VALID + "\n")
        code, out = self.run_tool("--check")
        self.assertEqual(code, 0)
        self.assertIn("通过", out)

    def test_check_invalid_exits_nonzero(self):
        self.write_config("[device]\ndevice_id = " + "a" * 31 + "\n")
        code, out = self.run_tool("--check")
        self.assertEqual(code, 1, "不合法必须用退出码表达，否则脚本里看不出来")
        self.assertIn("不合法", out)

    def test_write_replaces_value(self):
        self.write_config("[device]\ndevice_id = " + "a" * 32 + "\n")
        code, out = self.run_tool("--write")
        self.assertEqual(code, 0)
        self.assertIn("旧值", out)
        config_loader._config = None
        current = config_loader.get_or_create_device_id()
        self.assertNotEqual(current, "a" * 32)
        self.assertTrue(config_loader.validate_device_id(current)[0])

    def test_set_rejects_and_exits_nonzero(self):
        self.write_config("[device]\ndevice_id = " + "a" * 32 + "\n")
        before = self.read_config()
        code, out = self.run_tool("--set", "not-a-device-id")
        self.assertEqual(code, 1)
        self.assertIn("拒绝写入", out)
        self.assertEqual(self.read_config(), before)

    def test_set_force_keeps_uppercase(self):
        self.write_config("[device]\ndevice_id = " + "a" * 32 + "\n")
        code, _ = self.run_tool("--set", VALID.upper(), "--force")
        self.assertEqual(code, 0)
        self.assertIn(VALID.upper(), self.read_config(),
                      "--force 是「跳过校验」，不是「顺手改大小写」")

    def test_webui_value_takes_precedence(self):
        self.write_config("[device]\ndevice_id = " + "a" * 32 + "\n")
        self.webui_path.write_text(
            json.dumps({"device_id": VALID}), encoding="utf-8")
        code, out = self.run_tool("--show")
        self.assertEqual(code, 0)
        self.assertIn(VALID, out)
        self.assertIn("网页控制台", out)
        self.assertIn("config.ini 里是", out, "两个来源不一致时必须提示")

    def test_broken_webui_config_is_ignored(self):
        self.write_config("[device]\ndevice_id = " + VALID + "\n")
        self.webui_path.write_text("{not json", encoding="utf-8")
        code, out = self.run_tool("--show")
        self.assertEqual(code, 0)
        self.assertIn(VALID, out)


if __name__ == "__main__":
    unittest.main()
