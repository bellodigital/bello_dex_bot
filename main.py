import os
import sys
import json
import time
import threading
import logging
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional

import requests
from flask import Flask, jsonify

# ----------------------------------------------------------------------
# Logging Setup
# ----------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("scalper")

# ----------------------------------------------------------------------
# Configuration (Environment Variables with safe defaults)
# ----------------------------------------------------------------------
PAPER_MODE = os.getenv("PAPER_MODE", "true").lower() == "true"
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
TARGET_CHAIN = os.getenv("TARGET_CHAIN", "bsc")
MAX_TRADE_SIZE = float(os.getenv("MAX_TRADE_SIZE", "1.0"))

# Scalping defaults — wider SL to avoid wick-outs
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "-3.0"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "3.0"))

# Trailing stop
TRAILING_STOP_ENABLED = os.getenv("TRAILING_STOP_ENABLED", "true").lower() == "true"
TRAILING_ACTIVATION_PCT = float(os.getenv("TRAILING_ACTIVATION_PCT", "1.5"))
TRAILING_DISTANCE_PCT = float(os.getenv("TRAILING_DISTANCE_PCT", "1.0"))

# Max hold time (minutes) — 0 = disabled
MAX_HOLD_MINUTES = float(os.getenv("MAX_HOLD_MINUTES", "0"))

# Entry filter: only buy if price has pulled back from 5-min high by this %
PULLBACK_ENTRY_PCT = float(os.getenv("PULLBACK_ENTRY_PCT", "0.5"))  # 0 = buy at any price

# Slippage
SLIPPAGE_PCT = float(os.getenv("SLIPPAGE_PCT", "0.3"))

# Filters
MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY", "10000.0"))
MIN_VOLUME = float(os.getenv("MIN_VOLUME", "5000.0"))
MIN_CHANGE = float(os.getenv("MIN_CHANGE", "2.0"))   # raised to 2% to find stronger momentum
MIN_AGE_HOURS = float(os.getenv("MIN_AGE_HOURS", "0.0"))
MIN_PRICE_USD = float(os.getenv("MIN_PRICE_USD", "1e-8"))

# Chain mapping
CHAIN_ID_MAP = {
    "bsc": 56, "ethereum": 1, "polygon": 137, "avalanche": 43114,
    "fantom": 250, "arbitrum": 42161, "optimism": 10, "base": 8453,
}
TARGET_CHAIN_NUMERIC = CHAIN_ID_MAP.get(TARGET_CHAIN, 56)

# Search terms
SEARCH_TERMS = [
    "pepe", "shib", "doge", "elon", "floki", "moon",
    "inu", "baby", "pump", "king", "rocket", "cat",
    "ai", "gpt", "bot", "safe", "based", "chad",
]

# ----------------------------------------------------------------------
# Global State
# ----------------------------------------------------------------------
active_trades: Dict[str, dict] = {}
recent: Dict[str, float] = {}
trade_lock = threading.Lock()
scan_cycle_count = 0

# ----------------------------------------------------------------------
# Discord Alerts
# ----------------------------------------------------------------------
def send_discord_alert(content: str, embed: dict = None) -> bool:
    if not DISCORD_WEBHOOK_URL:
        return False
    payload = {"content": content}
    if embed:
        payload["embeds"] = [embed]
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        return resp.status_code == 204
    except Exception as e:
        logger.error(f"Discord webhook error: {e}")
        return False

# ----------------------------------------------------------------------
# API Functions
# ----------------------------------------------------------------------
def fetch_boosted_tokens(endpoint: str = "latest") -> List[dict]:
    url = f"https://api.dexscreener.com/token-boosts/{endpoint}/v1"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 429:
            return []
        data = resp.json()
        if isinstance(data, list):
            return data
        elif isinstance(data, dict) and "url" in data:
            return [data]
        return []
    except Exception as e:
        logger.error(f"Error fetching boosted tokens: {e}")
        return []

def fetch_pair_by_address(token_address: str) -> Optional[dict]:
    url = f"https://api.dexscreener.com/latest/dex/search?q={token_address}"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 429:
            return None
        data = resp.json()
        pairs = data.get("pairs", [])
        return pairs[0] if pairs else None
    except Exception as e:
        logger.error(f"Error fetching pair for {token_address}: {e}")
        return None

def fetch_dex_pairs(query: str) -> List[dict]:
    url = f"https://api.dexscreener.com/latest/dex/search?q={query}"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 429:
            return []
        data = resp.json()
        return data.get("pairs", [])
    except Exception as e:
        logger.error(f"Error fetching pairs for '{query}': {e}")
        return []

def fetch_pair_price(pair_address: str) -> Optional[float]:
    """Fast price check for a single pair."""
    url = f"https://api.dexscreener.com/latest/dex/pairs/{TARGET_CHAIN}/{pair_address}"
    try:
        resp = requests.get(url, timeout=5)
        data = resp.json()
        pair_data = data.get("pair")
        if pair_data:
            return float(pair_data.get("priceUsd", 0))
    except:
        pass
    return None

# ----------------------------------------------------------------------
# Filtering
# ----------------------------------------------------------------------
def filter_pairs(pairs: List[dict]) -> List[dict]:
    now_ms = int(time.time() * 1000)
    valid = []
    for pair in pairs:
        try:
            if pair.get("chainId") != TARGET_CHAIN:
                continue
            price = float(pair.get("priceUsd", 0))
            if price < MIN_PRICE_USD:
                continue
            liq = float(pair.get("liquidity", {}).get("usd", 0))
            vol = float(pair.get("volume", {}).get("h24", 0))
            m5 = float(pair.get("priceChange", {}).get("m5", 0))
            created = int(pair.get("pairCreatedAt", 0))
            age_hours = (now_ms - created) / 3600000 if created else 0
            if liq < MIN_LIQUIDITY or vol < MIN_VOLUME or m5 < MIN_CHANGE:
                continue
            if MIN_AGE_HOURS > 0 and age_hours < MIN_AGE_HOURS:
                continue
            pair["_liq"] = liq
            pair["_vol"] = vol
            pair["_m5"] = m5
            pair["_age_hours"] = age_hours
            pair["_price"] = price
            valid.append(pair)
        except Exception as e:
            logger.debug(f"Filter error: {e}")
            continue
    return valid

def is_pullback_entry(pair: dict) -> bool:
    """
    Check if current price is below the 5-minute high by at least PULLBACK_ENTRY_PCT.
    Uses priceChange.m5 to estimate the 5-min high.
    If m5 is positive, the high is approximately current_price / (1 + m5/100).
    We buy only if current price <= high * (1 - PULLBACK_ENTRY_PCT/100).
    """
    if PULLBACK_ENTRY_PCT <= 0:
        return True  # filter disabled
    try:
        price = pair.get("_price", float(pair.get("priceUsd", 0)))
        m5 = pair.get("_m5", 0)
        if m5 <= 0:
            return False  # no positive momentum, skip
        # Estimate 5-min high: if price rose m5%, then high = price / (1 + m5/100)
        estimated_high = price / (1 + m5 / 100)
        pullback_target = estimated_high * (1 - PULLBACK_ENTRY_PCT / 100)
        return price <= pullback_target
    except:
        return True  # on error, allow entry

# ----------------------------------------------------------------------
# Security
# ----------------------------------------------------------------------
def get_token_security(chain_id: int, token_address: str) -> Optional[dict]:
    url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
    params = {"contract_addresses": token_address}
    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        if data.get("code") != 1:
            return None
        return data.get("result", {}).get(token_address.lower(), None)
    except:
        return None

def is_token_safe(security_data: Optional[dict]) -> bool:
    if not security_data:
        return False
    try:
        honeypot = security_data.get("is_honeypot", "1")
        buy_tax = float(security_data.get("buy_tax", "100"))
        sell_tax = float(security_data.get("sell_tax", "100"))
        return honeypot == "0" and buy_tax <= 10 and sell_tax <= 10
    except:
        return False

def calculate_pair_score(pair: dict, is_safe: bool) -> float:
    try:
        liq = pair.get("_liq", 0)
        vol = pair.get("_vol", 0)
        m5 = pair.get("_m5", 0)
        liq_score = min(liq / 100000, 1) * 30
        vol_score = min(vol / 50000, 1) * 20
        momentum_score = min(abs(m5) / 10, 1) * 30
        safety_score = 20 if is_safe else 0
        return round(liq_score + vol_score + momentum_score + safety_score, 2)
    except:
        return 0.0

# ----------------------------------------------------------------------
# Paper Trading — Entry
# ----------------------------------------------------------------------
def simulate_buy(pair: dict) -> Optional[dict]:
    token_addr = pair.get("baseToken", {}).get("address", "").lower()
    pair_addr = pair.get("pairAddress", "")
    if not token_addr or not pair_addr:
        return None

    now = time.time()
    with trade_lock:
        if token_addr in recent and (now - recent[token_addr]) < 1800:
            return None
        if token_addr in active_trades:
            return None
        try:
            price = pair.get("_price", float(pair.get("priceUsd", 0)))
            if price <= 0:
                return None
            trade_usd = MAX_TRADE_SIZE
            entry_price = price * (1 + SLIPPAGE_PCT / 100)
            quantity = trade_usd / entry_price

            trade = {
                "token": pair["baseToken"]["symbol"],
                "token_address": token_addr,
                "pair_address": pair_addr,
                "entry_price": entry_price,
                "amount_usd": trade_usd,
                "quantity": quantity,
                "timestamp": now,
                "score": 0.0,
                "highest_price": entry_price,
            }
            active_trades[token_addr] = trade
            recent[token_addr] = now
            logger.info(f"Paper BUY: {trade['token']} qty={quantity:.6f} at ${entry_price:.8f}")
            return trade
        except Exception as e:
            logger.error(f"Simulate buy error: {e}")
            return None

# ----------------------------------------------------------------------
# Fast Monitoring (runs in separate thread every 15 seconds)
# ----------------------------------------------------------------------
def monitor_positions_fast() -> List[dict]:
    """Check all open trades. Called every 15 seconds."""
    closed = []
    now = time.time()

    with trade_lock:
        items = list(active_trades.items())

    for token_addr, trade in items:
        try:
            current_price = fetch_pair_price(trade["pair_address"])
            if current_price is None or current_price <= 0:
                continue

            entry_price = trade["entry_price"]
            pct_change = ((current_price - entry_price) / entry_price) * 100

            # Update highest price
            with trade_lock:
                if token_addr in active_trades:
                    if current_price > active_trades[token_addr]["highest_price"]:
                        active_trades[token_addr]["highest_price"] = current_price

            # Exit conditions
            exit_reason = None

            # Fixed TP/SL
            if pct_change >= TAKE_PROFIT_PCT:
                exit_reason = "TAKE_PROFIT"
            elif pct_change <= STOP_LOSS_PCT:
                exit_reason = "STOP_LOSS"

            # Trailing stop
            if exit_reason is None and TRAILING_STOP_ENABLED:
                highest = trade.get("highest_price", entry_price)
                profit_from_high = ((highest - entry_price) / entry_price) * 100
                if profit_from_high >= TRAILING_ACTIVATION_PCT:
                    trailing_stop_price = highest * (1 - TRAILING_DISTANCE_PCT / 100)
                    if current_price <= trailing_stop_price:
                        exit_reason = "TRAILING_STOP"

            # Max hold time
            if exit_reason is None and MAX_HOLD_MINUTES > 0:
                age_min = (now - trade.get("timestamp", now)) / 60
                if age_min >= MAX_HOLD_MINUTES:
                    exit_reason = "MAX_HOLD"

            if exit_reason:
                with trade_lock:
                    closed_trade = active_trades.pop(token_addr, None)
                if closed_trade:
                    closed_trade["exit_price"] = current_price
                    closed_trade["exit_reason"] = exit_reason
                    closed_trade["pnl_pct"] = round(pct_change, 2)
                    closed_trade["pnl_usd"] = round(
                        closed_trade["quantity"] * current_price - closed_trade["amount_usd"], 2
                    )
                    closed.append(closed_trade)
                    logger.info(f"{exit_reason}: {closed_trade['token']} at {pct_change:.2f}%")
        except Exception as e:
            logger.error(f"Fast monitor error for {trade.get('token','')}: {e}")
            continue

    return closed

def fast_monitor_loop():
    """Runs in a separate thread — checks positions every 15 seconds."""
    logger.info("Fast monitor thread started (15s interval)")
    while True:
        try:
            closed = monitor_positions_fast()
            for ct in closed:
                reason = ct["exit_reason"]
                emoji = "🟢" if ct["pnl_usd"] >= 0 else "🔴"
                embed = {
                    "title": f"{emoji} PAPER {reason}: {ct['token']}",
                    "color": 0x00FF00 if reason in ("TAKE_PROFIT", "TRAILING_STOP") else 0xFF0000,
                    "fields": [
                        {"name": "Entry", "value": f"${ct['entry_price']:.8f}", "inline": True},
                        {"name": "Exit", "value": f"${ct['exit_price']:.8f}", "inline": True},
                        {"name": "P&L %", "value": f"{ct['pnl_pct']}%", "inline": True},
                        {"name": "P&L $", "value": f"${ct['pnl_usd']:.2f}", "inline": True},
                        {"name": "Reason", "value": reason, "inline": False},
                        {"name": "DexScreener", "value": f"https://dexscreener.com/{TARGET_CHAIN}/{ct['pair_address']}", "inline": False},
                    ],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                send_discord_alert(f"Paper {reason.lower()} executed", embed)
        except Exception as e:
            logger.error(f"Fast monitor loop error: {e}")
        time.sleep(15)  # check every 15 seconds

# ----------------------------------------------------------------------
# Memory Cleanup
# ----------------------------------------------------------------------
def clean_memory():
    now = time.time()
    with trade_lock:
        stale = [addr for addr, ts in recent.items() if now - ts > 1800]
        for addr in stale:
            del recent[addr]
        if stale:
            logger.debug(f"Cleaned {len(stale)} old cooldown entries")

# ----------------------------------------------------------------------
# Main Scanner Loop
# ----------------------------------------------------------------------
def scanner_loop():
    global scan_cycle_count
    logger.info("=" * 50)
    logger.info("SCALPER BOT STARTED (v4 — Fast Monitor + Pullback Entry)")
    logger.info(f"Paper Mode: {PAPER_MODE}, Chain: {TARGET_CHAIN} ({TARGET_CHAIN_NUMERIC})")
    logger.info(f"Trade Size: ${MAX_TRADE_SIZE}, SL: {STOP_LOSS_PCT}%, TP: {TAKE_PROFIT_PCT}%")
    logger.info(f"Trailing: {TRAILING_STOP_ENABLED} (activate at +{TRAILING_ACTIVATION_PCT}%, distance {TRAILING_DISTANCE_PCT}%)")
    logger.info(f"Pullback Entry: {PULLBACK_ENTRY_PCT}% below 5-min high")
    logger.info(f"Slippage: {SLIPPAGE_PCT}%, Max Hold: {MAX_HOLD_MINUTES} min")
    logger.info("=" * 50)

    last_clean = time.time()
    while True:
        try:
            scan_cycle_count += 1
            all_pairs = []

            # 1. Boosts API
            boosted = fetch_boosted_tokens("latest")
            if boosted:
                chain_boosted = [b for b in boosted if b.get("chainId") == TARGET_CHAIN]
                logger.info(f"Boosts: {len(chain_boosted)} tokens on {TARGET_CHAIN}")
                for b in chain_boosted[:15]:
                    token_addr = b.get("tokenAddress")
                    if token_addr:
                        pair = fetch_pair_by_address(token_addr)
                        if pair:
                            all_pairs.append(pair)
                        time.sleep(0.15)

            # 2. Keyword fallback
            if len(all_pairs) < 20:
                seen = set(p.get("pairAddress") for p in all_pairs)
                for term in SEARCH_TERMS[:8]:
                    pairs = fetch_dex_pairs(term)
                    for p in pairs:
                        addr = p.get("pairAddress")
                        if addr and addr not in seen:
                            seen.add(addr)
                            all_pairs.append(p)
                    time.sleep(0.25)

            # 3. Filter + pullback check
            valid_pairs = filter_pairs(all_pairs)
            # Apply pullback entry filter
            if PULLBACK_ENTRY_PCT > 0:
                before = len(valid_pairs)
                valid_pairs = [p for p in valid_pairs if is_pullback_entry(p)]
                logger.info(f"Pullback filter: {before} → {len(valid_pairs)} candidates")

            valid_pairs.sort(key=lambda x: x.get("_m5", 0), reverse=True)
            seen_tokens = set()
            top_pairs = []
            for p in valid_pairs:
                token_addr = p.get("baseToken", {}).get("address", "").lower()
                if token_addr and token_addr not in seen_tokens:
                    seen_tokens.add(token_addr)
                    top_pairs.append(p)
                if len(top_pairs) >= 10:
                    break
            logger.info(f"Top momentum tokens: {len(top_pairs)}")

            # 4. Security & Trade
            for pair in top_pairs:
                token_addr = pair["baseToken"]["address"]
                security = get_token_security(TARGET_CHAIN_NUMERIC, token_addr)
                safe = is_token_safe(security)
                if not safe:
                    continue
                if not PAPER_MODE:
                    continue
                trade = simulate_buy(pair)
                if trade:
                    trade["score"] = calculate_pair_score(pair, safe)
                    embed = {
                        "title": f"🟢 PAPER BUY: {trade['token']}",
                        "color": 0x00FF00,
                        "fields": [
                            {"name": "Price", "value": f"${trade['entry_price']:.8f}", "inline": True},
                            {"name": "Amount", "value": f"${trade['amount_usd']:.2f}", "inline": True},
                            {"name": "Quantity", "value": f"{trade['quantity']:.6f}", "inline": True},
                            {"name": "Score", "value": str(trade['score']), "inline": True},
                            {"name": "SL / TP", "value": f"{STOP_LOSS_PCT}% / {TAKE_PROFIT_PCT}%", "inline": True},
                            {"name": "DexScreener", "value": f"https://dexscreener.com/{TARGET_CHAIN}/{trade['pair_address']}", "inline": False},
                        ],
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    send_discord_alert("New paper trade entered", embed)

            # 5. Memory Cleanup
            #    (Position monitoring runs in its own thread — see fast_monitor_loop)
            if time.time() - last_clean > 600:
                clean_memory()
                last_clean = time.time()

            time.sleep(60)

        except Exception as e:
            logger.error(f"Main loop error: {e}", exc_info=True)
            time.sleep(30)

# ----------------------------------------------------------------------
# Flask Server
# ----------------------------------------------------------------------
app = Flask(__name__)

@app.route("/")
def status():
    with trade_lock:
        trades_list = []
        for addr, t in active_trades.items():
            trades_list.append({
                "token": t.get("token"),
                "token_address": t.get("token_address"),
                "pair_address": t.get("pair_address"),
                "entry_price": t.get("entry_price"),
                "amount_usd": t.get("amount_usd"),
                "quantity": t.get("quantity"),
                "score": t.get("score"),
                "timestamp": t.get("timestamp"),
                "highest_price": t.get("highest_price"),
            })
    return jsonify({
        "status": "running",
        "paper_mode": PAPER_MODE,
        "target_chain": TARGET_CHAIN,
        "scan_cycles": scan_cycle_count,
        "active_trades": len(trades_list),
        "trades": trades_list,
    })


def run_flask():
    port = int(os.getenv("PORT", "8080"))
    logger.info(f"Flask health server starting on port {port}")
    app.run(host="0.0.0.0", port=port, use_reloader=False)


if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=fast_monitor_loop, daemon=True).start()
    scanner_loop()
