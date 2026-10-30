# -*- coding: utf-8 -*-
"""运动世界校园 7.3.92 协议套件 — 本地网页控制台"""
import json
import os
import sys
import threading
import time
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from flask import Flask, Response, request

from sport_client import SportClient792
from core.session import load_session, save_session
from config_loader import (get_session_file_path, get_webui_config, get_realism_defaults,
                           get_device_id_from_file, normalize_device_id, validate_device_id)
from login.captcha import detect_node_executable, detect_node_paths
from core.pace import record_pace_text
from history.models import parse_history_records, record_verdict, detail_for
from history.client import fetch_detail_map

# Windows 控制台默认按 GBK 输出，一旦把 stdout 重定向到文件（webui.log），
# 中文就会变成乱码且无法跨平台查看。这里统一成 UTF-8；reconfigure 不可用
# 时（极老的 Python 或非标准流）静默跳过，不影响主流程。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

# 与 login/captcha.py 共用的 Node 运行时环境注入（优先外部变量,否则自动探测）
os.environ.setdefault("NODE_EXECUTABLE", detect_node_executable())
os.environ.setdefault("NODE_PATH", detect_node_paths())

SESSION_FILE = get_session_file_path()
CONFIG_FILE = os.path.join(BASE_DIR, ".webui_config.json")

app = Flask(__name__)

# ---------------- 全局配置（代理 / 设备指纹） ----------------
# device_id 必须持久化：登录时手填的指纹如果丢弃，后续跑步就会退回
# config.ini 里的默认指纹，服务端会判为「同账号换设备」并直接返回 10121。
_config = {"proxy": None, "device_id": ""}

def _load_config():
    global _config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                _config.update(json.load(f))
        except Exception as exc:  # noqa
            # 配置坏了不该阻断启动，但必须留痕：否则用户改了代理却不生效时无从排查
            print(f"[webui] 配置读取失败，使用默认值: {exc}", file=sys.stderr, flush=True)

def _save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(_config, f, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa
        # 原先静默 pass，用户会以为「已保存」而实际没写入（只读目录 / 权限不足）
        print(f"[webui] 配置写入失败（设备指纹与代理不会被记住）: {exc}",
              file=sys.stderr, flush=True)

_load_config()

# ---------------- 全局任务状态（同一时间只跑一个任务） ----------------
_job_lock = threading.Lock()
_job = {"running": False, "done": True, "log": [], "result": None, "name": ""}


def _log(msg):
    stamp = time.strftime("%H:%M:%S")
    line = f"[{stamp}] {msg}"
    _job["log"].append(line)
    print(line, flush=True)


def _resolve_device_id(device_id=None):
    """解析本次请求该用哪个设备指纹：显式指定 > 登录时记住的 > 空（交给默认值）。

    中间这层不能省 —— 登录用 A、跑步用 B，服务端立刻判为换设备并返回 10121。
    用户填了 DeviceId 反而被拦，是这版最隐蔽的自伤。
    """
    return str(device_id or _config.get("device_id") or "").replace("-", "").strip()


def _device_id_state():
    """返回当前**实际生效**的指纹与来源，用于页面上「现在用的是哪一个」的提示。

    优先级必须和 ``_resolve_device_id`` 完全一致（网页记住的 > config.ini > 未设置），
    否则页面显示 A、实际发出去的是 B，比不显示更容易误导。
    """
    stored = normalize_device_id(_config.get("device_id") or "")
    if stored:
        value, source = stored, "网页控制台"
    else:
        value = get_device_id_from_file()
        source = "config.ini" if value else "未设置"
    if not value:
        return {"current": "", "source": source, "valid": True, "reason": ""}
    ok, _, reason = validate_device_id(value)
    return {"current": value, "source": source, "valid": ok, "reason": reason}


def _new_client(device_id=None):
    session = load_session(SESSION_FILE)
    device = _resolve_device_id(device_id)
    device_config = {"DeviceId": device} if device else {}
    return SportClient792(
        uid=session.get("uid"), token=session.get("token"), unid=session.get("unid"),
        device_config=device_config or None, proxy=_config.get("proxy"))


# ---------------- 各操作实现 ----------------

def do_login(username, password, device_id=None):
    client = _new_client(device_id=device_id)
    res = client.login(username, password, progress_callback=_log)
    if not res.success:
        text = f"登录失败: code={res.error_code} 原因={res.message}"
        if res.error_code == 10121:
            text += "（账号在其他设备登录中：请在手机 App 内退出登录后重试，或填写设备指纹 DeviceId）"
        _log(text)
        return {"ok": False, "message": text}
    save_session(SESSION_FILE, client.token, client.uid, client.unid)
    # 记住本次登录「实际使用」的指纹（而不是入参），这样不填也能沿用上一次的。
    # 后续 do_run 会读回来，保证登录与提交用的是同一个设备。
    effective = str(device_id or _config.get("device_id") or "").replace("-", "").strip()
    _config["device_id"] = effective
    _save_config()
    note = f"，设备指纹 {effective[:8]}…" if effective else ""
    _log(f"登录成功: uid={client.uid} unid={client.unid}（凭据已缓存{note}）")
    return {"ok": True, "uid": client.uid, "unid": client.unid, "device_id": effective}


def do_profile():
    client = _new_client()
    if not client.token:
        return {"ok": False, "message": "未登录"}
    prof = client.get_user_profile()
    if not prof:
        return {"ok": False, "message": "查询为空"}
    _log(f"个人档案: 姓名={prof.name} 学校={prof.campus_name}")
    return {"ok": True, "data": {
        "姓名": prof.name, "性别": prof.sex, "学校": prof.campus_name,
        "学院": prof.depart, "班级": prof.grade_class, "学号": prof.student_id}}


def do_rules(mode=1):
    client = _new_client()
    if not client.token:
        return {"ok": False, "message": "未登录"}
    rule = client.get_run_policy(run_mode=mode)
    fences = client.get_geo_fences()
    if not rule:
        reason = client.policy.last_policy_error or "规则查询为空"
        _log(f"跑步规则: 失败 {reason}")
        return {"ok": False, "message": reason}
    data = {"模式": "计分跑" if mode == 1 else "自由跑",
            "单日上限": f"{rule.day_goal}m", "单次最低": f"{rule.min_distance}m",
            "配速区间": rule.speed_range_text(),
            "步频区间": f"{rule.step_bottom}~{rule.step_top} 步/分",
            "围栏数": len(fences)}
    _log(f"跑步规则: {data}")
    return {"ok": True, "data": data}


def do_checkpoints():
    client = _new_client()
    if not client.token:
        return {"ok": False, "message": "未登录"}
    fences = client.get_geo_fences()
    usable = [f for f in fences if f.polygon_points]
    if not usable:
        return {"ok": False, "message": "未取得有效围栏锚点"}
    points = client.policy.get_checkpoints_for_run(usable[0])
    if not points:
        return {"ok": False, "message": client.policy.last_checkpoint_error or "未获取点位"}
    names = [p.name or f"点位{i+1}" for i, p in enumerate(points)]
    _log(f"官方点位: {len(points)} 个")
    return {"ok": True, "data": {"点位数": len(points), "点位": names}}


def do_history():
    client = _new_client()
    if not client.token:
        return {"ok": False, "message": "未登录"}
    h_res = client.get_history_records(page_num=1, page_size=10)
    if not (isinstance(h_res, dict) and h_res.get("error") == 10000):
        msg = h_res.get("message") if isinstance(h_res, dict) else "网络异常"
        return {"ok": False, "message": msg}
    recs = parse_history_records(h_res.get("data"))
    # 列表接口不给 reasonList：实测 complete 全 True，其中 2 条其实是「未达标: 配速…」。
    # 所以逐条回查详情，没有详情才退回列表字段（详见 history.models.record_verdict）。
    if recs:
        _log(f"历史记录: 逐条回查判定详情（{len(recs)} 条，约 {len(recs) * 0.4:.0f} 秒）…")
    details = fetch_detail_map(client, recs)
    rows = []
    for r in recs:
        # 配速取服务端保存的 speed 字段 —— 与手机 App 记录页逐字一致，
        # 不能在这里用里程/耗时另算一份。字段缺失时 core.pace 会自动回退。
        rrid = r.get("rrid")
        verdict, reason = record_verdict(r, detail_for(details, rrid))
        rows.append({"rrid": rrid,
                     "里程": f"{r.get('totalDis')}m",
                     "耗时": f"{r.get('totalTime')}s",
                     "配速": record_pace_text(r),
                     "状态": verdict,
                     "原因": reason})
    failed = sum(1 for row in rows if row["状态"] != "达标")
    _log(f"历史记录: {len(rows)} 条，其中非达标 {failed} 条")
    return {"ok": True, "data": rows}


def do_semester():
    client = _new_client()
    if not client.token:
        return {"ok": False, "message": "未登录"}
    info = client.get_semester_progress(run_mode=1)
    if not info:
        return {"ok": False, "message": "学期进度查询为空"}
    pct = (info.total_completed_distance / info.target_distance * 100) if info.target_distance else 0
    data = {"学期": info.semester_name or "本学期",
            "已完成里程": f"{info.total_completed_distance:.0f}m",
            "目标里程": f"{info.target_distance:.0f}m",
            "剩余里程": f"{info.remaining_distance:.0f}m",
            "有效次数": info.valid_count,
            "完成度": f"{pct:.1f}%"}
    _log(f"学期进度: 已完成{info.total_completed_distance:.0f}m/目标{info.target_distance:.0f}m ({pct:.1f}%) 有效{info.valid_count}次")
    return {"ok": True, "data": data}


def do_run(mode, distance, time_sec, realism=True, simulate_physical_time=True):
    client = _new_client()
    if not client.token or not client.uid:
        return {"ok": False, "message": "未登录，请先登录"}
    res = client.execute_run(target_distance_m=distance, target_time_sec=time_sec,
                             sport_type=mode, realism=realism,
                             simulate_physical_time=simulate_physical_time,
                             progress_callback=_log)
    if not res.success:
        return {"ok": False, "message": res.message}
    return {"ok": True, "data": {"rrid": res.rrid, "判定": res.status_text,
                                 "里程": f"{res.total_distance}m", "耗时": f"{res.total_time}s",
                                 "配速": res.pace_text or "-"}}


# ---------------- 任务执行器 ----------------

def _run_job(name, fn):
    if _job["running"]:
        return False
    with _job_lock:
        if _job["running"]:
            return False
        _job.update({"running": True, "done": False, "log": [], "result": None, "name": name})
    def worker():
        try:
            _job["result"] = fn()
        except Exception as exc:  # noqa
            import traceback
            # 把完整堆栈写入日志，否则线上排查只能看到异常摘要
            _log(f"执行异常: {exc}")
            for line in traceback.format_exc().rstrip().splitlines():
                _log("    " + line)
            _job["result"] = {"ok": False, "message": str(exc)}
        finally:
            _job["running"] = False
            _job["done"] = True
    threading.Thread(target=worker, daemon=True).start()
    return True


# ---------------- HTTP 接口 ----------------

@app.get("/")
def index():
    html_path = os.path.join(BASE_DIR, "webui.html")
    with open(html_path, "r", encoding="utf-8") as fp:
        return Response(fp.read(), mimetype="text/html")


@app.get("/api/status")
def api_status():
    s = load_session(SESSION_FILE)
    token = s.get("token")
    return {"logged_in": bool(token),
            "uid": s.get("uid"), "unid": s.get("unid")}


@app.get("/api/job")
def api_job():
    return {"running": _job["running"], "done": _job["done"], "name": _job["name"],
            "log": _job["log"][-200:], "result": _job["result"]}


@app.post("/api/login")
def api_login():
    body = request.get_json(force=True, silent=True) or {}
    username = str(body.get("username", "")).strip()
    # 口令首尾的空格可能是密码本身的一部分，不能 strip，否则这类账号永远登不上
    password = str(body.get("password", ""))
    device_id = str(body.get("device_id", "")).strip() or None
    if not username or not password:
        return {"ok": False, "message": "请填写账号与密码"}
    started = _run_job("登录", lambda: do_login(username, password, device_id))
    return {"started": started, "message": "" if started else "已有任务在执行中"}


@app.post("/api/run")
def api_run():
    body = request.get_json(force=True, silent=True) or {}
    try:
        mode = int(body.get("mode", 1))
        distance = int(body.get("distance", 2150))
        time_sec = int(body.get("time_sec", 760))

        # 拟真增强和物理时间模拟默认值从 config.ini 读取
        realism_cfg = get_realism_defaults()
        realism = bool(body.get("realism", realism_cfg["enabled"]))
        simulate_physical_time = bool(body.get("simulate_physical_time", realism_cfg["simulate_physical_time"]))
    except (TypeError, ValueError):
        return {"ok": False, "message": "参数格式错误"}
    if mode not in (1, 6) or distance <= 0 or time_sec <= 0:
        return {"ok": False, "message": "参数范围错误"}
    # 上限：单次跑步不可能超过 50km / 6 小时。do_run 会按 time_sec 真等物理时间，
    # 误填一个 72000 就是干等 20 小时，前端 min/max 拦不住直接调接口的情况。
    if distance > 50000 or time_sec > 21600:
        return {"ok": False, "message": "距离需 ≤50000 米、耗时需 ≤6 小时"}
    started = _run_job("跑步提交", lambda: do_run(mode, distance, time_sec, realism, simulate_physical_time))
    return {"started": started, "message": "" if started else "已有任务在执行中"}


@app.post("/api/action")
def api_action():
    body = request.get_json(force=True, silent=True) or {}
    action = body.get("action")
    mapping = {"profile": ("个人档案", do_profile),
               "rules": ("跑步规则", lambda: do_rules(int(body.get("mode", 1)))),
               "checkpoints": ("官方点位", do_checkpoints),
               "history": ("历史记录", do_history),
               "semester": ("学期进度", do_semester)}
    if action not in mapping:
        return {"ok": False, "message": "未知操作"}
    name, fn = mapping[action]
    started = _run_job(name, fn)
    return {"started": started, "message": "" if started else "已有任务在执行中"}


@app.post("/api/logout")
def api_logout():
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)
        return {"ok": True, "message": "已清除本地登录凭据"}
    return {"ok": True, "message": "当前没有缓存凭据"}


@app.get("/api/config")
def api_get_config():
    return {"ok": True, "proxy": _config.get("proxy") or ""}


@app.get("/api/device-id")
def api_get_device_id():
    return dict(ok=True, **_device_id_state())


@app.post("/api/device-id/new")
def api_new_device_id():
    """生成一个候选指纹，**只返回、不写盘**。

    写入时机是「登录成功」——do_login 会把本次实际使用的值记进 webui 配置。
    在这里落盘会造出一个真实的坑：用户点了「随机生成」却没登录（或又改成
    别的值），指纹已经被换掉，而账号还绑在旧设备上，下次登录直接 10121。
    生成规则与 device_id_tool.generate 一致：uuid4 的 32 位小写十六进制。
    """
    value = uuid.uuid4().hex
    ok, _, reason = validate_device_id(value)
    if not ok:  # 除非标准库被替换，否则不可达；保持出口闭合
        return {"ok": False, "message": "生成失败：%s" % reason}
    return {"ok": True, "device_id": value}


@app.post("/api/config")
def api_set_config():
    body = request.get_json(force=True, silent=True) or {}
    proxy = str(body.get("proxy", "")).strip() or None
    _config["proxy"] = proxy
    _save_config()
    return {"ok": True, "message": "代理配置已保存（重启任务后生效）", "proxy": proxy or ""}


if __name__ == "__main__":
    webui_cfg = get_webui_config()
    host = webui_cfg["host"]
    port = webui_cfg["port"]
    print(f"运动世界校园 网页控制台: http://{host}:{port}", flush=True)

    def _open_browser():
        url = f"http://{host}:{port}"
        try:
            time.sleep(1.2)  # 等服务监听就绪后再拉起默认浏览器
            if sys.platform == "win32":
                os.startfile(url)  # Windows 直接调用系统默认浏览器，最可靠
            else:
                import webbrowser
                webbrowser.open(url)
        except Exception as exc:  # noqa
            print(f"自动打开浏览器失败，请手动访问 {url} （{exc}）", flush=True)

    threading.Thread(target=_open_browser, daemon=True).start()
    app.run(host=host, port=port, debug=False)
