"""Offline regressions for current and legacy history response formats."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from history.models import parse_history_records, history_record_status

class HistoryCliTests(unittest.TestCase):
    def test_list_and_legacy_envelope(self):
        rows=[{'rrid':1,'totalDis':2150,'totalTime':760,'status':1,'complete':True}]
        for data in (rows,{'recordList':rows},json.dumps(rows),json.dumps({'recordList':rows})):
            self.assertEqual(parse_history_records(data),rows)
        for data in (None,[],{'recordList':None},{'recordList':[]}):
            self.assertEqual(parse_history_records(data),[])

    def test_malformed_response_is_not_silently_empty(self):
        for data in ({},1,{'recordList':{}},['invalid'],'{broken'):
            with self.assertRaises(ValueError):parse_history_records(data)

    def test_normal_status_does_not_override_incomplete_flag(self):
        self.assertEqual(history_record_status({'status':1,'complete':False}),'未达标')
        self.assertEqual(history_record_status({'status':1,'complete':True}),'达标')
        self.assertNotEqual(history_record_status({'status':1}),'达标')

    def test_detail_speed_field_is_pace_not_mps(self):
        from history.models import RunRecordDetail
        d = RunRecordDetail.from_api_data({
            'rrid': 1345090379, 'totalDis': 2000.0, 'totalTime': 720,
            # 服务端 speed 存的是**配速**（分/公里）：2000m/720s → 6.0 分/km。
            # 曾经这里写成 2.778（速度 m/s），于是 CLI 打出「配速 6.0 · 速度 2.78」
            # 看似合理，实际是把两个不同量纲顺手对上了。
            'speed': 6.0, 'avgStepFreq': 227, 'validDis': 2000.0,
            'validTime': 720, 'calorie': 135, 'complete': True,
        })
        self.assertIsInstance(d.avg_speed, float)
        # 配速 6.0 分/km ⇔ 2.778 m/s
        self.assertAlmostEqual(d.avg_speed, 2.778, places=3)
        self.assertAlmostEqual(d.pace_min_per_km, 6.0, places=3)
        self.assertEqual(d.pace_text, "6′00″/km")

    def test_detail_pace_falls_back_to_distance_and_time(self):
        from history.models import RunRecordDetail
        # speed 缺失或为 0 时用里程/耗时自算，不能返回 0 分/km
        d = RunRecordDetail.from_api_data({
            'rrid': 1, 'totalDis': 4080.0, 'totalTime': 2180, 'speed': 0,
            'complete': True,
        })
        self.assertEqual(d.pace_text, "8′54″/km")
        self.assertAlmostEqual(d.avg_speed, 1.872, places=3)

    def test_history_cli_formats_rows_and_handles_bad_payload(self):
        spec=importlib.util.spec_from_file_location('history_test_cli',Path(__file__).with_name('cli.py'))
        cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
        for data in ([{'rrid':1,'totalDis':2150,'totalTime':760,'status':1,'complete':True}],
                     {'recordList':[]},['invalid']):
            client=Mock();client.token='DUMMY';client.uid=1
            client.get_history_records.return_value={'error':10000,'data':data}
            with patch.object(cli,'SportClient792',return_value=client), \
                 patch.object(sys,'argv',['cli.py','-t','DUMMY','--uid','1','--history']), \
                 patch('sys.stdout',new_callable=io.StringIO) as output:
                cli.main()
            text=output.getvalue()
            if data==['invalid']:self.assertIn('响应解析失败',text)
            else:self.assertIn('历史记录: 条数=',text)
            client.execute_run.assert_not_called()

if __name__=='__main__':unittest.main()
