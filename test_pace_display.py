# -*- coding: utf-8 -*-
"""配速显示的跨端一致性：手机记录 = CLI 回查 = 网页控制台。

三个界面对同一个字段有三种说法，用户看到的就是「对不上」：

- App 记录页：``配速 8′54″``
- CLI 回查：``配速=8.9分/km 速度=8.91m/s``（速度把配速当成了 m/s）
- 网页：``配速 8.9 分/km``，历史记录表干脆没有配速列

所以本文件的断言不是「格式好看」，而是**同一个 rrid 在三处必须得到同一个字符串**。
``core.pace`` 是唯一实现，这里逐层确认没人又写了一份。
"""
import importlib.util
import os
import re
import unittest

from core.pace import (
    format_pace,
    format_pace_from_server_speed,
    format_pace_range,
    pace_min_per_km,
    record_pace_text,
)
from history.models import RunRecordDetail

HAS_FLASK = importlib.util.find_spec("flask") is not None
_ROOT = os.path.dirname(os.path.abspath(__file__))


def _read(rel_path: str) -> str:
    """读取项目内文件（用例要核对的是真实产物，不是记忆中的文案）。"""
    with open(os.path.join(_ROOT, rel_path), encoding="utf-8") as fp:
        return fp.read()


def _pace_seconds(text: str) -> int:
    """把 ``8′54″/km`` 还原成每公里秒数，用于比较两个显示值差多少秒。"""
    m = re.match(r"(\d+)′(\d{2})″", text)
    if not m:
        raise AssertionError(f"配速文案格式不合预期: {text!r}")
    return int(m.group(1)) * 60 + int(m.group(2))


class PaceFormatTests(unittest.TestCase):
    """分′秒″：与 App 记录页、服务端拒绝文案同一套写法。"""

    def test_real_run_4080m_2180s(self):
        # 就是被服务端判「未达标」的那一条，8.905 分/km
        self.assertEqual(format_pace(4080, 2180), "8′54″/km")

    def test_seconds_are_zero_padded(self):
        # 6.05 分/km = 363 秒 = 6′03″，不能输出 6′3″
        self.assertEqual(format_pace(pace_min=6.05), "6′03″/km")

    def test_rounding_carries_into_next_minute(self):
        # 539.6 秒四舍五入到 540 —— 应显示 9′00″，而不是 8′60″
        self.assertEqual(format_pace(pace_sec=539.6), "9′00″/km")

    def test_half_second_rounds_up_like_javascript(self):
        """前端用 Math.round、后端用 Python，.5 秒处必须同向。

        Python 内置 round(534.5) == 534（银行家舍入），JS Math.round(534.5) == 535。
        若后端用 round，同一个 8′54.5″ 会一边显示 8′54″、一边 8′55″。
        """
        self.assertEqual(format_pace(pace_sec=534.5), "8′55″/km")
        self.assertEqual(format_pace(pace_sec=535.5), "8′56″/km")

    def test_no_unit_variant(self):
        self.assertEqual(format_pace(pace_min=8.905, with_unit=False), "8′54″")

    def test_invalid_input_renders_dash(self):
        for kwargs in ({"distance_m": 0, "time_sec": 100},
                       {"distance_m": 1000, "time_sec": 0},
                       {"distance_m": None, "time_sec": None}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(format_pace(**kwargs), "-")

    def test_range_text_matches_app_wording(self):
        # 规则区间：speedTop=2.0 / speedBottom=10.0
        self.assertEqual(format_pace_range(2.0, 10.0), "2′00″~10′00″/km")
        self.assertEqual(format_pace_range(2.0, 2.0), "固定 2′00″/km")
        self.assertEqual(format_pace_range(None, None), "不限")


class RecordPaceTests(unittest.TestCase):
    """历史记录的配速：优先服务端字段，缺了才自算。"""

    def test_totals_win_over_polluted_speed_field(self):
        # 真实案例：rrid=1350559546 的 speed 存成了 1.872（早期版本误填 m/s）。
        # 照抄字段会显示 1′52″/km；按里程/耗时才是 8′54″/km。
        record = {"totalDis": 4080, "totalTime": 2180, "speed": 1.871999979019165}
        self.assertEqual(record_pace_text(record), "8′54″/km")

    def test_speed_field_used_only_when_totals_missing(self):
        # 个别列表接口只回 rrid + speed，此时才用字段兜底
        self.assertEqual(record_pace_text({"speed": 8.905}), "8′54″/km")
        self.assertEqual(record_pace_text({"speed": 8905}), "8′54″/km")

    def test_real_app_records_agree_within_one_second(self):
        """真实记录：服务端 speed 只有两位小数，换算成秒最多差 0.3 秒。

        允许 1 秒差是诚实的边界 —— 7.81 分/km 与 2143/4.576/60 = 7.8048 会分别
        落在 7′49″ 与 7′48″。**显示只能有一个来源**，所以网页取服务端字段；
        这条用例只用来确认服务端字段没被写成别的量纲（差一个数量级就会被抓出来）。
        """
        for dist, secs, server_speed in [(4007, 2002, 8.33), (4576, 2143, 7.81),
                                         (3104, 983, 5.28), (4201, 1797, 7.13)]:
            with self.subTest(dist=dist, secs=secs):
                from_server = _pace_seconds(format_pace_from_server_speed(server_speed))
                from_totals = _pace_seconds(format_pace(dist, secs))
                self.assertLessEqual(abs(from_server - from_totals), 1)

    def test_falls_back_to_totals_without_speed(self):
        self.assertEqual(record_pace_text({"totalDis": 4080, "totalTime": 2180}), "8′54″/km")
        self.assertEqual(record_pace_text({"totalDis": 4080, "totalTime": 2180, "speed": 0}),
                         "8′54″/km")

    def test_accepts_thousandths_form(self):
        # 落库形态若是报文原值（8905），也要解析成同一条配速
        self.assertEqual(format_pace_from_server_speed(8905), "8′54″/km")

    def test_missing_everything_is_dash(self):
        self.assertEqual(record_pace_text({}), "-")
        self.assertEqual(record_pace_text(None), "-")


class RecordDetailDisplayTests(unittest.TestCase):
    """详情模型：配速与速度必须自洽（旧实现同一行里 8.9 分/km 配 8.91m/s）。"""

    def test_detail_exposes_app_style_pace(self):
        d = RunRecordDetail.from_api_data({
            "rrid": 1350559546, "totalDis": 4080.0, "totalTime": 2180,
            "speed": 8.905, "complete": True, "reasonList": [],
        })
        self.assertEqual(d.pace_text, "8′54″/km")
        self.assertAlmostEqual(d.avg_speed, 1.872, places=3)
        # 速度与配速互为倒数，不可能同时是 8.9 和 1.87
        self.assertLess(d.avg_speed, 3.0)

    def test_detail_pace_matches_raw_record(self):
        raw = {"rrid": 1, "totalDis": 3104.0, "totalTime": 983, "speed": 5.28,
               "complete": True}
        d = RunRecordDetail.from_api_data(raw)
        self.assertEqual(d.pace_text, record_pace_text(raw))
        self.assertAlmostEqual(d.pace_min_per_km, pace_min_per_km(3104, 983), places=2)


class CliOutputTests(unittest.TestCase):
    """CLI 记录回查的文本里，配速与速度必须同时对。"""

    def test_cli_detail_line_uses_shared_format(self):
        import history.cli as cli_module  # noqa: F401  确认可导入
        src = _read(os.path.join("history", "cli.py"))
        self.assertIn("detail.pace_text", src)
        # 旧写法把配速另算一遍（pace = total_time/60/...），是两边漂移的来源
        self.assertNotIn("pace = detail.total_time", src)


@unittest.skipUnless(HAS_FLASK, "需要 Flask（CI 干净环境跳过，与冒烟策略一致）")
class WebUiPaceTests(unittest.TestCase):
    """网页历史记录表必须带配速列，且与 CLI 同一个字符串。"""

    def _row(self):
        import webui
        from unittest.mock import patch
        client = type("C", (), {
            "token": "DUMMY", "uid": 1, "unid": None,
            "get_history_records": staticmethod(lambda **_: {
                "error": 10000,
                "data": {"recordList": [
                    {"rrid": 1350559546, "totalDis": 4080, "totalTime": 2180,
                     "speed": 8.905, "complete": True},
                    {"rrid": 2, "totalDis": 2000, "totalTime": 720, "complete": True},
                ]},
            }),
        })()
        with patch.object(webui, "_new_client", return_value=client):
            return webui.do_history()

    def test_history_rows_carry_pace_column(self):
        res = self._row()
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"][0]["配速"], "8′54″/km")
        # 服务端没给 speed 的那条按里程/耗时自算，同样是分′秒″
        self.assertEqual(res["data"][1]["配速"], "6′00″/km")

    def test_page_renders_pace_column(self):
        html = _read("webui.html")
        self.assertIn("<th>配速</th>", html)
        # 前端实时提示与后端同格式；旧的小数写法必须消失
        self.assertIn("fmtPace", html)
        self.assertNotIn("toFixed(1)+' 分/km'", html)

    def test_frontend_and_backend_produce_same_text(self):
        """把 JS 的 fmtPace 与 Python 的 format_pace 逐点对表。"""
        html = _read("webui.html")
        body = re.search(r"function fmtPace\(secPerKm\)\{(.*?)\n\}", html, re.S)
        self.assertIsNotNone(body, "webui.html 里找不到 fmtPace 实现")
        # JS 版本必须用同一组分隔符与两位秒
        self.assertIn("′", body.group(1))
        self.assertIn("″", body.group(1))
        self.assertIn("padStart(2,'0')", body.group(1))

    def test_run_result_exposes_pace(self):
        # 结果卡片直接展示后端算好的文案，不在前端再算一次
        src = _read("webui.py")
        self.assertIn('"配速": res.pace_text or "-"', src)


def _find_node():
    """找一个可用的 node 可执行文件；找不到就跳过 JS 对表。"""
    import shutil
    for cand in (os.environ.get("NODE_EXECUTABLE"), shutil.which("node"),
                 shutil.which("node.exe")):
        if cand and os.path.exists(cand):
            return cand
    return None


NODE_BIN = _find_node()


@unittest.skipUnless(NODE_BIN, "需要 node 才能执行前端 fmtPace")
class FrontendParityTests(unittest.TestCase):
    """真跑一遍 JS：抽取 webui.html 里的 fmtPace，逐点与 Python 对表。

    只断言「格式片段存在」是不够的 —— 舍入方向这类差异只有执行才看得出来
    （Python round 是银行家舍入，JS Math.round 是四舍五入）。
    """

    POINTS = [534.3, 534.5, 535.5, 539.6, 359.4, 600.0, 468.29, 494.9, 0, -3]

    def test_js_and_python_agree(self):
        import json
        import subprocess

        html = _read("webui.html")
        body = re.search(r"(function fmtPace\(secPerKm\)\{.*?\n\})", html, re.S)
        self.assertIsNotNone(body, "webui.html 里找不到 fmtPace 实现")
        script = (body.group(1)
                  + f"\nconst pts={json.dumps(self.POINTS)};"
                    "\nconsole.log(JSON.stringify(pts.map(fmtPace)));")
        proc = subprocess.run([NODE_BIN, "-e", script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr[:400])

        js_paces = json.loads(proc.stdout)
        for point, js_text in zip(self.POINTS, js_paces):
            with self.subTest(sec_per_km=point):
                self.assertEqual(js_text, format_pace(pace_sec=point, with_unit=False))


if __name__ == "__main__":
    unittest.main()