import os
import json
import time
import hmac
import uuid
import queue
import atexit
import random
import string
import hashlib
import datetime
import threading
import pytz
import requests
import websocket
from dotenv import load_dotenv
load_dotenv()

# =========
# Settings
# =========

IST = pytz.timezone("Asia/Kolkata")

WEBSOCKET_URL = os.getenv("DELTA_WS_URL", "wss://socket.india.delta.exchange")
API_BASE = os.getenv("DELTA_API_BASE", "https://api.india.delta.exchange")  # change to https://api.delta.exchange if needed
API_KEY = os.getenv("DELTA_API_KEY")
API_SECRET = os.getenv("DELTA_API_SECRET")

# Business logic
USER_MULTIPLIER = float(os.getenv("USER_MULTIPLIER", "2.0"))  # e.g., 2.0 means "match + 1x top-up"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
MAX_TOPUP_PER_TRADE = int(os.getenv("MAX_TOPUP_PER_TRADE", "1_000_000"))  # contracts
MAX_TOPUP_PER_SYMBOL = int(os.getenv("MAX_TOPUP_PER_SYMBOL", "10_000_000"))  # running session cap
ALLOW_SYMBOLS = set(s.strip().upper() for s in os.getenv("ALLOW_SYMBOLS", "ALL").split(","))  # "ALL" or list: "BTCUSDT,ETHUSDT"
TIF = os.getenv("TIME_IN_FORCE", "IOC")  # IOC or FOK where supported
ORDER_TYPE = os.getenv("ORDER_TYPE", "market")  # "market" recommended (no price logic required)
SELF_TAG_PREFIX = os.getenv("SELF_TAG_PREFIX", "BOTMULT_")  # used in client_order_id or text to mark our orders

# Reliability
PING_INTERVAL = int(os.getenv("PING_INTERVAL", "30"))
PING_TIMEOUT = int(os.getenv("PING_TIMEOUT", "5"))
LOG_DIR = os.getenv("LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

if not API_KEY or not API_SECRET:
    raise RuntimeError("Missing DELTA_API_KEY / DELTA_API_SECRET in environment")

def now_ist():
    return datetime.datetime.now(IST)

def now_ist_iso():
    return now_ist().isoformat(timespec="milliseconds")

def log_path():
    day = now_ist().date().isoformat()
    return os.path.join(LOG_DIR, f"delta_ws_events_{day}.jsonl")

def log_event(event_type, data):
    entry = {"ts": now_ist_iso(), "type": event_type, "data": data}
    try:
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[{now_ist_iso()}] Log write error: {e} | entry={entry}")

# ===================
# REST auth & orders
# ===================

def _sign(method: str, path: str, timestamp: str, body: str = "") -> str:
    # Delta typically signs as: method + timestamp + path + body (body only for POST/DELETE with JSON)
    msg = method + timestamp + path + (body or "")
    return hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def _headers(method: str, path: str, body_json: dict | None) -> dict:
    ts = str(int(time.time()))
    body = json.dumps(body_json, separators=(",", ":"), ensure_ascii=False) if body_json else ""
    sig = _sign(method, path, ts, body)
    h = {
        "api-key": API_KEY,
        "timestamp": ts,
        "signature": sig,
    }
    if body_json is not None:
        h["Content-Type"] = "application/json"
    return h

def rest_post(path: str, payload: dict) -> tuple[int, dict | str]:
    url = API_BASE.rstrip("/") + path
    headers = _headers("POST", path, payload)
    try:
        r = requests.post(url, headers=headers, data=json.dumps(payload, separators=(",", ":")))
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text
    except Exception as e:
        return 0, str(e)

# Build a unique client ID we can recognize in WS streams
def build_client_order_id() -> str:
    suffix = uuid.uuid4().hex[:10]
    return f"{SELF_TAG_PREFIX}{suffix}"

def place_order_topup(symbol: str | None, product_id: int | None, side: str, size: int):
    """
    Places our top-up order. Uses MARKET + IOC by default to avoid leaving rests.
    We tag with client_order_id to prevent loops.
    """
    if size <= 0:
        return 200, {"skipped": "non_positive_size"}

    if DRY_RUN:
        log_event("dry_run_order", {"symbol": symbol, "product_id": product_id, "side": side, "size": size})
        print(f"[{now_ist_iso()}] DRY_RUN place {side} {size} on {symbol or product_id}")
        return 200, {"dry_run": True}

    body = {
        # Prefer product_id if you have it; else many endpoints accept symbol. We send both when available.
        "side": side.lower(),                  # "buy" or "sell"
        "order_type": ORDER_TYPE.lower(),      # "market" recommended
        "time_in_force": TIF.upper(),          # "IOC" or "FOK" if supported
        "size": int(size),                     # contracts (same unit as provider)
        "reduce_only": False,
        "client_order_id": build_client_order_id(),
    }
    if product_id is not None:
        body["product_id"] = int(product_id)
    if symbol is not None:
        body["symbol"] = str(symbol).upper()

    status, resp = rest_post("/orders", body)
    log_event("order_submit", {"status": status, "resp": resp, "req": body})
    if status != 200:
        print(f"[{now_ist_iso()}] ORDER ERROR status={status} resp={resp}")
    else:
        print(f"[{now_ist_iso()}] Placed top-up: {side} {size} {symbol or product_id}")
    return status, resp

# ===============================
# WebSocket: auth & event engine
# ===============================

# Work queue so REST calls don't block the WS thread
order_q: "queue.Queue[dict]" = queue.Queue(maxsize=1000)

# Track what's ours, and caps per symbol
our_client_prefix = SELF_TAG_PREFIX
session_topup_used: dict[str, int] = {}  # symbol->contracts we've added this session
seen_trade_ids: set[str] = set()         # de-dup usertrades
order_fill_cum: dict[str, int] = {}      # order_id->last_seen_cum_filled (for orders channel fallback)

def _is_allowed_symbol(symbol: str | None) -> bool:
    if not symbol:
        return True if "ALL" in ALLOW_SYMBOLS else False
    symbol_u = symbol.upper()
    return True if "ALL" in ALLOW_SYMBOLS else (symbol_u in ALLOW_SYMBOLS)

def _looks_like_ours(client_order_id: str | None, text: str | None = None) -> bool:
    cid = (client_order_id or "")[:len(our_client_prefix)]
    tx = (text or "")[:len(our_client_prefix)]
    return cid == our_client_prefix or tx == our_client_prefix

def _cap_ok(symbol: str | None, add_size: int) -> bool:
    if not symbol:
        return True
    used = session_topup_used.get(symbol, 0)
    return (used + add_size) <= MAX_TOPUP_PER_SYMBOL

def _bump_cap(symbol: str | None, add_size: int):
    if not symbol:
        return
    session_topup_used[symbol] = session_topup_used.get(symbol, 0) + add_size

def compute_topup_size(filled_qty: int) -> int:
    """
    If provider fills N, we add (multiplier-1)*N.
    """
    if USER_MULTIPLIER <= 1.0:
        return 0
    # round to int contracts
    add = int(round((USER_MULTIPLIER - 1.0) * filled_qty))
    return max(0, min(add, MAX_TOPUP_PER_TRADE))

def on_error(ws, error):
    print(f"[{now_ist_iso()}] Socket Error: {error}")
    log_event("error", str(error))

def on_close(ws, code, msg):
    print(f"[{now_ist_iso()}] Socket closed code={code} reason={msg}")
    log_event("close", {"code": code, "reason": msg})

def generate_signature(secret, message):
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()

def send_auth(ws):
    # WS auth per /live signature: method+timestamp+path
    method = "GET"
    timestamp = str(int(time.time()))
    path = "/live"
    signature = generate_signature(API_SECRET, method + timestamp + path)
    ws.send(json.dumps({
        "type": "auth",
        "payload": {
            "api-key": API_KEY,
            "signature": signature,
            "timestamp": timestamp
        }
    }))

def subscribe(ws, channel, symbols):
    ws.send(json.dumps({
        "type": "subscribe",
        "payload": { "channels": [ { "name": channel, "symbols": symbols } ] }
    }))

def on_open(ws):
    print(f"[{now_ist_iso()}] Socket opened; authenticating…")
    log_event("open", "ws_open")
    send_auth(ws)

def _extract_symbol(ev: dict) -> str | None:
    for k in ("symbol", "product_symbol", "product_symbol_name"):
        if k in ev and ev[k]:
            return str(ev[k]).upper()
    return None

def _extract_product(ev: dict) -> int | None:
    for k in ("product_id", "instrument_id"):
        if k in ev and ev[k] is not None:
            try:
                return int(ev[k])
            except:
                pass
    return None

def _extract_side(ev: dict) -> str | None:
    s = ev.get("side") or ev.get("order_side")
    if not s:
        return None
    s = str(s).lower()
    if s.startswith("b"):
        return "buy"
    if s.startswith("s"):
        return "sell"
    return None

def handle_usertrade(ev: dict):
    """
    Preferred path: usertrades stream includes a single fill with its quantity.
    """
    trade_id = str(ev.get("id") or ev.get("trade_id") or "")
    if trade_id and trade_id in seen_trade_ids:
        return
    if trade_id:
        seen_trade_ids.add(trade_id)

    client_order_id = ev.get("client_order_id") or ev.get("client_id") or ev.get("text")
    if _looks_like_ours(client_order_id, ev.get("text")):
        # Our own fill; ignore (prevents loops)
        return

    symbol = _extract_symbol(ev)
    if not _is_allowed_symbol(symbol):
        return

    side = _extract_side(ev)
    qty = ev.get("size") or ev.get("fill_size") or ev.get("quantity") or ev.get("filled_quantity")
    try:
        qty = int(qty)
    except Exception:
        return

    add = compute_topup_size(qty)
    if add <= 0:
        return
    if not _cap_ok(symbol, add):
        log_event("cap_block", {"symbol": symbol, "add": add})
        return

    product_id = _extract_product(ev)
    order_q.put({"symbol": symbol, "product_id": product_id, "side": side, "size": add})

def handle_order_update(ev: dict):
    """
    Fallback if usertrades is unavailable: detect incremental fills
    using (cum_filled - last_seen).
    """
    client_order_id = ev.get("client_order_id") or ev.get("client_id") or ev.get("text")
    if _looks_like_ours(client_order_id, ev.get("text")):
        return  # ours; ignore

    oid = str(ev.get("id") or ev.get("order_id") or "")
    if not oid:
        return
    symbol = _extract_symbol(ev)
    if not _is_allowed_symbol(symbol):
        return

    cum = ev.get("filled_size") or ev.get("total_filled") or ev.get("cumulative_qty")
    try:
        cum = int(cum)
    except Exception:
        return

    prev = order_fill_cum.get(oid, 0)
    if cum <= prev:
        order_fill_cum[oid] = cum  # keep in sync
        return

    delta = cum - prev
    order_fill_cum[oid] = cum

    side = _extract_side(ev)
    add = compute_topup_size(delta)
    if add <= 0:
        return
    if not _cap_ok(symbol, add):
        log_event("cap_block", {"symbol": symbol, "add": add})
        return

    product_id = _extract_product(ev)
    order_q.put({"symbol": symbol, "product_id": product_id, "side": side, "size": add})

def on_message(ws, message):
    try:
        msg = json.loads(message)
    except Exception:
        log_event("parse_error", message)
        return

    t = msg.get("type")
    if t == "success" and msg.get("message") == "Authenticated":
        # Subscribe to private channels after auth
        subscribe(ws, "orders", ["all"])
        subscribe(ws, "positions", ["all"])
        # Subscribe to usertrades (sometimes named 'usertrades' / 'user_trades'); subscribe both for safety
        subscribe(ws, "usertrades", ["all"])
        subscribe(ws, "user_trades", ["all"])
        print(f"[{now_ist_iso()}] Authenticated. Subscribed to orders/positions/usertrades.")
        log_event("subscriptions", {"ok": True})
        return

    log_event("message", msg)

    # Route events
    if t in ("usertrades", "user_trades"):
        # expect either a single dict or list
        payload = msg.get("payload") or msg.get("data") or msg.get("trades") or msg.get("usertrades") or msg
        events = payload if isinstance(payload, list) else [payload]
        for ev in events:
            if isinstance(ev, dict):
                handle_usertrade(ev)
        return

    if t == "orders":
        payload = msg.get("payload") or msg.get("data") or msg.get("orders") or msg
        events = payload if isinstance(payload, list) else [payload]
        for ev in events:
            if isinstance(ev, dict):
                handle_order_update(ev)
        return

    # positions updates are useful for logs/debug; we don't trigger on them (loop-safe)

# =========================
# Worker to place top-ups
# =========================

def order_worker():
    while True:
        job = order_q.get()
        if job is None:
            return
        symbol = job.get("symbol")
        product_id = job.get("product_id")
        side = job.get("side")
        size = int(job.get("size", 0))

        if size <= 0 or side not in ("buy", "sell"):
            continue
        if not _cap_ok(symbol, size):
            continue

        status, resp = place_order_topup(symbol, product_id, side, size)
        if status == 200:
            _bump_cap(symbol, size)
        else:
            # Small randomized backoff on errors
            time.sleep(0.25 + random.random() * 0.75)

def run_ws_forever():
    backoff = 1
    while True:
        ws = websocket.WebSocketApp(
            WEBSOCKET_URL,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        ws.run_forever(ping_interval=PING_INTERVAL, ping_timeout=PING_TIMEOUT, sslopt={"cert_reqs": 0})
        print(f"[{now_ist_iso()}] Reconnecting in {backoff}s…")
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)

def shutdown():
    try:
        order_q.put(None)
    except Exception:
        pass

if __name__ == "__main__":
    print(f"[{now_ist_iso()}] Starting Delta multiplier bot | multiplier={USER_MULTIPLIER} | DRY_RUN={DRY_RUN}")
    atexit.register(shutdown)
    t = threading.Thread(target=order_worker, daemon=True)
    t.start()
    run_ws_forever()
