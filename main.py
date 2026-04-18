import os, time, requests
from datetime import datetime, timedelta
from flask import Flask
from threading import Thread

WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
recent = {}

app = Flask('')

@app.route('/')
def home():
    return "✅ Bot is Running! Filters: Liq $50k+, Vol $30k+, Change 8%+, Age 24h+"

Thread(target=lambda: app.run(host='0.0.0.0', port=8080)).start()

def send(msg):
    if WEBHOOK:
        try:
            requests.post(WEBHOOK, json={"content": msg})
            print(f"✅ Alert sent")
        except Exception as e:
            print(f"❌ Discord Error: {e}")

def check(addr):
    try:
        r = requests.get(f"https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses={addr}", timeout=5)
        if r.status_code != 200:
            return True, 0, 0
        d = r.json().get("result", {}).get(addr.lower(), {})
        return d.get("is_honeypot", "1") == "0", float(d.get("buy_tax", 0)), float(d.get("sell_tax", 0))
    except:
        return True, 0, 0

# Startup message
print("🚀 Bot Starting with TIGHTER filters...")
send("🚀 **Bot Updated!** Stricter filters active:\n💧 Liq: $50k+\n📊 Vol: $30k+\n📈 Change: 8%+\n⏰ Age: 24h+")
time.sleep(2)

while True:
    try:
        print(f"\n🔍 Scanning... Active filters: Liq $50k+, Vol $30k+, Change 8%+")

        for chain in [{"n": "BSC", "q": "bsc"}, {"n": "SOL", "q": "solana"}]:
            try:
                resp = requests.get(f"https://api.dexscreener.com/latest/dex/search?q={chain['q']}", timeout=10)

                if resp.status_code != 200:
                    print(f"⚠️ {chain['n']} API error: {resp.status_code}")
                    continue                
                pairs = resp.json().get("pairs", [])

                if not pairs:
                    print(f"⚠️ No pairs for {chain['n']}")
                    continue

                print(f"✅ {chain['n']}: Checking {len(pairs[:10])} pairs...")

                for p in pairs[:10]:  # Check top 10 instead of 5
                    addr = p.get("baseToken", {}).get("address")
                    sym = p.get("baseToken", {}).get("symbol", "?")
                    price = p.get("priceUsd")

                    if not addr or not price:
                        continue

                    # Get metrics
                    liq = float(p.get("liquidity", {}).get("usd", 0))
                    vol = float(p.get("volume", {}).get("h24", 0))
                    chg = float(p.get("priceChange", {}).get("m5", 0))
                    mcap = float(p.get("marketCap", 0))

                    # TIGHTER FILTERS
                    if liq < 50000:  # Increased from $20k to $50k
                        continue
                    if vol < 30000:  # Increased from $15k to $30k
                        continue
                    if chg < 8:  # Increased from 5% to 8%
                        continue
                    if mcap < 50000:  # NEW: Min $50k market cap
                        continue

                    # Check token age (if available)
                    pair_created_at = p.get("pairCreatedAt")
                    if pair_created_at:
                        token_age_hours = (datetime.now().timestamp() * 1000 - pair_created_at) / (1000 * 60 * 60)
                        if token_age_hours < 24:  # NEW: Token must be at least 24h old
                            print(f"⏰ {sym}: Too new ({token_age_hours:.1f}h), skipping")
                            continue

                    # Check cooldown
                    now = datetime.now()
                    if addr in recent and (now - recent[addr]) < timedelta(minutes=30):
                        continue

                    # Security check
                    safe, bt, st = check(addr)
                    if not safe or bt > 5 or st > 5:
                        print(f"🛡️ {sym}: Failed security check")                        continue

                    # Send alert
                    recent[addr] = now
                    msg = (f"🚨 **HIGH-QUALITY** [{chain['n']}] ${sym}\n"
                           f"💰 ${price} | +{chg}%\n"
                           f"💧 ${liq:,.0f} | Vol ${vol:,.0f}\n"
                           f"📊 MC ${mcap:,.0f}\n"
                           f"🛡️ Tax: {bt}%/{st}%\n"
                           f"🔗 https://dexscreener.com/{chain['q']}/{addr}")

                    send(msg)
                    print(f"✅ ALERT SENT: {sym} (Liq: ${liq:,.0f}, Vol: ${vol:,.0f}, +{chg}%)")
                    time.sleep(2)

            except Exception as e:
                print(f"⚠️ {chain['n']} scan error: {e}")
                continue

        print("⏳ Waiting 60 seconds before next scan...\n")
        time.sleep(60)

    except Exception as e:
        print(f"💥 Critical Error: {e}")
        time.sleep(60)