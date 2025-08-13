# main.py
"""
Thin CLI wrapper for websocket_orders_positions.py

- Sets env vars from CLI flags BEFORE importing the bot module
- Starts the order worker + websocket loop
- Cleanly shuts down on exit
"""

import os
import argparse
import atexit
import threading

def parse_args():
    p = argparse.ArgumentParser(description="Delta Exchange Copy-Trade Multiplier Bot")
    # Connection
    p.add_argument("--ws-url", dest="DELTA_WS_URL")
    p.add_argument("--api-base", dest="DELTA_API_BASE")
    p.add_argument("--ws-insecure", dest="WS_INSECURE", action="store_true")

    # Auth (prefer .env; these flags just override)
    p.add_argument("--api-key", dest="DELTA_API_KEY")
    p.add_argument("--api-secret", dest="DELTA_API_SECRET")

    # Behavior
    p.add_argument("--multiplier", type=float, dest="USER_MULTIPLIER")
    p.add_argument("--dry-run", dest="DRY_RUN", action="store_true")
    p.add_argument("--allow-symbols", dest="ALLOW_SYMBOLS",
                   help='CSV list like "BTCUSD,ETHUSD" or "ALL"')
    p.add_argument("--order-type", dest="ORDER_TYPE", choices=["market_order", "limit_order"])
    p.add_argument("--tif", dest="TIME_IN_FORCE", choices=["IOC", "GTC", "FOK", "ioc", "gtc", "fok"])
    p.add_argument("--limit-slippage-bps", type=float, dest="LIMIT_SLIPPAGE_BPS")
    p.add_argument("--limit-ioc-fallback-market", dest="LIMIT_IOC_FALLBACK_MARKET", action="store_true")

    # Risk controls
    p.add_argument("--max-topup-per-trade", type=int, dest="MAX_TOPUP_PER_TRADE")
    p.add_argument("--max-topup-per-symbol", type=int, dest="MAX_TOPUP_PER_SYMBOL")

    # Reliability / logs
    p.add_argument("--ping-interval", type=int, dest="PING_INTERVAL")
    p.add_argument("--ping-timeout", type=int, dest="PING_TIMEOUT")
    p.add_argument("--log-dir", dest="LOG_DIR")
    p.add_argument("--verbose-decisions", dest="VERBOSE_DECISIONS", action="store_true")
    p.add_argument("--user-agent", dest="USER_AGENT")

    # HTTP
    p.add_argument("--http-timeout", type=float, dest="HTTP_TIMEOUT")
    p.add_argument("--http-conn-timeout", type=float, dest="HTTP_CONN_TIMEOUT")
    p.add_argument("--http-retries", type=int, dest="HTTP_RETRIES")

    # Backoff
    p.add_argument("--backoff-base", type=float, dest="BACKOFF_BASE")
    p.add_argument("--backoff-max", type=float, dest="BACKOFF_MAX")
    p.add_argument("--backoff-jitter", type=float, dest="BACKOFF_JITTER")

    return p.parse_args()

def set_env_from_args(ns: argparse.Namespace):
    # Only set env vars for args the user actually passed (not None / not False flags)
    for k, v in vars(ns).items():
        if v is None:
            continue
        # booleans -> "true"/"false" for consistency with the bot
        if isinstance(v, bool):
            os.environ[k] = "true" if v else "false"
        else:
            os.environ[k] = str(v)

def main():
    args = parse_args()
    set_env_from_args(args)

    # Import AFTER env overrides so the bot reads them at import time
    import websocket_orders_positions as bot  # noqa

    print(f"[{bot.now_ist_iso()}] Starting Delta multiplier bot | "
          f"multiplier={bot.USER_MULTIPLIER} | DRY_RUN={bot.DRY_RUN}")

    atexit.register(bot.shutdown)

    worker = threading.Thread(target=bot.order_worker, daemon=False)
    worker.start()
    try:
        bot.run_ws_forever()
    finally:
        bot.shutdown("finally")
        worker.join(timeout=5.0)
        bot.log_action("shutdown_done", {})
        # exit code managed by caller

if __name__ == "__main__":
    main()
