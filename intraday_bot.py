#!/usr/bin/env python3
"""
intraday_bot.py — Intraday Momentum with Trailing Stop

Entry  : stock up >= 1% from day-open, after 10:00 ET, before 13:30 ET
TP     : +2% from entry (hard limit)
Trail  : once up >= 1% from entry, trail 0.5% below HWM to lock in gains
SL     : -1.5% from entry (hard floor)
EOD    : force-close all positions at 15:55 ET
Size   : BUY_INCREMENT_GBP converted to USD (default £100)
Cap    : 100 concurrent positions
"""
import csv
import logging
import os
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from alpaca.data.live import StockDataStream
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

load_dotenv()

TAKE_PROFIT_PCT  = 0.02    # +2%  hard take-profit
TRAIL_ACTIVATE   = 0.01    # start trailing once up 1% from entry
TRAIL_STOP_PCT   = 0.005   # trail 0.5% below high-water mark
STOP_LOSS_PCT    = 0.015   # -1.5% hard stop-loss
ENTRY_THRESH_PCT = 0.01    # entry signal: up >= 1% from day-open
MAX_POSITIONS    = 100
ET               = ZoneInfo("America/New_York")
ENTRY_OPEN_T     = dtime(10, 0)   # skip first 30 min (price discovery noise)
ENTRY_CLOSE_T    = dtime(13, 30)  # no new entries after 13:30 ET
FORCE_CLOSE_T    = dtime(15, 55)  # force-close everything

os.makedirs("live_output", exist_ok=True)
logging.basicConfig(
    filename="live_output/intraday.log",
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("intraday")

day_open:  dict = {}  # sym -> float (first bar close of the day)
positions: dict = {}  # sym -> {entry, qty, hwm}

trading = TradingClient(
    api_key=os.environ["INTRADAY_API_KEY"],
    secret_key=os.environ["INTRADAY_SECRET_KEY"],
    paper=True,
)
stream = StockDataStream(
    api_key=os.environ["INTRADAY_API_KEY"],
    secret_key=os.environ["INTRADAY_SECRET_KEY"],
)


def _notional_usd() -> float:
    rate = float(os.environ.get("GBP_USD_RATE", "1.27"))
    return float(os.environ.get("BUY_INCREMENT_GBP", "100")) * rate


def _now_et():
    return datetime.now(ET)


def _in_entry_window() -> bool:
    t = _now_et().time()
    return ENTRY_OPEN_T <= t < ENTRY_CLOSE_T


def _in_trading_window() -> bool:
    t = _now_et().time()
    return ENTRY_OPEN_T <= t < FORCE_CLOSE_T


def _past_close() -> bool:
    return _now_et().time() >= FORCE_CLOSE_T


def _load_tickers() -> list:
    tickers, seen = [], set()
    for fname in ("ftse250.csv", "ftse_aim.csv"):
        if not os.path.exists(fname):
            continue
        with open(fname, newline="", encoding="utf-8-sig") as f:
            for row in csv.reader(f):
                t = row[0].strip() if row else ""
                if not t or t.lower() in ("ticker", "symbol"):
                    continue
                if t not in seen:
                    tickers.append(t)
                    seen.add(t)
    return tickers


def _buy(sym: str, price: float) -> None:
    notional = _notional_usd()
    qty = round(notional / price, 6)
    if qty <= 0:
        return
    trading.submit_order(MarketOrderRequest(
        symbol=sym, qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
    ))
    positions[sym] = {"entry": price, "qty": qty, "hwm": price}
    log.info("BUY  %-6s @ $%8.2f  qty=%.4f  $%.2f  [%d/%d]",
             sym, price, qty, notional, len(positions), MAX_POSITIONS)


def _sell(sym: str, price: float, reason: str) -> None:
    pos = positions.pop(sym, None)
    if not pos:
        return
    trading.submit_order(MarketOrderRequest(
        symbol=sym, qty=pos["qty"],
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    ))
    pct = (price - pos["entry"]) / pos["entry"] * 100
    log.info("SELL %-6s @ $%8.2f  entry=$%.2f  pnl=%+.2f%%  hwm=$%.2f  [%s]",
             sym, price, pos["entry"], pct, pos["hwm"], reason)


async def on_bar(bar) -> None:
    sym   = bar.symbol
    price = float(bar.close)

    # First bar of the day — record open reference price, don't trade yet
    if sym not in day_open:
        day_open[sym] = price
        return

    # Force-close anything still open past 15:55 ET
    if _past_close():
        if sym in positions:
            _sell(sym, price, "EOD")
        return

    if not _in_trading_window():
        return

    # ── Manage open positions ──────────────────────────────────────────────
    if sym in positions:
        pos   = positions[sym]
        entry = pos["entry"]

        if price > pos["hwm"]:
            pos["hwm"] = price

        pct_from_entry = (price - entry) / entry
        pct_from_hwm   = (price - pos["hwm"]) / pos["hwm"]

        # Hard take-profit at +2%
        if pct_from_entry >= TAKE_PROFIT_PCT:
            _sell(sym, price, f"TP +{pct_from_entry*100:.1f}%")
            return

        # Hard stop-loss at -1.5%
        if pct_from_entry <= -STOP_LOSS_PCT:
            _sell(sym, price, f"SL {pct_from_entry*100:.1f}%")
            return

        # Trailing stop: activate once up >= 1%, trail 0.5% below HWM
        hwm_gain = (pos["hwm"] - entry) / entry
        if hwm_gain >= TRAIL_ACTIVATE and pct_from_hwm <= -TRAIL_STOP_PCT:
            _sell(sym, price,
                  f"TRAIL hwm=${pos['hwm']:.2f} drop={pct_from_hwm*100:.1f}%")
        return

    # ── Entry logic ────────────────────────────────────────────────────────
    if not _in_entry_window():
        return
    if len(positions) >= MAX_POSITIONS:
        return

    if (price - day_open[sym]) / day_open[sym] >= ENTRY_THRESH_PCT:
        try:
            _buy(sym, price)
        except Exception as exc:
            log.error("BUY failed %-6s: %s", sym, exc)


def main() -> None:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    tickers = _load_tickers()
    log.info(
        "Starting | symbols=%d | TP=+%.0f%% | Trail=%.1f%%@%.1f%% | "
        "SL=-%.1f%% | entry=10:00-13:30 ET | MAX=%d | size=£%.0f",
        len(tickers),
        TAKE_PROFIT_PCT * 100,
        TRAIL_STOP_PCT * 100,
        TRAIL_ACTIVATE * 100,
        STOP_LOSS_PCT * 100,
        MAX_POSITIONS,
        float(os.environ.get("BUY_INCREMENT_GBP", "100")),
    )
    print(f"Subscribing to {len(tickers)} symbols — streaming live bars …")
    print("TP=+2%  Trail=0.5%@1%  SL=-1.5%  Entries: 10:00–13:30 ET")
    print("Press Ctrl+C to stop.")
    stream.subscribe_bars(on_bar, *tickers)
    stream.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Intraday bot stopped.")
        print("\nStopped.")
