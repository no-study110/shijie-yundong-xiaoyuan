#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""代理使用完整示例

演示如何通过 HTTP/HTTPS/SOCKS5 代理执行全链路跑步提交。
"""
import sys
import os

# 添加父目录到路径以便导入协议套件
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sport_client import SportClient792


def check_proxy_connection(proxy_url: str) -> bool:
    """测试代理连通性"""
    import urllib.request
    import urllib.error

    print(f"\n🔍 测试代理连通性: {proxy_url}")

    proxy_handler = urllib.request.ProxyHandler({
        "http": proxy_url,
        "https": proxy_url
    })
    opener = urllib.request.build_opener(proxy_handler)

    try:
        resp = opener.open("https://www.baidu.com", timeout=5)
        print(f"✓ 代理可用 (HTTP {resp.status})")
        return True
    except urllib.error.URLError as e:
        print(f"✗ 代理不可用: {e}")
        return False
    except Exception as e:
        print(f"✗ 测试失败: {e}")
        return False


def main():
    # ============ 配置区 ============

    # 代理地址(根据实际情况修改)
    PROXY_URL = "http://127.0.0.1:7890"  # Clash 默认端口
    # PROXY_URL = "socks5://127.0.0.1:1080"  # V2Ray SOCKS5
    # PROXY_URL = "http://user:pass@proxy.example.com:8080"  # 带认证

    # 账号信息(从环境变量或命令行参数获取)
    USERNAME = os.getenv("SPORT_USERNAME", "")
    PASSWORD = os.getenv("SPORT_PASSWORD", "")

    # 跑步参数
    TARGET_DISTANCE = 2100  # 目标距离(米)
    TARGET_TIME = 780       # 目标时长(秒)

    # ==============================

    if not USERNAME or not PASSWORD:
        print("错误: 请设置环境变量 SPORT_USERNAME 和 SPORT_PASSWORD")
        print("或直接修改本文件中的账号信息")
        return 1

    # 1. 测试代理连通性
    if not check_proxy_connection(PROXY_URL):
        print("\n⚠️  代理连接失败,请检查:")
        print("  1. 代理软件(Clash/V2Ray)是否已启动")
        print("  2. 代理地址与端口是否正确")
        print("  3. 代理是否允许局域网连接")
        return 1

    # 2. 创建客户端(所有请求都通过代理)
    print(f"\n🚀 创建客户端 (代理: {PROXY_URL})")
    client = SportClient792(proxy=PROXY_URL)

    # 3. 登录
    print(f"\n🔐 登录账号: {USERNAME[:3]}****{USERNAME[-4:]}")
    login_res = client.login(USERNAME, PASSWORD, progress_callback=lambda msg: print(f"  {msg}"))

    if not login_res.success:
        print(f"\n✗ 登录失败: {login_res.message}")
        if login_res.error_code == 10121:
            print("\n提示: 账号在其他设备登录中")
            print("  解决方法:")
            print("  1. 在手机 App 内退出登录")
            print("  2. 或在登录时传入设备指纹 DeviceId")
        return 1

    print(f"\n✓ 登录成功")
    print(f"  uid: {login_res.uid}")
    print(f"  unid: {login_res.unid}")

    # 4. 查询个人档案(验证代理生效)
    print("\n📋 查询个人档案...")
    profile = client.get_user_profile()
    if profile:
        print(f"  姓名: {profile.name}")
        print(f"  学校: {profile.campus_name}")
        print(f"  学院: {profile.depart}")
        print(f"  班级: {profile.grade_class}")

    # 5. 查询跑步规则
    print("\n📐 查询跑步规则...")
    rule = client.get_run_policy(run_mode=1)
    if not rule:
        print(f"✗ 规则查询失败: {client.policy.last_policy_error}")
        return 1

    print(f"  单日上限: {rule.day_goal}m")
    print(f"  单次最低: {rule.min_distance}m")
    print(f"  配速区间: {rule.speed_range_text()}")
    print(f"  步频区间: {rule.step_bottom}~{rule.step_top} 步/分")

    # 6. 检查参数是否合规
    if TARGET_DISTANCE < rule.min_distance:
        print(f"\n⚠️  目标距离({TARGET_DISTANCE}m)小于学校最低要求({rule.min_distance}m)")
        TARGET_DISTANCE = rule.min_distance
        print(f"  已调整为: {TARGET_DISTANCE}m")

    if TARGET_DISTANCE > rule.day_goal:
        print(f"\n⚠️  目标距离({TARGET_DISTANCE}m)超过单日上限({rule.day_goal}m)")
        TARGET_DISTANCE = rule.day_goal
        print(f"  已调整为: {TARGET_DISTANCE}m")

    # 7. 执行跑步提交(全程通过代理)
    print(f"\n🏃 提交跑步记录: 距离={TARGET_DISTANCE}m 耗时={TARGET_TIME}s")
    print("  (启用拟真增强: 非匀速配速 + 生理参数波动 + GPS抖动)")

    run_res = client.execute_run(
        target_distance_m=TARGET_DISTANCE,
        target_time_sec=TARGET_TIME,
        sport_type=1,
        realism=True,  # 启用拟真增强
        progress_callback=lambda msg: print(f"  {msg}")
    )

    # 8. 输出结果
    print("\n" + "=" * 60)
    if run_res.success:
        print(f"✅ 提交成功!")
        print(f"  流水号: {run_res.rrid}")
        print(f"  实际距离: {run_res.total_distance}m")
        print(f"  实际时长: {run_res.total_time}s")
        print(f"  判定结果: {run_res.status_text}")

        if run_res.is_valid:
            print(f"\n🎉 本次跑步有效!")
        else:
            print(f"\n⚠️  本次跑步无效: {run_res.status_text}")
    else:
        print(f"❌ 提交失败: {run_res.message}")
        if run_res.error_code:
            print(f"  错误代码: {run_res.error_code}")
    print("=" * 60)

    # 9. 查询学期进度
    print("\n📊 查询学期进度...")
    semester = client.get_semester_progress(run_mode=1)
    if semester:
        completion = (semester.total_completed_distance / semester.target_distance * 100
                     if semester.target_distance > 0 else 0)
        print(f"  学期: {semester.semester_name}")
        print(f"  已完成: {semester.total_completed_distance:.0f}m / {semester.target_distance:.0f}m ({completion:.1f}%)")
        print(f"  剩余: {semester.remaining_distance:.0f}m")
        print(f"  有效次数: {semester.valid_count}")

    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n\n用户中断")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n程序异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
