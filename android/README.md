# Android App（APK）

把网页控制台装进手机：**APK = Python 后端 + WebView 前端**。

App 启动后在 `127.0.0.1` 的随机端口跑 Flask（就是 `webui.py`），WebView 再加载它。
好处是手机端与电脑端共用同一套业务代码和界面，不会出现「两份实现各自漂移」。

## 为什么登录要在电脑上做

登录需要过极验滑块，而滑块求解依赖 Node + Playwright 驱动 Chromium —— Android
上没有等价环境（Termux 也装不了 Chromium）。所以手机版走这条路：

1. 电脑上正常登录一次（`python cli.py -u 手机号 -p 密码`）；
2. 把项目目录下的 `.session.json` 内容复制出来；
3. 手机 App 首页「账号」卡片最下面，粘贴进输入框 → 点「导入登录态」。

导入后跑步、查历史、查规则都正常。换账号时回电脑重登一次即可。
密码永远不会进 APK，手机上也只保存 token/uid/unid。

## 构建

前置工具（默认放在 `D:/.toolchain`，可用环境变量覆盖）：

| 组件 | 版本 | 环境变量 |
|---|---|---|
| JDK | 17 | `JAVA_HOME` |
| Gradle | 8.7 | `GRADLE_BIN` |
| Android SDK | platform 34 + build-tools 34.0.0 | `ANDROID_HOME` |
| Python | 3.13（必须与 `chaquopy.version` 主次版本一致） | `BUILD_PYTHON` |

```bash
python android/build_apk.py            # 默认 assembleRelease，产物在 dist/
python android/build_apk.py assembleDebug
```

脚本会自动：同步源码 → 写 `local.properties` → 调 Gradle → 把 APK 拷到 `dist/`。

装到手机：

```bash
adb install -r dist/app-release.apk
```

或者把 APK 拷进手机点开安装（需允许「安装未知来源应用」）。

## 目录

```
android/
├── build_apk.py           # 一键构建入口
├── sync_python.py         # 把业务源码同步到 app/src/main/python（增量，不整删）
├── settings.gradle        # 仓库与插件源
├── build.gradle           # AGP 8.5.2 / Chaquopy 17.2.0
├── gradle.properties
└── app/
    ├── build.gradle       # Python 3.13、abiFilters、flask 依赖
    └── src/main/
        ├── AndroidManifest.xml
        ├── java/com/zjwh/runner/MainActivity.java   # 起 Python、等端口、加载 WebView
        ├── res/values/strings.xml
        └── python/          # 构建时生成，不入库
```

## 已知限制

- **首版用 debug 签名**：能装能用，但不能上架应用商店，且换签名需先卸载重装。
- **无后台保活**：App 退到后台被系统回收后，网页会打不开，重新进 App 即可。
- **没有通知/定时**：定时任务请在电脑上跑。
- **极验滑块不可用**：见上面「为什么登录要在电脑上做」。
- Python 版本必须与 `chaquopy.version` 一致，否则 pip 安装与字节码编译会失败。
