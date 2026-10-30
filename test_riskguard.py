# -*- coding: utf-8 -*-
"""风控预检与本地账本的单元测试。

覆盖三件事：
1. 每个检查项的判定边界（信用分跌幅、频次阈值、时段偏离、设备变更）；
2. 账本的容错语义（损坏留档而非删除、原子写、长度收敛）；
3. 预检的兜底契约 —— **任何子项异常都不能阻断主流程**。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.ledger import RunLedger, MAX_RUNS
from running import riskguard
from running.riskguard import (
    LEVEL_BLOCK, LEVEL_OK, LEVEL_WARN, RiskFinding, RiskReport,
    extract_credit_score, preflight,
)

HOUR_MAP = {}


def fake_hour(ms):
    """测试用小时映射：未登记的时间戳一律回落到 12 时。"""
    return HOUR_MAP.get(int(ms), 12)


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        HOUR_MAP.clear()

    def make_ledger(self, runs=None, credits=None, devices=None):
        ledger = RunLedger(self.tmp / "run_ledger.json")
        ledger._data = {
            "version": 1,
            "devices": list(devices or []),
            "runs": list(runs or []),
            "credits": list(credits or []),
        }
        return ledger

    def run_entry(self, at_ms, ok=True):
        return {"at_ms": at_ms, "distance_m": 2000, "time_sec": 780, "ok": ok}


# ------------------------------------------------------------------ 账本
class LedgerTests(LedgerCase):
    def test_missing_file_yields_empty_ledger(self):
        ledger = RunLedger(self.tmp / "nope.json")
        self.assertEqual(ledger.data["runs"], [])
        self.assertEqual(ledger.data["devices"], [])

    def test_corrupt_file_is_archived_not_deleted(self):
        """账本损坏必须留档 —— 直接覆盖等于丢用户几十次运行历史。"""
        target = self.tmp / "run_ledger.json"
        target.write_text("{ 这不是 JSON", encoding="utf-8")
        ledger = RunLedger(target)
        self.assertEqual(ledger.data["runs"], [])
        archived = list(self.tmp.glob("run_ledger.json.corrupt-*"))
        self.assertEqual(len(archived), 1, "损坏文件应改名留档")
        self.assertFalse(target.exists(), "原文件应已改名")

    def test_save_is_atomic_and_leaves_no_tmp(self):
        ledger = RunLedger(self.tmp / "run_ledger.json")
        ledger.record_run(at_ms=1000, distance_m=2000, time_sec=600, device_id="a" * 32)
        self.assertTrue((self.tmp / "run_ledger.json").exists())
        self.assertEqual(list(self.tmp.glob("*.tmp")), [], "临时文件必须被 replace 掉")

    def test_device_status_transitions(self):
        ledger = self.make_ledger()
        self.assertEqual(ledger.touch_device("a" * 32), "new")
        self.assertEqual(ledger.touch_device("a" * 32), "same")
        self.assertEqual(ledger.touch_device("b" * 32), "changed")

    def test_runs_are_trimmed_to_cap(self):
        ledger = self.make_ledger()
        for index in range(MAX_RUNS + 20):
            ledger.data["runs"].append(self.run_entry(1000 + index))
        ledger._trim()
        self.assertEqual(len(ledger.data["runs"]), MAX_RUNS)
        self.assertEqual(ledger.data["runs"][-1]["at_ms"], 1000 + MAX_RUNS + 19,
                         "裁剪必须保留最新的记录")

    def test_runs_since_filters_by_time_and_success(self):
        ledger = self.make_ledger(runs=[
            self.run_entry(1000, ok=True),
            self.run_entry(5000, ok=False),
            self.run_entry(9000, ok=True),
        ])
        self.assertEqual(len(ledger.runs_since(4000)), 1, "只保留时间窗内的成功记录")
        self.assertEqual(len(ledger.runs_since(4000, only_successful=False)), 2)

    def test_credit_trail_skips_unparsable_entries(self):
        ledger = self.make_ledger(credits=[
            {"at_ms": 1, "score": 100},
            {"at_ms": 2, "score": "bad"},
            {"at_ms": 3, "score": 95},
        ])
        self.assertEqual(ledger.credit_trail(5), [100, 95])
        self.assertEqual(ledger.last_credit(), 95)

    def test_artifact_saved_under_state_dir(self):
        ledger = RunLedger(self.tmp / "run_ledger.json")
        path = ledger.save_artifact("cheat_blacklist.json", {"data": [1, 2]})
        self.assertTrue(path.exists())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["data"], [1, 2])


# ------------------------------------------------------------ 信用分解析
class CreditScoreParsingTests(unittest.TestCase):
    def test_recognises_common_key_names(self):
        self.assertEqual(extract_credit_score({"creditScore": 92}), 92)
        self.assertEqual(extract_credit_score({"score": "88"}), 88)
        self.assertEqual(extract_credit_score({"data": {"creditScore": 75.0}}), 75)

    def test_ignores_non_score_numbers(self):
        self.assertEqual(extract_credit_score({"error": 10000, "count": 3}), None)

    def test_ignores_bool_values(self):
        """bool 是 int 的子类，不过滤会把 True 当成 1 分写进账本。"""
        self.assertIsNone(extract_credit_score({"score": True}))

    def test_returns_none_for_unrecognised_shape(self):
        self.assertIsNone(extract_credit_score({"foo": "bar"}))
        self.assertIsNone(extract_credit_score(None))


# ------------------------------------------------------------ 各项检查
class CreditCheckTests(LedgerCase):
    def check(self, payload, credits=None):
        ledger = self.make_ledger(credits=credits)
        telemetry = type("T", (), {"get_credit_popup": lambda *a, **k: payload})()
        return riskguard._check_credit(telemetry, None, ledger)

    def test_first_reading_is_baseline_not_a_drop(self):
        finding = self.check({"creditScore": 100})
        self.assertEqual(finding.level, LEVEL_OK)
        self.assertIn("基线", finding.message)

    def test_small_drop_warns(self):
        finding = self.check({"creditScore": 98}, credits=[{"at_ms": 1, "score": 100}])
        self.assertEqual(finding.level, LEVEL_WARN)
        self.assertIn("下跌 2 分", finding.message)

    def test_large_drop_blocks(self):
        finding = self.check({"creditScore": 85}, credits=[{"at_ms": 1, "score": 100}])
        self.assertEqual(finding.level, LEVEL_BLOCK)

    def test_flat_score_passes(self):
        finding = self.check({"creditScore": 100}, credits=[{"at_ms": 1, "score": 100}])
        self.assertEqual(finding.level, LEVEL_OK)

    def test_unparsable_response_warns_without_guessing(self):
        finding = self.check({"data": "unknown"})
        self.assertEqual(finding.level, LEVEL_WARN)
        self.assertIn("未识别", finding.message)

    def test_network_failure_warns(self):
        def boom(self, c):
            raise RuntimeError("timeout")
        ledger = self.make_ledger()
        telemetry = type("T", (), {"get_credit_popup": boom})()
        finding = riskguard._check_credit(telemetry, None, ledger)
        self.assertEqual(finding.level, LEVEL_WARN)


class FrequencyCheckTests(LedgerCase):
    NOW = 1_700_000_000_000
    DAY = 86_400_000

    def test_no_runs_today_passes(self):
        findings = riskguard._check_frequency(self.make_ledger(), self.NOW)
        self.assertEqual(findings[0].level, LEVEL_OK)

    def test_one_run_today_warns(self):
        ledger = self.make_ledger(runs=[self.run_entry(self.NOW - 3600_000)])
        findings = riskguard._check_frequency(ledger, self.NOW)
        self.assertEqual(findings[0].level, LEVEL_WARN)

    def test_two_runs_today_blocks(self):
        runs = [self.run_entry(self.NOW - hours * 3600_000) for hours in (1, 2)]
        findings = riskguard._check_frequency(self.make_ledger(runs=runs), self.NOW)
        self.assertEqual(findings[0].level, LEVEL_BLOCK)
        self.assertIn("2 次", findings[0].message)

    def test_yesterday_runs_do_not_count(self):
        """按自然日统计：昨晚跑过一次，今天再跑是「今天第一次」，不该拦。"""
        runs = [self.run_entry(self.NOW - self.DAY),
                self.run_entry(self.NOW - self.DAY - 3600_000)]
        findings = riskguard._check_frequency(self.make_ledger(runs=runs), self.NOW)
        self.assertEqual(findings[0].level, LEVEL_OK)

    def test_failed_runs_do_not_count(self):
        runs = [self.run_entry(self.NOW - 1000, ok=False) for _ in range(5)]
        findings = riskguard._check_frequency(self.make_ledger(runs=runs), self.NOW)
        self.assertEqual(findings[0].level, LEVEL_OK, "失败提交不构成频次异常")

    def test_corrupt_timestamp_does_not_crash(self):
        """账本里混入负数时间戳时，时段判断必须跳过而不是抛异常。"""
        runs = [self.run_entry(-1) for _ in range(4)]
        findings = riskguard._check_frequency(self.make_ledger(runs=runs), self.NOW)
        self.assertEqual(findings[-1].level, LEVEL_OK)


class HourCheckTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.patcher = patch.object(riskguard, "time_hour_of", side_effect=fake_hour)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_insufficient_samples_pass(self):
        ledger = self.make_ledger(runs=[self.run_entry(1000), self.run_entry(2000)])
        finding = riskguard._check_hour(ledger, 3000)
        self.assertEqual(finding.level, LEVEL_OK)
        self.assertIn("不足以判断", finding.message)

    def test_consistent_hour_passes(self):
        for ms in (1000, 2000, 3000):
            HOUR_MAP[ms] = 19
        HOUR_MAP[4000] = 19
        ledger = self.make_ledger(runs=[self.run_entry(ms) for ms in (1000, 2000, 3000)])
        finding = riskguard._check_hour(ledger, 4000)
        self.assertEqual(finding.level, LEVEL_OK)

    def test_shifted_hour_warns(self):
        for ms in (1000, 2000, 3000):
            HOUR_MAP[ms] = 19
        HOUR_MAP[4000] = 3
        ledger = self.make_ledger(runs=[self.run_entry(ms) for ms in (1000, 2000, 3000)])
        finding = riskguard._check_hour(ledger, 4000)
        self.assertEqual(finding.level, LEVEL_WARN)
        self.assertIn("偏差 8 小时", finding.message)

    def test_wrap_around_hour_is_not_treated_as_far(self):
        """23 点与 1 点相差 2 小时，不是 22 小时 —— 环形距离而非直线差。"""
        for ms in (1000, 2000, 3000):
            HOUR_MAP[ms] = 23
        HOUR_MAP[4000] = 1
        ledger = self.make_ledger(runs=[self.run_entry(ms) for ms in (1000, 2000, 3000)])
        finding = riskguard._check_hour(ledger, 4000)
        self.assertEqual(finding.level, LEVEL_OK)


class DeviceCheckTests(LedgerCase):
    def test_first_device_registers(self):
        finding = riskguard._check_device(self.make_ledger(), "a" * 32, 1000)
        self.assertEqual(finding.level, LEVEL_OK)
        self.assertIn("首次登记", finding.message)

    def test_same_device_passes(self):
        ledger = self.make_ledger(devices=[{"id": "a" * 32, "first_ms": 1, "last_ms": 1, "runs": 1}])
        finding = riskguard._check_device(ledger, "a" * 32, 2000)
        self.assertEqual(finding.level, LEVEL_OK)

    def test_changed_device_warns_about_10121(self):
        ledger = self.make_ledger(devices=[{"id": "a" * 32, "first_ms": 1, "last_ms": 1, "runs": 1}])
        finding = riskguard._check_device(ledger, "b" * 32, 2000)
        self.assertEqual(finding.level, LEVEL_WARN)
        self.assertIn("10121", finding.message)


class ServerSideCheckTests(LedgerCase):
    def test_control_message_with_ban_keyword_blocks(self):
        telemetry = type("T", (), {
            "check_control_message": lambda self: {"status": 200, "body": "账号已被封禁"}})()
        finding = riskguard._check_control_message(telemetry)
        self.assertEqual(finding.level, LEVEL_BLOCK)

    def test_empty_control_message_passes(self):
        telemetry = type("T", (), {
            "check_control_message": lambda self: {"status": 200, "body": ""}})()
        self.assertEqual(riskguard._check_control_message(telemetry).level, LEVEL_OK)

    def test_control_message_failure_does_not_block(self):
        telemetry = type("T", (), {
            "check_control_message": lambda self: {"error": -1, "message": "timeout"}})()
        self.assertEqual(riskguard._check_control_message(telemetry).level, LEVEL_WARN)

    def test_blacklist_is_persisted(self):
        ledger = RunLedger(self.tmp / "run_ledger.json")
        payload = {"error": 10000, "data": [{"pkg": "com.x"}, {"pkg": "com.y"}]}
        telemetry = type("T", (), {"get_cheat_blacklist": lambda *a, **k: payload})()
        finding = riskguard._check_cheat_blacklist(telemetry, None, ledger)
        self.assertEqual(finding.level, LEVEL_OK)
        self.assertTrue((self.tmp / "cheat_blacklist.json").exists())

    def test_blacklist_failure_warns(self):
        ledger = RunLedger(self.tmp / "run_ledger.json")
        telemetry = type("T", (), {"get_cheat_blacklist": lambda *a, **k: {"error": 500}})()
        self.assertEqual(riskguard._check_cheat_blacklist(telemetry, None, ledger).level, LEVEL_WARN)

    def test_barrier_free_policy_is_persisted(self):
        ledger = RunLedger(self.tmp / "run_ledger.json")
        telemetry = type("T", (), {"get_barrier_free_policy": lambda *a, **k: {"error": 0, "data": {}}})()
        finding = riskguard._check_barrier_free(telemetry, None, ledger)
        self.assertEqual(finding.level, LEVEL_OK)
        self.assertTrue((self.tmp / "barrier_free_policy.json").exists())

    def test_tls_finding_is_marked_as_known_limitation(self):
        finding = riskguard._check_tls()
        self.assertEqual(finding.level, LEVEL_WARN)
        self.assertTrue(finding.is_known_limitation, "TLS 项属于已知限制，不应混在行为告警里")


# ------------------------------------------------------------ 报告聚合
class RiskReportTests(unittest.TestCase):
    def test_level_is_the_max_of_findings(self):
        report = RiskReport([RiskFinding("a", LEVEL_OK, ""), RiskFinding("b", LEVEL_WARN, "")])
        self.assertEqual(report.level, LEVEL_WARN)
        self.assertFalse(report.blocked)
        report.findings.append(RiskFinding("c", LEVEL_BLOCK, "挂了"))
        self.assertEqual(report.level, LEVEL_BLOCK)
        self.assertTrue(report.blocked)
        self.assertEqual(report.blocking_reason(), "挂了")

    def test_empty_report_is_ok(self):
        self.assertEqual(RiskReport().level, LEVEL_OK)

    def test_known_limitations_are_merged_into_one_line(self):
        report = RiskReport([RiskFinding("a", LEVEL_OK, "正常"),
                             RiskFinding("tls", LEVEL_WARN, "无 JA3", {"known_limitation": True})])
        lines = report.lines()
        self.assertIn("[通过] 正常", lines)
        self.assertTrue(any(line.startswith("已知限制:") for line in lines))

    def test_include_ok_false_hides_passing_items(self):
        report = RiskReport([RiskFinding("a", LEVEL_OK, "正常"), RiskFinding("b", LEVEL_WARN, "注意")])
        self.assertEqual(report.lines(include_ok=False), ["[注意] 注意"])


# ------------------------------------------------------------ 端到端
class PreflightTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.patcher = patch.object(riskguard, "time_hour_of", side_effect=fake_hour)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def healthy_telemetry(self):
        return type("T", (), {
            "check_control_message": lambda s: {"status": 200, "body": ""},
            "get_credit_popup": lambda *a, **k: {"creditScore": 100},
            "get_cheat_blacklist": lambda *a, **k: {"error": 10000, "data": []},
            "get_barrier_free_policy": lambda *a, **k: {"error": 0, "data": {}},
        })()

    def test_healthy_path_is_not_blocked(self):
        report = preflight(self.healthy_telemetry(), None, self.make_ledger(),
                           now_ms=10_000_000, device_id="c" * 32)
        self.assertFalse(report.blocked)
        codes = {item.code for item in report.findings}
        self.assertTrue({"control_message", "credit", "cheat_blacklist",
                         "barrier_free", "device", "frequency", "tls_fingerprint"} <= codes)

    def test_every_subcheck_exception_is_contained(self):
        """所有子项同时炸掉，预检也必须返回报告而不是抛异常。"""
        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        telemetry = type("T", (), {
            "check_control_message": boom,
            "get_credit_popup": boom,
            "get_cheat_blacklist": boom,
            "get_barrier_free_policy": boom,
        })()
        report = preflight(telemetry, None, self.make_ledger(),
                           now_ms=10_000_000, device_id="d" * 32)
        self.assertFalse(report.blocked, "查不到不是罪证，不得阻断")
        self.assertTrue(report.warnings)

    def test_blocked_when_frequency_exceeded(self):
        now = 1_700_000_000_000
        runs = [self.run_entry(now - hours * 3600_000) for hours in (1, 5, 9)]
        ledger = self.make_ledger(runs=runs, credits=[{"at_ms": 1, "score": 100}])
        report = preflight(self.healthy_telemetry(), None, ledger,
                           now_ms=now, device_id="e" * 32)
        self.assertTrue(report.blocked)
        self.assertIn("频次", report.blocking_reason())


if __name__ == '__main__':
    unittest.main()
