# bridge/campus_relay_bridge.py
# 角色：校園端橋接器（主動連到雲端中繼，收到 payload 後 POST 給本機 127.0.0.1:5000）
# 需求：pip install websocket-client requests

import os, json, time
import requests
from urllib.parse import urlencode
from websocket import create_connection, WebSocketConnectionClosedException

RELAY_URL = os.getenv("RELAY_URL", "wss://你的render子網域.onrender.com/ws")
RELAY_SECRET = os.getenv("RELAY_SECRET", "change-me-relay")
CLIENT_ID = os.getenv("CLIENT_ID", "RoomA-PC1")
LOCAL_FORWARD_URL = os.getenv("LOCAL_FORWARD_URL", "http://127.0.0.1:5000/api/remote")

PING_INTERVAL = 20
RETRY_DELAY = 5

def forward_to_local(payload: dict):
    try:
        r = requests.post(LOCAL_FORWARD_URL, json=payload, timeout=5)
        r.raise_for_status()
        return True, r.text
    except Exception as e:
        return False, str(e)

def run():
    while True:
        try:
            qs = urlencode({"client_id": CLIENT_ID, "secret": RELAY_SECRET})
            ws = create_connection(f"{RELAY_URL}?{qs}", timeout=10)
            print("[Bridge] Connected to cloud relay as", CLIENT_ID)
            last_ping = 0

            while True:
                now = time.time()
                if now - last_ping > PING_INTERVAL:
                    ws.send(json.dumps({"type":"ping","ts":now}))
                    last_ping = now

                ws.settimeout(5)
                try:
                    msg = ws.recv()
                except Exception:
                    continue

                try:
                    j = json.loads(msg)
                except Exception:
                    continue

                if j.get("type") == "cmd":
                    payload = j.get("payload")
                    ok, detail = forward_to_local(payload)
                    print(f"[Bridge] Forward -> local: {ok}, {detail}")

        except (ConnectionRefusedError, WebSocketConnectionClosedException, OSError) as e:
            print("[Bridge] Disconnected, retry in", RETRY_DELAY, "sec. Reason:", e)
            time.sleep(RETRY_DELAY)
        except Exception as e:
            print("[Bridge] Error:", e)
            time.sleep(RETRY_DELAY)

if __name__ == "__main__":
    run()
