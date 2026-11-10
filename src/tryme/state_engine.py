"""Turns raw candles, book and account into one small numeric snapshot.

Strictly causal: only candles that closed at or before `now_ms` are used, and every input is
checked for gaps, staleness and clock skew before anything downstream may place an order.
"""

from __future__ import annotations

import math

import numpy as np

from .models import Account, Book, Candle, RiskState, StateSnapshot


class DataError(Exception):
    """Base for every reason a candle must be skipped."""


class MissingData(DataError):
    pass


class StaleFeed(DataError):
    pass


class ClockSkew(DataError):
    pass


def check_clock(local_ms: int, exchange_ms: int, max_skew_s: float) -> None:
    skew = abs(local_ms - exchange_ms) / 1000
    if skew > max_skew_s:
        raise ClockSkew(f"clock skew {skew:.2f}s exceeds {max_skew_s}s")


def _ema(x: np.ndarray, n: int) -> float:
    a = 2 / (n + 1)
    e = x[0]
    for v in x[1:]:
        e = a * v + (1 - a) * e
    return float(e)


def closed_candles(candles: list[Candle], now_ms: int) -> list[Candle]:
    return [c for c in candles if c.close_ts <= now_ms]


def build_snapshot(
    symbol: str,
    candles: list[Candle],
    book: Book | None,
    account: Account,
    now_ms: int,
    *,
    tf_ms: int,
    lookback: int,
    risk_state: RiskState,
    equity_peak: float,
    day_start_equity: float,
    max_feed_age_s: float = 30.0,
    ema_fast: int = 12,
    ema_slow: int = 48,
    assumed_spread_bps: float = 1.0,
) -> StateSnapshot:
    cs = closed_candles(candles, now_ms)[-lookback:]
    if len(cs) < max(ema_slow + 1, 50):
        raise MissingData(f"{symbol}: {len(cs)} closed candles, need {max(ema_slow + 1, 50)}")
    for a, b in zip(cs, cs[1:]):
        if b.ts - a.ts != tf_ms:
            raise MissingData(f"{symbol}: candle gap at {a.ts}")
    last = cs[-1]
    if now_ms - last.close_ts > tf_ms:
        raise StaleFeed(
            f"{symbol}: last closed candle is {(now_ms - last.close_ts) / 1000:.0f}s old"
        )
    for c in cs:
        vals = (c.open, c.high, c.low, c.close, c.volume)
        if any(not math.isfinite(v) for v in vals) or min(vals[:4]) <= 0 or c.high < c.low:
            raise MissingData(f"{symbol}: bad candle at {c.ts}")

    close = np.array([c.close for c in cs])
    high = np.array([c.high for c in cs])
    low = np.array([c.low for c in cs])
    vol = np.array([c.volume for c in cs])
    lr = np.diff(np.log(close))
    tr = np.maximum(
        high[1:] - low[1:], np.maximum(abs(high[1:] - close[:-1]), abs(low[1:] - close[:-1]))
    )
    atr = float(tr[-14:].mean())

    if book is not None:
        if now_ms - book.ts > max_feed_age_s * 1000:
            raise StaleFeed(f"{symbol}: book is {(now_ms - book.ts) / 1000:.0f}s old")
        if book.ts > now_ms:
            raise ClockSkew(f"{symbol}: book timestamp is in the future")
        if not book.bids or not book.asks or book.bids[0][0] >= book.asks[0][0]:
            raise MissingData(f"{symbol}: empty or crossed book")
        bid, ask = book.bids[0][0], book.asks[0][0]
        mid = (bid + ask) / 2
        spread_bps = (ask - bid) / mid * 1e4
        bq = sum(q for _, q in book.bids[:10])
        aq = sum(q for _, q in book.asks[:10])
        imbalance = (bq - aq) / (bq + aq) if bq + aq > 0 else 0.0
    else:
        mid, spread_bps, imbalance = float(close[-1]), assumed_spread_bps, None

    eq = account.equity
    if eq <= 0 or not math.isfinite(eq):
        raise MissingData("account equity unavailable")
    pos = account.positions.get(symbol)
    marks = {symbol: mid}
    hi, lo = float(high.max()), float(low.min())
    ef, es = _ema(close, ema_fast), _ema(close, ema_slow)
    return StateSnapshot(
        symbol=symbol,
        ts=last.close_ts,
        mid=mid,
        spread_bps=spread_bps,
        imbalance=imbalance,
        vol=float(lr[-48:].std()),
        atr_pct=atr / mid,
        ret_1=float(close[-1] / close[-2] - 1),
        ret_12=float(close[-1] / close[-13] - 1),
        ret_48=float(close[-1] / close[-49] - 1),
        trend=(ef - es) / es,
        range_pos=(float(close[-1]) - lo) / (hi - lo) if hi > lo else 0.5,
        vol_ratio=float(vol[-1] / vol[-48:].mean()) if vol[-48:].mean() > 0 else 0.0,
        inventory=(pos.qty * mid / eq) if pos else 0.0,
        exposure=account.exposure(marks) / eq,
        drawdown=max(0.0, 1 - eq / equity_peak) if equity_peak > 0 else 0.0,
        daily_pnl=eq / day_start_equity - 1 if day_start_equity > 0 else 0.0,
        open_positions=len(account.positions),
        risk_state=risk_state,
    )
