import os
import time
import requests
from datetime import datetime, timedelta
from flask import Flask
from threading import Thread

# === CONFIGURATION (Set in Railway Variables) ===
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")  # Your public BSC address
PRIVATE_KEY = os.getenv("PRIVATE_KEY")  # NEVER share - for Phase 3 only

# === RISK SETTINGS (START SMALL!) ===
PAPER_MODE = True  # ← Set to False ONLY when ready for real trades
MAX_TRADE_SIZE = 2.0  # Max $2 per trade (START WITH $1!)
STOP_LOSS_PCT = -10  # Sell if down 10%
TAKE_PROFIT_PCT = 20  # Sell if up 20%
MAX_POSITIONS = 2  # Max open trades at once
DAILY_LOSS_LIMIT = -5  # Stop trading if down $5 today
MIN_LIQUIDITY = 50000  # Min $50k liquidity
MIN_VOLUME = 30000  # Min $30k 24h volume
MIN_CHANGE = 8  # Min +8% price change in 5min
MIN_AGE_HOURS = 24  # Token must be 24h+ old

# === GLOBAL STATE ===
recent = {}  # Cooldown: prevent repeat alerts
active_trades = {}  # Track open paper positions
daily_pnl = 0.0  # Track today's profit/loss
last_reset = datetime.now().date()

app = Flask('')

@app.route('/')
def home():
    mode = "PAPER" if PAPER_MODE else "LIVE"
    return f"Auto-Trading Bot {mode} - Max Trade: ${MAX_TRADE_SIZE} - BSC Only"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

Thread(target=run_flask).start()

def send(msg):
    """Send Discord alert"""
    if WEBHOOK:
        try:
            requests.post(WEBHOOK, json={"content": msg})
            print("Discord: " + msg[:50] + "...")
        except Exception as e:
            print("Discord Error: " + str(e))
def get_bnb_balance():
    """Check wallet BNB balance (BSCScan API)"""
    if not WALLET_ADDRESS:
        return 0
    try:
        url = "https://api.bscscan.com/api?module=account&action=balance&address=" + WALLET_ADDRESS + "&tag=latest&apikey=YourFreeBscScanKey"
        resp = requests.get(url, timeout=5)
        data = resp.json()
        if data.get("status") == "1":
            return float(data["result"]) / 1e18  # Wei to BNB
    except:
        pass
    return 0

def simulate_trade(symbol, price, amount_usd, side):
    """
    Paper trade simulator with realistic slippage and gas
    Returns: dict with fill price, slippage, gas cost
    """
    # Estimate slippage: 0.5% base + 0.1% per $1000 trade size
    slippage = 0.5 + (amount_usd / 1000) * 0.1
    gas_cost = 0.25  # BSC gas ~$0.25
    
    if side == "buy":
        fill_price = price * (1 + slippage / 100)
        total_cost = amount_usd + gas_cost
    else:
        fill_price = price * (1 - slippage / 100)
        total_cost = gas_cost
    
    return {
        "success": True,
        "side": side,
        "symbol": symbol,
        "requested": amount_usd,
        "fill_price": round(fill_price, 8),
        "slippage_pct": round(slippage, 2),
        "gas_cost": gas_cost,
        "total_cost": round(total_cost, 2)
    }

def check_security(addr):
    """GoPlus security check"""
    try:
        url = "https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses=" + addr
        r = requests.get(url, timeout=5)
        if r.status_code != 200:
            return True, 0, 0, True
        data = r.json()
        result = data.get("result", {})
        token = result.get(addr.lower(), {})
        
        is_honeypot = token.get("is_honeypot", "1") == "0"
        buy_tax = float(token.get("buy_tax", 0) or 0)
        sell_tax = float(token.get("sell_tax", 0) or 0)
        owner_renounced = token.get("is_owner_changed", "0") == "1"
        
        return is_honeypot, buy_tax, sell_tax, owner_renounced
    except:
        return True, 0, 0, True

def calculate_score(liq, vol, chg, sec_score):
    """Simple 0-100 signal score"""
    score = 0
    if liq >= 100000: score += 25
    elif liq >= 50000: score += 15
    if vol / max(liq, 1) > 1: score += 20  # Volume > Liquidity
    if chg >= 10: score += 25
    elif chg >= 8: score += 15
    score += sec_score * 30 // 100  # Security weight
    risk = "low" if score >= 80 else "medium" if score >= 60 else "high"
    return min(100, score), risk

# === STARTUP ===
print("Auto-Trading Bot starting...")
mode_text = "🧪 PAPER MODE" if PAPER_MODE else "💰 LIVE MODE"
send(f"🤖 {mode_text} Started\n" +
     f"Max Trade: ${MAX_TRADE_SIZE} | Stop-Loss: {STOP_LOSS_PCT}%\n" +
     f"Take-Profit: {TAKE_PROFIT_PCT}% | Max Positions: {MAX_POSITIONS}")
time.sleep(2)

# Check wallet on startup
balance = get_bnb_balance()
print(f"Wallet BNB: {balance:.4f} (~${balance * 200:.2f})")
if balance < 0.01 and not PAPER_MODE:
    send("⚠️ Warning: Low BNB balance. Add BNB for gas fees.")

# === MAIN LOOP ===
while True:
    try:
        # Reset daily P&L if new day
        today = datetime.now().date()
        if today != last_reset:
            daily_pnl = 0.0
            last_reset = today
            print("Daily P&L reset")
        
        print(f"\n🔍 Scanning BSC... (P&L today: ${daily_pnl:.2f})")
        chain = {"name": "BSC", "query": "bsc", "id": "56"}
        
        try:
            # Fetch pairs from DexScreener
            url = "https://api.dexscreener.com/latest/dex/search?q=" + chain["query"]
            resp = requests.get(url, timeout=10)
            
            if resp.status_code != 200:
                print("BSC API error")
                time.sleep(60)
                continue
            
            json_data = resp.json()
            pairs = json_data.get("pairs", [])
            
            if not pairs:
                continue
            
            print(f"Found {len(pairs[:10])} pairs, evaluating...")
            
            for p in pairs[:10]:
                base = p.get("baseToken", {})
                addr = base.get("address")
                sym = base.get("symbol", "?")
                price = p.get("priceUsd")
                
                if not addr or not price:
                    continue
                
                # Extract metrics
                liq = float(p.get("liquidity", {}).get("usd", 0))
                vol = float(p.get("volume", {}).get("h24", 0))
                chg = float(p.get("priceChange", {}).get("m5", 0))
                mcap = float(p.get("marketCap", 0))
                
                # === FILTERS ===
                if liq < MIN_LIQUIDITY or vol < MIN_VOLUME or chg < MIN_CHANGE or mcap < 50000:
                    continue
                
                # Age check
                created = p.get("pairCreatedAt")
                if created:
                    now_ms = datetime.now().timestamp() * 1000
                    age_hours = (now_ms - created) / (1000 * 60 * 60)
                    if age_hours < MIN_AGE_HOURS:
                        continue
                
                # Cooldown (no repeat alerts within 30 min)
                now = datetime.now()
                if addr in recent and (now - recent[addr]).seconds < 1800:                    continue
                
                # Security check
                is_safe, buy_tax, sell_tax, owner_renounced = check_security(addr)
                if not is_safe or buy_tax > 5 or sell_tax > 5:
                    continue
                
                # === POSITION MONITORING (Check existing trades first) ===
                if addr in active_trades:
                    entry = active_trades[addr]["entry_price"]
                    pnl_pct = ((float(price) - entry) / entry) * 100
                    
                    # Stop-loss
                    if pnl_pct <= STOP_LOSS_PCT:
                        result = simulate_trade(sym, float(price), active_trades[addr]["amount"], "sell")
                        pnl_usd = (float(price) - entry) * active_trades[addr]["quantity"]
                        daily_pnl += pnl_usd
                        send(f"🔴 STOP-LOSS ${sym}\nExit: ${price}\nP/L: ${pnl_usd:.2f} ({pnl_pct:.1f}%)\nReason: Hit stop-loss")
                        print(f"Closed {sym}: stop-loss")
                        del active_trades[addr]
                        recent[addr] = now
                        continue
                    
                    # Take-profit
                    elif pnl_pct >= TAKE_PROFIT_PCT:
                        result = simulate_trade(sym, float(price), active_trades[addr]["amount"], "sell")
                        pnl_usd = (float(price) - entry) * active_trades[addr]["quantity"]
                        daily_pnl += pnl_usd
                        send(f"🟢 TAKE-PROFIT ${sym}\nExit: ${price}\nP/L: +${pnl_usd:.2f} (+{pnl_pct:.1f}%)\nReason: Hit target")
                        print(f"Closed {sym}: take-profit")
                        del active_trades[addr]
                        recent[addr] = now
                        continue
                    
                    # Still holding - skip new alert
                    continue
                
                # === RISK CHECKS FOR NEW TRADE ===
                
                # Daily loss limit
                if daily_pnl <= DAILY_LOSS_LIMIT:
                    print("Daily loss limit hit - pausing trades")
                    time.sleep(300)
                    continue
                
                # Max positions
                if len(active_trades) >= MAX_POSITIONS:
                    print("Max positions reached - skipping")
                    continue
                                # Wallet balance check (for live mode)
                if not PAPER_MODE:
                    balance = get_bnb_balance()
                    if balance * 200 < MAX_TRADE_SIZE + 1:  # Need trade + gas
                        print("Low balance - skipping")
                        continue
                
                # === CALCULATE POSITION SIZE ===
                trade_amount = min(MAX_TRADE_SIZE, liq * 0.01)  # Max 1% of liquidity
                if trade_amount < 1:  # Minimum viable trade
                    continue
                
                # === SIMULATE/EXECUTE BUY ===
                result = simulate_trade(sym, float(price), trade_amount, "buy")
                
                if not result["success"]:
                    continue
                
                # Record the trade
                quantity = trade_amount / result["fill_price"]
                active_trades[addr] = {
                    "entry_price": result["fill_price"],
                    "amount": trade_amount,
                    "quantity": quantity,
                    "timestamp": now
                }
                recent[addr] = now
                
                # Calculate signal score
                sec_score = 100 if is_safe else 0
                score, risk = calculate_score(liq, vol, chg, sec_score)
                risk_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(risk, "⚪")
                
                # Send alert
                mode_tag = "🧪 PAPER" if PAPER_MODE else "💰 LIVE"
                alert = (f"{mode_tag} {risk_emoji} BUY [{chain['name']}] ${sym}\n"
                        f"Score: {score}/100 | Entry: ${result['fill_price']:.8f}\n"
                        f"Amount: ${trade_amount:.2f} | Slippage: {result['slippage_pct']}%\n"
                        f"Gas: ${result['gas_cost']} | Total: ${result['total_cost']:.2f}\n"
                        f"Stop-Loss: {STOP_LOSS_PCT}% | Take-Profit: {TAKE_PROFIT_PCT}%\n"
                        f"Liq: ${liq:,.0f} | Vol: ${vol:,.0f} | Tax: {buy_tax}%/{sell_tax}%\n"
                        f"https://dexscreener.com/bsc/{addr}")
                
                send(alert)
                print(f"{'PAPER' if PAPER_MODE else 'LIVE'} BUY: {sym} @ ${result['fill_price']:.8f}")
                
                time.sleep(2)  # Avoid rate limits
                
        except Exception as e:
            print("BSC scan error: " + str(e))
            continue
        
        print("⏳ Waiting 60 seconds...\n")
        time.sleep(60)
        
    except Exception as e:
        print("Main loop error: " + str(e))
        time.sleep(60)