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
    return "Bot Running - Filters: Liq $50k+, Vol $30k+, Age 24h+"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

Thread(target=run_flask).start()

def send(msg):
    if WEBHOOK:
        try:
            requests.post(WEBHOOK, json={"content": msg})
            print("Alert sent to Discord")
        except Exception as e:
            print("Discord Error: " + str(e))

def check_security(addr):
    try:
        url = "https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses=" + addr
        r = requests.get(url, timeout=5)
        if r.status_code != 200:
            return True, 0, 0, True
        data = r.json()
        result = data.get("result", {})
        token_data = result.get(addr.lower(), {})
        
        is_honeypot = token_data.get("is_honeypot", "1")
        buy_tax = float(token_data.get("buy_tax", 0))
        sell_tax = float(token_data.get("sell_tax", 0))
        owner_change = token_data.get("is_owner_changed", "0")
        
        is_safe = (is_honeypot == "0")
        owner_renounced = (owner_change == "1")
        
        return is_safe, buy_tax, sell_tax, owner_renounced
    except:
        return True, 0, 0, True
print("Bot starting with updated filters...")
send("Bot Updated - Stricter filters active:\nLiquidity: $50k+\nVolume: $30k+\nPrice Change: 8%+\nToken Age: 24h+")
time.sleep(2)

while True:
    try:
        print("Scanning markets...")
        
        chains = [
            {"name": "BSC", "query": "bsc"},
            {"name": "SOL", "query": "solana"}
        ]
        
        for chain in chains:
            try:
                url = "https://api.dexscreener.com/latest/dex/search?q=" + chain["query"]
                resp = requests.get(url, timeout=10)
                
                if resp.status_code != 200:
                    print(chain["name"] + " API error")
                    continue
                
                json_data = resp.json()
                pairs = json_data.get("pairs", [])
                
                if not pairs:
                    continue
                
                print(chain["name"] + ": Found " + str(len(pairs[:10])) + " pairs")
                
                for p in pairs[:10]:
                    base_token = p.get("baseToken", {})
                    addr = base_token.get("address")
                    sym = base_token.get("symbol", "?")
                    price = p.get("priceUsd")
                    
                    if not addr or not price:
                        continue
                    
                    liq = float(p.get("liquidity", {}).get("usd", 0))
                    vol = float(p.get("volume", {}).get("h24", 0))
                    chg = float(p.get("priceChange", {}).get("m5", 0))
                    mcap = float(p.get("marketCap", 0))
                    
                    # FILTER 1: Liquidity >= $50,000
                    if liq < 50000:
                        continue
                    
                    # FILTER 2: Volume >= $30,000                    if vol < 30000:
                        continue
                    
                    # FILTER 3: Price change >= 8%
                    if chg < 8:
                        continue
                    
                    # FILTER 4: Market cap >= $50,000
                    if mcap < 50000:
                        continue
                    
                    # FILTER 5: Token age >= 24 hours
                    created_at = p.get("pairCreatedAt")
                    if created_at:
                        now_ms = datetime.now().timestamp() * 1000
                        age_hours = (now_ms - created_at) / (1000 * 60 * 60)
                        if age_hours < 24:
                            print(sym + " too new (" + str(round(age_hours, 1)) + "h)")
                            continue
                    
                    # FILTER 6: Cooldown (no repeat alerts within 30 min)
                    now = datetime.now()
                    if addr in recent:
                        time_diff = (now - recent[addr]).seconds
                        if time_diff < 1800:
                            continue
                    
                    # FILTER 7: Security check (GoPlus API)
                    is_safe, buy_tax, sell_tax, owner_renounced = check_security(addr)
                    if not is_safe:
                        print(sym + " failed security check")
                        continue
                    if buy_tax > 5 or sell_tax > 5:
                        print(sym + " tax too high: " + str(buy_tax) + "%/" + str(sell_tax) + "%")
                        continue
                    
                    # All filters passed - send alert
                    recent[addr] = now
                    
                    alert_text = "HIGH QUALITY ALERT [" + chain["name"] + "] $" + sym + "\n"
                    alert_text += "Price: $" + str(price) + " | +" + str(chg) + "%\n"
                    alert_text += "Liquidity: $" + str(int(liq)) + "\n"
                    alert_text += "Volume (24h): $" + str(int(vol)) + "\n"
                    alert_text += "Market Cap: $" + str(int(mcap)) + "\n"
                    alert_text += "Tax: " + str(buy_tax) + "%/" + str(sell_tax) + "%\n"
                    alert_text += "https://dexscreener.com/" + chain["query"] + "/" + addr
                    
                    send(alert_text)
                    print("ALERT SENT: " + sym)
                    time.sleep(2)                    
            except Exception as e:
                print(chain["name"] + " scan error: " + str(e))
                continue
        
        print("Waiting 60 seconds before next scan...")
        time.sleep(60)
        
    except Exception as e:
        print("Main loop error: " + str(e))
        time.sleep(60)