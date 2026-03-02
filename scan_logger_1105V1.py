# -*- coding: utf-8 -*-
"""
條碼掃描「Flask 單一來源 + SSE + 名冊持久化 + 內建 HTTPS（不需 ngrok）」v3.4
- 首頁 "/" 指向 ./static/ui/index.html（前端放這；submit.html 放 ./static/ui/submit.html）
- 即時同步：/events (SSE)；新增紀錄 / 載入／清除名冊都會推播到前端
- 名冊持久化：./data/roster_saved.csv + ./data/roster_meta.json；啟動自動還原
- 後端（Flask + Waitress/Werkzeug）：
    * HTTP：預設（Waitress 有裝就用，否則退回 Werkzeug）
    * HTTPS：勾選「啟用 HTTPS」＋指定 cert/key（使用 Werkzeug make_server 搭配 ssl_context）
- API：/health, /stats, /records, /search, /records/joined, /download.csv, /upload_roster, /roster/clear, /scan(GET/POST), /events

需求：
    pip install flask waitress
    （名冊 xlsx/csv）pip install pandas openpyxl
    （HTTPS 憑證建議）mkcert 產生 cert.pem / key.pem 並在客戶端安裝 Root CA
"""
import os, io, csv, sys, time, json, socket, threading, datetime, webbrowser, subprocess, queue, shutil, ssl
from typing import Dict, Any, List, Optional, Set
from pathlib import Path

# ---- Tkinter UI ----
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

# ---- 防睡眠（Windows） ----
try:
    import ctypes
    ES_CONTINUOUS       = 0x80000000
    ES_SYSTEM_REQUIRED  = 0x00000001
    ES_DISPLAY_REQUIRED = 0x00000002
    def set_prevent_sleep(enable=True):
        try:
            if enable:
                ctypes.windll.kernel32.SetThreadExecutionState(
                    ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
                )
            else:
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception:
            pass
except Exception:
    def set_prevent_sleep(enable=True): pass

try:
    import winsound
    def beep(ok=True):
        winsound.MessageBeep(winsound.MB_OK if ok else winsound.MB_ICONHAND)
except Exception:
    def beep(ok=True): pass

# ---- HTTP（Flask + Waitress / Werkzeug） ----
from flask import Flask, request, jsonify, Response, send_from_directory, stream_with_context
try:
    from waitress import create_server as waitress_create_server
except Exception:
    waitress_create_server = None
from werkzeug.serving import make_server as wz_make_server

# ---- 可選：pandas 解析名冊 ----
try:
    import pandas as pd
except Exception:
    pd = None

APP_DIR    = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"              # 前端根目錄（Flask 的 /static）
DEFAULT_HOME = "ui/kiosk.html"               # 首頁相對於 ./static 的路徑
RECORDS_CSV = APP_DIR / "records.csv"
DAILY_DIR   = APP_DIR / "daily"
DAILY_DIR.mkdir(exist_ok=True)

# ---- 名冊持久化路徑 ----
DATA_DIR    = APP_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
ROSTER_CSV  = DATA_DIR / "roster_saved.csv"
ROSTER_META = DATA_DIR / "roster_meta.json"
SETTINGS_JSON = DATA_DIR / "server_settings.json"

# ---- 資料狀態 ----
lock = threading.RLock()
records: List[Dict[str, str]] = []
roster_df = None
roster_id_key = None
roster_cols: List[str] = []

# ---- SSE 訂閱者 ----
sse_lock = threading.RLock()
sse_subscribers: Set[queue.Queue] = set()

def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]; s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def ensure_records_header():
    if not RECORDS_CSV.exists() or RECORDS_CSV.stat().st_size == 0:
        with open(RECORDS_CSV, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(["time","id"])

def load_records_from_csv():
    ensure_records_header()
    loaded = 0
    with open(RECORDS_CSV, "r", encoding="utf-8-sig", newline="") as f:
        r = csv.reader(f)
        _ = next(r, None)
        for row in r:
            if not row: continue
            if len(row) >= 2:
                records.append({"time": row[0], "id": row[1]})
                loaded += 1
    return loaded

def append_daily_log(ts: str, sid: str):
    date_part = ts[:10]
    daily_path = DAILY_DIR / f"{date_part}.csv"
    init = (not daily_path.exists()) or (daily_path.stat().st_size == 0)
    with open(daily_path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if init: w.writerow(["time","id"])
        w.writerow([ts, sid])

def try_join_row(rec: Dict[str,str]) -> Dict[str, Any]:
    if roster_df is None or roster_id_key is None:
        return dict(rec)
    try:
        sid = rec.get("id","").strip()
        if not sid: return dict(rec)
        m = roster_df[roster_df[roster_id_key].astype(str) == sid]
        out = dict(rec)
        if (pd is not None) and (not m.empty):
            row = m.iloc[0].to_dict()
            out.update({k: str(v) for k,v in row.items()})
        return out
    except Exception:
        return dict(rec)

# ---- SSE：廣播工具 ----
def sse_broadcast(payload: Dict[str, Any]):
    with sse_lock:
        subs = list(sse_subscribers)
    for q in subs:
        try:
            q.put_nowait(payload)
        except Exception:
            pass

def add_record(sid: str):
    if not sid: return False
    ts = now_str()
    with lock:
        records.append({"time": ts, "id": sid})
        with open(RECORDS_CSV, "a", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow([ts, sid])
    append_daily_log(ts, sid)
    sse_broadcast({"type": "scan_added", "data": try_join_row({"time": ts, "id": sid})})
    return True

def detect_id_key(cols: List[str]) -> Optional[str]:
    low = {c.lower(): c for c in cols}
    for key in ["學號", "student_id", "stu_id", "id", "學籍號", "studentid", "sid"]:
        if key in low: return low[key]
    for c in cols:
        lc = c.lower()
        if ("學" in c and "號" in c) or ("student" in lc and "id" in lc):
            return c
    return None

def load_roster_from_file(path: Path):
    global roster_df, roster_id_key, roster_cols
    if not path.exists():
        raise FileNotFoundError(f"檔案不存在：{path}")
    if path.suffix.lower() in [".csv", ".txt"]:
        if pd is not None:
            df = pd.read_csv(path, dtype=str, encoding="utf-8")
        else:
            rows = []
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                r = csv.DictReader(f)
                for row in r:
                    rows.append({k: str(v) if v is not None else "" for k,v in row.items()})
            import pandas as pd2
            df = pd2.DataFrame(rows)
    else:
        if pd is None:
            raise RuntimeError("未安裝 pandas/openpyxl，無法讀取 xlsx。請先安裝：pip install pandas openpyxl")
        df = pd.read_excel(path, dtype=str)

    df.columns = [str(c).strip() for c in df.columns]
    df = df.fillna("")
    key = detect_id_key(list(df.columns))
    if not key:
        for c in df.columns:
            s = df[c].astype(str).str.strip()
            if s.apply(lambda x: x.isdigit() and 3 <= len(x) <= 12).mean() > 0.6:
                key = c; break
    if not key:
        raise RuntimeError("無法自動辨識名冊中的學號欄位，請用『學號/ student_id / id』。")

    with lock:
        roster_df = df
        roster_id_key = key
        roster_cols = list(df.columns)

def persist_roster_to_disk() -> bool:
    if roster_df is None:
        return False
    try:
        roster_df.to_csv(ROSTER_CSV, index=False, encoding="utf-8-sig")
        meta = {"id_key": roster_id_key, "saved_at": now_str()}
        with open(ROSTER_META, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        return True
    except Exception as e:
        print("persist_roster_to_disk error:", e)
        return False

def restore_roster_from_disk() -> bool:
    global roster_df, roster_id_key, roster_cols
    if pd is None:
        return False
    if not ROSTER_CSV.exists():
        return False
    try:
        df = pd.read_csv(ROSTER_CSV, dtype=str, encoding="utf-8")
        saved_key = None
        if ROSTER_META.exists():
            try:
                with open(ROSTER_META, "r", encoding="utf-8") as f:
                    saved_key = (json.load(f) or {}).get("id_key")
            except Exception:
                pass
        if not saved_key or saved_key not in df.columns:
            saved_key = detect_id_key([str(c) for c in df.columns])

        with lock:
            roster_df   = df.fillna("")
            roster_id_key = saved_key
            roster_cols = list(roster_df.columns)
        return True
    except Exception as e:
        print("restore_roster_from_disk error:", e)
        return False

# ---- Flask App ----
def build_app() -> Flask:
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")

    @app.after_request
    def add_cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
        return resp

    @app.route("/")
    def home():
        target = STATIC_DIR / DEFAULT_HOME
        if target.exists():
            return send_from_directory(str(STATIC_DIR / "ui"), "kiosk.html")
        else:
            return jsonify({
                "ok": False,
                "msg": "找不到前端首頁。請將你的前端放在 ./static/ui/index.html",
                "expect_path": str(target)
            }), 404

    @app.route("/health")
    def health():
        return jsonify({"ok": True, "ts": now_str()})

    @app.route("/stats")
    def stats():
        with lock:
            return jsonify({
                "total_records": len(records),
                "roster_loaded": roster_df is not None,
                "roster_id_key": roster_id_key,
                "roster_cols": roster_cols,
            })

    @app.route("/records")
    def api_records():
        try:
            limit = int(request.args.get("limit", "500"))
        except Exception:
            limit = 500
        with lock:
            items = list(records)[-limit:]
        items = list(reversed(items))
        return jsonify({"items": items, "count": len(items)})

    @app.route("/search")
    def api_search():
        q = (request.args.get("q") or "").strip()
        try:
            limit = int(request.args.get("limit", "200"))
        except Exception:
            limit = 200
        out = []
        if q:
            with lock:
                for r in reversed(records):
                    if q in r["id"] or q in r["time"]:
                        out.append(dict(r))
                        if len(out) >= limit: break
        return jsonify({"items": out, "count": len(out)})

    @app.route("/records/joined")
    def api_joined():
        try:
            limit = int(request.args.get("limit", "500"))
        except Exception:
            limit = 500
        with lock:
            src = list(reversed(records[-limit:]))
        items = [try_join_row(r) for r in src]
        return jsonify({"items": items, "count": len(items)})

    @app.route("/download.csv")
    def api_download_csv():
        try:
            limit = int(request.args.get("limit", "20000"))
        except Exception:
            limit = 20000
        with lock:
            src = list(reversed(records[-limit:]))
        joined = [try_join_row(r) for r in src]
        cols, seen = [], set()
        for r in joined:
            for k in r.keys():
                if k not in seen:
                    seen.add(k); cols.append(k)
        front = ["time","id"]
        cols = front + [c for c in cols if c not in front]
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols)
        w.writeheader()
        for r in joined:
            w.writerow(r)
        data = buf.getvalue().encode("utf-8-sig")
        return Response(
            data,
            headers={"Content-Disposition": "attachment; filename=records_joined.csv"},
            content_type="text/csv; charset=utf-8"
        )

    # 同時支援 GET /scan?id=... 與 POST
    @app.route("/scan", methods=["GET","POST","OPTIONS"])
    def api_scan():
        sid = ""
        if request.method == "POST":
            if request.is_json:
                j = request.get_json(silent=True) or {}
                sid = str(j.get("id", "")).strip()
            else:
                sid = (request.form.get("id") or request.args.get("id") or "").strip()
        else:
            sid = (request.args.get("id") or "").strip()

        if not sid:
            return jsonify({"ok": False, "err": "empty_id"}), 400

        ok = add_record(sid)
        return jsonify({"ok": ok, "id": sid, "time": now_str()})

    @app.route("/upload_roster", methods=["POST"])
    def api_upload_roster():
        if "file" not in request.files:
            return jsonify({"ok": False, "err": "no_file"}), 400
        f = request.files["file"]
        if not f or not f.filename:
            return jsonify({"ok": False, "err": "empty"}), 400
        tmp = APP_DIR / ("_upload_" + os.path.basename(f.filename))
        f.save(tmp)
        try:
            load_roster_from_file(tmp)
            saved = persist_roster_to_disk()
            with lock:
                cnt = 0 if (roster_df is None) else len(roster_df)
                kid = roster_id_key
            sse_broadcast({"type": "roster_loaded", "data": {"count": cnt, "id_key": kid}})
            return jsonify({"ok": True, "count": cnt, "id_key": kid, "persisted": bool(saved)})
        except Exception as e:
            return jsonify({"ok": False, "err": str(e)}), 400
        finally:
            try: tmp.unlink()
            except Exception: pass

    @app.route("/roster/clear", methods=["POST"])
    def api_roster_clear():
        global roster_df, roster_id_key, roster_cols
        with lock:
            roster_df = None
            roster_id_key = None
            roster_cols = []
        try:
            if ROSTER_CSV.exists(): ROSTER_CSV.unlink()
            if ROSTER_META.exists(): ROSTER_META.unlink()
        except Exception:
            pass
        sse_broadcast({"type": "roster_cleared", "data": {"ts": now_str()}})
        return jsonify({"ok": True})

    @app.route("/events")
    def sse_events():
        q = queue.Queue(maxsize=100)
        with sse_lock:
            sse_subscribers.add(q)

        def stream():
            yield "data: " + json.dumps({"type": "ping", "ts": now_str()}, ensure_ascii=False) + "\n\n"
            try:
                while True:
                    msg = q.get()
                    yield "data: " + json.dumps(msg, ensure_ascii=False) + "\n\n"
            except GeneratorExit:
                pass
            finally:
                with sse_lock:
                    sse_subscribers.discard(q)

        resp = Response(stream_with_context(stream()), mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp

    @app.route("/", methods=["OPTIONS"])
    @app.route("/<path:_u>", methods=["OPTIONS"])
    def api_opts(_u=None):
        return Response("", status=204)

    return app

# ---- Windows 防火牆 ----
def ensure_firewall_rule(port: int, name_prefix="Barcode"):
    try:
        name = f"{name_prefix}_{port}"
        chk = subprocess.run(
            ["netsh","advfirewall","firewall","show","rule",f"name={name}"],
            capture_output=True, text=True, timeout=3, encoding="utf-8", errors="ignore"
        )
        if "No rules match" in chk.stdout:
            subprocess.run(
                ["netsh","advfirewall","firewall","add","rule",
                 f"name={name}","dir=in","action=allow","protocol=TCP",f"localport={port}"],
                check=True, timeout=4, encoding="utf-8", errors="ignore"
            )
    except Exception:
        pass

# ===================== Tkinter App（含 HTTPS 選項） =====================
root = tk.Tk()
root.title("條碼掃描後端（Flask + SSE + 名冊持久化 + 內建 HTTPS）v3.4")
root.geometry("1280x900")
try:
    root.attributes("-topmost", True)
except Exception:
    pass

style = ttk.Style()
try: style.theme_use("clam")
except Exception: pass

def log(msg: str):
    evt_list.insert(tk.END, msg)
    evt_list.see(tk.END)

# 狀態
http_thread = None
http_running = False
http_srv = None

# 基本參數
host_var = tk.StringVar(value=get_local_ip())   # 也可填 0.0.0.0
http_port_var = tk.StringVar(value="8080")

# HTTPS 選項
https_var = tk.BooleanVar(value=False)
cert_path_var = tk.StringVar(value=str(APP_DIR / "cert.pem"))
key_path_var  = tk.StringVar(value=str(APP_DIR / "key.pem"))

# 其他選項
keep_focus_var = tk.BooleanVar(value=True)
prevent_sleep_var = tk.BooleanVar(value=False)
force_werkzeug_var = tk.BooleanVar(value=False)

# 讀取設定
def load_settings():
    try:
        if SETTINGS_JSON.exists():
            s = json.loads(SETTINGS_JSON.read_text(encoding="utf-8"))
            host_var.set(s.get("host", host_var.get()))
            http_port_var.set(str(s.get("port", http_port_var.get())))
            https_var.set(bool(s.get("https_enabled", False)))
            cert_path_var.set(s.get("cert_path", cert_path_var.get()))
            key_path_var.set(s.get("key_path", key_path_var.get()))
            force_werkzeug_var.set(bool(s.get("force_werkzeug", False)))
    except Exception:
        pass

def save_settings():
    try:
        SETTINGS_JSON.write_text(json.dumps({
            "host": host_var.get().strip(),
            "port": int(http_port_var.get()),
            "https_enabled": bool(https_var.get()),
            "cert_path": cert_path_var.get().strip(),
            "key_path": key_path_var.get().strip(),
            "force_werkzeug": bool(force_werkzeug_var.get()),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

load_settings()

topbar = ttk.Frame(root); topbar.pack(fill="x", padx=12, pady=8)
ttk.Label(topbar, text=f"本機 IP：{get_local_ip()}   主機名：{socket.gethostname()}",
          foreground="#0b63d1", font=("Microsoft JhengHei", 12, "bold")).pack(side="left")

# ---- 後端控制（HTTP/HTTPS 啟停） ----
backend_fr = ttk.LabelFrame(root, text="後端（Flask API）"); backend_fr.pack(fill="x", padx=12, pady=6)
ttk.Label(backend_fr, text="綁定 IP：").pack(side="left")
ttk.Entry(backend_fr, textvariable=host_var, width=18).pack(side="left", padx=(2,8))
ttk.Label(backend_fr, text="埠：").pack(side="left")
ttk.Entry(backend_fr, textvariable=http_port_var, width=8).pack(side="left", padx=(2,12))
ttk.Button(backend_fr, text="啟動伺服器", command=lambda: start_server()).pack(side="left", padx=6)
ttk.Button(backend_fr, text="停止伺服器", command=lambda: stop_server()).pack(side="left", padx=6)
ttk.Button(backend_fr, text="開啟後端首頁（/ → static/ui/index.html）", command=lambda: open_backend_home()).pack(side="left", padx=6)

# ---- HTTPS 選項 ----
https_fr = ttk.LabelFrame(root, text="HTTPS（不走 ngrok）"); https_fr.pack(fill="x", padx=12, pady=6)
ttk.Checkbutton(https_fr, text="啟用 HTTPS（需提供 cert/key 或使用已部署的可信憑證）", variable=https_var).pack(side="left", padx=(4,12))

def browse_cert():
    p = filedialog.askopenfilename(title="選擇憑證檔（.pem/.crt）",
                                   filetypes=[("PEM/CRT",".pem .crt .cer"),("All Files","*.*")])
    if p: cert_path_var.set(p)
def browse_key():
    p = filedialog.askopenfilename(title="選擇私鑰檔（.key/.pem）",
                                   filetypes=[("KEY/PEM",".key .pem"),("All Files","*.*")])
    if p: key_path_var.set(p)

ttk.Label(https_fr, text="cert 憑證：").pack(side="left")
ttk.Entry(https_fr, textvariable=cert_path_var, width=42).pack(side="left", padx=(2,6))
ttk.Button(https_fr, text="瀏覽…", command=browse_cert).pack(side="left", padx=(0,12))
ttk.Label(https_fr, text="key 私鑰：").pack(side="left")
ttk.Entry(https_fr, textvariable=key_path_var, width=42).pack(side="left", padx=(2,6))
ttk.Button(https_fr, text="瀏覽…", command=browse_key).pack(side="left", padx=(0,12))

# ---- 其他選項 ----
opts = ttk.LabelFrame(root, text="選項"); opts.pack(fill="x", padx=12, pady=6)
def on_keep_focus():
    if keep_focus_var.get():
        entry.focus_set()
ttk.Checkbutton(opts, text="保持游標在輸入框", variable=keep_focus_var, command=on_keep_focus).pack(side="left", padx=8)
def on_toggle_sleep():
    set_prevent_sleep(prevent_sleep_var.get())
    log("🛡 已啟用防睡眠" if prevent_sleep_var.get() else "💤 已恢復系統睡眠設定")
ttk.Checkbutton(opts, text="防止睡眠/螢幕保護", variable=prevent_sleep_var, command=on_toggle_sleep).pack(side="left", padx=8)
ttk.Checkbutton(opts, text="強迫使用 Werkzeug (若 Waitress 不穩定)", variable=force_werkzeug_var).pack(side="left", padx=8)

# ---- 輸入列（桌面端手動入列） ----
entry_fr = ttk.LabelFrame(root, text="條碼掃描 / 手動輸入（與 /scan 同效）"); entry_fr.pack(fill="x", padx=12, pady=6)
ttk.Label(entry_fr, text="學號/ID：").pack(side="left")
entry = ttk.Entry(entry_fr, width=24)
entry.pack(side="left", padx=6); entry.focus_set()
def do_submit(event=None):
    sid = entry.get().strip()
    if not sid:
        beep(False); return
    if add_record(sid):
        log(f"📦 入列：{sid}")
        refresh_recent()
        beep(True)
    entry.delete(0, tk.END)
    if keep_focus_var.get():
        entry.focus_set()
entry.bind("<Return>", do_submit)
ttk.Button(entry_fr, text="入列", command=do_submit).pack(side="left", padx=6)

# ---- 名冊／記錄檔操作 ----
file_fr = ttk.LabelFrame(root, text="名冊與記錄管理"); file_fr.pack(fill="x", padx=12, pady=6)

def load_roster_dialog():
    p = filedialog.askopenfilename(title="選擇名冊（xlsx或csv）",
                                   filetypes=[("Excel/CSV","*.xlsx *.xls *.csv"),
                                              ("Excel","*.xlsx *.xls"),
                                              ("CSV","*.csv")])
    if not p: return
    try:
        load_roster_from_file(Path(p))
        saved = persist_roster_to_disk()
        with lock:
            cnt = 0 if (roster_df is None) else len(roster_df)
            key = roster_id_key
            cols = roster_cols
        sse_broadcast({"type":"roster_loaded","data":{"count":cnt,"id_key":key}})
        log(f"📗 名冊已載入：{p}，共 {cnt} 筆，學號欄：{key}，欄位：{', '.join(cols)}（已持久化：{bool(saved)}）")
    except Exception as e:
        messagebox.showerror("名冊載入失敗", str(e))

def export_records_csv():
    p = filedialog.asksaveasfilename(defaultextension=".csv",
                                     filetypes=[("CSV","*.csv"),("All Files","*.*")],
                                     initialfile="records_export.csv")
    if not p: return
    with lock:
        src = list(records)
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["time","id"])
        for r in src:
            w.writerow([r["time"], r["id"]])
    log(f"✅ 記錄已匯出：{p}")

def clear_records():
    if not messagebox.askyesno("清空確認", "確定清空所有記錄？（records.csv 也會被覆寫清空）"):
        return
    with lock:
        records.clear()
        ensure_records_header()
    log("🧹 已清空記錄")
    refresh_recent()

ttk.Button(file_fr, text="載入名冊（xlsx/csv）", command=load_roster_dialog).pack(side="left", padx=6)
ttk.Button(file_fr, text="匯出記錄 CSV", command=export_records_csv).pack(side="left", padx=6)
ttk.Button(file_fr, text="清空所有記錄", command=clear_records).pack(side="left", padx=6)

# ---- 事件/最近紀錄 ----
panes = ttk.Frame(root); panes.pack(fill="both", expand=True, padx=12, pady=6)
left = ttk.LabelFrame(panes, text="事件紀錄"); left.pack(side="left", fill="both", expand=True)
evt_list = tk.Listbox(left, height=18); evt_list.pack(fill="both", expand=True, padx=6, pady=6)

right = ttk.LabelFrame(panes, text="最近紀錄（最新在上）"); right.pack(side="left", fill="both", expand=True, padx=(10,0))
rec_list = tk.Listbox(right, height=18); rec_list.pack(fill="both", expand=True, padx=6, pady=6)

def refresh_recent(n=200):
    rec_list.delete(0, tk.END)
    with lock:
        for r in list(reversed(records[-n:])):
            rec_list.insert(tk.END, f"{r['time']}  {r['id']}")

# ---- 伺服器啟停 ----
def build_ssl_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    # 可視需要：ctx.set_ciphers("ECDHE+AESGCM:!aNULL:!MD5:!DSS")
    return ctx

def start_server():
    global http_thread, http_running, http_srv
    if http_running:
        messagebox.showinfo("提示", "伺服器已在執行中"); return
    try:
        host = host_var.get().strip() or get_local_ip()
        port = int(http_port_var.get())
    except Exception:
        messagebox.showerror("錯誤", "請輸入正確的 IP 與 Port"); return

    ensure_firewall_rule(port, "BarcodeBackend")
    app = build_app()
    use_https = bool(https_var.get())
    cert_path = cert_path_var.get().strip()
    key_path  = key_path_var.get().strip()

    # 儲存目前設定
    save_settings()

    def runner():
        global http_running, http_srv
        http_running = True
        scheme = "https" if use_https else "http"
        log(f"🌐 後端啟動於 {scheme}://{host}:{port}/  （/health, /events, /scan, /upload_roster）")
        try:
            if use_https:
                if not (os.path.exists(cert_path) and os.path.exists(key_path)):
                    log("❌ 啟用 HTTPS 需要有效的 cert/key 檔案"); raise RuntimeError("cert/key not found")
                ctx = build_ssl_context(cert_path, key_path)
                http_srv = wz_make_server(host, port, app, ssl_context=ctx)
                http_srv.serve_forever()
            else:
                if (waitress_create_server is not None) and (not force_werkzeug_var.get()):
                    try:
                        http_srv = waitress_create_server(app, host=host, port=port, threads=8)
                        http_srv.run()
                    except (OSError, Exception) as e:
                        if "10038" in str(e) or "10054" in str(e):
                            log(f"⚠️ Waitress 遇到通訊端錯誤 ({e})，正在切換為 Werkzeug 模式...")
                            http_srv = wz_make_server(host, port, app)
                            http_srv.serve_forever()
                        else:
                            raise e
                else:
                    scheme_fallback = "http"
                    log(f"ℹ️ 使用 Werkzeug 模式啟動伺服器...")
                    http_srv = wz_make_server(host, port, app)
                    http_srv.serve_forever()
        except Exception as e:
            log(f"HTTP(S) 執行錯誤：{e}")
        finally:
            http_running = False
            http_srv = None
            log("🛑 伺服器已停止")

    http_thread = threading.Thread(target=runner, daemon=True)
    http_thread.start()

def stop_server():
    global http_srv, http_running
    if not http_running or http_srv is None:
        messagebox.showinfo("提示", "伺服器目前未執行"); return
    try:
        if hasattr(http_srv, "close"):
            http_srv.close()
        if hasattr(http_srv, "shutdown"):
            http_srv.shutdown()
        log("已送出伺服器停止指令…")
    except Exception as e:
        log(f"停止失敗：{e}")

def open_backend_home():
    host = host_var.get().strip() or get_local_ip()
    try:
        port = int(http_port_var.get())
    except Exception:
        port = 8080
    scheme = "https" if https_var.get() else "http"
    webbrowser.open(f"{scheme}://{host}:{port}/")

# ---- 保持焦點輪詢 ----
def focus_loop():
    try:
        if keep_focus_var.get():
            if entry != root.focus_get():
                entry.focus_set()
    except Exception:
        pass
    root.after(250, focus_loop)

# ---- 關閉處理 ----
def on_close():
    try:
        stop_server()
    except Exception:
        pass
    set_prevent_sleep(False)
    root.destroy()

# ---- 初始化 ----
ensure_records_header()
def _init():
    loaded_n = load_records_from_csv()
    log(f"🗂 已載入歷史記錄 {loaded_n} 筆")

    restored = restore_roster_from_disk()
    if restored:
        with lock:
            log(f"📗 已還原名冊（{len(roster_df)} 筆；學號欄：{roster_id_key}）")
    else:
        log("📗 未發現已保存的名冊（或未安裝 pandas），可於前端或後端手動上傳")

    refresh_recent()
    focus_loop()
    # ⛳ 程式啟動後自動啟動 HTTP/HTTPS 伺服器
    try:
        root.after(100, start_server)
    except Exception as e:
        log(f"自動啟動伺服器失敗：{e}")


_init()
root.protocol("WM_DELETE_WINDOW", on_close)
root.mainloop()
