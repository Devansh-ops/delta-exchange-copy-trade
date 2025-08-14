# Delta Exchange Copy-Trade Multiplier Bot

A production‑minded Python bot that listens to your **private Delta Exchange** websocket streams and **automatically tops up** fills (copy/mirror) using a configurable multiplier.  
It can place **market** or **IOC limit** orders, has backoff/retries, idempotent de‑dup on fills, per‑symbol caps, and structured JSONL logs in IST.

> ⚠️ Use at your own risk. Test in `DRY_RUN=true` first.

---

## Features

- Authenticated WS (orders, positions, user_trades) with heartbeat & reconnect backoff  
- Fill de‑dup (by `fill_id` and `trade_id`) + per‑order cumulative tracking fallback  
- Multiplier‑based top‑ups with per‑trade and per‑symbol caps  
- Market or limit‑IOC with optional **market fallback** on insufficient liquidity  
- Structured logs as JSONL with IST timestamps  
- Clean shutdown via signals (Ctrl+C, SIGTERM)  

---

## Project layout

```

.
├─ .env                     # your secrets (NEVER COMMIT)
├─ .gitignore
├─ pyproject.toml           # uv / pip build metadata
├─ uv.lock
├─ main.py                  # thin entry (calls the bot)
├─ websocket\_orders\_positions.py  # the bot
└─ logs/                    # runtime JSONL logs (git‑ignored)

````

---

## Docker

You can either pull the **prebuilt image** or **build locally** via the Dockerfile 

Image Name: `devanshsehgal02/delta-copytrader:latest`

`docker-compose.yml`

Option A — Use the image directly with docker
```bash
# pull the image
docker pull devanshsehgal02/delta-copytrader:latest

# run it (uses your .env and persists logs)
docker run -d --name delta-multiplier \
  --env-file .env \
  -v "$(pwd)/logs:/app/logs" \
  --restart unless-stopped \
  devanshsehgal02/delta-copytrader:latest
```

Option B - Use docker compose

- Make sure that the `.env` file in the same folder with `docker-compose.yml` file

```bash
services:
  delta-copytrader:
    image: devanshsehgal02/delta-copytrader:latest
    container_name: delta-copytrader
    pull_policy: always       # always pull the latest on start
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - ./logs:/app/logs
    stop_grace_period: 10s    # let the bot shut down cleanly
```

---

## Quick start

### 1) Prereqs
- Python 3.12  
- [uv](https://docs.astral.sh/uv/) or pip

### 2) Install
```bash
# using uv
uv sync
# OR: pip install -r <generated requirements> (if you prefer)
````

### 3) Configure

Create **.env** (or copy from `.env.example` below):

```ini
DELTA_WS_URL=wss://socket.india.delta.exchange
DELTA_API_BASE=https://api.india.delta.exchange
DELTA_API_KEY=YOUR_KEY
DELTA_API_SECRET=YOUR_SECRET

# Behavior
USER_MULTIPLIER=2.0             # e.g. 2.0 means "match + 1x top-up"
DRY_RUN=true                    # start true to test
ALLOW_SYMBOLS=ALL               # or CSV like: BTCUSDT,ETHUSDT
ORDER_TYPE=market_order         # or limit_order
TIME_IN_FORCE=IOC               # IOC recommended for limit top-ups
LIMIT_SLIPPAGE_BPS=0            # e.g. 1.5 means 1.5 bps; 0 disables
LIMIT_IOC_FALLBACK_MARKET=true  # fallback to market on insufficient size

# Risk controls
MAX_TOPUP_PER_TRADE=1000000
MAX_TOPUP_PER_SYMBOL=10000000

# Reliability / logging
PING_INTERVAL=30
PING_TIMEOUT=5
LOG_DIR=logs
VERBOSE_DECISIONS=true
USER_AGENT=python-rest-client

# HTTP
HTTP_TIMEOUT=10
HTTP_CONN_TIMEOUT=3.05
HTTP_RETRIES=3
```

### 4) Run

```bash
# uv
uv run python main.py
# or directly:
uv run websocket_orders_positions.py
```

Windows (PowerShell):

```powershell
uv run python .\main.py
```

---

## How it works (high level)

* On start, the bot authenticates to the WS feed and subscribes to **orders**, **positions**, and **user\_trades**.
* When a **new fill** (not seen before) is detected:

  * It computes a **top‑up size** = `(USER_MULTIPLIER - 1) * filled_qty`, clipped by `MAX_TOPUP_PER_TRADE` and per‑symbol cap.
  * Places a **market** order (default) or **limit IOC** with optional **market fallback** if the IOC limit can’t fill.
* All events/actions are written to daily JSONL under `logs/` with IST timestamps.

---

## Important environment flags

* `DRY_RUN=true` — logs every decision and the *request* that would be sent, but **does not** hit the order API.
* `ALLOW_SYMBOLS` — `ALL` or a CSV (e.g., `BTCUSDT,ETHUSDT`) to whitelist instruments.
* `ORDER_TYPE` + `TIME_IN_FORCE` — `market_order` (simple) or `limit_order` with `IOC`. For limits, `LIMIT_SLIPPAGE_BPS` lets you nudge price.
* `LIMIT_IOC_FALLBACK_MARKET` — if IOC is cancelled due to insufficient book size, place a market order instead.

---

## Logs

* Per‑day JSONL file at `logs/delta_ws_events_YYYY-MM-DD.jsonl`
* Each line: `{"ts": "...IST...", "type": "...", "data": {...}}`
* Useful `type`s: `open`, `auth_send`, `subscriptions`, `message`, `enqueue_topup`, `order_submit`, `order_result`, `skip:<reason>`, etc.

---

## Safety & ops tips

* Start with `DRY_RUN=true`. Watch `logs/` to confirm decisions look right.
* Use **small** `USER_MULTIPLIER` and **tight caps** initially.
* Consider IP whitelisting on your API key if the exchange supports it.
* Run under a supervisor (systemd, pm2, Docker) for auto‑restart; the bot already backs off and reconnects.

---

## Troubleshooting

* **No fills mirrored**: Check `ALLOW_SYMBOLS`, dedup (`fill_id`/`trade_id`), and `USER_MULTIPLIER` > 1.
* **IOC limit cancels**: Increase `LIMIT_SLIPPAGE_BPS` or enable `LIMIT_IOC_FALLBACK_MARKET=true`.
* **403 / auth errors**: Recheck clock sync, API key/secret, and base URLs.
* **High spam logs**: Set `VERBOSE_DECISIONS=false`.

---
