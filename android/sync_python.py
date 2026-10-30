# -*- coding: utf-8 -*-
"""把项目源码同步到 Android 工程的 python 源目录。

只拷运行时真正需要的部分（业务包 + 网页控制台），测试、文档、构建脚本一律不带 ——
APK 里的 Python 代码是打包进 assets 的，多带一份测试就多一份体积。

采用「增量覆盖 + 精确删除」：先覆盖写入，再按上次记录的清单删掉本次已消失的文件。
不整目录删除有两个原因：一是批量删除会被安全策略拦下，二是整删重建会让
Gradle 每次都判定资源全变、无法增量编译。
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 协议/7.3.92
ANDROID = Path(__file__).resolve().parent              # 协议/7.3.92/android
TARGET = ANDROID / "app" / "src" / "main" / "python"
MANIFEST = TARGET / ".synced.json"

# 顶层单文件模块：只带运行时用得到的
TOP_FILES = [
    "config_loader.py",
    "sport_client.py",
    "webui.py",
    "webui.html",
    "android_entry.py",
    "device_id_tool.py",
]
# 业务包：整个目录拷过去（忽略缓存）
PACKAGES = ["core", "login", "running", "policy", "history", "account", "ads", "telemetry"]
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")


def _previous_files() -> set:
    if not MANIFEST.exists():
        return set()
    try:
        return set(json.loads(MANIFEST.read_text(encoding="utf-8")))
    except (ValueError, OSError):
        return set()


def main() -> int:
    TARGET.mkdir(parents=True, exist_ok=True)
    previous = _previous_files()

    missing = []
    for name in TOP_FILES:
        src = ROOT / name
        if not src.exists():
            missing.append(name)
            continue
        shutil.copy2(src, TARGET / name)

    for pkg in PACKAGES:
        src = ROOT / pkg
        if not src.is_dir():
            missing.append(pkg + "/")
            continue
        shutil.copytree(src, TARGET / pkg, dirs_exist_ok=True, ignore=IGNORE)

    if missing:
        print("缺少以下源文件：" + ", ".join(missing), file=sys.stderr)
        return 1

    current = {p.relative_to(TARGET).as_posix()
               for p in TARGET.rglob("*") if p.is_file() and p != MANIFEST}
    stale = previous - current
    for rel in sorted(stale):
        try:
            (TARGET / rel).unlink()
        except OSError as exc:
            print(f"清理旧文件失败 {rel}: {exc}", file=sys.stderr)
    MANIFEST.write_text(json.dumps(sorted(current), ensure_ascii=False, indent=1),
                        encoding="utf-8")

    note = f"，清理旧文件 {len(stale)} 个" if stale else ""
    print(f"已同步 {len(current)} 个文件 -> {TARGET}{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
