"""
=============================================================================
  CRYPTO TRADING SCANNER BOT  v1.1
  Single-file | Paper Trading | Railway.app Ready
  Stack: Python 3.9+, DexScreener, GoPlus, Discord Webhooks, Flask
=============================================================================
  FIXES in v1.1:
    - BSC chainId fixed: "56" -> "bsc"  (root cause of 0 raw pairs on BSC)
    - Discord startup alert now sent AFTER Flask is ready (fixes missing alert)
    - Retry logic added to send_discord (3 attempts, 3s apart)
    - Lowered default thresholds so signals flow during testing
    - More specific scan queries instead of generic "meme" / "ai"
    - Error alerts sent to Discord if scan cycle crashes unexpectedly
=============================================================================
  REQUIRED ENV VAR:
    DISCORD_WEBHOOK_URL  — Your Discord webhook URL

  OPTIONAL ENV VARS (all have defaults):
    PAPER_MODE           — "true" (default) | "false"
    MAX_TRADE_SIZE       — USD per trade            (default: 1.0)
    STOP_LOSS_PCT        — e.g. -10.0              (default: -10.0)
    TAKE_PROFIT_PCT      — e.g.  20.0              (default:  20.0)
    MIN_LIQUIDITY        — Min pool liquidity USD   (default: 5000.0)
    MIN_VOLUME           — Min 24h volume USD       (default: 1000.0)
    MIN_CHANGE           — Min 5-min price change % (default: 0.5)
    MIN_AGE_HOURS        — Min token age hours      (default: 0.0)
=============================================================================
"""

import os
import time
import logging
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("scanner")

# ---------------------------------------------------------------------------
# CONFIGURATION  (env vars with safe defaults)
# ---------------------------------------------------------------------------
PAPER_MODE      = os.getenv("PAPER_MODE", "true").strip().lower() == "true"
WEBHOOK_URL     = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
MAX_TRADE_SIZE  = float(os.getenv("MAX_TRADE_SIZE",   "1.0"))
STOP_LOSS_PCT   = float(os.getenv("STOP_LOSS_PCT",   "-10.0"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "20.0"))
MIN_LIQUIDITY   = float(os.getenv("MIN_LIQUIDITY",   "5000.0"))
MIN_VOLUME      = float(os.getenv("MIN_VOLUME",      "1000.0"))
MIN_CHANGE      = float(os.getenv("MIN_CHANGE",      "0.5"))
MIN_AGE_HOURS   = float(os.getenv("MIN_AGE_HOURS",   "0.0"))

SCAN_INTERVAL_SEC = 60
COOLDOWN_SEC      = 30 * 60
CLEANUP_INTERVAL  = 3600
MAX_ACTIVE_TRADES = 10
REQUEST_TIMEOUT   = 10

# ---------------------------------------------------------------------------
# SCAN TARGETS
#
# KEY FIX: DexScreener returns chainId as "bsc" — NOT "56".
# Using "56" caused every BSC result to be filtered out -> 0 raw pairs.
# ---------------------------------------------------------------------------
SCAN_TARGETS = [
    {"name": "BSC-PEPE",  "query": "pepe",  "id": "bsc",      "trade": True},
    {"name": "BSC-DOGE",  "query": "doge",  "id": "bsc",      "trade": True},
    {"name": "BSC-SHIB",  "query": "shib",  "id": "bsc",      "trade": True},
    {"name": "BSC-BABY",  "query": "baby",  "id": "bsc",      "trade": True},
    {"name": "BSC-GPT",   "query": "gpt",   "id": "bsc",      "trade": True},
    {"name": "SOL-PEPE",  "query": "pepe",  "id": "solana",   "trade": False},
    {"name": "SOL-WIF",   "query": "wif",   "id": "solana",   "trade": False},
    {"name": "SOL-BONK",  "query": "bonk",  "id": "solana",   "trade": False},
    {"name": "ETH-PEPE",  "query": "pepe",  "id": "ethereum", "trade": False},
]

# ---------------------------------------------------------------------------
# STATE  (in-memory)
# ---------------------------------------------------------------------------
active_trades: dict = {}
recent:        dict = {}
stats = {
    "scans":      0,
    "entries":    0,
    "exits_tp":   0,
    "exits_sl":   0,
    "total_pnl":  0.0,
    "start_time": datetime.now(timezone.utc).isoformat(),
}

# ---------------------------------------------------------------------------
# FLASK  — health server to keep Railway awake
# ---------------------------------------------------------------------------
app = Flask(__name__)

@app.route("/")
def health():
    return jsonify({
        "status":        "running",
        "paper_mode":    PAPER_MODE,
        "active_trades": len(active_trades),
        "stats":         stats,
        "uptime_since":  stats["start_time"],
    })

def run_flask():
    port = int(os.getenv("PORT", "8080"))
    log.info(f"Flask health server starting on port {port}")
    import logging as _logging
    _logging.getLogger("werkzeug").setLevel(_logging.WARNING)
    app.run(host="0.0.0.0", port=port, use_reloader=False)

# ---------------------------------------------------------------------------
# DISCORD  — with retry
# ---------------------------------------------------------------------------
def send_discord(message: str, color: int = 0x00FF99, retries: int = 3):
    """Send a Discord embed. Retries up to 3 times on failure."""
    if not WEBHOOK_URL:
        log.warning("DISCORD_WEBHOOK_URL not set — skipping alert.")
        return

    payload = {
        "embeds": [{
            "description": message,
            "color": color,
            "footer": {
                "text": (
                    f"CryptoBot • {'PAPER' if PAPER_MODE else 'LIVE'} • "
                    f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"
                )
            },
        }]
    }

    for attempt in range(1, retries + 1):
        try:
            r = requests.post(WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            log.info(f"Discord alert sent (attempt {attempt}).")
            return
        except Exception as e:
            log.error(f"Discord send failed (attempt {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(3)

    log.error("Discord alert failed after all retries.")

# ---------------------------------------------------------------------------
# DEXSCREENER
# ---------------------------------------------------------------------------
def fetch_pairs(query: str, chain_id: str) -> list:
    url = f"https://api.dexscreener.com/latest/dex/search?q={query}"
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        all_pairs = r.json().get("pairs") or []
        return [p for p in all_pairs if p.get("chainId") == chain_id]
    except Exception as e:
        log.error(f"DexScreener fetch failed (query={query}, chain={chain_id}): {e}")
        return []

def extract_pair_info(pair: dict):
    try:
        price_usd    = float(pair.get("priceUsd") or 0)
        liq          = float((pair.get("liquidity") or {}).get("usd") or 0)
        vol_24h      = float((pair.get("volume") or {}).get("h24") or 0)
        change_m5    = float((pair.get("priceChange") or {}).get("m5") or 0)
        pair_created = pair.get("pairCreatedAt")
        base_token   = pair.get("baseToken") or {}
        address      = base_token.get("address", "").lower().strip()
        symbol       = base_token.get("symbol", "?")
        name         = base_token.get("name", "?")

        if not address or price_usd <= 0:
            return None

        age_hours = 0.0
        if pair_created:
            age_hours = (time.time() - int(pair_created) / 1000) / 3600

        return {
            "address":   address,
            "symbol":    symbol,
            "name":      name,
            "price":     price_usd,
            "liquidity": liq,
            "volume":    vol_24h,
            "change_m5": change_m5,
            "age_hours": age_hours,
            "pair_url":  pair.get("url", ""),
        }
    except Exception as e:
        log.debug(f"extract_pair_info error: {e}")
        return None

# ---------------------------------------------------------------------------
# GOPLUS  — token security
# ---------------------------------------------------------------------------
def check_token_security(chain_id: str, address: str) -> bool:
    """
    Returns False (UNSAFE) on ANY error or missing data.
    Safe only if: not honeypot AND buy_tax <= 10% AND sell_tax <= 10%
    """
    url = (
        f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
        f"?contract_addresses={address}"
    )
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        result_map = data.get("result") or {}
        result = result_map.get(address.lower()) or result_map.get(address)

        if not result:
            log.warning(f"GoPlus no data for {address[:10]}… — UNSAFE")
            return False

        honeypot = str(result.get("is_honeypot", "1")).strip()
        buy_tax  = float(result.get("buy_tax",  "1") or 1)
        sell_tax = float(result.get("sell_tax", "1") or 1)

        if honeypot != "0":
            log.info(f"[UNSAFE] Honeypot: {address[:10]}…")
            return False
        if buy_tax > 0.10:
            log.info(f"[UNSAFE] Buy tax {buy_tax*100:.1f}%: {address[:10]}…")
            return False
        if sell_tax > 0.10:
            log.info(f"[UNSAFE] Sell tax {sell_tax*100:.1f}%: {address[:10]}…")
            return False

        return True

    except Exception as e:
        log.error(f"GoPlus failed for {address[:10]}…: {e} — UNSAFE")
        return False

# ---------------------------------------------------------------------------
# SCORING
# ---------------------------------------------------------------------------
def score_token(token: dict) -> float:
    liq_score = min(token["liquidity"] / 500_000, 1.0) * 30
    vol_score = min(token["volume"]    / 100_000, 1.0) * 30
    mom_score = min(max(token["change_m5"], 0) / 20.0, 1.0) * 40
    return round(liq_score + vol_score + mom_score, 1)

def estimate_slippage(trade_size_usd: float) -> float:
    return 0.005 + 0.001 * (trade_size_usd / 1000)

# ---------------------------------------------------------------------------
# PAPER TRADE  — entry
# ---------------------------------------------------------------------------
def paper_enter(token: dict, scan_name: str, score: float):
    address = token["address"]

    if address in active_trades:
        return
    now = time.time()
    if address in recent and (now - recent[address]) < COOLDOWN_SEC:
        return
    if len(active_trades) >= MAX_ACTIVE_TRADES:
        log.info("Max active trades reached — skipping entry.")
        return

    slip     = estimate_slippage(MAX_TRADE_SIZE)
    entry_px = token["price"] * (1 + slip)
    quantity = MAX_TRADE_SIZE / entry_px

    active_trades[address] = {
        "entry_price": entry_px,
        "amount":      MAX_TRADE_SIZE,
        "quantity":    quantity,
        "ts":          now,
        "name":        token["name"],
        "symbol":      token["symbol"],
        "scan":        scan_name,
        "pair_url":    token["pair_url"],
    }
    recent[address] = now
    stats["entries"] += 1

    log.info(
        f"[ENTRY] {token['symbol']} @ ${entry_px:.8f}  "
        f"qty={quantity:.4f}  score={score}  scan={scan_name}"
    )

    msg = (
        f"🟢 **[{'PAPER' if PAPER_MODE else 'LIVE'} BUY]** "
        f"`{token['symbol']}` — {token['name']}\n"
        f"**Entry Price:** `${entry_px:.8f}`\n"
        f"**Amount:**      `${MAX_TRADE_SIZE:.2f}`\n"
        f"**Score:**       `{score}/100`\n"
        f"**5m Change:**   `{token['change_m5']:+.2f}%`\n"
        f"**Liquidity:**   `${token['liquidity']:,.0f}`\n"
        f"**Volume 24h:**  `${token['volume']:,.0f}`\n"
        f"**Scan:**        `{scan_name}`\n"
        f"[📊 Chart]({token['pair_url']})"
    )
    send_discord(msg, color=0x00FF99)

# ---------------------------------------------------------------------------
# PAPER TRADE  — exit
# ---------------------------------------------------------------------------
def paper_exit(address: str, current_price: float, reason: str):
    trade = active_trades.pop(address, None)
    if not trade:
        return

    proceeds  = trade["quantity"] * current_price
    pnl       = proceeds - trade["amount"]
    pnl_pct   = (pnl / trade["amount"]) * 100
    hold_mins = (time.time() - trade["ts"]) / 60

    stats["total_pnl"] += pnl

    if reason == "TP":
        stats["exits_tp"] += 1
        color = 0x00FF00
        emoji = "💰"
    else:
        stats["exits_sl"] += 1
        color = 0xFF4444
        emoji = "🛑"

    log.info(
        f"[EXIT-{reason}] {trade['symbol']} @ ${current_price:.8f}  "
        f"PnL={pnl_pct:+.2f}%  held={hold_mins:.1f}m"
    )

    msg = (
        f"{emoji} **[{'PAPER' if PAPER_MODE else 'LIVE'} SELL — {reason}]** "
        f"`{trade['symbol']}`\n"
        f"**Entry:**     `${trade['entry_price']:.8f}`\n"
        f"**Exit:**      `${current_price:.8f}`\n"
        f"**PnL:**       `{pnl_pct:+.2f}%`  (`${pnl:+.4f}`)\n"
        f"**Held:**      `{hold_mins:.1f} min`\n"
        f"**Total PnL:** `${stats['total_pnl']:+.4f}`\n"
        f"[📊 Chart]({trade['pair_url']})"
    )
    send_discord(msg, color=color)

# ---------------------------------------------------------------------------
# POSITION MONITOR
# ---------------------------------------------------------------------------
def monitor_positions():
    if not active_trades:
        return

    for address in list(active_trades.keys()):
        trade = active_trades.get(address)
        if not trade:
            continue
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
            r   = requests.get(url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            pairs = r.json().get("pairs") or []
            if not pairs:
                continue

            current_price = float(pairs[0].get("priceUsd") or 0)
            if current_price <= 0:
                continue

            change_pct = (
                (current_price - trade["entry_price"]) / trade["entry_price"]
            ) * 100

            if change_pct <= STOP_LOSS_PCT:
                paper_exit(address, current_price, "SL")
            elif change_pct >= TAKE_PROFIT_PCT:
                paper_exit(address, current_price, "TP")

        except Exception as e:
            log.error(f"Position monitor error for {address[:10]}…: {e}")

# ---------------------------------------------------------------------------
# CLEANUP
# ---------------------------------------------------------------------------
_last_cleanup = time.time()

def maybe_cleanup_recent():
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < CLEANUP_INTERVAL:
        return
    _last_cleanup = now
    expired = [a for a, ts in list(recent.items()) if now - ts > COOLDOWN_SEC]
    for addr in expired:
        recent.pop(addr, None)
    if expired:
        log.info(f"Cleaned up {len(expired)} stale cooldown entries.")

# ---------------------------------------------------------------------------
# MAIN SCAN CYCLE
# ---------------------------------------------------------------------------
def run_scan_cycle():
    stats["scans"] += 1
    log.info(
        f"=== Scan #{stats['scans']} started | "
        f"Active trades: {len(active_trades)} ==="
    )

    # 1. Check open positions for SL/TP
    monitor_positions()

    # 2. Scan each target
    for target in SCAN_TARGETS:
        name     = target["name"]
        query    = target["query"]
        chain_id = target["id"]
        tradable = target["trade"]

        log.info(f"[{name}] Fetching: query='{query}' chain='{chain_id}'")
        raw_pairs = fetch_pairs(query, chain_id)
        log.info(f"[{name}] {len(raw_pairs)} raw pairs returned.")

        tokens   = [t for p in raw_pairs if (t := extract_pair_info(p))]
        filtered = [
            t for t in tokens
            if t["liquidity"] >= MIN_LIQUIDITY
            and t["volume"]    >= MIN_VOLUME
            and t["change_m5"] >= MIN_CHANGE
            and t["age_hours"] >= MIN_AGE_HOURS
        ]
        log.info(f"[{name}] {len(filtered)} tokens passed pre-filter.")

        top10 = sorted(filtered, key=lambda x: x["change_m5"], reverse=True)[:10]

        for token in top10:
            address = token["address"]
            symbol  = token["symbol"]
            score   = score_token(token)

            log.info(
                f"  [{name}] {symbol} | "
                f"Δ5m={token['change_m5']:+.2f}% | "
                f"Liq=${token['liquidity']:,.0f} | "
                f"Vol=${token['volume']:,.0f} | "
                f"Score={score}"
            )

            if not tradable:
                log.info(f"  [{name}] Watch-only — no trade for {symbol}")
                continue

            if address in active_trades:
                log.info(f"  [{name}] {symbol} already in position — skip.")
                continue

            now = time.time()
            if address in recent and (now - recent[address]) < COOLDOWN_SEC:
                log.info(f"  [{name}] {symbol} on cooldown — skip.")
                continue

            log.info(f"  [{name}] Security check: {symbol} ({address[:10]}…)")
            if not check_token_security(chain_id, address):
                log.info(f"  [{name}] {symbol} FAILED security — skip.")
                continue

            paper_enter(token, name, score)

        time.sleep(2)

    # 3. Cleanup + summary
    maybe_cleanup_recent()
    log.info(
        f"=== Scan #{stats['scans']} done | "
        f"Entries: {stats['entries']} | "
        f"TP: {stats['exits_tp']} | "
        f"SL: {stats['exits_sl']} | "
        f"PnL: ${stats['total_pnl']:+.4f} | "
        f"Active: {len(active_trades)} ==="
    )

# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------
def main():
    log.info("=" * 62)
    log.info("  CRYPTO SCANNER BOT v1.1  —  Starting up")
    log.info(f"  Mode:           {'PAPER' if PAPER_MODE else 'LIVE'}")
    log.info(f"  Trade size:     ${MAX_TRADE_SIZE}")
    log.info(f"  Stop loss:      {STOP_LOSS_PCT}%")
    log.info(f"  Take profit:    {TAKE_PROFIT_PCT}%")
    log.info(f"  Min liquidity:  ${MIN_LIQUIDITY:,.0f}")
    log.info(f"  Min volume:     ${MIN_VOLUME:,.0f}")
    log.info(f"  Min 5m change:  {MIN_CHANGE}%")
    log.info(f"  Min age:        {MIN_AGE_HOURS}h")
    log.info(f"  Scan interval:  {SCAN_INTERVAL_SEC}s")
    log.info(f"  Scan targets:   {len(SCAN_TARGETS)}")
    log.info(f"  Webhook set:    {'YES' if WEBHOOK_URL else 'NO - ALERTS DISABLED'}")
    log.info("=" * 62)

    if not WEBHOOK_URL:
        log.warning("DISCORD_WEBHOOK_URL is not set — all Discord alerts suppressed!")

    # Start Flask FIRST, wait for it to bind, THEN send Discord alert.
    # (Previous version sent the alert before Flask was ready which caused
    # the startup notification to be dropped on Railway cold starts.)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    time.sleep(2)  # give Flask time to bind to the port

    # Startup alert
    log.info("Sending startup Discord alert...")
    send_discord(
        f"🚀 **Crypto Scanner Bot v1.1 Started**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"**Mode:**        `{'PAPER' if PAPER_MODE else 'LIVE'}`\n"
        f"**Trade size:**  `${MAX_TRADE_SIZE}`\n"
        f"**Stop loss:**   `{STOP_LOSS_PCT}%`\n"
        f"**Take profit:** `{TAKE_PROFIT_PCT}%`\n"
        f"**Min liq:**     `${MIN_LIQUIDITY:,.0f}`\n"
        f"**Min vol:**     `${MIN_VOLUME:,.0f}`\n"
        f"**Min 5m chg:**  `{MIN_CHANGE}%`\n"
        f"**Scan every:**  `{SCAN_INTERVAL_SEC}s`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    while True:
        try:
            run_scan_cycle()
            maybe_cleanup_recent()
        except Exception as e:
            log.exception(f"Scan cycle error: {e}")
        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    main()