# -*- coding: utf-8 -*-
"""一键构建 Android APK。

做四件事，把「手工点点点」变成可重复的一条命令：
  1. 同步 Python 源码到 android/app/src/main/python；
  2. 生成 local.properties（sdk.dir），不把它提交进仓库；
  3. 把 JDK / Gradle / Android SDK / 构建机 Python 的位置通过环境变量传给 Gradle；
  4. 构建完成后把 APK 拷到 dist/ 并打印路径。

工具链默认放在 D:/.toolchain（JDK 17、gradle-8.7、android-sdk），
可用环境变量覆盖：JAVA_HOME / ANDROID_HOME / GRADLE_BIN / BUILD_PYTHON。
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ANDROID_DIR = Path(__file__).resolve().parent
ROOT = ANDROID_DIR.parent
TOOLCHAIN = Path(os.environ.get("ZJWH_TOOLCHAIN", r"D:/.toolchain"))
DIST = ROOT / "dist"


def _first_existing(*candidates) -> str:
    for path in candidates:
        if path and Path(path).exists():
            return str(path)
    return ""


def resolve_tools():
    jdk = _first_existing(
        os.environ.get("JAVA_HOME"),
        *(TOOLCHAIN.glob("jdk-17*")),
    )
    sdk = _first_existing(
        os.environ.get("ANDROID_HOME"),
        TOOLCHAIN / "android-sdk",
    )
    gradle = _first_existing(
        os.environ.get("GRADLE_BIN"),
        TOOLCHAIN / "gradle-8.7" / "bin" / "gradle.bat",
    )
    build_py = _first_existing(
        os.environ.get("BUILD_PYTHON"),
        # 构建机上的 Python 必须与 chaquopy.version 主次版本一致（3.13）
        Path.home() / ".workbuddy/binaries/python/versions/3.13.12/python.exe",
        sys.executable,
    )
    return jdk, sdk, gradle, build_py


def write_local_properties(sdk: str) -> None:
    (ANDROID_DIR / "local.properties").write_text(
        "sdk.dir=%s\n" % sdk.replace("\\", "/"), encoding="utf-8")


def main() -> int:
    task = sys.argv[1] if len(sys.argv) > 1 else "assembleRelease"
    jdk, sdk, gradle, build_py = resolve_tools()
    missing = [name for name, value in
               (("JDK 17", jdk), ("Android SDK", sdk), ("Gradle 8.7", gradle), ("Python 3.13", build_py))
               if not value]
    if missing:
        print("缺少构建工具：" + "、".join(missing), file=sys.stderr)
        print("可用环境变量指定：JAVA_HOME / ANDROID_HOME / GRADLE_BIN / BUILD_PYTHON", file=sys.stderr)
        return 1

    print(f"JDK    : {jdk}")
    print(f"SDK    : {sdk}")
    print(f"Gradle : {gradle}")
    print(f"Python : {build_py}")

    if subprocess.call([sys.executable, str(ANDROID_DIR / "sync_python.py")]) != 0:
        print("源码同步失败", file=sys.stderr)
        return 1
    write_local_properties(sdk)

    env = dict(os.environ)
    env["JAVA_HOME"] = jdk
    env["ANDROID_HOME"] = sdk
    env["ANDROID_SDK_ROOT"] = sdk
    env["BUILD_PYTHON"] = build_py
    env["PATH"] = str(Path(jdk) / "bin") + os.pathsep + env.get("PATH", "")
    # 控制台默认 GBK，中文报错会变成乱码；让 JVM 统一按 UTF-8 输出
    env["JAVA_TOOL_OPTIONS"] = "-Dfile.encoding=UTF-8"

    # JVM 不读 HTTP_PROXY 环境变量，代理要显式转成 -D 参数
    proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    if proxy:
        host_port = proxy.split("//", 1)[-1].rstrip("/")
        if ":" in host_port:
            host, port = host_port.rsplit(":", 1)
            opts = f"-Dhttp.proxyHost={host} -Dhttp.proxyPort={port} " \
                   f"-Dhttps.proxyHost={host} -Dhttps.proxyPort={port}"
            env["GRADLE_OPTS"] = (env.get("GRADLE_OPTS", "") + " " + opts).strip()

    cmd = ["cmd", "/c", gradle, task, "--no-daemon", "--console=plain"]
    print("执行:", " ".join(cmd))
    if subprocess.call(cmd, cwd=str(ANDROID_DIR), env=env) != 0:
        print("构建失败", file=sys.stderr)
        return 1

    apk_dir = ANDROID_DIR / "app" / "build" / "outputs" / "apk"
    apks = sorted(apk_dir.rglob("*.apk"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not apks:
        print("构建成功但没找到 APK 产物", file=sys.stderr)
        return 1

    DIST.mkdir(exist_ok=True)
    out = DIST / apks[0].name
    shutil.copy2(apks[0], out)
    size_mb = out.stat().st_size / 1024 / 1024
    print("=" * 60)
    print(f"APK: {out}  ({size_mb:.1f} MB)")
    print("安装: adb install -r \"%s\"" % out)
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
