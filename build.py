#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键打包脚本 - 生成 Windows/Linux 可执行文件"""
import os
import sys
import shutil
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).parent.absolute()

def check_pyinstaller():
    """检查 PyInstaller 是否已安装"""
    try:
        import PyInstaller
        version = PyInstaller.__version__
        print(f"✅ PyInstaller {version} 已安装")
        return True
    except ImportError:
        print("❌ PyInstaller 未安装")
        print("请运行: pip install pyinstaller")
        return False

def clean_build():
    """清理旧的构建文件"""
    dirs_to_clean = ["build", "dist"]
    files_to_clean = ["*.spec~"]

    for d in dirs_to_clean:
        path = BASE_DIR / d
        if path.exists():
            print(f"🧹 清理 {d}/")
            shutil.rmtree(path)

    print("✅ 清理完成")

def build():
    """执行打包"""
    spec_file = BASE_DIR / "build.spec"
    if not spec_file.exists():
        print(f"❌ 打包配置文件不存在: {spec_file}")
        return False

    print(f"\n📦 开始打包...")
    print(f"   配置文件: {spec_file}")

    cmd = [sys.executable, "-m", "PyInstaller", str(spec_file), "--clean"]

    try:
        result = subprocess.run(cmd, cwd=BASE_DIR, check=True)
        return result.returncode == 0
    except subprocess.CalledProcessError as e:
        print(f"❌ 打包失败: {e}")
        return False

def show_result():
    """显示打包结果"""
    dist_dir = BASE_DIR / "dist" / "sport_campus_7.3.92"

    if not dist_dir.exists():
        print("❌ 打包输出目录不存在")
        return

    print("\n" + "=" * 60)
    print("✅ 打包完成!")
    print("=" * 60)

    print(f"\n📁 输出目录: {dist_dir}")
    print("\n可执行文件:")

    if os.name == "nt":
        cli_exe = dist_dir / "sport_cli.exe"
        webui_exe = dist_dir / "sport_webui.exe"
    else:
        cli_exe = dist_dir / "sport_cli"
        webui_exe = dist_dir / "sport_webui"

    if cli_exe.exists():
        size = cli_exe.stat().st_size / (1024 * 1024)
        print(f"  ✅ {cli_exe.name} ({size:.1f} MB)")

    if webui_exe.exists():
        size = webui_exe.stat().st_size / (1024 * 1024)
        print(f"  ✅ {webui_exe.name} ({size:.1f} MB)")

    print("\n使用方法:")
    print("  1. 复制 config.ini.example 为 config.ini（可选）")
    print("  2. 命令行版本:")
    if os.name == "nt":
        print("     sport_cli.exe --help")
        print("     sport_cli.exe -u 手机号 -p 密码")
        print("     sport_cli.exe --run --distance 2150 --time-sec 760")
    else:
        print("     ./sport_cli --help")
        print("     ./sport_cli -u 手机号 -p 密码")
        print("     ./sport_cli --run --distance 2150 --time-sec 760")

    print("  3. Web UI 版本:")
    if os.name == "nt":
        print("     sport_webui.exe")
    else:
        print("     ./sport_webui")
    # 端口从 config.ini 读，别写死：这里曾固定 8765，而实际默认 9999，
    # 打包提示直接把用户指到一个打不开的地址。
    try:
        from config_loader import get_webui_config
        _port = get_webui_config()["port"]
    except Exception:  # noqa
        _port = 9999
    print(f"     然后访问 http://127.0.0.1:{_port}")

    print("\n" + "=" * 60)

def main():
    print("=" * 60)
    print("运动世界校园 7.3.92 协议套件 - 打包工具")
    print("=" * 60)

    # 1. 检查 PyInstaller
    if not check_pyinstaller():
        return 1

    # 2. 清理旧构建
    clean_build()

    # 3. 执行打包
    if not build():
        return 1

    # 4. 显示结果
    show_result()

    return 0

if __name__ == "__main__":
    sys.exit(main())
