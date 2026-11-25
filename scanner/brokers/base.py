"""The Broker interface. The scanner only ever talks to this, never to a real broker.

PaperBroker (phase 4), ManualBroker (v2) and IBKRBroker (v3) all implement it,
so swapping one for another needs no change to the rest of the program.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from scanner.models import DailyBar, Fill, OrderId, OrderTicket, Position


@runtime_checkable
class Broker(Protocol):
    def submit(self, ticket: OrderTicket) -> OrderId:
        """Place (or simulate) the order described by a logged ticket."""
        ...

    def cancel(self, order_id: OrderId) -> None:
        """Cancel a working order. Unknown or already-filled IDs raise KeyError."""
        ...

    def positions(self) -> list[Position]:
        """Currently open positions."""
        ...

    def equity(self) -> float:
        """Sleeve equity: starting capital + realized P&L + open positions at last close."""
        ...

    def on_bar(self, ticker: str, bar: DailyBar) -> list[Fill]:
        """Paper only: feed one daily bar and return any fills it causes."""
        ...
