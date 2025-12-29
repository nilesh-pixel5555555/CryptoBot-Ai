import os
import ccxt
import pandas as pd
import numpy as np
import asyncio
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Bot
from flask import Flask, jsonify, render_template_string
import threading
import time
import traceback
from dotenv import load_dotenv

# --- CONFIGURATION ---
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
# Assets to monitor
CRYPTOS = [s.strip() for s in os.getenv("CRYPTOS", "BTC/USDT,ETH/USDT,SOL/USDT").split(',')]
TIMEFRAME_MAIN = "4h"  # Major Trend
TIMEFRAME_ENTRY = "1h" # Entry Precision

# --- SAFETY CHECK ---
if not TELEGRAM_BOT_TOKEN:
    print("CRITICAL ERROR: TELEGRAM_BOT_TOKEN is missing from Environment Variables!")
    # We do not stop execution here to allow Gunicorn to report the error in logs,
    # but the bot will likely fail later.

# Initialize Bot and Exchange
try:
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
except Exception as e:
    print(f"Error initializing Bot: {e}")

exchange = ccxt.kraken({'enableRateLimit': True, 'rateLimit': 2000})

bot_stats = {
    "status": "initializing",
    "total_analyses": 0,
    "last_analysis": None,
    "monitored_assets": CRYPTOS,
    "uptime_start": datetime.now().isoformat(),
    "version": "V2.5 Premium Quant"
}

# --- TRACKING LIST ---
daily_trades = []

# =========================================================================
# === LOGIC ===
# =========================================================================

def calculate_cpr_levels(df_daily):
    if df_daily.empty or len(df_daily) < 2: return None
    prev_day = df_daily.iloc[-2]
    H, L, C = prev_day['high'], prev_day['low'], prev_day['close']
    PP = (H + L + C) / 3.0
    BC = (H + L) / 2.0
    TC = PP - BC + PP
    return {
        'PP': PP, 'TC': TC, 'BC': BC,
        'R1': 2*PP - L, 'S1': 2*PP - H,
        'R2': PP + (H - L), 'S2': PP - (H - L)
    }

def fetch_data_safe(symbol, timeframe):
    max_retries = 3
    for attempt in range(max_retries):
        try:
            if not exchange.markets: exchange.load_markets()
            market_id = exchange.market(symbol)['id']
            ohlcv = exchange.fetch_ohlcv(market_id, timeframe, limit=100, params={'timeout': 20000})
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            df['sma9'] = df['close'].rolling(9).mean()
            df['sma20'] = df['close'].rolling(20).mean()
            return df.dropna()
        except:
            if attempt < max_retries - 1: time.sleep(5)
    return pd.DataFrame()

def send_daily_report():
    global daily_trades
    if not daily_trades:
        return 

    wins = 0
    total_signals = len(daily_trades)

    for trade in daily_trades:
        try:
            df = fetch_data_safe(trade['symbol'], '1h')
            future_data = df[df.index > trade['time']]
            if not future_data.empty:
                if trade['side'] == 'BUY':
                    if future_data['high'].max() >= trade['tp']:
                        wins += 1
                elif trade['side'] == 'SELL':
                    if future_data['low'].min() <= trade['tp']:
                        wins += 1
        except:
            continue

    msg = (
        f"📅 <b>DAILY REPORT</b>\n"
        f"Signals: {total_signals}\n"
        f"Targets Hit: {wins}\n"
    )
    try:
        asyncio.run(bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode='HTML'))
    except Exception as e:
        print(f"Report Error: {e}")
    
    daily_trades = []

def generate_and_send_signal(symbol):
    global bot_stats
    global daily_trades
    try:
        df_4h = fetch_data_safe(symbol, TIMEFRAME_MAIN)
        df_1h = fetch_data_safe(symbol, TIMEFRAME_ENTRY)
        
        if not exchange.markets: exchange.load_markets()
        market_id = exchange.market(symbol)['id']
        ohlcv_d = exchange.fetch_ohlcv(market_id, '1d', limit=5)
        df_d = pd.DataFrame(ohlcv_d, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        cpr = calculate_cpr_levels(df_d)

        if df_4h.empty or df_1h.empty or cpr is None: return

        price = df_4h.iloc[-1]['close']
        trend_4h = "BULLISH" if df_4h.iloc[-1]['sma9'] > df_4h.iloc[-1]['sma20'] else "BEARISH"
        trend_1h = "BULLISH" if df_1h.iloc[-1]['sma9'] > df_1h.iloc[-1]['sma20'] else "BEARISH"
        
        signal = "WAIT (Neutral)"
        emoji = "⏳"
        
        if trend_4h == "BULLISH" and trend_1h == "BULLISH" and price > cpr['PP']:
            signal = "STRONG BUY"
            emoji = "🚀"
        elif trend_4h == "BEARISH" and trend_1h == "BEARISH" and price < cpr['PP']:
            signal = "STRONG SELL"
            emoji = "🔻"

        is_buy = "BUY" in signal
        tp1 = cpr['R1'] if is_buy else cpr['S1']
        tp2 = cpr['R2'] if is_buy else cpr['S2']
        sl = min(cpr['BC'], cpr['TC']) if is_buy else max(cpr['BC'], cpr['TC'])

        if "STRONG" in signal:
            daily_trades.append({
                'symbol': symbol,
                'tp': tp1,
                'side': 'BUY' if is_buy else 'SELL',
                'time': df_1h.index[-1]
            })

            message = (
                f"╔════════════════════════════════╗\n"
                f"  🏆 <b>PREMIUM AI SIGNAL</b>\n"
                f"╚════════════════════════════════╝\n\n"
                f"<b>Asset:</b> {symbol}\n"
                f"<b>Price:</b> <code>{price:,.2f}</code>\n\n"
                f"--- 🚨 {emoji} <b>SIGNAL: {signal}</b> 🚨 ---\n\n"
                f"<b>📈 CONFLUENCE ANALYSIS:</b>\n"
                f"• 4H Trend: <code>{trend_4h}</code>\n"
                f"• 1H Trend: <code>{trend_1h}</code>\n"
                f"• Pivot: {'Above' if price > cpr['PP'] else 'Below'} PP\n\n"
                f"<b>🎯 TRADE TARGETS:</b>\n"
                f"✅ <b>Take Profit 1:</b> <code>{tp1:,.2f}</code>\n"
                f"🔥 <b>Take Profit 2:</b> <code>{tp2:,.2f}</code>\n"
                f"🛑 <b>Stop Loss:</b> <code>{sl:,.2f}</code>\n\n"
                f"----------------------------------------\n"
                f"<i>Powered by Advanced CryptoBotAi</i>"
            )

            asyncio.run(bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='HTML'))
        
        bot_stats['total_analyses'] += 1
        bot_stats['last_analysis'] = datetime.now().isoformat()
        bot_stats['status'] = "operational"

    except Exception as e:
        print(f"❌ Analysis failed for {symbol}: {e}")

# =========================================================================
# === STARTUP ===
# =========================================================================

def start_bot():
    print(f"🚀 Initializing {bot_stats['version']}...")
    scheduler = BackgroundScheduler()
    for s in CRYPTOS:
        scheduler.add_job(generate_and_send_signal, 'cron', minute='0,30', args=[s.strip()])
    
    scheduler.add_job(send_daily_report, 'interval', hours=24)
    scheduler.start()
    
    for s in CRYPTOS:
        threading.Thread(target=generate_and_send_signal, args=(s.strip(),)).start()

# Only run start_bot if this is NOT being imported by gunicorn unexpectedly
# (Though for this simple bot, we usually want it to run on import)
try:
    start_bot()
except Exception as e:
    print(f"Error starting bot threads: {e}")

app = Flask(__name__)

@app.route('/')
def home():
    return render_template_string("<h1>Bot Running</h1>")

@app.route('/health')
def health(): return jsonify({"status": "healthy"}), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
