"""
Gold Trend-Following Trading Bot for Capital.com (EMA Crossover)
====================================================================

A SHORTER-TERM version of trend-following: 9/21 EMA crossover on
HOURLY candles (rather than the daily/4H timeframe trend-following
normally uses), checked once per hour.

LOGIC:
  - Fast EMA (9) crosses ABOVE slow EMA (21)  -> bullish flip -> close any
    short, open LONG
  - Fast EMA (9) crosses BELOW slow EMA (21)  -> bearish flip -> close any
    long, open SHORT
  - No crossover -> hold whatever position currently exists (let stop
    loss / take profit manage the exit)

SETUP: same as the mean-reversion bot - pip install requests, set
CAPITAL_API_KEY / CAPITAL_IDENTIFIER / CAPITAL_PASSWORD as env vars
(GitHub secrets in Actions), confirm GOLD_EPIC with --find-epic if you
haven't already from the first bot.

ACCOUNT SEPARATION: each strategy bot lives in its own repo with its own
Capital.com demo account and API key, so there's no need for account
switching here - positions naturally stay isolated per bot.

This targets the DEMO base URL only.
"""

import os
import sys
import time
import csv
import requests
from datetime import datetime

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

BASE_URL = "https://demo-api-capital.backend-capital.com/api/v1"

API_KEY = os.getenv("CAPITAL_API_KEY")
IDENTIFIER = os.getenv("CAPITAL_IDENTIFIER")
PASSWORD = os.getenv("CAPITAL_PASSWORD")

GOLD_EPIC = "GOLD"          # use the same epic you already confirmed works
RESOLUTION = "HOUR"         # hourly candles - shorter-term than typical daily trend-following
FAST_EMA_PERIOD = 9
SLOW_EMA_PERIOD = 21

POSITION_SIZE = 0.1

# Wider than the mean-reversion bot's stops, since trend trades are meant
# to ride bigger moves. Still placeholders - check gold's recent hourly
# ATR before trusting these.
STOP_LOSS_POINTS = 15.0
TAKE_PROFIT_POINTS = 30.0

LOG_FILE = "trend_trades_log.csv"
HEARTBEAT_FILE = "trend_bot_heartbeat.csv"

# ---------------------------------------------------------------------------
# SESSION HANDLING
# ---------------------------------------------------------------------------

class CapitalSession:
    def __init__(self):
        self.cst = None
        self.security_token = None
        self.last_auth_time = 0

    def authenticate(self):
        if not all([API_KEY, IDENTIFIER, PASSWORD]):
            sys.exit("Missing CAPITAL_API_KEY / CAPITAL_IDENTIFIER / CAPITAL_PASSWORD env vars.")
        resp = requests.post(
            f"{BASE_URL}/session",
            headers={"X-CAP-API-KEY": API_KEY, "Content-Type": "application/json"},
            json={"identifier": IDENTIFIER, "password": PASSWORD, "encryptedPassword": False},
        )
        resp.raise_for_status()
        self.cst = resp.headers["CST"]
        self.security_token = resp.headers["X-SECURITY-TOKEN"]
        self.last_auth_time = time.time()
        print(f"[{datetime.now()}] Authenticated successfully.")

    def headers(self):
        if time.time() - self.last_auth_time > 8 * 60:
            self.authenticate()
        return {
            "CST": self.cst,
            "X-SECURITY-TOKEN": self.security_token,
            "Content-Type": "application/json",
        }


session = CapitalSession()

# ---------------------------------------------------------------------------
# MARKET / EPIC DISCOVERY (run once with --find-epic, optional)
# ---------------------------------------------------------------------------

def find_gold_epic():
    session.authenticate()
    resp = requests.get(f"{BASE_URL}/markets", headers=session.headers(),
                         params={"searchTerm": "gold"})
    resp.raise_for_status()
    markets = resp.json().get("markets", [])
    if not markets:
        print("No markets found for 'gold'. Try searchTerm='XAU' instead.")
        return
    print("Matching markets:")
    for m in markets:
        print(f"  epic={m.get('epic'):<15} name={m.get('instrumentName')}")

# ---------------------------------------------------------------------------
# PRICE DATA + EMA
# ---------------------------------------------------------------------------

def get_recent_closes(num_bars=150):
    resp = requests.get(
        f"{BASE_URL}/prices/{GOLD_EPIC}",
        headers=session.headers(),
        params={"resolution": RESOLUTION, "max": num_bars},
    )
    resp.raise_for_status()
    data = resp.json().get("prices", [])
    return [(p["closePrice"]["bid"] + p["closePrice"]["ask"]) / 2 for p in data]


def calculate_ema_series(closes, period):
    if len(closes) < period:
        return None
    ema = []
    multiplier = 2 / (period + 1)
    for i, price in enumerate(closes):
        ema.append(price if i == 0 else (price - ema[-1]) * multiplier + ema[-1])
    return ema

# ---------------------------------------------------------------------------
# POSITIONS
# ---------------------------------------------------------------------------

def get_open_position():
    resp = requests.get(f"{BASE_URL}/positions", headers=session.headers())
    resp.raise_for_status()
    for pos in resp.json().get("positions", []):
        if pos["market"]["epic"] == GOLD_EPIC:
            return pos
    return None


def open_position(direction, current_price):
    if direction == "BUY":
        stop_level = current_price - STOP_LOSS_POINTS
        profit_level = current_price + TAKE_PROFIT_POINTS
    else:
        stop_level = current_price + STOP_LOSS_POINTS
        profit_level = current_price - TAKE_PROFIT_POINTS

    payload = {
        "epic": GOLD_EPIC,
        "direction": direction,
        "size": POSITION_SIZE,
        "stopLevel": round(stop_level, 2),
        "profitLevel": round(profit_level, 2),
    }
    resp = requests.post(f"{BASE_URL}/positions", headers=session.headers(), json=payload)
    resp.raise_for_status()
    deal_ref = resp.json().get("dealReference")
    print(f"[{datetime.now()}] Opened {direction} at ~{current_price:.2f} (ref {deal_ref})")
    log_trade(direction, current_price, "OPEN")
    return deal_ref


def close_position(position):
    deal_id = position["position"]["dealId"]
    resp = requests.delete(f"{BASE_URL}/positions/{deal_id}", headers=session.headers())
    resp.raise_for_status()
    print(f"[{datetime.now()}] Closed position {deal_id}")
    log_trade(position["position"]["direction"], position["position"]["level"], "CLOSE")

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

def log_trade(direction, price, action):
    file_exists = os.path.isfile(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "action", "direction", "price"])
        writer.writerow([datetime.now().isoformat(), action, direction, price])


def write_heartbeat(price, fast_ema, slow_ema, position):
    file_exists = os.path.isfile(HEARTBEAT_FILE)
    with open(HEARTBEAT_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "price", "fast_ema", "slow_ema", "position_open"])
        writer.writerow([
            datetime.now().isoformat(),
            round(price, 2) if price is not None else "",
            round(fast_ema, 2) if fast_ema is not None else "",
            round(slow_ema, 2) if slow_ema is not None else "",
            "yes" if position else "no",
        ])

# ---------------------------------------------------------------------------
# MAIN LOGIC (single run, invoked by GitHub Actions cron every hour)
# ---------------------------------------------------------------------------

def run_once():
    session.authenticate()
    try:
        closes = get_recent_closes()
        fast = calculate_ema_series(closes, FAST_EMA_PERIOD)
        slow = calculate_ema_series(closes, SLOW_EMA_PERIOD)

        if fast is None or slow is None or len(fast) < 2:
            print(f"[{datetime.now()}] Not enough data yet for EMA calculation.")
            return

        current_price = closes[-1]
        prev_fast, prev_slow = fast[-2], slow[-2]
        curr_fast, curr_slow = fast[-1], slow[-1]
        position = get_open_position()

        print(f"[{datetime.now()}] Price={current_price:.2f}  "
              f"FastEMA={curr_fast:.2f}  SlowEMA={curr_slow:.2f}")
        write_heartbeat(current_price, curr_fast, curr_slow, position)

        crossed_up = prev_fast <= prev_slow and curr_fast > curr_slow
        crossed_down = prev_fast >= prev_slow and curr_fast < curr_slow

        if crossed_up:
            if position and position["position"]["direction"] == "SELL":
                close_position(position)
                position = None
            if position is None:
                open_position("BUY", current_price)

        elif crossed_down:
            if position and position["position"]["direction"] == "BUY":
                close_position(position)
                position = None
            if position is None:
                open_position("SELL", current_price)

    except requests.HTTPError as e:
        print(f"[{datetime.now()}] API error: {e}")
    except Exception as e:
        print(f"[{datetime.now()}] Unexpected error: {e}")


if __name__ == "__main__":
    if "--find-epic" in sys.argv:
        find_gold_epic()
    else:
        run_once()
