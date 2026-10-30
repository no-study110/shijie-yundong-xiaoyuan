# -*- coding: utf-8 -*-
import argparse
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

cur_dir = os.path.dirname(os.path.abspath(__file__))
if cur_dir not in sys.path:
    sys.path.insert(0, cur_dir)

from sport_client import SportClient792
from core.config import load_customer_device
from core.session import load_session, save_session
from config_loader import get_session_file_path, get_realism_defaults
from history.models import parse_history_records, record_verdict, detail_for
from history.client import fetch_detail_map

SESSION_FILE = get_session_file_path()


def main():
    parser = argparse.ArgumentParser(description="运动世界校园 7.3.92 纯协议套件")
    parser.add_argument("-u", "--username", help="登录账号 (手机号)")
    parser.add_argument("-p", "--password", help="登录密码")
    parser.add_argument("-t", "--token", help="已有登录 Token")
    parser.add_argument("--uid", help="已有用户 UID")
    parser.add_argument("--unid", help="已有学校 UNID")
    parser.add_argument("--device-id", help="指定设备指纹 DeviceId(32 位)，复用原手机设备可规避 10121 设备保护拦截")
    parser.add_argument("--device-config", help="自定义设备配置 JSON 路径(读取 setting.phone.brand/model/vdid)")
    parser.add_argument("--logout", action="store_true", help="退出登录：清除本地缓存的 Token 凭据")
    parser.add_argument("--sync-time", action="store_true", help="执行服务端时间对齐")
    parser.add_argument("--rules", action="store_true", help="查询跑步规则与电子围栏")
    parser.add_argument("--checkpoints", action="store_true", help="单独查询官方计分跑点位，不上传成绩")
    parser.add_argument("--profile", action="store_true", help="查询学生个人档案")
    parser.add_argument("--history", action="store_true", help="查询最近历史记录与打卡日历")

    # 跑步相关选项
    parser.add_argument("--run", action="store_true", help="触发一键全时序跑步与结算")
    parser.add_argument("--mode", type=int, default=1, choices=[1, 6], help="运动类型: 1计分跑 6自由跑")
    parser.add_argument("--distance", type=int, default=2150, help="跑步目标距离 (米，默认 2150)")
    parser.add_argument("--time-sec", type=int, default=760, help="跑步耗时 (秒，默认 760)")

    # 物理时间模拟选项（默认值从 config.ini 读取）
    realism_cfg = get_realism_defaults()
    parser.add_argument("--no-physical-time", action="store_true",
                       help="跳过物理时间模拟（跑前准备+跑步时长+跑后上传），仅保留网络延迟，适用于批量测试")
    parser.add_argument("--no-realism", dest="realism", action="store_false", default=realism_cfg["enabled"],
                       help="禁用配速波动与生理字段拟真增强（默认启用）")
    parser.add_argument("--match-history", action="store_true",
                       help="按本人历史配速修正目标时长，让新记录落在个人分布内（样本不足自动放弃）")
    parser.add_argument("--no-risk-check", dest="risk_check", action="store_false", default=True,
                       help="跳过提交前的风控预检（设备指纹/频次/信用分/时段），默认启用")

    # 广告相关选项
    parser.add_argument("--watch-ad", action="store_true", help="模拟看广告与激励视频曝光链路")
    parser.add_argument("--ad-pos", default="4", choices=["1", "2", "4"], help="广告位: 1Banner 2弹窗 4激励视频(默认)")
    parser.add_argument("--ad-duration", type=int, default=15, help="广告观看耗时 (秒，默认 15)")

    args = parser.parse_args()

    # 退出登录：清除本地缓存凭据
    if args.logout:
        if os.path.exists(SESSION_FILE):
            os.remove(SESSION_FILE)
            print(f"已退出登录: 本地凭据 {SESSION_FILE} 已删除")
            print("说明: 本项目未实现服务端注销接口，手机 App 端如需退出请在 App 内操作（我的 → 设置 → 退出登录）")
        else:
            print("当前没有缓存的登录凭据")
        return

    # 设备指纹：默认随机 DeviceId 会被服务端判定为新设备，支持手动复用原手机指纹
    device_config = None
    if args.device_config:
        device_config = load_customer_device(args.device_config)
    if args.device_id:
        device_config = device_config or {}
        device_config["DeviceId"] = args.device_id.replace("-", "").strip()

    # 未显式提供凭据时，自动复用上次登录成功缓存的 Token
    session = load_session(SESSION_FILE)
    token = args.token or session.get("token")
    uid = args.uid or session.get("uid")
    unid = args.unid or session.get("unid")

    # 占位符校验：避免把文档里的 <Token>/UID 原样传入被服务端 401「头信息不全」
    def _placeholder_check(name, value, numeric=False):
        if value is None:
            return True
        s = str(value).strip()
        bad = (not s) or ("<" in s or ">" in s) or (numeric and not s.isdigit()) \
            or s.upper() in {"UID", "TOKEN", "UNID", "手机号", "密码"}
        if bad:
            print(f"参数错误: {name} 的值 '{s}' 不是真实凭据（占位符请替换为实际值，或直接省略以使用缓存凭据）")
            return False
        return True

    if not _placeholder_check("--uid", uid, numeric=True):
        return
    if not _placeholder_check("--token", token):
        return
    if unid is not None and not _placeholder_check("--unid", unid, numeric=True):
        return

    client = SportClient792(uid=uid, token=token, unid=unid, device_config=device_config)

    # 1. 登录认证
    if not _login(client, args):
        return

    # 2. 时钟对齐（未指定任何具体动作时默认执行一次，用于校时）
    if args.sync_time or _no_explicit_action(args):
        st = client.sync_server_time()
        print(f"时钟同步: 服务端时间={st.server_datetime} 偏斜={st.offset_seconds:+.3f}s")

    # 3. 个人档案
    if args.profile:
        _print_profile(client)

    # 4. 跑步规则与围栏
    if args.rules:
        _print_rules(client, args.mode)

    if args.checkpoints:
        _print_checkpoints(client)

    # 5. 广告模拟
    if args.watch_ad:
        _simulate_ad(client, args)

    # 6. 历史记录
    if args.history:
        _print_history(client)

    # 7. 拟人跑步提交
    if args.run:
        _submit_run(client, args)


# --------------------------------------------------------------------------- #
# 各子命令的实现
#
# 每个函数只管一件事，只依赖 (client, args)，不共享状态 —— 这样想改某条
# 命令的输出时不必通读 main。main 现在只负责「按参数依次调用它们」。
# --------------------------------------------------------------------------- #

def _no_explicit_action(args) -> bool:
    """没有指定任何具体动作时，默认只做一次时钟对齐。"""
    return not any((args.rules, args.checkpoints, args.profile,
                    args.history, args.run, args.watch_ad))


def _login(client, args) -> bool:
    """有账号密码就走登录并缓存凭据；返回 False 表示应中止后续流程。"""
    if not (args.username and args.password):
        if client.token:
            print(f"已加载缓存凭据: uid={client.uid} unid={client.unid}")
        return True

    res = client.login(args.username, args.password, progress_callback=print)
    if not res.success:
        print(f"登录失败: code={res.error_code} 原因={res.message}")
        if res.error_code == 10121:
            print("提示: 账号已在其他设备登录。请在手机 App 内「退出登录」后重试，"
                  "或用 --device-id 指定原设备的 DeviceId，或改用 -t <手机抓包 Token> 跳过登录。")
        return False

    save_session(SESSION_FILE, client.token, client.uid, client.unid)
    print(f"登录成功: token={client.token} uid={client.uid} unid={client.unid}")
    print(f"凭据已缓存至 {SESSION_FILE}，后续可直接省略 -t/--uid/--unid")
    return True


def _print_profile(client) -> None:
    """打印学生个人档案。"""
    if not client.token:
        print("个人档案: 失败 原因=缺少Token")
        return
    prof = client.get_user_profile()
    if prof:
        print(f"个人档案: 姓名={prof.name} 性别={prof.sex} 学校={prof.campus_name} "
              f"学院={prof.depart} 班级={prof.grade_class} 学号={prof.student_id}")
    else:
        print("个人档案: 失败 原因=查询为空")


def _print_rules(client, mode: int) -> None:
    """打印学校的跑步规则与围栏数量。"""
    if not client.token or not client.uid:
        print("跑步规则: 失败 原因=缺少Token或UID")
        return
    rule = client.get_run_policy(run_mode=mode)
    fences = client.get_geo_fences()
    mode_name = "计分跑" if mode == 1 else "自由跑"
    if rule:
        print(f"跑步规则: 模式={mode_name} 单日上限={rule.day_goal}m "
              f"单次最低={rule.min_distance}m 配速={rule.speed_range_text()} "
              f"步频={rule.step_bottom}~{rule.step_top}步/分 围栏数={len(fences)}")
    else:
        reason = client.policy.last_policy_error or "规则查询为空"
        print(f"跑步规则: 失败 模式={mode_name} 原因={reason}")


def _print_checkpoints(client) -> None:
    """单独查询官方计分跑点位，不上传成绩。"""
    if not client.token:
        print("官方点位: 失败 原因=缺少Token")
        return
    fences = client.get_geo_fences()
    usable = [fence for fence in fences if fence.polygon_points]
    if not usable:
        print("官方点位: 失败 原因=未取得有效围栏锚点")
        return
    points = client.policy.get_checkpoints_for_run(usable[0])
    if points:
        print(f"官方点位: 成功 来源=pointsResModels 点数={len(points)}；未上传成绩")
    else:
        print(f"官方点位: 失败 原因={client.policy.last_checkpoint_error}")


def _simulate_ad(client, args) -> None:
    """模拟看激励视频广告的完整曝光链路。"""
    ad_res = client.watch_ad(
        ad_position=args.ad_pos,
        duration_sec=args.ad_duration,
        progress_callback=print
    )
    if ad_res.success:
        print(f"广告模拟: 成功 位置={args.ad_pos} 事件数={ad_res.events_count} "
              f"耗时={args.ad_duration}s")
    else:
        print(f"广告模拟: 失败 位置={args.ad_pos} 原因={ad_res.message}")


def _print_history(client) -> None:
    """打印最近 5 条历史记录。"""
    if not client.token or not client.uid:
        print("历史记录: 失败 原因=缺少Token或UID")
        return
    h_res = client.get_history_records(page_num=1, page_size=5)
    if not (isinstance(h_res, dict) and h_res.get("error") == 10000):
        err_msg = h_res.get("message") if isinstance(h_res, dict) else "网络异常"
        print(f"历史记录: 失败 原因={err_msg}")
        return
    try:
        recs = parse_history_records(h_res.get("data"))
    except (TypeError, ValueError) as exc:
        print(f"历史记录: 失败 原因=响应解析失败: {exc}")
        return
    print(f"历史记录: 条数={len(recs)}")
    # 列表没给 reasonList，逐条回查详情才能知道「为什么未达标」
    details = fetch_detail_map(client, recs)
    for r in recs:
        rrid = r.get("rrid")
        verdict, reason = record_verdict(r, detail_for(details, rrid))
        status_str = f"{verdict}: {reason}" if reason else verdict
        print(f"记录明细: rrid={rrid} 里程={r.get('totalDis')}m "
              f"耗时={r.get('totalTime')}s 状态={status_str}")


def _submit_run(client, args) -> None:
    """执行一键全时序跑步与结算。"""
    if not client.token or not client.uid:
        print("跑步提交: 失败 原因=缺少Token或UID")
        return
    mode_name = "计分跑" if args.mode == 1 else "自由跑"
    run_res = client.execute_run(
        target_distance_m=args.distance,
        target_time_sec=args.time_sec,
        sport_type=args.mode,
        realism=args.realism,
        simulate_physical_time=not args.no_physical_time,
        match_history=args.match_history,
        risk_check=args.risk_check,
        progress_callback=print
    )
    if run_res.success:
        print(f"跑步提交: 成功 模式={mode_name} rrid={run_res.rrid} "
              f"里程={run_res.total_distance}m 耗时={run_res.total_time}s "
              f"配速={run_res.pace_text or '-'} 状态={run_res.status_text}")
    else:
        print(f"跑步提交: 失败 模式={mode_name} code={run_res.error_code} "
              f"原因={run_res.message}")


if __name__ == "__main__":
    main()
