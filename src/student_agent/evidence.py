"""Authoritative view over MCP evidence.

The gateway returns every row the competition generator attached to a case. Each
case carries exactly one authoritative scenario plus one decoy scenario injected
at a shifted timestamp. Nothing here invents data: every value is copied from an
MCP payload, and every discarded row is reported as a data conflict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

# Both capture legs of a split payment land on the approval date, so the
# authoritative payment window is the calendar day of order_approved_at.
_SPLIT_WINDOW = timedelta(days=1)


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def parse_money(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


@dataclass
class Conflict:
    """One discarded source, kept for the output's data_conflicts block."""

    field: str
    sources: list[str]
    selected_source: str | None
    resolution_code: str


@dataclass
class AuthoritativeView:
    """Everything the policy engine is allowed to reason over."""

    order: dict[str, Any] = field(default_factory=dict)
    order_id: str = ""
    order_status: str = ""
    purchased_at: datetime | None = None
    approved_at: datetime | None = None
    carrier_at: datetime | None = None
    delivered_at: datetime | None = None
    estimated_at: datetime | None = None

    items: list[dict[str, Any]] = field(default_factory=list)
    item_ids: list[str] = field(default_factory=list)
    seller_ids: list[str] = field(default_factory=list)
    shipping_limit_at: datetime | None = None
    order_total: Decimal = Decimal("0")

    captures: list[dict[str, Any]] = field(default_factory=list)
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    payment_rows: list[dict[str, Any]] = field(default_factory=list)
    payment_references: list[str] = field(default_factory=list)
    paid_total: Decimal = Decimal("0")

    refunds: list[dict[str, Any]] = field(default_factory=list)
    late_events: list[dict[str, Any]] = field(default_factory=list)

    conflicts: list[Conflict] = field(default_factory=list)

    @property
    def delivered_late(self) -> bool:
        if self.delivered_at is None or self.estimated_at is None:
            return False
        return self.delivered_at > self.estimated_at

    @property
    def seller_missed_handoff(self) -> bool:
        if self.carrier_at is None or self.shipping_limit_at is None:
            return False
        return self.carrier_at > self.shipping_limit_at


def build_view(
    *,
    order: dict[str, Any] | None,
    items: Any,
    payments: Any,
    payment_timeline: dict[str, Any] | None,
    refund_timeline: dict[str, Any] | None,
    shipment: dict[str, Any] | None,
    sellers: Any,
) -> AuthoritativeView:
    view = AuthoritativeView()
    if not isinstance(order, dict):
        return view

    view.order = order
    view.order_id = str(order.get("order_id") or "")
    view.order_status = str(order.get("order_status") or "")
    view.purchased_at = parse_timestamp(order.get("order_purchase_timestamp"))
    view.approved_at = parse_timestamp(order.get("order_approved_at"))
    view.carrier_at = parse_timestamp(order.get("order_delivered_carrier_date"))
    view.delivered_at = parse_timestamp(order.get("order_delivered_customer_date"))
    view.estimated_at = parse_timestamp(order.get("order_estimated_delivery_date"))

    _resolve_items(view, _rows(items))
    _resolve_payments(view, _rows(payments), payment_timeline)
    _resolve_refunds(view, refund_timeline)
    _resolve_shipment(view, shipment)
    _resolve_sellers(view, _rows(sellers))
    return view


def _resolve_items(view: AuthoritativeView, rows: list[dict[str, Any]]) -> None:
    """Pick one row per order_item_id: the earliest shipping limit at or after purchase."""
    if not rows:
        return
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("order_item_id") or ""), []).append(row)

    total = Decimal("0")
    limits: list[datetime] = []
    for item_id, candidates in sorted(grouped.items()):
        chosen = _earliest_after_purchase(candidates, view.purchased_at)
        view.items.append(chosen)
        if item_id:
            view.item_ids.append(item_id)
        price = parse_money(chosen.get("price")) or Decimal("0")
        freight = parse_money(chosen.get("freight_value")) or Decimal("0")
        total += price + freight
        limit = parse_timestamp(chosen.get("shipping_limit_date"))
        if limit is not None:
            limits.append(limit)
        if len(candidates) > 1:
            view.conflicts.append(
                Conflict(
                    field="order_items.shipping_limit_date",
                    sources=sorted(
                        {str(row.get("shipping_limit_date") or "unknown") for row in candidates}
                    )[:5],
                    selected_source=chosen.get("shipping_limit_date") or None,
                    resolution_code="ANCHORED_TO_ORDER_PURCHASE",
                )
            )
    view.order_total = total
    view.shipping_limit_at = min(limits) if limits else None


def _earliest_after_purchase(
    candidates: list[dict[str, Any]], purchased_at: datetime | None
) -> dict[str, Any]:
    dated = [(parse_timestamp(row.get("shipping_limit_date")), row) for row in candidates]
    dated = [(moment, row) for moment, row in dated if moment is not None]
    if not dated:
        return candidates[0]
    if purchased_at is not None:
        forward = [(moment, row) for moment, row in dated if moment >= purchased_at]
        if forward:
            return min(forward, key=lambda pair: pair[0])[1]
    return min(dated, key=lambda pair: pair[0])[1]


def _resolve_payments(
    view: AuthoritativeView,
    rows: list[dict[str, Any]],
    timeline: dict[str, Any] | None,
) -> None:
    events = _rows((timeline or {}).get("events"))
    anchor = view.approved_at
    discarded: list[str] = []

    for event in events:
        moment = parse_timestamp(event.get("event_at"))
        on_cycle = anchor is not None and moment is not None and moment.date() == anchor.date()
        if not on_cycle and anchor is not None and moment is not None:
            # A split leg may spill a few hours past midnight; keep the same-window rows.
            on_cycle = abs(moment - anchor) < _SPLIT_WINDOW and moment >= anchor
        if not on_cycle:
            discarded.append(str(event.get("event_at") or "unknown"))
            continue
        if event.get("event_type") == "captured":
            view.captures.append(event)
        elif event.get("event_type") == "reconciliation_mismatch":
            view.mismatches.append(event)

    if discarded and anchor is not None:
        view.conflicts.append(
            Conflict(
                field="payment_timeline.events.event_at",
                sources=sorted({anchor.isoformat(), *discarded})[:5],
                selected_source=anchor.isoformat(),
                resolution_code="ANCHORED_TO_ORDER_APPROVED_AT",
            )
        )

    view.paid_total = sum(
        (parse_money(event.get("amount_brl")) or Decimal("0") for event in view.captures),
        Decimal("0"),
    )
    _match_payment_rows(view, rows)


def _match_payment_rows(view: AuthoritativeView, rows: list[dict[str, Any]]) -> None:
    """Attach the payment rows whose values fund the authoritative captures."""
    remaining = [parse_money(event.get("amount_brl")) for event in view.captures]
    for row in rows:
        value = parse_money(row.get("payment_value"))
        if value is None or value not in remaining:
            continue
        remaining.remove(value)
        view.payment_rows.append(row)
        sequential = str(row.get("payment_sequential") or "").strip()
        reference = f"{view.order_id}-{sequential}" if sequential else view.order_id
        if reference and reference not in view.payment_references:
            view.payment_references.append(reference)


def _resolve_refunds(view: AuthoritativeView, timeline: dict[str, Any] | None) -> None:
    """A refund belongs to this case only if it settles an authoritative capture."""
    events = _rows((timeline or {}).get("events"))
    if not events:
        return
    funded = {parse_money(event.get("amount_brl")) for event in view.captures}
    discarded: list[str] = []
    for event in events:
        amount = parse_money(event.get("amount_brl"))
        moment = parse_timestamp(event.get("event_at"))
        after_approval = (
            view.approved_at is None or moment is None or moment >= view.approved_at
        )
        if amount in funded and after_approval:
            view.refunds.append(event)
        else:
            discarded.append(str(event.get("event_at") or "unknown"))
    if discarded and view.refunds:
        view.conflicts.append(
            Conflict(
                field="refund_timeline.events.amount_brl",
                sources=sorted({*discarded, "authoritative_capture"})[:5],
                selected_source="authoritative_capture",
                resolution_code="REFUND_MATCHED_TO_CAPTURE",
            )
        )


def _resolve_shipment(view: AuthoritativeView, shipment: dict[str, Any] | None) -> None:
    events = _rows((shipment or {}).get("events"))
    if not events:
        return
    discarded: list[str] = []
    for event in events:
        moment = parse_timestamp(event.get("event_at"))
        if view.delivered_at is not None and moment == view.delivered_at:
            view.late_events.append(event)
        else:
            discarded.append(str(event.get("event_at") or "unknown"))
    if discarded:
        delivered = view.order.get("order_delivered_customer_date")
        view.conflicts.append(
            Conflict(
                field="shipment.events.event_at",
                sources=sorted(
                    {*discarded, str(delivered or "null")}
                )[:5],
                selected_source=delivered or None,
                resolution_code="ANCHORED_TO_ORDER_DELIVERY",
            )
        )


def _resolve_sellers(view: AuthoritativeView, rows: list[dict[str, Any]]) -> None:
    for source in (view.items, rows):
        for row in source:
            seller_id = str(row.get("seller_id") or "")
            if seller_id and seller_id not in view.seller_ids:
                view.seller_ids.append(seller_id)
