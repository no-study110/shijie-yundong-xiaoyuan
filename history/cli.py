# -*- coding: utf-8 -*-
import argparse
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 入口脚本引导：把套件根目录加入 sys.path，以便使用绝对包导入
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from history.client import HistoryClient, fetch_detail_map
from history.models import record_verdict, detail_for
from core.pace import record_pace_text


def main():
    parser = argparse.ArgumentParser(description="运动世界 7.3.92 历史记录与成绩回查终端")
    parser.add_argument("-t", "--token", required=True, help="登录 Token")
    parser.add_argument("-u", "--uid", required=True, help="用户 UID")
    parser.add_argument("--unid", help="学校 UNID")
    parser.add_argument("-r", "--rrid", type=int, help="指定回查的单次跑步记录 RRID")
    parser.add_argument("-p", "--page", type=int, default=1, help="页码")
    parser.add_argument("--no-detail", action="store_true",
                        help="列表模式不回查逐条判定详情（更快，但未达标原因看不到）")
    args = parser.parse_args()

    client = HistoryClient(uid=args.uid, token=args.token, unid=args.unid)

    if args.rrid:
        detail = client.get_one_record(args.rrid)
        if detail:
            # 配速直接取服务端记录里的 speed 字段（App 记录页显示的就是它），
            # 速度由配速换算 —— 两者必须自洽，不能再各算一套。
            print(f"记录回查: rrid={args.rrid} 状态={detail.status_text()} 里程={detail.total_distance:.2f}m 有效里程={detail.valid_distance:.2f}m 耗时={detail.total_time}s 配速={detail.pace_text} 速度={detail.avg_speed:.2f}m/s 步频={detail.avg_step_freq}步/分 热量={detail.calorie}kcal")
        else:
            print(f"记录回查: 失败 rrid={args.rrid} 原因=未找到记录")
        return

    records_res = client.get_run_records(page_num=args.page)
    if isinstance(records_res, dict) and records_res.get("error") == 10000 and "data" in records_res:
        items = records_res["data"].get("recordList", []) or []
        print(f"历史记录: 页码={args.page} 条数={len(items)}")
        rows = items[:5]
        # 列表没给 reasonList：不逐条回查就只能报「达标」，而实测有记录其实是未达标
        details = {} if args.no_detail else fetch_detail_map(client, rows)
        for it in rows:
            rrid = it.get("rrid")
            verdict, reason = record_verdict(it, detail_for(details, rrid))
            status_str = f"{verdict}: {reason}" if reason else verdict
            print(f"记录明细: rrid={rrid} 里程={it.get('totalDis')}m "
                  f"耗时={it.get('totalTime')}s 配速={record_pace_text(it)} 状态={status_str}")
    else:
        err_msg = records_res.get("message") if isinstance(records_res, dict) else "网络异常"
        print(f"历史记录: 失败 页码={args.page} 原因={err_msg}")

    cal_res = client.get_calendar_list()
    if isinstance(cal_res, dict) and cal_res.get("error") == 10000:
        print("打卡日历: 成功")
    else:
        cal_err = cal_res.get("message") if isinstance(cal_res, dict) else "网络异常"
        print(f"打卡日历: 失败 原因={cal_err}")


if __name__ == "__main__":
    main()
