import os
import time
import requests
from datetime import datetime, timedelta
from flask import Flask
from threading import Thread

WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
recent = {}

app = Flask('')

@app.route('/')
def home():
    return "Bot Running"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

Thread(target=run_flask).start()

def send(msg):
    if WEBHOOK:
        try:
            requests.post(WEBHOOK, json={"content": msg})
            print("Alert sent")
        except Exception as e:
            print("Discord Error: " + str(e))

def check_security(addr):
    try:
        r = requests.get("https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses=" + addr, timeout=5)
        if r.status_code != 200:
            return True, 0, 0
        d = r.json().get("result", {}).get(addr.lower(), {})
        is_safe = d.get("is_honeypot", "1") == "0"
        buy_tax = float(d.get("buy_tax", 0))
        sell_tax = float(d.get("sell_tax", 0))
        return is_safe, buy_tax, sell_tax
    except:
        return True, 0, 0

print("Bot starting...")
send("Bot Updated - Filters Active")
time.sleep(2)

while True:
    try:
        print("Scanning...")
        chains = [            {"name": "BSC", "query": "bsc"},
            {"name": "SOL", "query": "solana"}
        ]
        
        for chain in chains:
            try:
                url = "https://api.dexscreener.com/latest/dex/search?q=" + chain["query"]
                resp = requests.get(url, timeout=10)
                
                if resp.status_code != 200:
                    continue
                
                pairs = resp.json().get("pairs", [])
                if not pairs:
                    continue
                
                for p in pairs[:10]:
                    token = p.get("baseToken", {})
                    addr = token.get("address")
                    sym = token.get("symbol", "?")
                    price = p.get("priceUsd")
                    
                    if not addr or not price:
                        continue
                    
                    liq = float(p.get("liquidity", {}).get("usd", 0))
                    vol = float(p.get("volume", {}).get("h24", 0))
                    chg = float(p.get("priceChange", {}).get("m5", 0))
                    mcap = float(p.get("marketCap", 0))
                    
                    if liq < 50000:
                        continue
                    if vol < 30000:
                        continue
                    if chg < 8:
                        continue
                    if mcap < 50000:
                        continue
                    
                    created = p.get("pairCreatedAt")
                    if created:
                        age_ms = datetime.now().timestamp() * 1000 - created
                        age_hours = age_ms / (1000 * 60 * 60)
                        if age_hours < 24:
                            continue
                    
                    now = datetime.now()
                    if addr in recent and (now - recent[addr]).seconds < 1800:
                        continue
                    safe, bt, st = check_security(addr)
                    if not safe or bt > 5 or st > 5:
                        continue
                    
                    recent[addr] = now
                    
                    alert_msg = "ALERT [" + chain["name"] + "] $" + sym + "\n"
                    alert_msg += "Price: $" + str(price) + " | +" + str(chg) + "%\n"
                    alert_msg += "Liq: $" + str(int(liq)) + " | Vol: $" + str(int(vol)) + "\n"
                    alert_msg += "https://dexscreener.com/" + chain["query"] + "/" + addr
                    
                    send(alert_msg)
                    print("Sent: " + sym)
                    time.sleep(2)
                    
            except Exception as e:
                print("Error: " + str(e))
                continue
        
        print("Waiting 60s...")
        time.sleep(60)
        
    except Exception as e:
        print("Main error: " + str(e))
        time.sleep(60)