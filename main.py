import os, time, requests
from datetime import datetime, timedelta
from flask import Flask
from threading import Thread

WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
recent = {}

app = Flask('')

@app.route('/')
def home():
    return "✅ Bot is Running!"

Thread(target=lambda: app.run(host='0.0.0.0', port=8080)).start()

def send(msg):
    if WEBHOOK:
        try:
            requests.post(WEBHOOK, json={"content": msg})
        except:
            pass

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
print("🚀 Bot Starting...")
send("🚀 **Bot Started!** Scanning BSC & Solana...")

while True:
    try:
        for chain in [{"n": "BSC", "q": "bsc"}, {"n": "SOL", "q": "solana"}]:
            try:
                resp = requests.get(f"https://api.dexscreener.com/latest/dex/search?q={chain['q']}", timeout=10)
                if resp.status_code != 200:
                    continue
                pairs = resp.json().get("pairs", [])[:5]
                for p in pairs:
                    addr = p.get("baseToken", {}).get("address")
                    sym = p.get("baseToken", {}).get("symbol", "?")
                    price = p.get("priceUsd")
                    liq = float(p.get("liquidity", {}).get("usd", 0))
                    vol = float(p.get("volume", {}).get("h24", 0))
                    chg = float(p.get("priceChange", {}).get("m5", 0))

                    if not addr or not price or liq < 20000 or vol < 15000 or chg < 5:
                        continue

                    now = datetime.now()
                    if addr in recent and (now - recent[addr]) < timedelta(minutes=30):
                        continue

                    safe, bt, st = check(addr)
                    if not safe or bt > 5 or st > 5:
                        continue

                    recent[addr] = now
                    send(f"🚨 [{chain['n']}] ${sym}\n💰 ${price} | +{chg}%\n💧 ${liq:,.0f}\n🔗 https://dexscreener.com/{chain['q']}/{addr}")
                    time.sleep(2)
            except:
                pass
    except:
        pass
    time.sleep(60)