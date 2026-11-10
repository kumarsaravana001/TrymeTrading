import dataclasses

import pytest

from conftest import SYM, TF, account
from tryme.models import Book
from tryme.state_engine import ClockSkew, MissingData, StaleFeed, build_snapshot, check_clock


def snap(candles, now, book=None, **kw):
    return build_snapshot(
        SYM,
        candles,
        book,
        account(),
        now,
        tf_ms=TF,
        lookback=120,
        risk_state="safe",
        equity_peak=1000,
        day_start_equity=1000,
        **kw,
    )


def test_snapshot_fits_token_budget(candles):
    s = snap(candles, candles[-1].close_ts)
    text = s.to_prompt()
    # ~3 characters per token is a conservative bound for digit-heavy text
    assert len(text) / 3 < 400
    assert "mid=" in text and "risk_state=safe" in text


def test_no_future_data_leaks(candles):
    """Changing every candle after `now` must not change the snapshot."""
    now = candles[300].close_ts
    a = snap(candles, now)
    tampered = candles[:301] + [
        dataclasses.replace(c, close=c.close * 5, high=c.high * 5) for c in candles[301:]
    ]
    assert snap(tampered, now) == a
    assert a.ts == now


def test_unclosed_candle_is_ignored(candles):
    now = candles[300].close_ts - 1  # candle 300 still forming
    assert snap(candles, now).ts == candles[299].close_ts


def test_missing_and_gapped_data(candles):
    with pytest.raises(MissingData):
        snap(candles[:20], candles[19].close_ts)
    gapped = candles[:350] + candles[351:]
    with pytest.raises(MissingData, match="gap"):
        snap(gapped, candles[-1].close_ts)
    bad = candles[:-1] + [dataclasses.replace(candles[-1], close=float("nan"))]
    with pytest.raises(MissingData, match="bad candle"):
        snap(bad, candles[-1].close_ts)


def test_stale_candles_and_book(candles):
    now = candles[-1].close_ts
    with pytest.raises(StaleFeed):
        snap(candles, now + 3 * TF)
    px = candles[-1].close
    old = Book(now - 60_000, [(px * 0.999, 5)], [(px * 1.001, 5)])
    with pytest.raises(StaleFeed, match="book"):
        snap(candles, now, old)
    future = Book(now + 5_000, [(px * 0.999, 5)], [(px * 1.001, 5)])
    with pytest.raises(ClockSkew):
        snap(candles, now, future)
    crossed = Book(now, [(px * 1.001, 5)], [(px * 0.999, 5)])
    with pytest.raises(MissingData, match="crossed"):
        snap(candles, now, crossed)


def test_book_features(candles):
    now = candles[-1].close_ts
    px = candles[-1].close
    s = snap(candles, now, Book(now, [(px - 0.01, 30)], [(px + 0.01, 10)]))
    assert s.imbalance == pytest.approx(0.5)
    assert s.spread_bps == pytest.approx(0.02 / px * 1e4)
    assert snap(candles, now).imbalance is None


def test_clock_skew():
    check_clock(1_000_000, 1_001_000, 2.0)
    with pytest.raises(ClockSkew):
        check_clock(1_000_000, 1_005_000, 2.0)
