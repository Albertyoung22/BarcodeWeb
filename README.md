# Cloud Relay (Render-ready)

這個最小專案可直接部署到 **Render** 當雲端中繼：
- `GET /`：手機控制台（輸入 ADMIN_KEY，下指令給客戶端）
- `GET /state`：查看目前連線 client
- `POST /api/send`：伺服器端 API（需 admin_key）
- `WS /ws?client_id=...&secret=...`：客戶端（橋接器）連線端點

## 部署到 Render（免費方案即可）
1. 將本 repo 推到 GitHub。
2. Render → New → **Web Service** → 連你的 GitHub repo。
3. 設定：
   - Build Command：`pip install -r requirements.txt`
   - Start Command：`uvicorn relay_server:app --host 0.0.0.0 --port $PORT`
   - Environment Variables：
     - `RELAY_SECRET`：給「橋接器」用的金鑰（自行設定強金鑰）
     - `ADMIN_KEY`：給「手機/控制台」用的管理金鑰（自行設定強金鑰）
4. 部署完成後，打開 `https://你的子網域.onrender.com` 即可看到控制台。

## Windows 橋接器（校園端）
使用 `bridge/campus_relay_bridge.py`：
- 安裝套件：`pip install websocket-client requests`
- 設定環境變數（或直接改檔案頂端常數）：
  - `RELAY_URL`：例如 `wss://你的子網域.onrender.com/ws`
  - `RELAY_SECRET`：要與 Render 一致
  - `CLIENT_ID`：這台電腦代號（例如 RoomA-PC1）
  - `LOCAL_FORWARD_URL`：你的本機接收程式 API（例如 `http://127.0.0.1:5000/api/remote`）
- 執行：`python campus_relay_bridge.py`
- 建議設為排程器「開機自動執行」。

## 範例 Payload（在控制台輸入區）
```json
{"cmd":"Say","text":"現在上課，請安靜就座","lang":"zh-TW"}
```
```json
{"cmd":"Bell","file":"bell1.mp3","volume":80}
```
```json
{"cmd":"YT","url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ","mode":"fullscreen"}
```
```json
{"cmd":"Relay","channel":1,"state":"ON","min_ms":3000}
```

> 中繼只負責「轉傳」。請依你的本機接收程式 API 來定義實際 payload。
