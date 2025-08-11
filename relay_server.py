# relay_server.py
# FastAPI + WebSocket 雲端中繼（含簡易控制頁）
# 啟動命令（Render）： uvicorn relay_server:app --host 0.0.0.0 --port $PORT

import os, json, asyncio, time
from typing import Dict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse
from starlette.websockets import WebSocketState

RELAY_SECRET = os.getenv("RELAY_SECRET", "change-me-relay")
ADMIN_KEY = os.getenv("ADMIN_KEY", "change-me-admin")

app = FastAPI(title="Cloud Relay")

clients_lock = asyncio.Lock()
clients: Dict[str, WebSocket] = {}
heartbeats: Dict[str, float] = {}

CONTROL_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Cloud Relay Control</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font-family:system-ui,Segoe UI,Roboto,Arial;padding:18px;background:#0b1220;color:#e7eefc}
h1{font-size:20px;margin:0 0 12px}
.card{background:#141c2f;border:1px solid #263045;border-radius:14px;padding:16px;margin-bottom:14px;box-shadow:0 6px 16px rgba(0,0,0,.25)}
label{display:block;margin:8px 0 6px;color:#a7b4cc}
input,textarea,select{width:100%;padding:10px 12px;border-radius:10px;border:1px solid #2b3a55;background:#0f1626;color:#e7eefc}
button{margin-top:12px;padding:10px 14px;border-radius:12px;border:1px solid #4769ff;background:#3252ff;color:#fff;font-weight:600}
.small{font-size:12px;color:#90a2c8}
.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media(max-width:700px){.row{grid-template-columns:1fr}}
pre{white-space:pre-wrap;background:#0f1626;border-radius:10px;padding:10px}
</style>
</head>
<body>
  <h1>Cloud Relay Control</h1>
  <div class="card">
    <label>Admin Key</label>
    <input id="adm" type="password" placeholder="請輸入 ADMIN_KEY">
    <div class="small">此金鑰僅用於雲端 API 授權，不會儲存於伺服器。</div>
  </div>

  <div class="card">
    <div class="row">
      <div>
        <label>目標</label>
        <select id="targetType">
          <option value="all">全部連線</option>
          <option value="one">指定 client_id</option>
        </select>
      </div>
      <div>
        <label>client_id（選擇「指定」才需填）</label>
        <input id="clientId" placeholder="例如: RoomA-PC1">
      </div>
    </div>
    <label>Payload（將原封不動轉交到校園端橋接器 → 本機 127.0.0.1:5000）</label>
    <textarea id="payload" rows="6" placeholder='例如：{"cmd":"Say","text":"現在上課", "lang":"zh-TW"}'></textarea>
    <button onclick="send()">送出</button>
    <div class="small">建議與本機接收主程式既有 API 格式一致（例如 /api/remote 期待的 JSON）。</div>
  </div>

  <div class="card">
    <button onclick="refreshState()">更新目前連線</button>
    <pre id="stateBox">（點「更新目前連線」查看）</pre>
  </div>

<script>
async function refreshState(){
  const r = await fetch("/state");
  const j = await r.json();
  document.getElementById("stateBox").textContent = JSON.stringify(j,null,2);
}
async function send(){
  const adm = document.getElementById("adm").value.trim();
  if(!adm){ alert("請先輸入 Admin Key"); return; }
  const ttype = document.getElementById("targetType").value;
  const cid = document.getElementById("clientId").value.trim();
  let payloadText = document.getElementById("payload").value.trim();
  if(!payloadText){ alert("請填 payload"); return; }
  let payload;
  try{ payload = JSON.parse(payloadText); }
  catch(e){ alert("Payload 不是合法 JSON"); return; }

  const body = (ttype==="all") ? {admin_key:adm, all:true, payload}
                               : {admin_key:adm, targets:[cid], payload};
  const r = await fetch("/api/send",{
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify(body)
  });
  const j = await r.json();
  alert(JSON.stringify(j));
}
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(CONTROL_HTML)

@app.get("/health")
async def health():
    return {"ok": True, "ts": time.time()}

@app.get("/state")
async def state():
    async with clients_lock:
        return {
            "connected": list(clients.keys()),
            "count": len(clients),
            "heartbeats": heartbeats
        }

@app.post("/api/send")
async def api_send(req: Request):
    data = await req.json()
    if data.get("admin_key") != ADMIN_KEY:
        raise HTTPException(401, "Bad admin_key")

    payload = data.get("payload")
    if payload is None:
        raise HTTPException(400, "payload required")

    sent = 0
    async with clients_lock:
        if data.get("all"):
            targets = list(clients.keys())
        else:
            targets = data.get("targets") or []
        for cid in targets:
            ws = clients.get(cid)
            if ws and ws.client_state == WebSocketState.CONNECTED:
                try:
                    await ws.send_json({"type":"cmd","payload":payload})
                    sent += 1
                except Exception:
                    pass
    return {"ok": True, "sent": sent}

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    client_id = websocket.query_params.get("client_id") or ""
    secret = websocket.query_params.get("secret") or ""
    if not client_id or secret != RELAY_SECRET:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    async with clients_lock:
        old = clients.get(client_id)
        if old:
            try: await old.close()
            except: pass
        clients[client_id] = websocket
        heartbeats[client_id] = time.time()

    try:
        while True:
            msg = await websocket.receive_text()
            try:
                j = json.loads(msg)
                if j.get("type") == "ping":
                    heartbeats[client_id] = time.time()
                    await websocket.send_json({"type":"pong","ts":time.time()})
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        async with clients_lock:
            ws = clients.get(client_id)
            if ws is websocket:
                clients.pop(client_id, None)
                heartbeats.pop(client_id, None)
