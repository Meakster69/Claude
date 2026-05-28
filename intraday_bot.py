#!/usr/bin/env python3
"""
intraday_bot.py — Opening Range Breakout (ORB) with Adaptive Sizing

Strategy:
  1. 9:30-9:45 ET  — build Opening Range (high/low/avg-volume) for each symbol
  2. 9:45-13:00 ET — entry when bar closes ABOVE OR-high AND volume > 1.5× OR avg
  3. TP            — OR-high + (OR-size × 2)  proportional to each stock's volatility
  4. SL            — OR-low  (natural support floor)
  5. Trail         — activates once 50% to TP, trails 0.3% below HWM
  6. 15:55 ET      — force-close everything

Adaptive sizing:
  - Tracks last 20 trade results (win rate, avg win, avg loss, expectancy)
  - Win rate < 40% → halve position size (losing run protection)
  - Win rate > 60% → 1.5× position size (exploit winning streak)
  - Logs full performance summary on every trade
"""

import csv
import logging
import os
from collections import deque
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from alpaca.data.live import StockDataStream
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

load_dotenv()

# ── Strategy parameters ───────────────────────────────────────────────────────
OR_END_T        = dtime(9, 45)    # opening range collection ends
ENTRY_CLOSE_T   = dtime(13, 0)    # no new entries after 13:00 ET
FORCE_CLOSE_T   = dtime(15, 55)   # force-close all positions
TP_RATIO        = 2.0             # TP = OR-high + (OR-size × 2)
VOL_MULTIPLIER  = 1.5             # breakout bar must have volume > 1.5× OR average
TRAIL_ACTIVATE  = 0.5             # trailing stop activates at 50% of TP distance
TRAIL_PCT       = 0.003           # trail 0.3% below high-water mark
MAX_RISK_PCT    = 0.05            # skip trade if SL is > 5% below entry
MIN_REWARD_PCT  = 0.005           # skip trade if TP is < 0.5% above entry
MAX_POSITIONS   = 50              # max concurrent positions (quality over quantity)
PERF_WINDOW     = 20              # rolling window for win-rate calculation

ET = ZoneInfo("America/New_York")

_BASE_NOTIONAL = (
    float(os.environ.get("BUY_INCREMENT_GBP", "100"))
    * float(os.environ.get("GBP_USD_RATE", "1.27"))
)

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs("live_output", exist_ok=True)
logging.basicConfig(
    filename="live_output/intraday.log",
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("intraday")

# ── State ─────────────────────────────────────────────────────────────────────
# or_data[sym]  = {high, low, vols:[]}  — built during opening range window
# positions[sym]= {entry, qty, hwm, tp, sl, trail_trigger}
or_data:       dict  = {}
positions:     dict  = {}
recent_trades: deque = deque(maxlen=PERF_WINDOW)

# ── Alpaca clients ────────────────────────────────────────────────────────────
trading = TradingClient(
    api_key=os.environ["INTRADAY_API_KEY"],
    secret_key=os.environ["INTRADAY_SECRET_KEY"],
    paper=True,
)
stream = StockDataStream(
    api_key=os.environ["INTRADAY_API_KEY"],
    secret_key=os.environ["INTRADAY_SECRET_KEY"],
)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _now_et():
    return datetime.now(ET)

def _t():
    return _now_et().time()

def _past_close() -> bool:
    return _t() >= FORCE_CLOSE_T

def _in_or_window() -> bool:
    return _t() < OR_END_T

def _in_entry_window() -> bool:
    return OR_END_T <= _t() < ENTRY_CLOSE_T

def _in_trading_window() -> bool:
    return _t() < FORCE_CLOSE_T

def _notional() -> float:
    """Scale position size based on rolling win rate."""
    if len(recent_trades) < 10:
        return _BASE_NOTIONAL
    win_rate = sum(1 for p in recent_trades if p > 0) / len(recent_trades)
    if win_rate < 0.40:
        return _BASE_NOTIONAL * 0.5    # protect capital during losing run
    if win_rate > 0.60:
        return _BASE_NOTIONAL * 1.5    # press advantage during winning run
    return _BASE_NOTIONAL

def _perf_summary() -> str:
    if not recent_trades:
        return "no trades recorded yet"
    wins   = [p for p in recent_trades if p > 0]
    losses = [p for p in recent_trades if p <= 0]
    wr     = len(wins) / len(recent_trades) * 100
    aw     = sum(wins)   / len(wins)   if wins   else 0.0
    al     = sum(losses) / len(losses) if losses else 0.0
    exp    = (wr / 100 * aw) + ((1 - wr / 100) * al)
    size_note = ""
    if wr < 40:
        size_note = " [SIZE HALVED]"
    elif wr > 60:
        size_note = " [SIZE 1.5x]"
    return (
        f"last {len(recent_trades)} trades | WR={wr:.0f}% | "
        f"avgW=+{aw:.2f}% | avgL={al:.2f}% | expectancy={exp:+.3f}%{size_note}"
    )

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


# ── Order execution ───────────────────────────────────────────────────────────
def _buy(sym: str, price: float, tp: float, sl: float) -> None:
    notional = _notional()
    qty = round(notional / price, 6)
    if qty <= 0:
        return
    trading.submit_order(MarketOrderRequest(
        symbol=sym, qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
    ))
    trail_trigger = price + (tp - price) * TRAIL_ACTIVATE
    positions[sym] = {
        "entry": price, "qty": qty, "hwm": price,
        "tp": tp, "sl": sl, "trail_trigger": trail_trigger,
    }
    risk_pct   = (price - sl) / price * 100
    reward_pct = (tp - price) / price * 100
    rr_ratio   = reward_pct / risk_pct if risk_pct else 0
    log.info(
        "BUY  %-6s @ $%8.2f  TP=$%.2f(+%.1f%%)  SL=$%.2f(-%.1f%%)  "
        "R:R=1:%.1f  qty=%.4f  [%d/%d]  %s",
        sym, price, tp, reward_pct, sl, risk_pct, rr_ratio,
        qty, len(positions), MAX_POSITIONS, _perf_summary(),
    )

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
    recent_trades.append(pct)
    log.info(
        "SELL %-6s @ $%8.2f  entry=$%.2f  pnl=%+.2f%%  hwm=$%.2f  [%s]  ||  %s",
        sym, price, pos["entry"], pct, pos["hwm"], reason, _perf_summary(),
    )


# ── Bar handler ───────────────────────────────────────────────────────────────
async def on_bar(bar) -> None:
    sym    = bar.symbol
    price  = float(bar.close)
    volume = float(bar.volume)

    # Force-close past 15:55 ET
    if _past_close():
        if sym in positions:
            _sell(sym, price, "EOD")
        return

    if not _in_trading_window():
        return

    # ── Build opening range (9:30–9:44) ──────────────────────────────────────
    if _in_or_window():
        if sym not in or_data:
            or_data[sym] = {
                "high": float(bar.high),
                "low":  float(bar.low),
                "vols": [volume],
            }
        else:
            d = or_data[sym]
            d["high"] = max(d["high"], float(bar.high))
            d["low"]  = min(d["low"],  float(bar.low))
            d["vols"].append(volume)
        return

    # ── Manage open positions ─────────────────────────────────────────────────
    if sym in positions:
        pos = positions[sym]

        if price > pos["hwm"]:
            pos["hwm"] = price

        pct_entry = (price - pos["entry"]) / pos["entry"]

        # Hard take-profit
        if price >= pos["tp"]:
            _sell(sym, price, f"TP +{pct_entry*100:.1f}%")
            return

        # Hard stop-loss
        if price <= pos["sl"]:
            _sell(sym, price, f"SL {pct_entry*100:.1f}%")
            return

        # Trailing stop — activates once HWM passes trail_trigger
        if pos["hwm"] >= pos["trail_trigger"]:
            trail_floor = pos["hwm"] * (1 - TRAIL_PCT)
            if price <= trail_floor:
                _sell(sym, price,
                      f"TRAIL hwm=${pos['hwm']:.2f} pnl={pct_entry*100:+.1f}%")
        return

    # ── Entry: Opening Range Breakout ─────────────────────────────────────────
    if not _in_entry_window():
        return
    if len(positions) >= MAX_POSITIONS:
        return
    if sym not in or_data:
        return

    d       = or_data[sym]
    or_high = d["high"]
    or_low  = d["low"]
    or_size = or_high - or_low

    if or_size <= 0:
        return

    avg_or_vol = sum(d["vols"]) / len(d["vols"])

    # Signal: close above OR-high with volume confirmation
    if price > or_high and volume > avg_or_vol * VOL_MULTIPLIER:
        tp = or_high + or_size * TP_RATIO
        sl = or_low

        risk_pct   = (price - sl) / price
        reward_pct = (tp - price) / price

        # Reject if risk is too large or reward too small
        if risk_pct > MAX_RISK_PCT or reward_pct < MIN_REWARD_PCT:
            log.debug("SKIP %-6s  risk=%.1f%%  reward=%.1f%%  (filters)",
                      sym, risk_pct * 100, reward_pct * 100)
            return

        try:
            _buy(sym, price, tp, sl)
        except Exception as exc:
            log.error("BUY failed %-6s: %s", sym, exc)


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    tickers = _load_tickers()
    log.info(
        "Starting ORB | symbols=%d | OR=9:30-9:45 ET | "
        "TP=OR×%.1f | SL=OR_low | VOL=%.1fx | MAX=%d | base=$%.0f",
        len(tickers), TP_RATIO, VOL_MULTIPLIER, MAX_POSITIONS, _BASE_NOTIONAL,
    )
    print(f"Opening Range Breakout — {len(tickers)} symbols")
    print(f"OR: 9:30-9:45 ET  |  Entries: 9:45-13:00 ET  |  EOD: 15:55 ET")
    print(f"TP = OR×{TP_RATIO}  |  SL = OR-low  |  Vol filter: {VOL_MULTIPLIER}×")
    print("Press Ctrl+C to stop.\n")
    stream.subscribe_bars(on_bar, *tickers)
    stream.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped. %s", _perf_summary())
        print(f"\nStopped.  {_perf_summary()}")
