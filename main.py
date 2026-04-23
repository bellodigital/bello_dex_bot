"""
=============================================================================
  CRYPTO TRADING SCANNER BOT
  Single-file | Paper Trading | Railway.app Ready
  Stack: Python 3.9+, DexScreener, GoPlus, Discord Webhooks, Flask
=============================================================================
  SETUP (Environment Variables):
    DISCORD_WEBHOOK_URL  - Required. Your Discord webhook URL.
    PAPER_MODE           - "true" (default) / "false"
    MAX_TRADE_SIZE       - USD per trade (default: 1.0)
    STOP_LOSS_PCT        - e.g. -10.0  (default: -10.0)
    TAKE_PROFIT_PCT      - e.g.  20.0  (default: 20.0)
    MIN_LIQUIDITY        - Minimum pool liquidity in USD (default: 10000)
    MIN_VOLUME           - Minimum 24h volume in USD   (default: 5000)
    MIN_CHANGE           - Minimum 5-min price change % (default: 1.0)
    MIN_AGE_HOURS        - Minimum token age in hours  (default: 0.0)
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
# CONFIGURATION  (all from env vars with safe defaults)
# ---------------------------------------------------------------------------
PAPER_MODE       = os.getenv("PAPER_MODE", "true").strip().lower() == "true"
WEBHOOK_URL      = os.getenv("DISCORD_WEBHOOK_URL", "")
MAX_TRADE_SIZE   = float(os.getenv("MAX_TRADE_SIZE",    "1.0"))
STOP_LOSS_PCT    = float(os.getenv("STOP_LOSS_PCT",    "-10.0"))
TAKE_PROFIT_PCT  = float(os.getenv("TAKE_PROFIT_PCT",  "20.0"))
MIN_LIQUIDITY    = float(os.getenv("MIN_LIQUIDITY",    "10000.0"))
MIN_VOLUME       = float(os.getenv("MIN_VOLUME",       "5000.0"))
MIN_CHANGE       = float(os.getenv("MIN_CHANGE",       "1.0"))
MIN_AGE_HOURS    = float(os.getenv("MIN_AGE_HOURS",    "0.0"))

SCAN_INTERVAL_SEC  = 60          # seconds between full scan cycles
COOLDOWN_SEC       = 30 * 60     # 30 minutes re-buy cooldown per token
CLEANUP_INTERVAL   = 3600        # clean up `recent` dict every 1 hour
MAX_ACTIVE_TRADES  = 10          # cap simultaneous paper positions
REQUEST_TIMEOUT    = 10          # seconds for all HTTP calls

# ---------------------------------------------------------------------------
# SEARCH TERMS  — "Pro Volatility" strategy
#   name   : label used in logs / alerts
#   query  : keyword sent to DexScreener search
#   id     : chainId filter ("56" = BSC, "solana" = Solana, etc.)
#   trade  : whether to attempt entry (False = watch-only)
# ---------------------------------------------------------------------------
SCAN_TARGETS = [
    {"name": "BSC-MEME",  "query": "meme", "id": "56",      "trade": True},
    {"name": "BSC-AI",    "query": "ai",   "id": "56",      "trade": True},
    {"name": "SOL-MEME",  "query": "meme", "id": "solana",  "trade": False},
]

# ---------------------------------------------------------------------------
# STATE  (in-memory — resets on restart, fine for paper trading)
# ---------------------------------------------------------------------------
active_trades: dict = {}   # {tokenAddress: {entry_price, amount, quantity, ts, name, symbol}}
recent:        dict = {}   # {tokenAddress: timestamp}  — cooldown tracker
stats = {
    "scans":        0,
    "entries":      0,
    "exits_tp":     0,
    "exits_sl":     0,
    "total_pnl":    0.0,
    "start_time":   datetime.now(timezone.utc).isoformat(),
}

# ---------------------------------------------------------------------------
# FLASK  — keeps Railway free tier alive + provides a health endpoint
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
    """Run Flask in a background daemon thread."""
    port = int(os.getenv("PORT", "8080"))
    log.info(f"Flask health server starting on port {port}")
    app.run(host="0.0.0.0", port=port, use_reloader=False)

# ---------------------------------------------------------------------------
# DISCORD ALERTS
# ---------------------------------------------------------------------------
def send_discord(message: str, color: int = 0x00ff99):
    """Send an embed message to the configured Discord webhook."""
    if not WEBHOOK_URL:
        log.warning("DISCORD_WEBHOOK_URL not set — skipping alert.")
        return
    payload = {
        "embeds": [{
            "description": message,
            "color": color,
            "footer": {"text": f"CryptoBot • {'PAPER' if PAPER_MODE else 'LIVE'} • {datetime.utcnow().strftime('%H:%M:%S UTC')}"},
        }]
    }
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        log.error(f"Discord send failed: {e}")

# ---------------------------------------------------------------------------
# DEXSCREENER  — market data
# ---------------------------------------------------------------------------
def fetch_pairs(query: str, chain_id: str) -> list:
    """
    Search DexScreener for `query` and return pairs that belong to `chain_id`.
    Returns an empty list on any failure (never raises).
    """
    url = f"https://api.dexscreener.com/latest/dex/search?q={query}"
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        all_pairs = data.get("pairs") or []
        # Filter to the requested chain
        return [p for p in all_pairs if p.get("chainId") == chain_id]
    except Exception as e:
        log.error(f"DexScreener fetch failed (query={query}, chain={chain_id}): {e}")
        return []

def extract_pair_info(pair: dict) -> dict | None:
    """
    Pull out the fields we care about from a raw DexScreener pair object.
    Returns None if essential fields are missing.
    """
    try:
        price_usd   = float(pair.get("priceUsd") or 0)
        liq         = float((pair.get("liquidity") or {}).get("usd") or 0)
        vol_24h     = float((pair.get("volume") or {}).get("h24") or 0)
        change_m5   = float((pair.get("priceChange") or {}).get("m5") or 0)
        pair_created = pair.get("pairCreatedAt")  # epoch ms or None
        base_token  = pair.get("baseToken") or {}
        address     = base_token.get("address", "")
        symbol      = base_token.get("symbol", "?")
        name        = base_token.get("name", "?")

        if not address or price_usd <= 0:
            return None

        # Token age in hours
        age_hours = 0.0
        if pair_created:
            age_hours = (time.time() - pair_created / 1000) / 3600

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
# GOPLUS  — security checks
# ---------------------------------------------------------------------------
def check_token_security(chain_id: str, address: str) -> bool:
    """
    Query GoPlus for token safety.
    CRITICAL: Returns False (unsafe) on ANY error / unexpected response.
    Safe only if:
      - is_honeypot == "0"
      - buy_tax  <= 10 %
      - sell_tax <= 10 %
    """
    url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={address}"
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()

        result = (data.get("result") or {}).get(address.lower()) or \
                 (data.get("result") or {}).get(address)
        if not result:
            log.warning(f"GoPlus returned no data for {address} — marking UNSAFE")
            return False

        honeypot  = result.get("is_honeypot", "1")
        buy_tax   = float(result.get("buy_tax",  "1") or 1)
        sell_tax  = float(result.get("sell_tax", "1") or 1)

        if honeypot != "0":
            log.info(f"[UNSAFE] Honeypot detected: {address}")
            return False
        if buy_tax > 0.10:
            log.info(f"[UNSAFE] Buy tax {buy_tax*100:.1f}% > 10% for {address}")
            return False
        if sell_tax > 0.10:
            log.info(f"[UNSAFE] Sell tax {sell_tax*100:.1f}% > 10% for {address}")
            return False

        return True

    except Exception as e:
        log.error(f"GoPlus check failed for {address}: {e} — marking UNSAFE")
        return False

# ---------------------------------------------------------------------------
# SCORING  (0–100, higher = better opportunity)
# ---------------------------------------------------------------------------
def score_token(token: dict) -> float:
    """
    Simple composite score:
      - Liquidity component  (0–30)
      - Volume component     (0–30)
      - Momentum component   (0–40)
    """
    # Liquidity: max out at $500k
    liq_score = min(token["liquidity"] / 500_000, 1.0) * 30

    # Volume: max out at $100k
    vol_score = min(token["volume"] / 100_000, 1.0) * 30

    # Momentum: max out at 20% 5-min change
    mom_score = min(max(token["change_m5"], 0) / 20.0, 1.0) * 40

    return round(liq_score + vol_score + mom_score, 1)

# ---------------------------------------------------------------------------
# SLIPPAGE ESTIMATE
# ---------------------------------------------------------------------------
def estimate_slippage(trade_size_usd: float) -> float:
    """0.5% base + 0.1% per $1,000 of trade size."""
    return 0.005 + 0.001 * (trade_size_usd / 1000)

# ---------------------------------------------------------------------------
# PAPER TRADE  — entry
# ---------------------------------------------------------------------------
def paper_enter(token: dict, scan_name: str, score: float):
    """Record a simulated buy and send a Discord alert."""
    address = token["address"]

    # Guard: already in position or on cooldown
    if address in active_trades:
        return
    now = time.time()
    if address in recent and (now - recent[address]) < COOLDOWN_SEC:
        return
    # Guard: cap max concurrent positions
    if len(active_trades) >= MAX_ACTIVE_TRADES:
        log.info("Max active trades reached — skipping entry.")
        return

    slip     = estimate_slippage(MAX_TRADE_SIZE)
    entry_px = token["price"] * (1 + slip)         # slippage applied to entry
    quantity = MAX_TRADE_SIZE / entry_px            # token units purchased

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

    log.info(f"[ENTRY] {token['symbol']} @ ${entry_px:.8f}  "
             f"qty={quantity:.4f}  score={score}  scan={scan_name}")

    msg = (
        f"🟢 **[{'PAPER' if PAPER_MODE else 'LIVE'} BUY]** `{token['symbol']}` — {token['name']}\n"
        f"**Entry Price:** `${entry_px:.8f}`\n"
        f"**Amount:**      `${MAX_TRADE_SIZE:.2f}`\n"
        f"**Score:**       `{score}/100`\n"
        f"**5m Change:**   `{token['change_m5']:+.2f}%`\n"
        f"**Liquidity:**   `${token['liquidity']:,.0f}`\n"
        f"**Volume 24h:**  `${token['volume']:,.0f}`\n"
        f"**Scan:**        `{scan_name}`\n"
        f"[Chart]({token['pair_url']})"
    )
    send_discord(msg, color=0x00ff99)

# ---------------------------------------------------------------------------
# PAPER TRADE  — exit
# ---------------------------------------------------------------------------
def paper_exit(address: str, current_price: float, reason: str):
    """Record a simulated sell, update stats, and send a Discord alert."""
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
        color = 0x00ff00
        emoji = "💰"
    else:
        stats["exits_sl"] += 1
        color = 0xff4444
        emoji = "🛑"

    log.info(f"[EXIT-{reason}] {trade['symbol']} @ ${current_price:.8f}  "
             f"PnL={pnl_pct:+.2f}%  held={hold_mins:.1f}m")

    msg = (
        f"{emoji} **[{'PAPER' if PAPER_MODE else 'LIVE'} SELL — {reason}]** `{trade['symbol']}`\n"
        f"**Entry:**    `${trade['entry_price']:.8f}`\n"
        f"**Exit:**     `${current_price:.8f}`\n"
        f"**PnL:**      `{pnl_pct:+.2f}%`  (`${pnl:+.4f}`)\n"
        f"**Held:**     `{hold_mins:.1f} min`\n"
        f"**Total PnL:** `${stats['total_pnl']:+.4f}`\n"
        f"[Chart]({trade['pair_url']})"
    )
    send_discord(msg, color=color)

# ---------------------------------------------------------------------------
# POSITION MONITOR  — check SL / TP for open trades
# ---------------------------------------------------------------------------
def monitor_positions():
    """
    Iterate all open paper positions, fetch current price from DexScreener,
    and trigger exits when SL or TP thresholds are crossed.
    Called inside the main scan loop.
    """
    if not active_trades:
        return

    # Snapshot addresses to avoid mutation during iteration
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
            # Use the first pair's price as current price
            current_price = float(pairs[0].get("priceUsd") or 0)
            if current_price <= 0:
                continue

            change_pct = ((current_price - trade["entry_price"]) / trade["entry_price"]) * 100

            if change_pct <= STOP_LOSS_PCT:
                paper_exit(address, current_price, "SL")
            elif change_pct >= TAKE_PROFIT_PCT:
                paper_exit(address, current_price, "TP")

        except Exception as e:
            log.error(f"Position monitor error for {address}: {e}")

# ---------------------------------------------------------------------------
# CLEANUP  — prevent memory leaks in `recent` dict
# ---------------------------------------------------------------------------
_last_cleanup = time.time()

def maybe_cleanup_recent():
    """Remove cooldown entries older than COOLDOWN_SEC every CLEANUP_INTERVAL seconds."""
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < CLEANUP_INTERVAL:
        return
    _last_cleanup = now
    expired = [addr for addr, ts in list(recent.items()) if now - ts > COOLDOWN_SEC]
    for addr in expired:
        recent.pop(addr, None)
    if expired:
        log.info(f"Cleaned up {len(expired)} stale cooldown entries.")

# ---------------------------------------------------------------------------
# MAIN SCAN CYCLE
# ---------------------------------------------------------------------------
def run_scan_cycle():
    """
    Full scan cycle:
      1. Monitor existing positions for SL/TP.
      2. For each SCAN_TARGET: fetch → filter → sort → top 10 → security → entry.
      3. Log cycle summary.
    """
    stats["scans"] += 1
    log.info(f"=== Scan #{stats['scans']} started | Active trades: {len(active_trades)} ===")

    # -- 1. Monitor open positions first --
    monitor_positions()

    # -- 2. Scan targets --
    for target in SCAN_TARGETS:
        name     = target["name"]
        query    = target["query"]
        chain_id = target["id"]
        tradable = target["trade"]

        log.info(f"[{name}] Fetching pairs for query='{query}' chain='{chain_id}' ...")
        raw_pairs = fetch_pairs(query, chain_id)
        log.info(f"[{name}] {len(raw_pairs)} raw pairs returned.")

        # -- Extract structured info --
        tokens = []
        for p in raw_pairs:
            info = extract_pair_info(p)
            if info:
                tokens.append(info)

        # -- Pre-filter --
        filtered = [
            t for t in tokens
            if t["liquidity"] >= MIN_LIQUIDITY
            and t["volume"]    >= MIN_VOLUME
            and t["change_m5"] >= MIN_CHANGE
            and t["age_hours"] >= MIN_AGE_HOURS
        ]
        log.info(f"[{name}] {len(filtered)} tokens passed pre-filter.")

        # -- Sort by 5m momentum descending, take top 10 --
        top10 = sorted(filtered, key=lambda x: x["change_m5"], reverse=True)[:10]

        # -- Process each top token --
        for token in top10:
            address = token["address"]
            symbol  = token["symbol"]
            score   = score_token(token)

            log.info(f"  [{name}] {symbol} | +{token['change_m5']:.2f}% | "
                     f"Liq=${token['liquidity']:,.0f} | Score={score}")

            if not tradable:
                log.info(f"  [{name}] Watch-only scan — skipping trade for {symbol}")
                continue

            # Skip if already in a position or on cooldown
            if address in active_trades:
                continue
            now = time.time()
            if address in recent and (now - recent[address]) < COOLDOWN_SEC:
                log.info(f"  [{name}] {symbol} on cooldown — skip.")
                continue

            # -- Security check --
            log.info(f"  [{name}] Running security check on {symbol} ({address[:10]}…)")
            is_safe = check_token_security(chain_id, address)
            if not is_safe:
                log.info(f"  [{name}] {symbol} FAILED security check — skip.")
                continue

            # -- Enter position --
            paper_enter(token, name, score)

        # Small delay between targets to be polite to APIs
        time.sleep(2)

    # -- 3. Cleanup & summary --
    maybe_cleanup_recent()
    log.info(
        f"=== Scan #{stats['scans']} done | "
        f"Entries: {stats['entries']} | "
        f"TP exits: {stats['exits_tp']} | "
        f"SL exits: {stats['exits_sl']} | "
        f"Total PnL: ${stats['total_pnl']:+.4f} | "
        f"Active: {len(active_trades)} ==="
    )

# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------
def main():
    log.info("=" * 60)
    log.info("  CRYPTO SCANNER BOT  —  Starting up")
    log.info(f"  Mode:           {'📄 PAPER' if PAPER_MODE else '🔴 LIVE'}")
    log.info(f"  Trade size:     ${MAX_TRADE_SIZE}")
    log.info(f"  Stop loss:      {STOP_LOSS_PCT}%")
    log.info(f"  Take profit:    {TAKE_PROFIT_PCT}%")
    log.info(f"  Min liquidity:  ${MIN_LIQUIDITY:,.0f}")
    log.info(f"  Min volume:     ${MIN_VOLUME:,.0f}")
    log.info(f"  Min 5m change:  {MIN_CHA