"""ccxt adapter for Binance USDT-margined perps: public data, testnet, and (gated) live.

UNTESTED against a real endpoint from the build environment: its network policy blocks
fapi.binance.com, testnet.binancefuture.com and demo-fapi.binance.com. Unit tests cover this
class with a fake ccxt client only. Run `tryme check-exchange` on a host with access first.
"""

from __future__ import annotations

import os

import ccxt

from ..models import Book, Candle, Fill, OrderIntent
from .base import MarketInfo, OrderRejected, TransientError
from .paper import TF_MS


def _wrap(fn):
    def inner(*a, **k):
        try:
            return fn(*a, **k)
        except (ccxt.NetworkError, ccxt.RateLimitExceeded, ccxt.ExchangeNotAvailable) as e:
            raise TransientError(type(e).__name__) from e  # never include request details/keys
        except (ccxt.InvalidOrder, ccxt.InsufficientFunds, ccxt.BadRequest) as e:
            raise OrderRejected(type(e).__name__ + ": " + str(e)[:200]) from e

    return inner


class CcxtExchange:
    def __init__(
        self,
        exchange_id: str = "binanceusdm",
        *,
        mode: str = "public",
        testnet_kind: str = "sandbox",
        client=None,
    ):
        """mode: "public" (no keys, data only), "testnet", or "live"."""
        if mode not in ("public", "testnet", "live"):
            raise ValueError(mode)
        self.mode = mode
        if client is not None:
            self.x = client
            return
        opts: dict = {"enableRateLimit": True, "options": {"adjustForTimeDifference": True}}
        if mode != "public":
            prefix = "BINANCE_TESTNET" if mode == "testnet" else "BINANCE_LIVE"
            key, secret = (
                os.environ.get(f"{prefix}_API_KEY"),
                os.environ.get(f"{prefix}_API_SECRET"),
            )
            if not key or not secret:
                raise PermissionError(f"{prefix}_API_KEY / {prefix}_API_SECRET are not set")
            opts.update(apiKey=key, secret=secret)
        self.x = getattr(ccxt, exchange_id)(opts)
        if mode == "testnet":
            if testnet_kind == "demo":
                self.x.enable_demo_trading(True)
            else:
                self.x.set_sandbox_mode(True)

    def _private(self) -> None:
        if self.mode == "public":
            raise PermissionError("public data client cannot touch the account")

    @_wrap
    def server_time_ms(self) -> int:
        return int(self.x.fetch_time())

    @_wrap
    def market(self, symbol: str) -> MarketInfo:
        m = self.x.load_markets()[symbol]
        lim = m.get("limits", {})
        t = self.x.fetch_ticker(symbol)
        return MarketInfo(
            symbol,
            min_notional=float((lim.get("cost") or {}).get("min") or 5.0),
            qty_step=float((m.get("precision") or {}).get("amount") or 0.0),
            quote_volume_24h=t.get("quoteVolume"),
        )

    @_wrap
    def candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        tf = TF_MS[timeframe]
        rows = self.x.fetch_ohlcv(symbol, timeframe, limit=limit + 1)
        return [
            Candle(
                int(r[0]),
                float(r[1]),
                float(r[2]),
                float(r[3]),
                float(r[4]),
                float(r[5]),
                int(r[0]) + tf,
            )
            for r in rows
        ]

    @_wrap
    def book(self, symbol: str) -> Book | None:
        ob = self.x.fetch_order_book(symbol, limit=20)
        ts = ob.get("timestamp") or self.x.milliseconds()
        return Book(
            int(ts),
            [(float(p), float(q)) for p, q in ob["bids"]],
            [(float(p), float(q)) for p, q in ob["asks"]],
        )

    @_wrap
    def equity(self) -> float:
        self._private()
        b = self.x.fetch_balance()
        info = b.get("info", {})
        if "totalMarginBalance" in info:
            return float(info["totalMarginBalance"])
        return float(b["USDT"]["total"])

    @_wrap
    def positions(self) -> dict[str, float]:
        self._private()
        out = {}
        for p in self.x.fetch_positions():
            q = float(p.get("contracts") or 0)
            if q:
                out[p["symbol"]] = q if p.get("side") == "long" else -q
        return out

    @_wrap
    def submit(self, intent: OrderIntent) -> Fill:
        self._private()
        o = self.x.create_order(
            intent.symbol,
            "market",
            intent.side,
            intent.qty,
            None,
            {"newClientOrderId": intent.client_id, "reduceOnly": intent.reduce_only},
        )
        return self._to_fill(o, intent.client_id)

    def _to_fill(self, o: dict, client_id: str) -> Fill:
        filled = float(o.get("filled") or 0)
        status = "filled" if o.get("status") == "closed" else ("partial" if filled else "rejected")
        fee = float((o.get("fee") or {}).get("cost") or 0)
        return Fill(
            client_id,
            o["symbol"],
            o["side"],
            filled,
            float(o.get("average") or o.get("price") or 0),
            fee,
            int(o.get("timestamp") or 0),
            status,
            detail=self.mode.upper(),
        )

    @_wrap
    def find_fill(self, client_id: str) -> Fill | None:
        self._private()
        try:
            o = self.x.fetch_order(None, None, {"origClientOrderId": client_id})
        except ccxt.OrderNotFound:
            return None
        except ccxt.ArgumentsRequired:
            return None
        return self._to_fill(o, client_id)

    @_wrap
    def place_stop(self, symbol, side, qty, stop_price, client_id) -> None:
        """Exchange-side protective stop, so a dead process still has a stop in the market."""
        self._private()
        self.x.create_order(
            symbol,
            "STOP_MARKET",
            side,
            qty,
            None,
            {"stopPrice": stop_price, "reduceOnly": True, "newClientOrderId": client_id},
        )

    @_wrap
    def cancel_stops(self, symbol) -> None:
        self._private()
        self.x.cancel_all_orders(symbol)
