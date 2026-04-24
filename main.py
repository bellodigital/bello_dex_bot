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
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "-10.0"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "20.0"))
MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY", "10000.0"))
MIN_VOLUME = float(os.getenv("MIN_VOLUME", "5000.0"))
MIN_CHANGE = float(os.getenv("MIN_CHANGE", "1.0"))
MIN_AGE_HOURS = float(os.getenv("MIN_AGE_HOURS", "0.0"))

# GoPlus numeric chain IDs
CHAIN_ID_MAP = {
    "bsc": 56, "ethereum": 1, "polygon": 137, "avalanche": 43114,
    "fantom": 250, "arbitrum": 42161, "optimism": 10, "base": 8453,
}
TARGET_CHAIN_NUMERIC = CHAIN_ID_MAP.get(TARGET_CHAIN, 56)

# Fallback search terms (used only if boosted tokens don't yield enough)
SEARCH_TERMS = [
    "pepe", "shib", "doge", "elon", "floki", "moon",
    "inu", "baby", "pump", "king", "rocket", "cat",
    "ai", "gpt", "bot", "safe", "based", "chad",
]

# ----------------------------------------------------------------------
# Global State (for paper trading)
# ----------------------------------------------------------------------
active_trades: Dict[str, dict] = {}
recent: Dict[str, float] = {}
trade_lock = threading.Lock()
scan_cycle_count = 0  # for periodic status logging

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
# === NEW: Token Boosts API — Primary Trending Source ===
# ----------------------------------------------------------------------
def fetch_boosted_tokens(endpoint: str = "latest") -> List[dict]:
    """
    Fetch trending tokens from DexScreener's token-boosts API.
    endpoint: 'latest' or 'top'
    Returns list of dicts with keys: chainId, tokenAddress, amount, totalAmount
    """
    url = f"https://api.dexscreener.com/token-boosts/{endpoint}/v1"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 429:
            logger.warning("Token-boosts API rate limited (429). Skipping this cycle.")
            return []
        data = resp.json()
        if isinstance(data, list):
            return data
        elif isinstance(data, dict) and "url" in data:
            # Single object returned — wrap in list
            return [data]
        else:
            logger.debug(f"Unexpected token-boosts response format: {type(data)}")
            return []
    except Exception as e:
        logger.error(f"Error fetching boosted tokens ({endpoint}): {e}")
        return []

def fetch_pair_by_address(token_address: str) -> Optional[dict]:
    """
    Fetch full pair data for a token by searching its address.
    Returns the first pair dict from DexScreener, or None.
    """
    url = f"https://api.dexscreener.com/latest/dex/search?q={token_address}"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 429:
            logger.warning("Search API rate limited (429).")
            return None
        data = resp.json()
        pairs = data.get("pairs", [])
        if pairs:
            # Return the first pair (usually highest liquidity)
            return pairs[0]
        return None
    except Exception as e:
        logger.error(f"Error fetching pair for {token_address}: {e}")
        return None

# ----------------------------------------------------------------------
# Keyword Search (Keep as supplementary)
# ----------------------------------------------------------------------
def fetch_dex_pairs(query: str) -> List[dict]:
    url = f"https://api.dexscreener.com/latest/dex/search?q={query}"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 429:
            logger.warning("Search API rate limited (429).")
            return []
        data = resp.json()
        return data.get("pairs", [])
    except Exception as e:
        logger.error(f"Error fetching DexScreener pairs for '{query}': {e}")
        return []

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
            valid.append(pair)
        except Exception as e:
            logger.debug(f"Filter error: {e}")
            continue
    return valid

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
            logger.error(f"GoPlus API error for {token_address}: {data.get('message')}")
            return None
        return data.get("result", {}).get(token_address.lower(), None)
    except Exception as e:
        logger.error(f"GoPlus API exception for {token_address}: {e}")
        return None

def is_token_safe(security_data: Optional[dict]) -> bool:
    if not security_data:
        return False
    try:
        honeypot = security_data.get("is_honeypot", "1")
        buy_tax = float(security_data.get("buy_tax", "100"))
        sell_tax = float(security_data.get("sell_tax", "100"))
        if honeypot != "0":
            return False
        if buy_tax > 10 or sell_tax > 10:
            return False
        return True
    except Exception as e:
        logger.error(f"Safety check error: {e}")
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
# Paper Trading
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
            price = float(pair.get("priceUsd", 0))
            if price <= 0:
                return None
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
                "score": 0.0,
            }
            active_trades[token_addr] = trade
            recent[token_addr] = now
            logger.info(f"Paper BUY: {trade['token']} qty={quantity:.6f} at ${entry_price:.8f}")
            return trade
        except Exception as e:
            logger.error(f"Simulate buy error: {e}")
            return None

def monitor_positions() -> List[dict]:
    closed = []
    with trade_lock:
        items = list(active_trades.items())
    for token_addr, trade in items:
        try:
            pair_addr = trade["pair_address"]
            chain = TARGET_CHAIN
            url = f"https://api.dexscreener.com/latest/dex/pairs/{chain}/{pair_addr}"
            resp = requests.get(url, timeout=10)
            data = resp.json()
            pair_data = data.get("pair")
            if not pair_data:
                continue
            current_price = float(pair_data.get("priceUsd", 0))
            if current_price <= 0:
                continue
            entry_price = trade["entry_price"]
            pct_change = ((current_price - entry_price) / entry_price) * 100

            if pct_change <= STOP_LOSS_PCT:
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
    now = time.time()
    with trade_lock:
        stale = [addr for addr, ts in recent.items() if now - ts > 1800]
        for addr in stale:
            del recent[addr]
        if stale:
            logger.debug(f"Cleaned {len(stale)} old cooldown entries")

# ----------------------------------------------------------------------
# Main Scanner Loop (REVISED — Boosts API first, then keyword fallback)
# ----------------------------------------------------------------------
def scanner_loop():
    global scan_cycle_count
    logger.info("=" * 50)
    logger.info("SCALPER BOT STARTED (v2 — Token Boosts + Keyword Search)")
    logger.info(f"Paper Mode: {PAPER_MODE}, Chain: {TARGET_CHAIN} ({TARGET_CHAIN_NUMERIC})")
    logger.info(f"Trade Size: ${MAX_TRADE_SIZE}, SL: {STOP_LOSS_PCT}%, TP: {TAKE_PROFIT_PCT}%")
    logger.info(f"Min Liq: ${MIN_LIQUIDITY}, Vol: ${MIN_VOLUME}, m5%: {MIN_CHANGE}, Age: {MIN_AGE_HOURS}h")
    logger.info("=" * 50)

    last_clean = time.time()
    while True:
        try:
            scan_cycle_count += 1
            all_pairs = []

            # === STEP 1: Get trending tokens from Boosts API ===
            logger.debug("Fetching trending tokens from token-boosts API...")
            boosted = fetch_boosted_tokens("latest")
            if boosted:
                logger.info(f"Boosts API returned {len(boosted)} tokens")
                # Filter to target chain
                chain_boosted = [b for b in boosted if b.get("chainId") == TARGET_CHAIN]
                logger.info(f"  → {len(chain_boosted)} on chain '{TARGET_CHAIN}'")
                # Fetch full pair data for each boosted token
                for b in chain_boosted[:15]:  # limit to avoid rate issues
                    token_addr = b.get("tokenAddress", "")
                    if not token_addr:
                        continue
                    pair = fetch_pair_by_address(token_addr)
                    if pair:
                        all_pairs.append(pair)
                    time.sleep(0.15)  # small delay between calls
                logger.info(f"  → Fetched pair data for {len(all_pairs)} boosted tokens")

            # === STEP 2: Supplement with keyword search if not enough candidates ===
            if len(all_pairs) < 20:
                logger.debug("Supplementing with keyword search...")
                seen = set(p.get("pairAddress") for p in all_pairs)
                for term in SEARCH_TERMS[:8]:  # fewer terms to stay within rate limits
                    pairs = fetch_dex_pairs(term)
                    for p in pairs:
                        addr = p.get("pairAddress")
                        if addr and addr not in seen:
                            seen.add(addr)
                            all_pairs.append(p)
                    time.sleep(0.25)
                logger.debug(f"  → Total unique pairs after keyword search: {len(all_pairs)}")

            # === STEP 3: Filter ===
            valid_pairs = filter_pairs(all_pairs)
            valid_pairs.sort(key=lambda x: x.get("_m5", 0), reverse=True)
            # Top 10 unique tokens
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

            # === STEP 3.5: Periodic status for open positions ===
            if scan_cycle_count % 5 == 0:
                with trade_lock:
                    open_count = len(active_trades)
                if open_count > 0:
                    logger.info(f"📊 STATUS: {open_count} open position(s) — cycle #{scan_cycle_count}")
                    for addr, t in active_trades.items():
                        logger.info(f"   • {t['token']}: entry ${t['entry_price']:.8f}, amount ${t['amount_usd']:.2f}")
                else:
                    logger.info(f"📊 STATUS: No open positions — cycle #{scan_cycle_count}")

            # === STEP 4: Security Checks & Trading ===
            for pair in top_pairs:
                token_addr = pair["baseToken"]["address"]
                security = get_token_security(TARGET_CHAIN_NUMERIC, token_addr)
                safe = is_token_safe(security)
                if not safe:
                    logger.info(f"Token {pair['baseToken']['symbol']} failed security, skipping")
                    continue
                if not PAPER_MODE:
                    logger.info("Paper mode disabled, skipping live trade")
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

            # === STEP 5: Monitor Positions ===
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

            # === STEP 6: Memory Cleanup ===
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
    scanner_loop()
