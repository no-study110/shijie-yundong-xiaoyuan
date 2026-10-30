package com.zjwh.runner;

import android.app.Activity;
import android.os.Bundle;
import android.util.Log;
import android.view.ViewGroup;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import java.io.IOException;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.URL;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;

/**
 * 整个 App 就是「Python 后端 + WebView 前端」：
 * 先在回环地址上把网页控制台跑起来，再让 WebView 加载它。
 * 这样桌面端与手机端共用同一套界面和业务代码，不存在两份实现漂移的问题。
 */
public class MainActivity extends Activity {

    private static final String TAG = "ZjwhRunner";
    private static final int MATCH_PARENT = ViewGroup.LayoutParams.MATCH_PARENT;

    private WebView web;
    private int port;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        port = pickFreePort();

        web = new WebView(this);
        setContentView(web, new ViewGroup.LayoutParams(MATCH_PARENT, MATCH_PARENT));
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setCacheMode(WebSettings.LOAD_NO_CACHE);
        web.setWebViewClient(new WebViewClient());

        showHtml("<p style='font-family:sans-serif;padding:24px'>正在启动 Python 服务…</p>");
        startBackend();
        waitBackendThenLoad();
    }

    /** 让系统分配一个空闲端口，避免和别的应用撞上 9999。 */
    private static int pickFreePort() {
        try (ServerSocket socket = new ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))) {
            return socket.getLocalPort();
        } catch (IOException e) {
            Log.w(TAG, "取空闲端口失败，退回 9999", e);
            return 9999;
        }
    }

    private void startBackend() {
        new Thread(() -> {
            try {
                PyObject entry = Python.getInstance().getModule("android_entry");
                entry.callAttr("start", port, getFilesDir().getAbsolutePath());
            } catch (Throwable t) {
                // Python 里的异常在 Java 侧是 PyException，要整段打出来否则只剩一句 "Python error"
                Log.e(TAG, "Python 后端启动失败", t);
                runOnUiThread(() -> showHtml(
                        "<pre style='padding:16px;white-space:pre-wrap'>后端启动失败：\n"
                                + Log.getStackTraceString(t) + "</pre>"));
            }
        }, "python-backend").start();
    }

    /** 轮询 /api/status，等 Flask 真正监听上再加载页面（直接 loadUrl 会白屏）。 */
    private void waitBackendThenLoad() {
        new Thread(() -> {
            for (int i = 0; i < 120; i++) {
                if (pingStatus()) {
                    final String url = "http://127.0.0.1:" + port + "/";
                    runOnUiThread(() -> web.loadUrl(url));
                    return;
                }
                try {
                    Thread.sleep(500);
                } catch (InterruptedException e) {
                    return;
                }
            }
            runOnUiThread(() -> showHtml(
                    "<p style='font-family:sans-serif;padding:24px'>后端启动超时。"
                            + "请用 adb logcat -s python.stderr 查看原因。</p>"));
        }, "wait-backend").start();
    }

    private boolean pingStatus() {
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL("http://127.0.0.1:" + port + "/api/status").openConnection();
            conn.setConnectTimeout(800);
            conn.setReadTimeout(800);
            conn.setRequestMethod("GET");
            return conn.getResponseCode() == 200;
        } catch (IOException e) {
            return false;
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    private void showHtml(String html) {
        web.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }
}
