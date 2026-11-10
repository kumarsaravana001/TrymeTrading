"""Exchange interface. Paper, testnet and live all sit behind it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..models import Book, Candle, Fill, OrderIntent


class ExchangeError(Exception):
    pass


class TransientError(ExchangeError):
    """Network or rate-limit failure. Safe to retry with the same client order id."""


class OrderRejected(ExchangeError):
    """The exchange refused the order. Not retried."""


@dataclass(frozen=True)
class MarketInfo:
    symbol: str
    min_notional: float = 5.0
    qty_step: float = 0.0
    quote_volume_24h: float | None = None


class Exchange(Protocol):
    def server_time_ms(self) -> int: ...
    def market(self, symbol: str) -> MarketInfo: ...
    def candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]: ...
    def book(self, symbol: str) -> Book | None: ...
    def equity(self) -> float: ...
    def positions(self) -> dict[str, float]: ...  # symbol -> signed qty
    def submit(self, intent: OrderIntent) -> Fill: ...
    def find_fill(self, client_id: str) -> Fill | None: ...
    def place_stop(
        self, symbol: str, side: str, qty: float, stop_price: float, client_id: str
    ) -> None: ...
    def cancel_stops(self, symbol: str) -> None: ...
