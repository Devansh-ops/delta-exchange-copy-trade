import websocket
import hashlib
import hmac
import json
import time
from dotenv import load_dotenv
import os

# Load variables from .env file
load_dotenv()

# production websocket base url and api keys/secrets
WEBSOCKET_URL = "wss://socket.india.delta.exchange"
API_KEY = os.getenv("DELTA_API_KEY")
API_SECRET = os.getenv("DELTA_API_SECRET")


if not API_KEY or not API_SECRET:
    raise RuntimeError("Missing DELTA_API_KEY / DELTA_API_SECRET in .env")

def on_error(ws, error):
    print(f"Socket Error: {error}")

def on_close(ws, close_status_code, close_msg):
    print(f"Socket closed with status: {close_status_code} and message: {close_msg}")

def on_open(ws):
    print(f"Socket opened")
    # api key authentication
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
    message_json = json.loads(message)
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
    
    log_event(message_json)

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

def log_event(ev):
    with open("delta_ws_events.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")

if __name__ == "__main__":
  ws = websocket.WebSocketApp(WEBSOCKET_URL, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
  ws.run_forever(ping_interval=30, ping_timeout=5, sslopt={"cert_reqs": 0}) # runs indefinitely