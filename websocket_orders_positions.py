import websocket
import hashlib
import hmac
import json
import time
from dotenv import load_dotenv
import os
import pytz
import datetime

# Load variables from .env file
load_dotenv()

# production websocket base url and api keys/secrets
WEBSOCKET_URL = "wss://socket.india.delta.exchange"
API_KEY = os.getenv("DELTA_API_KEY")
API_SECRET = os.getenv("DELTA_API_SECRET")

IST = pytz.timezone("Asia/Kolkata")
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

if not API_KEY or not API_SECRET:
    raise RuntimeError("Missing DELTA_API_KEY / DELTA_API_SECRET in .env")


def now_ist():
    return datetime.datetime.now(IST)

def now_ist_iso():
    return now_ist().isoformat(timespec="milliseconds")

def log_path_for(dt: datetime.datetime) -> str:
    # one file per day in IST
    day = dt.date().isoformat()  # YYYY-MM-DD
    return os.path.join(LOG_DIR, f"delta_ws_events_{day}.jsonl")


def log_event(event_type, data):
    """Append timestamped event to JSONL log."""
    ts = now_ist()
    entry = {"ts": ts.isoformat(timespec="milliseconds"), "type": event_type, "data": data}
    path = log_path_for(ts)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        # Fallback to console if disk write has issues
        print(f"[{now_ist_iso()}] Log write error: {e} | entry={entry}")

def on_error(ws, error):
    print(f"[{now_ist()}] Socket Error: {error}")
    log_event("error", str(error))

def on_close(ws, close_status_code, close_msg):
    print(f"[{now_ist()}] Socket closed code={close_status_code} reason={close_msg}")
    log_event("close", {"code": close_status_code, "reason": close_msg})

def on_open(ws):
    print(f"[{now_ist()}] Socket opened")
    # api key authentication
    log_event("open", "WebSocket connection opened")
    send_authentication(ws)

def send_authentication(ws):
    method = 'GET'
    timestamp = str(int(time.time()))
    path = '/live'
    signature_data = method + timestamp + path
    signature = generate_signature(API_SECRET, signature_data)
    ws.send(json.dumps({
        "type": "auth",
        "payload": {
            "api-key": API_KEY,
            "signature": signature,
            "timestamp": timestamp
        }
    }))

def generate_signature(secret, message):
    message = bytes(message, 'utf-8')
    secret = bytes(secret, 'utf-8')
    hash = hmac.new(secret, message, hashlib.sha256)
    return hash.hexdigest()

def on_message(ws, message):
    try:
        message_json = json.loads(message)
    except json.JSONDecodeError:
        print(f"[{now_ist()}] Non-JSON message: {message}")
        log_event("parse_error", message)
        return
    
    # subscribe private channels after successful authentication
    if message_json['type'] == 'success' and message_json['message'] == 'Authenticated':
         # subscribe orders channel for order updates for all contracts
        subscribe(ws, "orders", ["all"])
        # subscribe positions channel for position updates for all contracts
        subscribe(ws, "positions", ["all"])
        return
    
    # Print brief, log full
    messageType = message_json.get("type")
    if messageType in ("subscriptions", "positions", "orders"):
        print({k: message_json[k] for k in ("type", "channels") if k in message_json} or {"type": messageType})
    else:
        print(message_json.get("type", "message"), "event")
    
    log_event("message", message_json)

def subscribe(ws, channel, symbols):
    payload = {
        "type": "subscribe",
        "payload": {
            "channels": [
                {
                    "name": channel,
                    "symbols": symbols
                }
            ]
        }
    }
    ws.send(json.dumps(payload))


def run_forever_with_retries():
    backoff = 1
    ws = websocket.WebSocketApp(WEBSOCKET_URL, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
    ws.run_forever(ping_interval=30, ping_timeout=5, sslopt={"cert_reqs": 0}) # runs indefinitely
    time.sleep(backoff)
    backoff = min(backoff * 2, 60)
    print(f"[{now_ist()}] Reconnecting in {backoff}s...")
    
if __name__ == "__main__":
  run_forever_with_retries()