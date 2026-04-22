import os
import time
import requests
from datetime import datetime
from flask import Flask
from threading import Thread

# === CONFIGURATION ===
paper_env = os.getenv("PAPER_MODE", "true").lower().strip()
PAPER_MODE = paper_env not in ["false", "0", "no"]
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")

# FILTERS (Optimized for Momentum/Trending)
MAX_TRADE_SIZE   = float(os.getenv("MAX_TRADE_SIZE",   "1.0"))
STOP_LOSS_PCT    = float(os.getenv("STOP_LOSS_PCT",    "-10"))
TAKE_PROFIT_PCT  = float(os.getenv("TAKE_PROFIT_PCT",  "20"))
MAX_POSITIONS    = int(os.getenv("MAX_POSITIONS",       "2"))
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "-5"))

MIN_LIQUIDITY    = float(os.getenv("MIN_LIQUIDITY",    "20000"))
MIN_VOLUME       = float(os.getenv("MIN_VOLUME",       "10000"))
MIN_CHANGE       = float(os.getenv("MIN_CHANGE",       "2")) # Lowered to catch early trends
MIN_AGE_HOURS    = float(os.getenv("MIN_AGE_HOURS",    "0.5"))

# === STATE ===
recent        = {}
active_trades = {}
daily_pnl     = 0.0
last_reset    = datetime.now().date()

# === FLASK SERVER ===
app = Flask('')

@app.route('/')
def home():
    mode = "PAPER" if PAPER_MODE else "LIVE"
    return f"NexusBot PRO {mode} | P&L: ${daily_pnl:.2f} | Pos: {len(active_trades)}/{MAX_POSITIONS}"

Thread(target=lambda: app.run(host='0.0.0.0', port=8080)).start()

# === HELPERS ===
def send(msg):
    if WEBHOOK:
        try:
            requests.post(WEBHOOK, json={"content": msg}, timeout=5)
            print("Discord:", msg[:80])
        except Exception as e:
            print("Discord Error:", e)

def simulate_trade(symbol, price, amount_usd, side):
    slippage = 0.5 + (amount_usd / 1000) * 0.1
    gas = 0.25
    fill = price * (1 + slippage/100) if side == "buy" else price * (1 - slippage/100)
    return {
        "success": True,
        "fill_price": round(fill, 10),
        "slippage_pct": round(slippage, 2),
        "gas_cost": gas,
        "total_cost": round(amount_usd + gas, 2)
    }

def check_security(chain_id, addr):
    """Returns (is_safe, buy_tax, sell_tax). Fails safe."""
    try:
        if chain_id == "solana":
            url = "https://api.gopluslabs.io/api/v1/solana/token_security"
        else:
            url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}"

        r = requests.get(url, params={"contract_addresses": addr}, timeout=5)
        if r.status_code != 200:
            return False, 99, 99

        data  = r.json()
        token = data.get("result", {}).get(addr.lower(), {})
        if not token:
            return False, 99, 99

        is_honeypot = token.get("is_honeypot", "1") == "1"
        is_safe     = not is_honeypot
        buy_tax  = float(token.get("buy_tax",  0) or 0)
        sell_tax = float(token.get("sell_tax", 0) or 0)

        return is_safe, buy_tax, sell_tax
    except Exception as e:
        return False, 99, 99

def calculate_score(liq, vol, chg, is_safe):
    score = 0
    if liq >= 100000: score += 25
    elif liq >= 50000: score += 15
    if vol / max(liq, 1) > 1: score += 20
    if chg >= 10: score += 25
    elif chg >= 2: score += 15
    if is_safe: score += 20
    risk = "low" if score >= 80 else "medium" if score >= 60 else "high"
    return min(100, score), risk

# === CHAINS CONFIG ===
CHAINS = [    {"name": "BSC", "query": "bsc", "id": "56", "trade": True},
    {"name": "SOL", "query": "solana", "id": "solana", "trade": False}
]

# === STARTUP ===
print("NexusBot PRO starting...")
mode_tag = "PAPER MODE" if PAPER_MODE else "LIVE MODE"
send(
    f"NexusBot [{mode_tag}] PRO TRENDING LOGIC\n"
    f"Trading: BSC | Watching: SOL\n"
    f"Strategy: Sort by Momentum (Top Movers)\n"
    f"Max: ${MAX_TRADE_SIZE} | SL: {STOP_LOSS_PCT}% | TP: {TAKE_PROFIT_PCT}%"
)
time.sleep(2)

# === MAIN LOOP ===
while True:
    try:
        today = datetime.now().date()
        if today != last_reset:
            daily_pnl  = 0.0
            last_reset = today
            print("Daily P&L reset")

        now_clean = datetime.now()
        keys_to_delete = [addr for addr, ts in recent.items() if (now_clean - ts).total_seconds() > 3600]
        for key in keys_to_delete: del recent[key]

        print(f"\nScanning PRO TRENDING... (P&L: ${daily_pnl:.2f})")

        for chain in CHAINS:
            try:
                print(f"Fetching {chain['name']} data...")
                # Fetch MORE pairs to find the real trends
                resp = requests.get(f"https://api.dexscreener.com/latest/dex/search?q={chain['query']}", timeout=10)
                
                if resp.status_code != 200: continue
                
                all_pairs = resp.json().get("pairs", [])
                if not all_pairs: continue

                # === PRO LOGIC: FILTER & SORT ===
                valid_pairs = []
                for p in all_pairs:
                    base = p.get("baseToken", {})
                    addr = base.get("address")
                    if not addr: continue
                    
                    liq = float((p.get("liquidity") or {}).get("usd", 0))
                    vol = float((p.get("volume") or {}).get("h24", 0))
                    chg = float((p.get("priceChange") or {}).get("m5", 0))
                    mcap = float(p.get("marketCap") or 0)
                    
                    # Basic Pre-Filter
                    if liq < MIN_LIQUIDITY or vol < MIN_VOLUME or chg < MIN_CHANGE:
                        continue
                    if mcap > 0 and mcap < 50000:
                        continue
                        
                    # Age Check
                    created = p.get("pairCreatedAt")
                    if created:
                        age_h = (datetime.now().timestamp() * 1000 - created) / (1000 * 60 * 60)
                        if age_h < MIN_AGE_HOURS:
                            continue
                    
                    valid_pairs.append(p)

                # SORT BY MOMENTUM (Highest 5min Change First)
                valid_pairs.sort(key=lambda x: float((x.get("priceChange") or {}).get("m5", 0)), reverse=True)
                
                # Take Top 10 Movers
                top_movers = valid_pairs[:10]
                print(f"{chain['name']}: Found {len(valid_pairs)} valid. Trading Top {len(top_movers)} Movers.")

                for p in top_movers:
                    base  = p.get("baseToken", {})
                    addr  = base.get("address")
                    sym   = base.get("symbol", "?")
                    price = p.get("priceUsd")
                    liq   = float((p.get("liquidity") or {}).get("usd", 0))
                    vol   = float((p.get("volume") or {}).get("h24", 0))
                    chg   = float((p.get("priceChange") or {}).get("m5", 0))

                    # Cooldown
                    now = datetime.now()
                    if addr in recent and (now - recent[addr]).total_seconds() < 1800:
                        continue

                    # Security Check
                    is_safe, buy_tax, sell_tax = check_security(chain["id"], addr)
                    if not is_safe or buy_tax > 5 or sell_tax > 5:
                        continue

                    score, risk = calculate_score(liq, vol, chg, is_safe)
                    risk_label = {"low":"[LOW]", "medium":"[MED]", "high":"[HIGH]"}.get(risk, "[?]")

                    # === SOL: ALERT ONLY ===
                    if not chain["trade"]:
                        send(f"🔥 SOL TRENDING {risk_label} ${sym}\nScore: {score}/100 | +{chg}%\nhttps://dexscreener.com/{chain['query']}/{addr}")
                        recent[addr] = now
                        continue

                    # === BSC: MONITOR EXISTING ===
                    if addr in active_trades:
                        entry = active_trades[addr]["entry_price"]
                        pnl_pct = ((float(price) - entry) / entry) * 100

                        if pnl_pct <= STOP_LOSS_PCT:
                            pnl_usd = ((float(price) - entry) * active_trades[addr]["quantity"])
                            daily_pnl += pnl_usd
                            send(f"🔴 STOP-LOSS ${sym} [BSC]\nP/L: ${pnl_usd:.4f} ({pnl_pct:.1f}%)")
                            del active_trades[addr]
                            recent[addr] = now
                        elif pnl_pct >= TAKE_PROFIT_PCT:
                            pnl_usd = ((float(price) - entry) * active_trades[addr]["quantity"])
                            daily_pnl += pnl_usd
                            send(f"🟢 TAKE-PROFIT ${sym} [BSC]\nP/L: +${pnl_usd:.4f} (+{pnl_pct:.1f}%)")
                            del active_trades[addr]
                            recent[addr] = now
                        continue

                    # === BSC: NEW TRADE ===
                    if daily_pnl <= DAILY_LOSS_LIMIT:
                        print("Daily loss limit hit")
                        break
                    if len(active_trades) >= MAX_POSITIONS:
                        print("Max positions reached")
                        break

                    trade_amt = min(MAX_TRADE_SIZE, liq * 0.01)
                    if trade_amt < 0.1: continue

                    result = simulate_trade(sym, float(price), trade_amt, "buy")
                    qty = trade_amt / result["fill_price"]

                    active_trades[addr] = {
                        "entry_price": result["fill_price"],
                        "amount": trade_amt,
                        "quantity": qty,
                        "chain": "BSC",
                        "symbol": sym,
                        "ts": now
                    }
                    recent[addr] = now

                    mode_lbl = "PAPER BUY" if PAPER_MODE else "LIVE BUY"
                    send(
                        f"🚀 {mode_lbl} {risk_label} [BSC] ${sym}\n"
                        f"Momentum: +{chg}% | Score: {score}/100\n"                        f"Entry: ${result['fill_price']:.8f}\n"
                        f"Amount: ${trade_amt:.2f} | Total: ${result['total_cost']:.2f}\n"
                        f"https://dexscreener.com/bsc/{addr}"
                    )
                    print(f"{'PAPER' if PAPER_MODE else 'LIVE'} BUY: {sym} @ ${result['fill_price']:.8f}")
                    time.sleep(2)

            except Exception as e:
                print(f"{chain['name']} error: {e}")
                continue

        print("Waiting 60 seconds...\n")
        time.sleep(60)

    except Exception as e:
        print(f"Main error: {e}")
        time.sleep(60)