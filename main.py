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
TARGET_CHAIN = os.getenv("TARGET_CHAIN", "bsc")  # DexScreener chain identifier
MAX_TRADE_SIZE = float(os.getenv("MAX_TRADE_SIZE", "1.0"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "-10.0"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "20.0"))
MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY", "10000.0"))
MIN_VOLUME = float(os.getenv("MIN_VOLUME", "5000.0"))
MIN_CHANGE = float(os.getenv("MIN_CHANGE", "1.0"))        # m5 price change %
MIN_AGE_HOURS = float(os.getenv("MIN_AGE_HOURS", "0.0"))
# GoPlus numeric chain IDs
CHAIN_ID_MAP = {
    "bsc": 56,
    "ethereum": 1,
    "polygon": 137,
    "avalanche": 43114,
    "fantom": 250,
    "arbitrum": 42161,
    "optimism": 10,
    "base": 8453,
}
TARGET_CHAIN_NUMERIC = CHAIN_ID_MAP.get(TARGET_CHAIN, 56)
# List of volatile keywords to search for trending tokens
SEARCH_TERMS = [
    "pepe", "shib", "doge", "elon", "floki", "woof", "moon",
    "inu", "baby", "pump", "king", "rocket", "cat", "frog",
    "ai", "gpt", "dragon", "bot", "safe", "moon", "based",
    "chad", "wojak", "basedai", "comfy", "pepedoge"
]

# ----------------------------------------------------------------------
# Global State (for paper trading)
# ----------------------------------------------------------------------
active_trades: Dict[str, dict] = {}  # token_address -> trade info
recent: Dict[str, float] = {}        # token_address -> last buy timestamp (to enforce cooldown)
trade_lock = threading.Lock()        # protects active_trades and recent

# ----------------------------------------------------------------------
# Helper Functions
# ----------------------------------------------------------------------
def send_discord_alert(content: str, embed: dict = None) -> bool:
    """Send a message to Discord via webhook. Returns True on success."""
    if not DISCORD_WEBHOOK_URL:
        logger.warning("Discord webhook URL not set, cannot send alert.")
        return False
    payload = {"content": content}
    if embed:
        payload["embeds"] = [embed]
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code == 204:
            return True
        logger.error(f"Discord webhook error {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error(f"Discord webhook exception: {e}")
    return False

def fetch_dex_pairs(query: str) -> List[dict]:
    """Fetch a list of trading pairs from DexScreener search API."""
    url = f"https://api.dexscreener.com/latest/dex/search?q={query}"
    try:
        resp = requests.get(url, timeout=15)
        data = resp.json()
        pairs = data.get("pairs", [])
        if not pairs:
            logger.debug(f"No pairs found for query '{query}'")
        return pairs
    except Exception as e:
        logger.error(f"Error fetching DexScreener pairs for '{query}': {e}")
        return []

def filter_pairs(pairs: List[dict]) -> List[dict]:
    """Apply pre-filters: chain, liquidity, volume, price change, age."""
    now_ms = int(time.time() * 1000)
    valid = []
    for pair in pairs:
        try:
            if pair.get("chainId") != TARGET_CHAIN:
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
            # Attach calculated fields for convenience
            pair["_liq"] = liq
            pair["_vol"] = vol
            pair["_m5"] = m5
            pair["_age_hours"] = age_hours
            valid.append(pair)
        except Exception as e:
            logger.debug(f"Skipping pair due to filter error: {e}")
            continue
    return valid

def get_token_security(chain_id: int, token_address: str) -> Optional[dict]:
    """Fetch token security info from GoPlus API."""
    url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
    params = {"contract_addresses": token_address}
    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        if data.get("code") != 1:
            logger.error(f"GoPlus API error for {token_address}: {data.get('message')}")
            return None
        result = data.get("result", {}).get(token_address.lower(), None)
        if not result:
            logger.warning(f"No security data for {token_address}")
            return None
        return result
    except Exception as e:
        logger.error(f"GoPlus API exception for {token_address}: {e}")
        return None

def is_token_safe(security_data: Optional[dict]) -> bool:
    """
    Check if token passes security checks.
    Returns True if safe, False if any check fails or data is missing.
    """
    if not security_data:
        return False  # Assume unsafe on any failure
    try:
        honeypot = security_data.get("is_honeypot", "1")
        buy_tax = float(security_data.get("buy_tax", "100"))
        sell_tax = float(security_data.get("sell_tax", "100"))
        if honeypot != "0":
            logger.info("Token flagged as honeypot")
            return False
        if buy_tax > 10 or sell_tax > 10:
            logger.info(f"High taxes: buy={buy_tax}%, sell={sell_tax}%")
            return False
        return True
    except Exception as e:
        logger.error(f"Safety check error: {e}")
        return False

def calculate_pair_score(pair: dict, is_safe: bool) -> float:
    """Simple 0-100 score based on liquidity, volume, momentum and safety."""
    try:
        liq = pair.get("_liq", 0)
        vol = pair.get("_vol", 0)
        m5 = pair.get("_m5", 0)
        liq_score = min(liq / 100000, 1) * 30
        vol_score = min(vol / 50000, 1) * 20
        momentum_score = min(abs(m5) / 10, 1) * 30
        safety_score = 20 if is_safe else 0
        total = liq_score + vol_score + momentum_score + safety_score
        return round(total, 2)
    except:
        return 0.0

def simulate_buy(pair: dict) -> Optional[dict]:
    """
    Simulate a paper trade entry. Checks cooldown and existing position.
    Returns the trade dict if successful, else None.
    """
    token_addr = pair.get("baseToken", {}).get("address", "").lower()
    pair_addr = pair.get("pairAddress", "")
    if not token_addr or not pair_addr:
        return None

    now = time.time()
    with trade_lock:
        # Cooldown check
        if token_addr in recent and (now - recent[token_addr]) < 1800:  # 30 minutes
            logger.info(f"Cooldown active for {token_addr}, skipping buy")
            return None
        # Already holding?
        if token_addr in active_trades:
            logger.info(f"Already holding {token_addr}, skipping duplicate")
            return None

        try:
            price = float(pair.get("priceUsd", 0))
            if price <= 0:
                return None
            # Simulate slippage: 0.5% base + 0.1% per $1000 trade size
            trade_usd = MAX_TRADE_SIZE
            slippage_pct = 0.5 + (trade_usd / 1000) * 0.1
            entry_price = price * (1 + slippage_pct / 100)
            quantity = trade_usd / entry_price

            trade = {
                "token": pair["baseToken"]["symbol"],
                "token_address": token_addr,
                "pair_address": pair_addr,
                "entry_price": entry_price,
                "amount_usd": trade_usd,
                "quantity": quantity,
                "timestamp": now,
                "score": 0.0,  # will be set later
            }
            active_trades[token_addr] = trade
            recent[token_addr] = now
            logger.info(f"Paper BUY: {trade['token']} qty={quantity:.6f} at ${entry_price:.8f}")
            return trade
        except Exception as e:
            logger.error(f"Simulate buy error: {e}")
            return None

def monitor_positions() -> List[dict]:
    """
    Check all open paper trades for stop-loss / take-profit.
    Returns a list of closed trades (for alerting).
    """
    closed = []
    with trade_lock:
        # Copy items to avoid dict size change during iteration
        items = list(active_trades.items())
    for token_addr, trade in items:
        try:
            pair_addr = trade["pair_address"]
            chain = TARGET_CHAIN  # we only trade on one chain
            url = f"https://api.dexscreener.com/latest/dex/pairs/{chain}/{pair_addr}"
            resp = requests.get(url, timeout=10)
            data = resp.json()
            pair_data = data.get("pair")
            if not pair_data:
                logger.warning(f"Could not fetch pair for {pair_addr}")
                continue
            current_price = float(pair_data.get("priceUsd", 0))
            if current_price <= 0:
                continue

            entry_price = trade["entry_price"]
            pct_change = ((current_price - entry_price) / entry_price) * 100
            logger.debug(f"{trade['token']} PnL: {pct_change:.2f}%")

            if pct_change <= STOP_LOSS_PCT:
                # Simulated stop-loss
                with trade_lock:
                    closed_trade = active_trades.pop(token_addr, None)
                if closed_trade:
                    closed_trade["exit_price"] = current_price
                    closed_trade["exit_reason"] = "STOP_LOSS"
                    closed_trade["pnl_pct"] = round(pct_change, 2)
                    closed_trade["pnl_usd"] = round(
                        closed_trade["quantity"] * current_price - closed_trade["amount_usd"], 2
                    )
                    closed.append(closed_trade)
                    logger.info(f"STOP LOSS: {closed_trade['token']} at {pct_change:.2f}%")
            elif pct_change >= TAKE_PROFIT_PCT:
                with trade_lock:
                    closed_trade = active_trades.pop(token_addr, None)
                if closed_trade:
                    closed_trade["exit_price"] = current_price
                    closed_trade["exit_reason"] = "TAKE_PROFIT"
                    closed_trade["pnl_pct"] = round(pct_change, 2)
                    closed_trade["pnl_usd"] = round(
                        closed_trade["quantity"] * current_price - closed_trade["amount_usd"], 2
                    )
                    closed.append(closed_trade)
                    logger.info(f"TAKE PROFIT: {closed_trade['token']} at {pct_change:.2f}%")
        except Exception as e:
            logger.error(f"Monitor error for {trade.get('token','')}: {e}")
            continue
    return closed

def clean_memory():
    """Remove outdated entries from the `recent` cooldown dict."""
    now = time.time()
    with trade_lock:
        stale = [addr for addr, ts in recent.items() if now - ts > 1800]  # 30 min
        for addr in stale:
            del recent[addr]
        if stale:
            logger.debug(f"Cleaned {len(stale)} old cooldown entries")

# ----------------------------------------------------------------------
# Core Scanner Loop
# ----------------------------------------------------------------------
def scanner_loop():
    """Main loop: scan, filter, security check, trade, monitor, clean."""
    logger.info("=" * 50)
    logger.info("SCALPER BOT STARTED")
    logger.info(f"Paper Mode: {PAPER_MODE}, Chain: {TARGET_CHAIN} ({TARGET_CHAIN_NUMERIC})")
    logger.info(f"Trade Size: ${MAX_TRADE_SIZE}, SL: {STOP_LOSS_PCT}%, TP: {TAKE_PROFIT_PCT}%")
    logger.info(f"Min Liq: ${MIN_LIQUIDITY}, Vol: ${MIN_VOLUME}, m5%: {MIN_CHANGE}, Age: {MIN_AGE_HOURS}h")
    logger.info("=" * 50)

    last_clean = time.time()
    while True:
        try:
            # --- 1. Collect candidate pairs from DexScreener using volatile keywords ---
            all_pairs = []
            for term in SEARCH_TERMS:
                pairs = fetch_dex_pairs(term)
                all_pairs.extend(pairs)
                time.sleep(0.2)  # respect API rate limits

            # Deduplicate by pair address
            seen = set()
            uniq_pairs = []
            for p in all_pairs:
                addr = p.get("pairAddress")
                if addr and addr not in seen:
                    seen.add(addr)
                    uniq_pairs.append(p)

            # --- 2. Pre-filter ---
            valid_pairs = filter_pairs(uniq_pairs)
            # Sort by m5 change descending
            valid_pairs.sort(key=lambda x: x.get("_m5", 0), reverse=True)
            # Top 10 momentum tokens (unique base token)
            seen_tokens = set()
            top_pairs = []
            for p in valid_pairs:
                token_addr = p.get("baseToken", {}).get("address", "").lower()
                if token_addr and token_addr not in seen_tokens:
                    seen_tokens.add(token_addr)
                    top_pairs.append(p)
                if len(top_pairs) >= 10:
                    break
            logger.info(f"Top momentum tokens after filtering: {len(top_pairs)}")

            # --- 3. Security Checks & Trading ---
            for pair in top_pairs:
                token_addr = pair["baseToken"]["address"]
                # Security check via GoPlus
                security = get_token_security(TARGET_CHAIN_NUMERIC, token_addr)
                safe = is_token_safe(security)
                if not safe:
                    logger.info(f"Token {pair['baseToken']['symbol']} failed security, skipping")
                    continue

                # Only trade in paper mode
                if not PAPER_MODE:
                    logger.info("Paper mode disabled, skipping live trade")
                    continue

                # Attempt simulated buy (function handles cooldown & duplicate)
                trade = simulate_buy(pair)
                if trade:
                    trade["score"] = calculate_pair_score(pair, safe)
                    # Send Discord alert for entry
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

            # --- 4. Monitor existing positions for SL/TP ---
            closed_trades = monitor_positions()
            for ct in closed_trades:
                reason = ct["exit_reason"]
                emoji = "🟢" if ct["pnl_usd"] >= 0 else "🔴"
                embed = {
                    "title": f"{emoji} PAPER {reason}: {ct['token']}",
                    "color": 0x00FF00 if reason == "TAKE_PROFIT" else 0xFF0000,
                    "fields": [
                        {"name": "Entry", "value": f"${ct['entry_price']:.8f}", "inline": True},
                        {"name": "Exit", "value": f"${ct['exit_price']:.8f}", "inline": True},
                        {"name": "P&L %", "value": f"{ct['pnl_pct']}%", "inline": True},
                        {"name": "P&L $", "value": f"${ct['pnl_usd']:.2f}", "inline": True},
                        {"name": "DexScreener", "value": f"https://dexscreener.com/{TARGET_CHAIN}/{ct['pair_address']}", "inline": False},
                    ],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                send_discord_alert(f"Paper {reason.lower()} executed", embed)

            # --- 5. Memory cleanup (every 10 minutes) ---
            if time.time() - last_clean > 600:
                clean_memory()
                last_clean = time.time()

            # Wait before next full scan
            time.sleep(60)

        except Exception as e:
            logger.error(f"Main loop error: {e}", exc_info=True)
            time.sleep(30)

# ----------------------------------------------------------------------
# Flask Server to keep Railway awake and show status
# ----------------------------------------------------------------------
app = Flask(__name__)

@app.route("/")
def status():
    """Return bot status and current positions."""
    with trade_lock:
        trades_list = []
        for addr, t in active_trades.items():
            trades_list.append({
                "token": t["token"],
                "address": addr,
                "entry_price": t["entry_price"],
                "amount_usd": t["amount_usd"],
                "timestamp": t["timestamp"],
            })
        recent_count = len(recent)
    return jsonify({
        "status": "running",
        "paper_mode": PAPER_MODE,
        "active_trades": len(active_trades),
        "cooldown_entries": recent_count,
        "trades": trades_list,
        "server_time": datetime.now(timezone.utc).isoformat(),
    })

def start_flask():
    """Run Flask in a background thread on the required port."""
    port = int(os.getenv("PORT", "8080"))
    logger.info(f"Starting flask server on port {port}")
    # Use threading to avoid blocking the main scanner
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ----------------------------------------------------------------------
# Entry Point
# ----------------------------------------------------------------------
if __name__ == "__main__":
    # Validate mandatory webhook
    if not DISCORD_WEBHOOK_URL:
        logger.warning("DISCORD_WEBHOOK_URL not set – alerts disabled")
    # Start Flask in a daemon thread
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()
    # Give Flask a second to start
    time.sleep(1)
    # Run main scanner loop forever
    scanner_loop()