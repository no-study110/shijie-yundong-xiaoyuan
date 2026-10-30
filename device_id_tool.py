# -*- coding: utf-8 -*-
"""DeviceId 生成、查看与校验工具。

DeviceId 是服务端用来认设备的指纹（32 位小写十六进制）。它有三个坑：

1. 手抄错一位，服务端按新设备处理，登录直接返回 10121；
2. 大小写不同也算两台设备 —— 服务端做的是字符串比对，且整个 headers 参与签名；
3. 一旦写进 config.ini 就会长期生效，错了很难想起来改回去。

所以这个工具默认**只生成不写入**，写入必须显式加 --write 或 --set。

用法::

    python device_id_tool.py                 生成 1 个（只打印）
    python device_id_tool.py -n 5            生成 5 个
    python device_id_tool.py --show          查看当前生效值与来源
    python device_id_tool.py --check         校验当前值格式
    python device_id_tool.py --write         生成并写入 config.ini
    python device_id_tool.py --set <值>      校验后写入指定值（--force 跳过校验）

退出码：0 成功，1 格式不合法或写入失败。
"""
import argparse
import json
import sys
import uuid
from pathlib import Path

import config_loader
from config_loader import normalize_device_id, set_device_id, validate_device_id

BASE_DIR = Path(__file__).parent.absolute()
WEBUI_CONFIG = BASE_DIR / ".webui_config.json"


def generate(count=1):
    """生成 count 个随机指纹。uuid4 的 128 位里 122 位随机，碰撞概率可忽略。"""
    return [uuid.uuid4().hex for _ in range(max(1, int(count)))]


def read_config_value():
    """读出 config.ini 里写的值（不做生成，空就是空）。"""
    return config_loader.get_device_id_from_file()


def read_webui_value(path=None):
    """读出网页控制台保存的指纹。

    它的优先级高于 config.ini（``_resolve_device_id`` 里显式入参 >
    webui 记住的 > config.ini），所以只看 config.ini 会得到错误的结论。
    """
    target = Path(path or WEBUI_CONFIG)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    return normalize_device_id(data.get("device_id") or "")


def resolve_effective():
    """返回 ``(生效值, 来源说明)``，优先级与运行时一致。"""
    webui = read_webui_value()
    if webui:
        return webui, "网页控制台配置（%s）" % WEBUI_CONFIG.name
    from_file = read_config_value()
    if from_file:
        return from_file, "config.ini"
    return "", "尚未生成（首次运行时会自动生成并写回 config.ini）"


def cmd_generate(args):
    values = generate(args.count)
    print("已生成 %d 个候选 DeviceId（32 位小写十六进制）：" % len(values))
    for value in values:
        print("  " + value)
    print()
    print("接下来：")
    print("  写入 config.ini : python device_id_tool.py --write")
    print("  写入指定值      : python device_id_tool.py --set <值>")
    print("  只看当前值      : python device_id_tool.py --show")
    return 0


def cmd_show(args):
    value, source = resolve_effective()
    ok, normalized, reason = validate_device_id(value)
    print("生效指纹: %s" % (value or "（空）"))
    print("来源    : %s" % source)
    print("格式检查: %s" % ("通过" if ok else ("未设置" if not value else "不合法 — " + reason)))

    from_file = read_config_value()
    if from_file and value and from_file != value:
        print()
        print("注意: config.ini 里是 %s，但网页控制台的值优先级更高，实际发送的是上面那个。" % from_file)
    if value and normalized and value != normalized:
        print("注意: 归一化后应为 %s（去掉了连字符与空白）。" % normalized)
    return 0 if ok or not value else 1


def cmd_check(args):
    value, source = resolve_effective()
    if not value:
        print("当前没有设置 DeviceId（来源：%s）。" % source)
        print("留空是合法的：首次运行会自动生成 32 位指纹并写回 config.ini。")
        return 0

    ok, normalized, reason = validate_device_id(value)
    if ok:
        print("通过: %s（来源：%s）" % (value, source))
        return 0
    print("不合法: %s" % reason)
    print("当前值: %r（长度 %d）" % (value, len(value)))
    if normalized and normalized != value:
        print("规范化后: %s" % normalized)
    print("修复: python device_id_tool.py --write    # 换成随机新值")
    return 1


def _common_tail(old, new):
    print()
    print("已写入 config.ini：[device] device_id = %s" % new)
    if old and old != new:
        print("旧值: %s（需要回退时把这一行改回去）" % old)
        print()
        print("提醒: 换指纹等于换设备。账号仍绑在旧设备上时，账号密码登录会返回")
        print("      10121，需要先在手机 App 内退出登录；或者改用 -t <Token> 跳过登录。")
    if read_webui_value():
        print()
        print("注意: 网页控制台里还存着旧指纹（%s），它的优先级高于 config.ini。"
              % WEBUI_CONFIG.name)
        print("      打开页面后请手动清空 DeviceId 输入框，否则登录会把旧值又传回来。")


def cmd_write(args):
    new_id = generate(1)[0]
    method = set_device_id if not args.force else _force_set
    old, new = method(new_id)
    _common_tail(old, new)
    return 0


def cmd_set(args):
    method = set_device_id if not args.force else _force_set
    try:
        old, new = method(args.value)
    except ValueError as exc:
        print("拒绝写入: %s" % exc)
        print()
        print("确认这个值确实来自原机抓包时，可加 --force 跳过格式校验。")
        return 1
    _common_tail(old, new)
    return 0


def _force_set(raw):
    """跳过格式校验直接写入。仅用于「真机值不符合本项目生成规则」的情形。"""
    value = normalize_device_id(raw)
    if not value:
        raise ValueError("值为空")
    old = read_config_value()
    config_loader._write_device_id(value)
    return old, value


def build_parser():
    parser = argparse.ArgumentParser(
        prog="device_id_tool.py",
        description="DeviceId 生成 / 查看 / 校验工具（默认只生成，不写入）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python device_id_tool.py --show\n"
               "  python device_id_tool.py --write\n"
               "  python device_id_tool.py --set 3de7c0707556404f8d50b78247debc90\n")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--show", action="store_true", help="查看当前生效值与来源")
    mode.add_argument("--check", action="store_true", help="校验当前值格式（不合法时退出码 1）")
    mode.add_argument("--write", action="store_true", help="生成随机值并写入 config.ini")
    mode.add_argument("--set", dest="value", metavar="VALUE",
                      help="校验后写入指定值")
    parser.add_argument("-n", "--count", type=int, default=1,
                        help="生成模式下的候选数量（默认 1）")
    parser.add_argument("--force", action="store_true",
                        help="与 --set 连用：跳过格式校验，用于真机抓包值不符合生成规则的情形")
    parser.add_argument("--config", metavar="PATH",
                        help="操作指定的 config.ini（默认项目根目录下那个）")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.config:
        # 让 config_loader 指向另一个文件，便于多环境或测试；同时清掉解析缓存
        config_loader._CONFIG_PATH = Path(args.config).absolute()
        config_loader._config = None

    if args.show:
        return cmd_show(args)
    if args.check:
        return cmd_check(args)
    if args.write:
        return cmd_write(args)
    if args.value:
        return cmd_set(args)
    return cmd_generate(args)


if __name__ == "__main__":
    sys.exit(main())
