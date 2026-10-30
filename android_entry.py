# -*- coding: utf-8 -*-
"""Android 启动入口（只打进 APK，桌面端不会用到这个模块）。

为什么需要它：APK 里 Python 源码位于**只读**的 assets，config.ini / .session.json /
state/ 都写不进去。所以先把可写目录指到应用私有空间（ZJWH_DATA_DIR），再在回环
地址上起网页控制台，由 WebView 加载 —— 桌面端与手机端共用同一套代码与界面。
"""
import os


def start(port, data_dir):
    """由 MainActivity 在后台线程调用：绑定 127.0.0.1:port，阻塞直到进程结束。"""
    os.environ["ZJWH_DATA_DIR"] = str(data_dir)
    # Chaquopy 建议把 HOME 也指向可写目录，某些库会往 ~ 里写缓存
    os.environ.setdefault("HOME", str(data_dir))

    import webui

    webui.app.run(host="127.0.0.1", port=int(port), debug=False,
                  threaded=True, use_reloader=False)
