import os
import time
import requests
from datetime import datetime, timedelta
from flask import Flask
from threading import Thread

# === CONFIGURATION ===
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
                       # Accepts: true, True, TRUE, 1, yes, Yes (case-insensitive)
                       paper_env = os.getenv("PAPER_MODE", "True").lower().strip()
                       PAPER_MODE = paper_env in ["true", "1", "yes", "y"] "True").lower() == "true"
MAX_TRADE_SIZE = float(os.getenv("MAX_TRADE_SIZE", "2.0"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "-10"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "20"))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "2"))
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "-5"))
MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY", "50000"))
MIN_VOLUME = float(os.getenv("MIN_VOLUME", "30000"))
MIN_CHANGE = float(os.getenv("MIN_CHANGE", "8"))
MIN_AGE_HOURS = float(os.getenv("MIN_AGE_HOURS", "24"))

# === GLOBAL STATE ===
recent = {}
active_trades = {}
daily_pnl = 0.0
last_reset = datetime.now().date()

# === FLASK SERVER ===
app = Flask('')
@app.route('/')
def home():
    mode = "PAPER" if PAPER_MODE else "LIVE"
    return f"Auto-Trading Bot {mode} - BSC Trading + SOL Alerts - Max: ${MAX_TRADE_SIZE}"

Thread(target=lambda: app.run(host='0.0.0.0', port=8080)).start()

# === HELPERS ===
def send(msg):
    if WEBHOOK:
        try:
            requests.post(WEBHOOK, json={"content": msg})
            print("Discord: " + msg[:60] + "...")
        except Exception as e:
            print("Discord Error: " + str(e))

def simulate_trade(symbol, price, amount_usd, side):
    slippage = 0.5 + (amount_usd / 1000) * 0.1
    gas = 0.25
    if side == "buy":
        fill = price * (1 + slippage / 100)
        total = amount_usd + gas
    else:
        fill = price * (1 - slippage / 100)
        total = gas
    return {"success": True, "side": side, "symbol": symbol, "requested": amount_usd,
            "fill_price": round(fill, 8), "slippage_pct": round(slippage, 2),
            "gas_cost": gas, "total_cost": round(total, 2)}

def check_security(chain_id, addr):
    try:
        if chain_id == "solana":
            url = "https://api.gopluslabs.io/api/v1/solana/token_security"
            params = {"contract_addresses": addr}
        else:
            url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
            params = {"contract_addresses": addr}
        
        r = requests.get(url, params=params, timeout=5)
        if r.status_code != 200: return True, 0, 0
        data = r.json()
        token = data.get("result", {}).get(addr.lower(), {})
        
        is_honeypot = token.get("is_honeypot", "1") == "0"
        buy_tax = float(token.get("buy_tax", 0) or 0)
        sell_tax = float(token.get("sell_tax", 0) or 0)
        return is_honeypot, buy_tax, sell_tax
    except:
        return True, 0, 0

def calculate_score(liq, vol, chg, is_safe):
    score = 0
    if liq >= 100000: score += 25
    elif liq >= 50000: score += 15
    if vol / max(liq, 1) > 1: score += 20
    if chg >= 10: score += 25
    elif chg >= 8: score += 15
    if is_safe: score += 20
    risk = "low" if score >= 80 else "medium" if score >= 60 else "high"
    return min(100, score), risk

# === STARTUP ===
print("Auto-Trading Bot starting...")
mode_tag = "🧪 PAPER MODE" if PAPER_MODE else "💰 LIVE MODE"
send(f"🤖 {mode_tag} Started\n🎯 Trading: BSC Only\n👀 Watching: Solana (alerts only)\nMax: ${MAX_TRADE_SIZE} | SL: {STOP_LOSS_PCT}% | TP: {TAKE_PROFIT_PCT}%")
time.sleep(2)

# === CHAINS CONFIG ===
chains = [
    {"name": "BSC", "query": "bsc", "id": "56", "trade": True},   # ✅ Will trade
    {"name": "SOL", "query": "solana", "id": "solana", "trade": False}  # 👀 Alerts only
]
# === MAIN LOOP ===
while True:
    try:
        today = datetime.now().date()
        if today != last_reset:
            daily_pnl = 0.0
            last_reset = today
            print("Daily P&L reset")
        
        print(f"\n🔍 Scanning... (P&L: ${daily_pnl:.2f} | Positions: {len(active_trades)}/{MAX_POSITIONS})")
        
        for chain in chains:
            try:
                print(f"→ Scanning {chain['name']}...")
                resp = requests.get(f"https://api.dexscreener.com/latest/dex/search?q={chain['query']}", timeout=10)
                if resp.status_code != 200:
                    print(f"⚠️ {chain['name']} API error")
                    time.sleep(5)
                    continue
                
                pairs = resp.json().get("pairs", [])
                if not pairs: continue
                
                print(f"✅ {chain['name']}: Found {len(pairs[:10])} pairs")
                
                for p in pairs[:10]:
                    base = p.get("baseToken", {})
                    addr = base.get("address")
                    sym = base.get("symbol", "?")
                    price = p.get("priceUsd")
                    if not addr or not price: continue
                    
                    liq = float(p.get("liquidity", {}).get("usd", 0))
                    vol = float(p.get("volume", {}).get("h24", 0))
                    chg = float(p.get("priceChange", {}).get("m5", 0))
                    mcap = float(p.get("marketCap", 0))
                    
                    # === FILTERS ===
                    if liq < MIN_LIQUIDITY or vol < MIN_VOLUME or chg < MIN_CHANGE or mcap < 50000:
                        continue
                    
                    created = p.get("pairCreatedAt")
                    if created:
                        age_h = (datetime.now().timestamp() * 1000 - created) / (1000 * 60 * 60)
                        if age_h < MIN_AGE_HOURS: continue
                    
                    now = datetime.now()
                    if addr in recent and (now - recent[addr]).seconds < 1800:
                        continue                    
                    is_safe, buy_tax, sell_tax = check_security(chain["id"], addr)
                    if not is_safe or buy_tax > 5 or sell_tax > 5:
                        continue
                    
                    score, risk = calculate_score(liq, vol, chg, is_safe)
                    risk_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(risk, "⚪")
                    
                    # === SOLOANA: INFO ALERT ONLY ===
                    if not chain["trade"]:
                        info_msg = (f"👀 {risk_emoji} SOLANA OPPORTUNITY [{chain['name']}] ${sym}\n"
                                   f"Score: {score}/100 | Price: ${float(price):.8f}\n"
                                   f"Liq: ${liq:,.0f} | Vol: ${vol:,.0f} | +{chg}%\n"
                                   f"Tax: {buy_tax}%/{sell_tax}%\n"
                                   f"⚠️ Info only - BSC trading enabled\n"
                                   f"https://dexscreener.com/{chain['query']}/{addr}")
                        send(info_msg)
                        recent[addr] = now
                        continue
                    
                    # === BSC: POSITION MONITORING ===
                    if addr in active_trades:
                        entry = active_trades[addr]["entry_price"]
                        pnl_pct = ((float(price) - entry) / entry) * 100
                        
                        if pnl_pct <= STOP_LOSS_PCT:
                            pnl_usd = (float(price) - entry) * active_trades[addr]["quantity"]
                            daily_pnl += pnl_usd
                            send(f"🔴 STOP-LOSS ${sym} [BSC]\nExit: ${price}\nP/L: ${pnl_usd:.2f} ({pnl_pct:.1f}%)")
                            print(f"Closed {sym}: stop-loss")
                            del active_trades[addr]
                            recent[addr] = now
                            continue
                        elif pnl_pct >= TAKE_PROFIT_PCT:
                            pnl_usd = (float(price) - entry) * active_trades[addr]["quantity"]
                            daily_pnl += pnl_usd
                            send(f"🟢 TAKE-PROFIT ${sym} [BSC]\nExit: ${price}\nP/L: +${pnl_usd:.2f} (+{pnl_pct:.1f}%)")
                            print(f"Closed {sym}: take-profit")
                            del active_trades[addr]
                            recent[addr] = now
                            continue
                        else:
                            continue
                    
                    # === RISK CHECKS ===
                    if daily_pnl <= DAILY_LOSS_LIMIT:
                        print("Daily loss limit hit - pausing")
                        time.sleep(300)
                        break
                    if len(active_trades) >= MAX_POSITIONS:
                        print("Max positions reached")
                        break
                    
                    trade_amt = min(MAX_TRADE_SIZE, liq * 0.01)
                    if trade_amt < 1: continue
                    
                    result = simulate_trade(sym, float(price), trade_amt, "buy")
                    if not result["success"]: continue
                    
                    qty = trade_amt / result["fill_price"]
                    active_trades[addr] = {"entry_price": result["fill_price"], "amount": trade_amt, 
                                          "quantity": qty, "chain": "BSC", "ts": now}
                    recent[addr] = now
                    
                    mode_lbl = "🧪 PAPER" if PAPER_MODE else "💰 LIVE"
                    alert = (f"{mode_lbl} {risk_emoji} BUY [BSC] ${sym}\n"
                             f"Score: {score}/100 | Entry: ${result['fill_price']:.8f}\n"
                             f"Amount: ${trade_amt:.2f} | Slippage: {result['slippage_pct']}%\n"
                             f"Gas: ${result['gas_cost']} | Total: ${result['total_cost']:.2f}\n"
                             f"SL: {STOP_LOSS_PCT}% | TP: {TAKE_PROFIT_PCT}%\n"
                             f"Liq: ${liq:,.0f} | Vol: ${vol:,.0f} | Tax: {buy_tax}%/{sell_tax}%\n"
                             f"https://dexscreener.com/bsc/{addr}")
                    
                    send(alert)
                    print(f"{'PAPER' if PAPER_MODE else 'LIVE'} BUY: {sym} @ ${result['fill_price']:.8f}")
                    time.sleep(2)
                    
            except Exception as e:
                print(f"⚠️ {chain['name']} error: {e}")
                continue
        
        print("⏳ Waiting 60 seconds...\n")
        time.sleep(60)
        
    except Exception as e:
        print(f"💥 Main error: {e}")
        time.sleep(60)