from __future__ import annotations

from datetime import date

import pytest

from scanner.brokers import Broker
from scanner.models import DailyBar, Fill, OrderId, OrderTicket, Position, Side


def make_ticket(**overrides) -> OrderTicket:
    fields = dict(
        logbook_id="MOM-001",
        ticker="ABCD",
        shares=133,
        entry_trigger=2.00,
        entry_limit=2.06,
        stop_price=1.70,
        target_price=4.00,
        risk_dollars=40.0,
        exit_strategy="scale_trail",
        thesis="Phase 3 topline beat",
        valid_for=date(2026, 11, 26),
    )
    fields.update(overrides)
    return OrderTicket(**fields)


class StubBroker:
    """Minimal in-memory broker used only to prove the interface is implementable."""

    def __init__(self, cash: float) -> None:
        self.cash = cash
        self.orders: dict[OrderId, OrderTicket] = {}

    def submit(self, ticket: OrderTicket) -> OrderId:
        oid = OrderId(ticket.logbook_id)
        self.orders[oid] = ticket
        return oid

    def cancel(self, order_id: OrderId) -> None:
        del self.orders[order_id]

    def positions(self) -> list[Position]:
        return []

    def equity(self) -> float:
        return self.cash

    def on_bar(self, ticker: str, bar: DailyBar) -> list[Fill]:
        return []


class Incomplete:
    def submit(self, ticket: OrderTicket) -> OrderId:  # pragma: no cover
        return OrderId("x")


def test_stub_satisfies_broker_protocol():
    b = StubBroker(2500.0)
    assert isinstance(b, Broker)
    oid = b.submit(make_ticket())
    assert oid == "MOM-001"
    b.cancel(oid)
    assert b.orders == {}
    assert b.equity() == 2500.0


def test_incomplete_class_is_not_a_broker():
    assert not isinstance(Incomplete(), Broker)


def test_ticket_validation():
    make_ticket()  # valid
    make_ticket(target_price=None)  # trail_only has no target
    with pytest.raises(ValueError, match="stop < entry_trigger"):
        make_ticket(stop_price=2.10)
    with pytest.raises(ValueError, match="entry_trigger <= entry_limit"):
        make_ticket(entry_limit=1.95)
    with pytest.raises(ValueError, match="shares"):
        make_ticket(shares=0)
    with pytest.raises(ValueError, match="target"):
        make_ticket(target_price=1.90)
    with pytest.raises(ValueError, match="thesis"):
        make_ticket(thesis="  ")


def test_daily_bar_validation_and_helpers():
    bar = DailyBar("ABCD", date(2026, 11, 25), open=2.0, high=2.5, low=1.8, close=2.3, volume=1_000_000)
    assert bar.midpoint == pytest.approx(2.15)
    assert bar.dollar_volume == pytest.approx(2_300_000)
    with pytest.raises(ValueError, match="out of order"):
        DailyBar("ABCD", date(2026, 11, 25), open=2.0, high=1.9, low=1.8, close=2.3, volume=1)
    with pytest.raises(ValueError, match="> 0"):
        DailyBar("ABCD", date(2026, 11, 25), open=0, high=1.9, low=0, close=1, volume=1)


def test_fill_slippage_sign():
    buy = Fill(OrderId("1"), "MOM-001", "ABCD", Side.BUY, 100, 2.00, 2.02, date(2026, 11, 26), "entry")
    sell = Fill(OrderId("1"), "MOM-001", "ABCD", Side.SELL, 100, 1.70, 1.683, date(2026, 11, 27), "stop")
    assert buy.slippage == pytest.approx(0.02)
    assert sell.slippage == pytest.approx(0.017)
