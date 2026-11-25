"""Shared domain types used by brokers, strategies, sizing and the backtest.

All are frozen dataclasses: once created they can't be changed, which keeps
ticket plans and price bars honest as they move through the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import NewType

OrderId = NewType("OrderId", str)


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class DailyBar:
    """One session's OHLCV for one ticker."""

    ticker: str
    session: date
    open: float
    high: float
    low: float
    close: float
    volume: int

    def __post_init__(self) -> None:
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError(f"{self.ticker} {self.session}: prices must be > 0")
        if self.volume < 0:
            raise ValueError(f"{self.ticker} {self.session}: volume must be >= 0")
        if not (self.low <= min(self.open, self.close) and max(self.open, self.close) <= self.high):
            raise ValueError(f"{self.ticker} {self.session}: OHLC out of order: {self}")

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2

    @property
    def dollar_volume(self) -> float:
        return self.close * self.volume


@dataclass(frozen=True)
class OrderTicket:
    """A buy plan written to the journal before any order exists.

    Entry is a buy-stop at ``entry_trigger`` with a limit at ``entry_limit``,
    valid only for session ``valid_for``. Exit rules are fixed at creation.
    """

    logbook_id: str
    ticker: str
    shares: int
    entry_trigger: float
    entry_limit: float
    stop_price: float
    target_price: float | None
    risk_dollars: float
    exit_strategy: str
    thesis: str
    valid_for: date
    side: Side = Side.BUY

    def __post_init__(self) -> None:
        if self.shares <= 0:
            raise ValueError(f"{self.logbook_id}: shares must be > 0")
        if not (0 < self.stop_price < self.entry_trigger <= self.entry_limit):
            raise ValueError(
                f"{self.logbook_id}: need 0 < stop < entry_trigger <= entry_limit, got "
                f"stop={self.stop_price} trigger={self.entry_trigger} limit={self.entry_limit}"
            )
        if self.target_price is not None and self.target_price <= self.entry_trigger:
            raise ValueError(f"{self.logbook_id}: target must be above entry trigger")
        if not self.thesis.strip():
            raise ValueError(f"{self.logbook_id}: a thesis line is required")


@dataclass(frozen=True)
class Fill:
    order_id: OrderId
    logbook_id: str
    ticker: str
    side: Side
    shares: int
    ref_price: float  # price before slippage
    price: float  # actual fill price
    session: date
    reason: str  # 'entry', 'stop', 'target', 'trail', 'time_stop', ...
    commission: float = 0.0

    @property
    def slippage(self) -> float:
        """Slippage per share, positive when it cost us money."""
        return self.price - self.ref_price if self.side is Side.BUY else self.ref_price - self.price


@dataclass(frozen=True)
class Position:
    logbook_id: str
    ticker: str
    shares: int
    entry_price: float
    stop_price: float
    target_price: float | None
    opened_on: date
    planned_risk_dollars: float
