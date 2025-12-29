import os
import ccxt
import pandas as pd
import numpy as np
import asyncio
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Bot
from flask import Flask, jsonify, render_template_string
import threading
import time
import traceback 

# --- CONFIGURATION ---
from dotenv import load_dotenv 
load_dotenv() 

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
# Assets to monitor
CRYPTOS = [s.strip() for s in os.getenv("CRYPTOS", "BTC/USDT,ETH/USDT,SOL/USDT").split(',')]
TIMEFRAME_MAIN = "4h"  # Major Trend
TIMEFRAME_ENTRY = "1h" # Entry Precision

# Initialize Bot and Exchange (Kraken Public)
bot = Bot(token=TELEGRAM_BOT_TOKEN)
exchange = ccxt.kraken({'enableRateLimit': True, 'rateLimit': 2000})

bot_stats = {
    "status": "initializing",
    "total_analyses": 0,
    "last_analysis": None,
    "monitored_assets": CRYPTOS,
    "uptime_start": datetime.now().isoformat(),
    "version": "V2.5 Premium Quant"
}

# ---------------------------------------------------------
# [ADDED] 1. List to store trades for the 24-hour report
# ---------------------------------------------------------
daily_trades = []

# =========================================================================
# === ADVANCED QUANT LOGIC ===
# =========================================================================

def calculate_cpr_levels(df_daily):
    """Calculates Daily Pivot Points for Professional Target Setting."""
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
    """Robust fetcher with retries and Kraken ID normalization."""
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

# ---------------------------------------------------------
# [ADDED] 2. The Function that creates the 24-hour report
# ---------------------------------------------------------
def send_daily_report():
    global daily_trades
    
    # If no trades happened in 24 hours, do nothing (or you can send "No trades today")
    if not daily_trades:
        return 

    wins = 0
    total_signals = len(daily_trades)

    # Check which trades hit their target
    for trade in daily_trades:
        try:
            # Get latest data to see if price hit the target
            df = fetch_data_safe(trade['symbol'], '1h')
            # Look at data only AFTER the signal was sent
            future_data = df[df.index > trade['time']]
            
            if not future_data.empty:
                if trade['side'] == 'BUY':
                    # Did price go higher than TP?
                    if future_data['high'].max() >= trade['tp']:
                        wins += 1
                else: # SELL
                    # Did price go lower than TP?
                    if future_data['low'].min() <= trade['tp']:
                        wins += 1
        except:
            continue

    # Create the message
    msg = (
        f"📅 <b>DAILY TRADING REPORT</b>\n"
        f"--------------------------\n"
        f"Signals Sent: {total_signals}\n"
        f"Targets Hit: {wins}\n"
        f"--------------------------\n"
        f"<i>Reseting for next 24h...</i>"
    )
    
    # Send it
    try:
        asyncio.run(bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode='HTML'))
    except Exception as e:
        print(f"Report Error: {e}")
    
    # Reset the list for tomorrow
    daily_trades = []

# =========================================================================
# === MULTI-TIMEFRAME CONFLUENCE ENGINE ===
# =========================================================================

def generate_and_send_signal(symbol):
    global bot_stats
    global daily_trades # Allow access to the list
    try:
        # 1. Fetch Multi-Timeframe Data
        df_4h = fetch_data_safe(symbol, TIMEFRAME_MAIN)
        df_1h = fetch_data_safe(symbol, TIMEFRAME_ENTRY)
        
        # Fetch Daily for CPR Targets
        if not exchange.markets: exchange.load_markets()
        market_id = exchange.market(symbol)['id']
        ohlcv_d = exchange.fetch_ohlcv(market_id, '1d', limit=5)
        df_d = pd.DataFrame(ohlcv_d, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        cpr = calculate_cpr_levels(df_d)

        if df_4h.empty or df_1h.empty or cpr is None: return

        # 2. Extract Key Values
        price = df_4h.iloc[-1]['close']
        trend_4h = "BULLISH" if df_4h.iloc[-1]['sma9'] > df_4h.iloc[-1]['sma20'] else "BEARISH"
        trend_1h = "BULLISH" if df_1h.iloc[-1]['sma9'] > df_1h.iloc[-1]['sma20'] else "BEARISH"
        
        # 3. Master Signal Logic (Confluence)
        signal = "WAIT (Neutral)"
        emoji = "⏳"
        
        if trend_4h == "BULLISH" and trend_1h == "BULLISH" and price > cpr['PP']:
            signal = "STRONG BUY"
            emoji = "🚀"
        elif trend_4h == "BEARISH" and trend_1h == "BEARISH" and price < cpr['PP']:
            signal = "STRONG SELL"
            emoji = "🔻"

        # 4. Calculate Risk/Reward Targets
        is_buy = "BUY" in signal
        tp1 = cpr['R1'] if is_buy else cpr['S1']
        tp2 = cpr['R2'] if is_buy else cpr['S2']
        sl = min(cpr['BC'], cpr['TC']) if is_buy else max(cpr['BC'], cpr['TC'])

        # --- PREMIUM HTML TEMPLATE ---
        if "STRONG" in signal:
            
            # ---------------------------------------------------------
            # [ADDED] 3. Save the trade details when a signal occurs
            # ---------------------------------------------------------
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
                f"<i>Powered by Advanced CPR By Nilesh</i>"
            )

            asyncio.run(bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='HTML'))
        
        bot_stats['total_analyses'] += 1
        bot_stats['last_analysis'] = datetime.now().isoformat()
        bot_stats['status'] = "operational"

    except Exception as e:
        print(f"❌ Analysis failed for {symbol}: {e}")

# =========================================================================
# === GUNICORN-SAFE INITIALIZATION ===
# =========================================================================

def start_bot():
    print(f"🚀 Initializing {bot_stats['version']}...")
    scheduler = BackgroundScheduler()
    for s in CRYPTOS:
        # Schedule for every hour and half-hour (THIS IS YOUR ORIGINAL 30 MIN LOGIC)
        scheduler.add_job(generate_and_send_signal, 'cron', minute='0,30', args=[s.strip()])
    
    # ---------------------------------------------------------
    # [ADDED] 4. Schedule the Report to run every 24 Hours
    # ---------------------------------------------------------
    scheduler.add_job(send_daily_report, 'interval', hours=24)

    scheduler.start()
    
    # Run first analysis immediately in the background
    for s in CRYPTOS:
        threading.Thread(target=generate_and_send_signal, args=(s.strip(),)).start()

start_bot()

app = Flask(__name__)

@app.route('/')
def home():
    return render_template_string("""
        <body style="font-family:sans-serif; background:#0f172a; color:#f8fafc; text-align:center; padding-top:100px;">
            <div style="background:#1e293b; display:inline-block; padding:40px; border-radius:15px; border: 1px solid #334155;">
                <h1 style="color:#22d3ee;">AI Quant Dashboard</h1>
                <p style="font-size:1.2em;">Status: <span style="color:#4ade80;">Active</span></p>
                <p>Analyses Streamed: <b>{{a}}</b></p>
                <p>Version: <i>{{v}}</i></p>
                <hr style="border-color:#334155;">
                <p style="font-size:0.8em; color:#94a3b8;">{{t}}</p>
            </div>
        </body>
    """, a=bot_stats['total_analyses'], v=bot_stats['version'], t=bot_stats['last_analysis'])

@app.route('/health')
def health(): return jsonify({"status": "healthy"}), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
