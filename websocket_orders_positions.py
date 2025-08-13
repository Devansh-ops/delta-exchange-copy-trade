import os
import json
import time
import hmac
import uuid
import queue
import atexit
import random
import hashlib
import datetime
import threading
import pytz
import requests
import websocket
from cachetools import TTLCache
import ssl
import sys
import signal
from time import monotonic as _now
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
WS_INSECURE = os.getenv("WS_INSECURE", "false").lower() == "true"

# Business logic
USER_MULTIPLIER = float(os.getenv("USER_MULTIPLIER", "2.0"))  # e.g., 2.0 means "match + 1x top-up"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
MAX_TOPUP_PER_TRADE = int(os.getenv("MAX_TOPUP_PER_TRADE", "1_000_000"))  # contracts
MAX_TOPUP_PER_SYMBOL = int(os.getenv("MAX_TOPUP_PER_SYMBOL", "10_000_000"))  # running session cap
ALLOW_SYMBOLS = set(s.strip().upper() for s in os.getenv("ALLOW_SYMBOLS", "ALL").split(","))  # "ALL" or list: "BTCUSDT,ETHUSDT"
TIF = os.getenv("TIME_IN_FORCE", "IOC")  # IOC or FOK where supported
SELF_TAG_PREFIX = os.getenv("SELF_TAG_PREFIX", "BOTMULT_")  # used in client_order_id or text to mark our orders
VERBOSE_DECISIONS = os.getenv("VERBOSE_DECISIONS", "true").lower() == "true"
ORDER_TYPE = os.getenv("ORDER_TYPE", "market_order")  # 'market_order' or 'limit_order'
LIMIT_SLIPPAGE_BPS = float(os.getenv("LIMIT_SLIPPAGE_BPS", "0"))  # e.g., 1.5 = 1.5 bps; 0 disables
LIMIT_IOC_FALLBACK_MARKET = os.getenv("LIMIT_IOC_FALLBACK_MARKET", "true").lower() == "true"
USER_AGENT = os.getenv("USER_AGENT", "python-rest-client")

# Reliability
PING_INTERVAL = int(os.getenv("PING_INTERVAL", "30"))
PING_TIMEOUT = int(os.getenv("PING_TIMEOUT", "5"))
LOG_DIR = os.getenv("LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# Backoff 
BACKOFF_BASE = float(os.getenv("BACKOFF_BASE", "1.0"))
BACKOFF_MAX  = float(os.getenv("BACKOFF_MAX", "60.0"))
BACKOFF_JITTER = float(os.getenv("BACKOFF_JITTER", "0.4"))  # 0..0.4x
LAST_CONN_OK_AT = 0.0

# Dedup store limits (keep your envs)
FILL_ID_TTL_SEC = int(os.getenv("FILL_ID_TTL_SEC", "86400"))   # 24h
FILL_ID_MAX     = int(os.getenv("FILL_ID_MAX", "200000"))
TRADE_ID_TTL_SEC = int(os.getenv("TRADE_ID_TTL_SEC", "86400"))   # 24h
TRADE_ID_MAX     = int(os.getenv("TRADE_ID_MAX", "200000"))

# Harden HTTP
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "10"))         # read timeout (s)
HTTP_CONN_TIMEOUT = float(os.getenv("HTTP_CONN_TIMEOUT", "3.05"))
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "3"))

# Shutdown / exit
STOP_EVENT = threading.Event()
CURRENT_WS = None          # set each time we create a WebSocketApp
SHUTDOWN_ONCE = threading.Event()

# top-level
seen_fill_ids = TTLCache(maxsize=FILL_ID_MAX, ttl=FILL_ID_TTL_SEC)

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

# --- Helpers (below log_event)
def log_skip(reason: str, ctx: dict):
    if VERBOSE_DECISIONS:
        log_event("skip", {"reason": reason, **ctx})

def log_action(action: str, ctx: dict):
    log_event("action", {"action": action, **ctx})

# ===================
# REST auth & orders
# ===================

def _sign(method: str, path: str, timestamp: str, body: str = "") -> str:
    # Delta typically signs as: method + timestamp + path + body (body only for POST/DELETE with JSON)
    msg = method + timestamp + path + (body or "")
    return hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

# --- REST signing/headers
def _headers(method: str, path: str, body_json: dict | None) -> dict:
    ts = str(int(time.time()))
    body = json.dumps(body_json, separators=(",", ":"), ensure_ascii=False) if body_json else ""
    sig = _sign(method, path, ts, body)
    h = {
        "api-key": API_KEY,
        "timestamp": ts,
        "signature": sig,
        "User-Agent": USER_AGENT,   # REQUIRED by Delta
    }
    if body_json is not None:
        h["Content-Type"] = "application/json"
    return h


def rest_post(path: str, payload: dict) -> tuple[int, dict | str]:
    url = API_BASE.rstrip("/") + path
    headers = _headers("POST", path, payload)
    attempt = 0
    while True:
        attempt += 1
        try:
            r = requests.post(
                url,
                headers=headers,
                data=json.dumps(payload, separators=(",", ":")),
                timeout=(HTTP_CONN_TIMEOUT, HTTP_TIMEOUT),
            )
            status = r.status_code
            try:
                data = r.json()
            except Exception:
                data = r.text

            # Retry on rate-limit or transient server errors
            if attempt < HTTP_RETRIES and (status == 429 or 500 <= status < 600):
                sleep_s = min(0.5 * (2 ** (attempt - 1)), 4.0) * (1 + random.random()*0.25)
                log_action("rest_retry", {"status": status, "attempt": attempt, "sleep": round(sleep_s, 2)})
            else:
                return status, data

            time.sleep(sleep_s)
        except Exception as e:
            if attempt >= HTTP_RETRIES:
                return 0, str(e)
            sleep_s = min(0.5 * (2 ** (attempt - 1)), 4.0) * (1 + random.random()*0.25)
            log_action("rest_exc_retry", {"attempt": attempt, "sleep": round(sleep_s, 2), "err": str(e)})
            time.sleep(sleep_s)


# Build a unique client ID we can recognize in WS streams
def build_client_order_id() -> str:
    suffix = uuid.uuid4().hex[:10]
    return f"{SELF_TAG_PREFIX}{suffix}"


def _adjust_limit(side: str, base_price: str | None) -> str | None:
    if not base_price:
        return None
    try:
        p = float(base_price)
    except Exception:
        return None
    slip = LIMIT_SLIPPAGE_BPS / 10_000.0
    if slip > 0:
        p = p * (1.0 + slip) if side == "buy" else p * (1.0 - slip)
    # keep plenty of precision; exchange will round if needed
    s = f"{p:.8f}".rstrip('0').rstrip('.')
    return s

def place_order_topup(symbol: str | None, product_id: int | None, side: str, size: int, price: str | None = None):
    """
    Places our top-up order. Uses MARKET + IOC by default to avoid leaving rests.
    We tag with client_order_id to prevent loops.
    """
    if size <= 0:
        return 200, {"skipped": "non_positive_size"}

    if DRY_RUN:
        log_event("dry_run_order", {"symbol": symbol, "product_id": product_id, "side": side, "size": size, "price": price})
        print(f"[{now_ist_iso()}] DRY_RUN place {side} {size} on {symbol or product_id} ({ORDER_TYPE}, {price})")
        return 200, {"dry_run": True}

    body = {
        "side": side.lower(),
        "order_type": ORDER_TYPE,   # must be 'market_order' or 'limit_order'
        "time_in_force": TIF.lower(),  # 'gtc' or 'ioc'
        "size": int(size),
        "reduce_only": False,
        "client_order_id": build_client_order_id(),
        "product_id": int(product_id) if product_id is not None else None,
    }
    
    if ORDER_TYPE == "limit_order":
        adj = _adjust_limit(side.lower(), price)
        if not adj:
            log_skip("missing_limit_price", {"symbol": symbol, "side": side, "size": size})
            return 400, {"error": "missing_limit_price"}
        body["limit_price"] = adj
    
    body.pop("product_id", None) if product_id is None else None
    status, resp = rest_post("/v2/orders", body)  # <-- v2 path
    
    log_event("order_submit", {"status": status, "resp": resp, "req": body})
    if status != 200:
        print(f"[{now_ist_iso()}] ORDER ERROR status={status} resp={resp}")
    else:
        print(f"[{now_ist_iso()}] Placed top-up: {side} {size} {symbol or product_id} ({ORDER_TYPE} @ {body.get('limit_price')})")
    
    # Optional: fallback to market if IOC limit couldn't fill
    if (status == 200 and isinstance(resp, dict)
        and (resp.get("result") or {}).get("state") == "cancelled"
        and (resp["result"].get("cancellation_reason") == "order_size_not_available_in_orderbook")
        and ORDER_TYPE == "limit_order" and TIF.lower() == "ioc"
        and LIMIT_IOC_FALLBACK_MARKET and not DRY_RUN):
        log_action("limit_ioc_cancel_fallback", {
            "symbol": symbol, "side": side, "size": size, "limit_price": body.get("limit_price")
        })
        print(f"[{now_ist_iso()}] IOC top-up order failed : {side} {size} {symbol or product_id} ({ORDER_TYPE} @ {body.get('limit_price')})")
        
        body2 = body.copy()
        body2["order_type"] = "market_order"
        body2["client_order_id"] = build_client_order_id()
        body2.pop("limit_price", None)
        status2, resp2 = rest_post("/v2/orders", body2)
        log_event("order_submit", {"status": status2, "resp": resp2, "req": body2})
        print(f"[{now_ist_iso()}] Placed market order : {side} {size} {symbol or product_id}")
        return status2, resp2
    
    return status, resp

# ===============================
# WebSocket: auth & event engine
# ===============================

# Work queue so REST calls don't block the WS thread
order_q: "queue.Queue[dict]" = queue.Queue(maxsize=1000)

# Track what's ours, and caps per symbol
our_client_prefix = SELF_TAG_PREFIX
session_topup_used: dict[str, int] = {}  # symbol->contracts we've added this session
seen_trade_ids = TTLCache(maxsize=TRADE_ID_MAX, ttl=TRADE_ID_TTL_SEC)

# de-dup usertrades
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

# --- send_auth (add a log before sending)
def send_auth(ws):
    method = "GET"
    timestamp = str(int(time.time()))
    path = "/live"
    signature = generate_signature(API_SECRET, method + timestamp + path)
    log_action("auth_send", {"timestamp": timestamp})
    ws.send(json.dumps({
        "type": "auth",
        "payload": {"api-key": API_KEY, "signature": signature, "timestamp": timestamp}
    }))

# --- subscribe (log each subscription)
def subscribe(ws, channel, symbols):
    log_action("subscribe", {"channel": channel, "symbols": symbols})
    ws.send(json.dumps({
        "type": "subscribe",
        "payload": {"channels": [{"name": channel, "symbols": symbols}]}
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

def _handle_sig(sig_num, _frame):
    log_action("signal", {"sig": int(sig_num)})
    shutdown(reason=f"signal_{int(sig_num)}")

# Linux/macOS
signal.signal(signal.SIGINT, _handle_sig)
try:
    signal.signal(signal.SIGTERM, _handle_sig)
except Exception:
    pass  # Windows may not have SIGTERM

# Windows consoles sometimes use SIGBREAK
if hasattr(signal, "SIGBREAK"):
    try:
        signal.signal(signal.SIGBREAK, _handle_sig)
    except Exception:
        pass


# --- handle_usertrade (augment with decision logs)
def handle_usertrade(ev: dict):
    fill_id = str(ev.get("fill_id") or "")
    if fill_id and fill_id in seen_fill_ids:
        log_skip("dup_fill_id", {"fill_id": fill_id})
        seen_fill_ids[fill_id] = True
        return
    if fill_id:
        seen_fill_ids[fill_id] = True
    trade_id = str(ev.get("id") or ev.get("trade_id") or "")
    audit_id = trade_id or ("ut_" + uuid.uuid4().hex[:8])
    if trade_id and trade_id in seen_trade_ids:
        log_skip("dup_trade_id", {"audit_id": audit_id})
        seen_trade_ids[trade_id] = True
        return
    if trade_id:
        seen_trade_ids[trade_id] = True

    client_order_id = ev.get("client_order_id") or ev.get("client_id") or ev.get("text")
    if _looks_like_ours(client_order_id, ev.get("text")):
        log_skip("own_fill", {"audit_id": audit_id, "client_order_id": client_order_id})
        return

    symbol = _extract_symbol(ev)
    if not _is_allowed_symbol(symbol):
        log_skip("symbol_not_allowed", {"audit_id": audit_id, "symbol": symbol})
        return

    side = _extract_side(ev)
    qty = ev.get("size") or ev.get("fill_size") or ev.get("quantity") or ev.get("filled_quantity")
    try:
        qty = int(qty)
    except Exception:
        log_skip("missing_or_invalid_qty", {"audit_id": audit_id})
        return

    add = compute_topup_size(qty)
    if add <= 0:
        log_skip("zero_topup", {"audit_id": audit_id, "qty": qty, "multiplier": USER_MULTIPLIER})
        return
    if not _cap_ok(symbol, add):
        log_skip("symbol_cap_exceeded", {"audit_id": audit_id, "symbol": symbol, "add": add})
        return

    product_id = _extract_product(ev)
    
    price = ev.get("price")
    
    job = {"audit_id": audit_id, "symbol": symbol, "product_id": product_id, "side": side, "size": add, "price": price}
    order_q.put(job)
    log_action("enqueue_topup", job)

# --- handle_order_update (same style)
def handle_order_update(ev: dict):
    fill_id = str(ev.get("fill_id") or "")
    if fill_id:
        if fill_id in seen_fill_ids:
            log_skip("dup_fill_id_order", {"fill_id": fill_id})
            seen_fill_ids[fill_id] = True
            return
        seen_fill_ids[fill_id] = True
    client_order_id = ev.get("client_order_id") or ev.get("client_id") or ev.get("text")
    if _looks_like_ours(client_order_id, ev.get("text")):
        log_skip("own_order_update", {"client_order_id": client_order_id})
        return

    oid = str(ev.get("id") or ev.get("order_id") or "")
    if not oid:
        log_skip("missing_order_id", {})
        return
    audit_id = "ord_" + oid
    
    state = (ev.get("state") or "").lower()
    unfilled = ev.get("unfilled_size")
    if state == "closed" or unfilled in (0, "0"):
        order_fill_cum.pop(oid, None)

    symbol = _extract_symbol(ev)
    if not _is_allowed_symbol(symbol):
        log_skip("symbol_not_allowed", {"audit_id": audit_id, "symbol": symbol})
        return

    cum = ev.get("filled_size") or ev.get("total_filled") or ev.get("cumulative_qty")
    try:
        cum = int(cum)
    except Exception:
        log_skip("missing_or_invalid_cum", {"audit_id": audit_id})
        return

    prev = order_fill_cum.get(oid, 0)
    if cum <= prev:
        order_fill_cum[oid] = cum
        log_skip("no_new_fill_delta", {"audit_id": audit_id, "cum": cum, "prev": prev})
        return

    delta = cum - prev
    order_fill_cum[oid] = cum

    side = _extract_side(ev)
    add = compute_topup_size(delta)
    if add <= 0:
        log_skip("zero_topup", {"audit_id": audit_id, "delta": delta})
        return
    if not _cap_ok(symbol, add):
        log_skip("symbol_cap_exceeded", {"audit_id": audit_id, "symbol": symbol, "add": add})
        return

    product_id = _extract_product(ev)
    
    price = ev.get("average_fill_price") or ev.get("price")
    
    job = {"audit_id": audit_id, "symbol": symbol, "product_id": product_id, "side": side, "size": add, "price": price}
    order_q.put(job)
    log_action("enqueue_topup", job)

def on_message(ws, message):
    global LAST_CONN_OK_AT
    try:
        msg = json.loads(message)
    except Exception:
        log_event("parse_error", message)
        return

    t = msg.get("type")
    if t == "success" and msg.get("message") == "Authenticated":
        LAST_CONN_OK_AT = _now()
        # Subscribe to private channels after auth
        subscribe(ws, "orders", ["all"])
        subscribe(ws, "positions", ["all"])
        # Subscribe to user_trades
        subscribe(ws, "user_trades", ["all"])
        print(f"[{now_ist_iso()}] Authenticated. Subscribed to orders/positions/usertrades.")
        log_event("subscriptions", {"ok": True})
        ws.send(json.dumps({"type": "enable_heartbeat"}))  # optional
        return

    if t == "heartbeat":
        LAST_CONN_OK_AT = _now()
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

# --- order_worker (log dequeue/result)
def order_worker():
    while not STOP_EVENT.is_set():
        job = order_q.get()
        if job is None:
            return
        log_action("dequeue_topup", job)

        symbol = job.get("symbol"); product_id = job.get("product_id")
        side = job.get("side"); size = int(job.get("size", 0))

        if size <= 0 or side not in ("buy", "sell"):
            log_skip("invalid_job", job)
            continue
        if not _cap_ok(symbol, size):
            log_skip("symbol_cap_exceeded_worker", {"symbol": symbol, "add": size, **job})
            continue

        status, resp = place_order_topup(symbol, product_id, side, size, price=job.get("price"))
        result = {"status": status, "resp": resp, **job}
        log_action("order_result", result)
        if status == 200:
            _bump_cap(symbol, size)
        else:
            # brief backoff on error, but also allow prompt shutdown
            for _ in range(5):
                if STOP_EVENT.is_set(): break
                time.sleep(0.25 + random.random() * 0.75)

# --- run_ws_forever (log backoff)
def run_ws_forever():
    global LAST_CONN_OK_AT
    backoff = BACKOFF_BASE
    while True:
        session_start = _now()
        ws = websocket.WebSocketApp(
            WEBSOCKET_URL, on_open=on_open, on_message=on_message,
            on_error=on_error, on_close=on_close
        )
        # expose current ws so shutdown() can close it
        global CURRENT_WS
        CURRENT_WS = ws
        sslopt = {"cert_reqs": ssl.CERT_NONE} if WS_INSECURE else {"cert_reqs": ssl.CERT_REQUIRED}
        ws.run_forever(ping_interval=PING_INTERVAL, ping_timeout=PING_TIMEOUT, sslopt=sslopt)
        CURRENT_WS = None
        
        if STOP_EVENT.is_set():
            break
        
        # Decide next backoff
        had_health = LAST_CONN_OK_AT >= session_start
        backoff = BACKOFF_BASE if had_health else min(backoff * 2.0, BACKOFF_MAX)
        wait = backoff * (1.0 + random.uniform(0.0, BACKOFF_JITTER))
        
        log_action("reconnect_wait", {"seconds": round(wait, 3), "had_health": had_health})
        print(f"[{now_ist_iso()}] Reconnecting in {wait:.2f}s…")
        for _ in range(int(wait * 10)):
            if STOP_EVENT.is_set(): break
            time.sleep(0.1)
        
# Graceful shutdown
def shutdown(reason="external"):
    # idempotent
    if SHUTDOWN_ONCE.is_set():
        return
    SHUTDOWN_ONCE.set()

    log_action("shutdown_start", {"reason": reason})
    STOP_EVENT.set()
    # wake the worker if it's blocked on .get()
    try:
        order_q.put_nowait(None)
    except Exception:
        pass

    # close the websocket so run_forever() returns
    try:
        if CURRENT_WS is not None:
            CURRENT_WS.close()             # triggers on_close -> run_forever returns
    except Exception as e:
        log_event("warn", f"ws_close_failed: {e}")

    log_action("shutdown_signal_sent", {})

if __name__ == "__main__":
    print(f"[{now_ist_iso()}] Starting Delta multiplier bot | multiplier={USER_MULTIPLIER} | DRY_RUN={DRY_RUN}")
    atexit.register(shutdown)
    worker = threading.Thread(target=order_worker, daemon=False)
    worker.start()
    try:
        run_ws_forever()
    finally:
        # ensure shutdown path if we fell out due to exception
        shutdown("finally")
        # give the worker up to 5s to finish current REST call and exit
        worker.join(timeout=5.0)
        log_action("shutdown_done", {})
        sys.exit(0)
