# -*- coding: utf-8 -*-
"""历史记录「状态」必须是真判定，不能只看列表字段。

背景：列表接口 /api/v70230/runnings/records 不返回 reasonList。实测 10 条记录里
列表的 complete 全是 True，其中 2 条真实判定是「未达标: 配速控制在2′00″-10′00″」。
所以列表页显示的「达标」是假的 —— 本文件的用例把这条差异钉死。
"""
import importlib.util
import io
import os
import sys
import unittest
from unittest.mock import patch

from history.models import (RunRecordDetail, detail_for, history_record_status,
                            parse_history_records, record_verdict)

HAS_FLASK = importlib.util.find_spec("flask") is not None
_ROOT = os.path.dirname(os.path.abspath(__file__))

# 实测记录：4080m / 2180s，列表说 complete=True，详情里有一条 reason 未完成
_LIST_ROW = {"rrid": 1350559546, "totalDis": 4080, "totalTime": 2180,
             "speed": 8.905, "complete": True}
_DETAIL_FAILED = {
    "rrid": 1350559546, "totalDis": 4080, "totalTime": 2180, "speed": 8.905,
    "complete": True, "unCompleteReason": 0, "isCheat": 0,
    "reasonList": [{"reason": "配速控制在2′00″-10′00″", "complete": False}],
}
_DETAIL_OK = {
    "rrid": 2, "totalDis": 2000, "totalTime": 720, "speed": 6.0,
    "complete": True, "unCompleteReason": 0, "isCheat": 0,
    "reasonList": [{"reason": "配速控制在2′00″-10′00″", "complete": True}],
}


class VerdictUnitTests(unittest.TestCase):
    def test_list_alone_says_pass_which_is_why_detail_is_needed(self):
        # 这条断言不是「期望行为」，而是记录「列表为什么会骗人」
        self.assertEqual(history_record_status(_LIST_ROW), "达标")
        self.assertEqual(record_verdict(_LIST_ROW), ("达标", ""))

    def test_detail_overrides_list(self):
        detail = RunRecordDetail.from_api_data(_DETAIL_FAILED)
        self.assertEqual(record_verdict(_LIST_ROW, detail),
                         ("未达标", "配速控制在2′00″-10′00″"))

    def test_detail_pass_has_no_reason(self):
        detail = RunRecordDetail.from_api_data(_DETAIL_OK)
        self.assertEqual(record_verdict(_DETAIL_OK, detail), ("达标", ""))

    def test_cheat_is_its_own_verdict(self):
        detail = RunRecordDetail.from_api_data(
            {"rrid": 3, "totalDis": 2000, "totalTime": 720, "complete": True, "isCheat": 1})
        self.assertEqual(detail.verdict, "判定作弊")
        self.assertEqual(detail.reason_text, "")

    def test_status_text_keeps_single_line_form(self):
        # running/client.py 与 CLI 都打这一行，结论+原因必须一次说完
        failed = RunRecordDetail.from_api_data(_DETAIL_FAILED)
        self.assertEqual(failed.status_text(), "未达标: 配速控制在2′00″-10′00″")
        self.assertEqual(RunRecordDetail.from_api_data(_DETAIL_OK).status_text(), "达标")

    def test_uncomplete_reason_used_when_reason_list_empty(self):
        detail = RunRecordDetail.from_api_data(
            {"rrid": 4, "totalDis": 2000, "totalTime": 720, "complete": False,
             "unCompleteReason": 7})
        self.assertEqual(detail.verdict, "未达标")
        self.assertEqual(detail.reason_text, "unCompleteReason=7")

    def test_detail_for_tolerates_bad_keys(self):
        detail = RunRecordDetail.from_api_data(_DETAIL_FAILED)
        mapping = {1350559546: detail}
        self.assertIs(detail_for(mapping, 1350559546), detail)
        self.assertIsNone(detail_for(mapping, None))
        self.assertIsNone(detail_for(mapping, "not-a-number"))
        self.assertIsNone(detail_for(mapping, 999))
        self.assertIsNone(detail_for({}, 1350559546))


class _FakeHistory(object):
    """只实现 get_detail_map 的最小替身。"""

    def __init__(self, details, raise_on_call=False):
        self._details = details
        self._raise = raise_on_call
        self.calls = []

    def get_detail_map(self, rrids):
        self.calls.append(list(rrids))
        if self._raise:
            raise RuntimeError("网络抖动")
        return {k: v for k, v in self._details.items() if k in [int(r) for r in rrids]}


class FetchDetailMapTests(unittest.TestCase):
    def test_accepts_sport_client_shape(self):
        from history.client import fetch_detail_map
        detail = RunRecordDetail.from_api_data(_DETAIL_FAILED)
        fake = type("C", (), {"history": _FakeHistory({1350559546: detail})})()
        got = fetch_detail_map(fake, [_LIST_ROW])
        self.assertEqual(got[1350559546].verdict, "未达标")

    def test_accepts_bare_history_client_shape(self):
        # history/cli.py 直接持有 HistoryClient：详情方法就在自己身上
        from history.client import fetch_detail_map
        detail = RunRecordDetail.from_api_data(_DETAIL_FAILED)
        got = fetch_detail_map(_FakeHistory({1350559546: detail}), [_LIST_ROW])
        self.assertEqual(got[1350559546].verdict, "未达标")

    def test_degrades_to_empty_on_error_or_missing_capability(self):
        from history.client import fetch_detail_map
        self.assertEqual(fetch_detail_map(_FakeHistory({}, raise_on_call=True), [_LIST_ROW]), {})
        self.assertEqual(fetch_detail_map(object(), [_LIST_ROW]), {})
        # Mock 会伪造任意属性，返回值不是 dict 时必须当作「没回查到」
        from unittest.mock import Mock
        self.assertEqual(fetch_detail_map(Mock(), [_LIST_ROW]), {})
        # 没有可回查的记录就不该发请求
        fake = _FakeHistory({})
        self.assertEqual(fetch_detail_map(fake, [{"rrid": 0}]), {})
        self.assertEqual(fake.calls, [])

    def test_get_detail_map_skips_failures_per_record(self):
        from history.client import HistoryClient
        detail = RunRecordDetail.from_api_data(_DETAIL_OK)
        calls = []

        def fake_one(self, rrid):
            calls.append(rrid)
            if rrid == 111:
                raise RuntimeError("单条超时")
            return detail if rrid == 222 else None

        holder = object.__new__(HistoryClient)
        with patch.object(HistoryClient, "get_one_record", fake_one):
            got = HistoryClient.get_detail_map(holder, [111, 222, 333, 0, None])
        self.assertEqual(calls, [111, 222, 333])          # 0/None 不发请求
        self.assertEqual(list(got.keys()), [222])          # 失败与空结果都不入表


@unittest.skipUnless(HAS_FLASK, "需要 Flask（CI 干净环境跳过，与冒烟策略一致）")
class WebUiVerdictTests(unittest.TestCase):
    def _history_rows(self, details=None):
        import webui
        client = type("C", (), {
            "token": "DUMMY", "uid": 1, "unid": None,
            "history": _FakeHistory(details or {}),
            "get_history_records": staticmethod(lambda **_: {
                "error": 10000, "data": [_LIST_ROW],
            }),
        })()
        with patch.object(webui, "_new_client", return_value=client):
            return webui.do_history()

    def test_row_shows_real_verdict_and_reason(self):
        detail = RunRecordDetail.from_api_data(_DETAIL_FAILED)
        res = self._history_rows({1350559546: detail})
        self.assertTrue(res["ok"])
        row = res["data"][0]
        self.assertEqual(row["状态"], "未达标")
        self.assertEqual(row["原因"], "配速控制在2′00″-10′00″")
        # 配速列不受影响
        self.assertEqual(row["配速"], "8′54″/km")

    def test_row_falls_back_to_list_when_detail_missing(self):
        res = self._history_rows({})
        row = res["data"][0]
        self.assertEqual(row["状态"], "达标")
        self.assertEqual(row["原因"], "")

    def test_table_has_reason_column(self):
        with io.open(os.path.join(_ROOT, "webui.html"), encoding="utf-8") as fp:
            html = fp.read()
        self.assertIn("<th>原因</th>", html)
        self.assertIn("r.原因", html)


class CliVerdictTests(unittest.TestCase):
    def test_root_cli_prints_verdict_with_reason(self):
        spec = importlib.util.spec_from_file_location(
            "verdict_root_cli", os.path.join(_ROOT, "cli.py"))
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        detail = RunRecordDetail.from_api_data(_DETAIL_FAILED)

        class FakeClient(object):
            token = "DUMMY"
            uid = 1
            unid = None
            history = _FakeHistory({1350559546: detail})

            def get_history_records(self, **_):
                return {"error": 10000, "data": [_LIST_ROW]}

        with patch.object(cli, "SportClient792", return_value=FakeClient()), \
                patch.object(sys, "argv", ["cli.py", "-t", "DUMMY", "--uid", "1", "--history"]), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            cli.main()
        text = out.getvalue()
        self.assertIn("状态=未达标: 配速控制在2′00″-10′00″", text)

    def test_history_cli_no_detail_flag_skips_lookup(self):
        spec = importlib.util.spec_from_file_location(
            "verdict_history_cli", os.path.join(_ROOT, "history", "cli.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fake_history = _FakeHistory({1350559546: RunRecordDetail.from_api_data(_DETAIL_FAILED)})

        class FakeClient(object):
            def __init__(self, **_):
                self.history = fake_history

            def get_run_records(self, **_):
                return {"error": 10000, "data": {"recordList": [_LIST_ROW]}}

            def get_calendar_list(self):
                return {"error": 10000}

        with patch.object(mod, "HistoryClient", FakeClient), \
                patch.object(sys, "argv", ["cli.py", "-t", "D", "-u", "1", "--no-detail"]), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            fake_history.calls = []
            mod.main()
        self.assertEqual(fake_history.calls, [])           # --no-detail 不发请求
        self.assertIn("状态=达标", out.getvalue())


if __name__ == "__main__":
    unittest.main()
