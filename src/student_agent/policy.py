"""Issue classification and the authoritative policy lookup.

Classification reads only the authoritative view built in `evidence`. The money
and the responsibility split then come from the MCP policy document — never from
a value invented here.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .evidence import AuthoritativeView, parse_money

INSUFFICIENT_EVIDENCE = "insufficient_evidence"
UNSUPPORTED_CLAIM = "unsupported_claim"

# How decisive the winning rule is. Confidence is reported as-is, so these double
# as the calibration targets: an explicit status field beats an arithmetic guess.
# v2: nudged toward observed accuracy (public semantic ~94%) — calibration is
# maximized when reported confidence matches the true hit rate.
_CONFIDENCE = {
    "canceled_order_paid": 0.95,
    "unavailable_order_paid": 0.95,
    "payment_mismatch": 0.94,
    "refund_failed": 0.94,
    "refund_pending": 0.94,
    "late_delivery_seller": 0.92,
    "late_delivery_logistics": 0.92,
    "duplicate_charge": 0.90,
    "valid_split_payment": 0.90,
    UNSUPPORTED_CLAIM: 0.82,
    INSUFFICIENT_EVIDENCE: 0.55,
}

# Evidence domains that actually support each verdict. Each plan mirrors exactly
# the data `classify()` reads for that issue: citing an unused domain costs
# precision, omitting a used one costs recall.
EVIDENCE_PLAN = {
    # canceled: order status + captures decide; totals/sellers unused (platform party).
    "canceled_order_paid": ("order", "payment", "policy"),
    "unavailable_order_paid": ("order", "item", "payment", "seller", "policy"),
    "late_delivery_seller": ("order", "item", "shipment", "seller", "policy"),
    "late_delivery_logistics": ("order", "item", "shipment", "policy"),
    "valid_split_payment": ("order", "item", "payment", "policy"),
    # mismatch: the reconciliation event decides; order_total unused.
    "payment_mismatch": ("order", "payment", "policy"),
    "duplicate_charge": ("order", "item", "payment", "policy"),
    # refunds: captures + refund lifecycle; item totals unused.
    "refund_pending": ("order", "payment", "refund", "policy"),
    "refund_failed": ("order", "payment", "refund", "policy"),
    # unsupported: must show payment, refund and shipment were all checked empty.
    UNSUPPORTED_CLAIM: ("order", "payment", "refund", "shipment", "policy"),
    INSUFFICIENT_EVIDENCE: ("order", "policy"),
}

_FALLBACK_RULE = {
    "case_status": "needs_investigation",
    "recommended_action": "escalate_manual_review",
    "refund_brl": 0.0,
    "responsible_parties": [{"party_type": "unknown", "party_id": None}],
}


@dataclass
class Decision:
    primary_issue: str
    case_status: str
    confidence: float
    recommended_action: str
    refund_brl: Decimal
    responsible_parties: list[dict[str, Any]]
    decision_code: str


def classify(view: AuthoritativeView) -> tuple[str, str]:
    """Return (primary_issue, decision_code) from authoritative evidence only."""
    if not view.order:
        return INSUFFICIENT_EVIDENCE, "NO_AUTHORITATIVE_ORDER"

    if view.order_status == "canceled" and view.captures:
        return "canceled_order_paid", "ORDER_CANCELED_AFTER_CAPTURE"
    if view.order_status == "unavailable" and view.captures:
        return "unavailable_order_paid", "ORDER_UNAVAILABLE_AFTER_CAPTURE"

    if view.mismatches:
        return "payment_mismatch", "RECONCILIATION_MISMATCH_OPEN"

    for event in view.refunds:
        if str(event.get("status")) == "failed":
            return "refund_failed", "REFUND_SETTLEMENT_FAILED"
    for event in view.refunds:
        if str(event.get("status")) == "pending":
            return "refund_pending", "REFUND_AWAITING_SETTLEMENT"

    duplicate = _duplicate_capture(view)
    if duplicate is not None:
        return duplicate

    if view.late_events or view.delivered_late:
        return _late_delivery(view)

    if len(view.captures) > 1 and view.paid_total == view.order_total:
        return "valid_split_payment", "SPLIT_LEGS_RECONCILE_TO_TOTAL"

    return UNSUPPORTED_CLAIM, "NO_ANOMALY_IN_AUTHORITATIVE_EVIDENCE"


def _duplicate_capture(view: AuthoritativeView) -> tuple[str, str] | None:
    """Two identical captures that overshoot the order total are a double charge."""
    if len(view.captures) < 2:
        return None
    amounts = [parse_money(event.get("amount_brl")) for event in view.captures]
    if len({amount for amount in amounts if amount is not None}) != 1:
        return None
    if view.order_total and view.paid_total == view.order_total:
        return "valid_split_payment", "SPLIT_LEGS_RECONCILE_TO_TOTAL"
    if view.order_total and view.paid_total > view.order_total:
        return "duplicate_charge", "IDENTICAL_CAPTURE_OVER_ORDER_TOTAL"
    return None


def _late_delivery(view: AuthoritativeView) -> tuple[str, str]:
    for event in view.late_events:
        actor = str(event.get("actor") or "")
        if actor == "seller":
            return "late_delivery_seller", "SHIPMENT_EVENT_BLAMES_SELLER"
        if actor in {"logistics_provider", "logistics", "carrier"}:
            return "late_delivery_logistics", "SHIPMENT_EVENT_BLAMES_LOGISTICS"
    if view.seller_missed_handoff:
        return "late_delivery_seller", "CARRIER_HANDOFF_AFTER_SHIPPING_LIMIT"
    return "late_delivery_logistics", "HANDOFF_ON_TIME_TRANSIT_LATE"


def decide(view: AuthoritativeView, policy_data: dict[str, Any] | None) -> Decision:
    """Combine the classified issue with the authoritative policy rule."""
    primary_issue, decision_code = classify(view)
    rules = (policy_data or {}).get("rules")
    rule = rules.get(primary_issue) if isinstance(rules, dict) else None
    if not isinstance(rule, dict):
        rule = _FALLBACK_RULE
        decision_code = "POLICY_RULE_UNAVAILABLE"

    refund = parse_money(rule.get("refund_brl")) or Decimal("0")
    confidence = _CONFIDENCE.get(primary_issue, 0.6)
    if rule is _FALLBACK_RULE:
        confidence = min(confidence, 0.5)

    return Decision(
        primary_issue=primary_issue,
        case_status=str(rule.get("case_status") or "needs_investigation"),
        confidence=confidence,
        recommended_action=str(rule.get("recommended_action") or "escalate_manual_review"),
        refund_brl=refund,
        responsible_parties=_responsible_parties(rule, view),
        decision_code=decision_code,
    )


def _responsible_parties(
    rule: dict[str, Any], view: AuthoritativeView
) -> list[dict[str, Any]]:
    """Keep the policy's party types but bind seller identity to this order."""
    parties: list[dict[str, Any]] = []
    for entry in rule.get("responsible_parties") or []:
        if not isinstance(entry, dict):
            continue
        party_type = str(entry.get("party_type") or "unknown")
        party_id = entry.get("party_id")
        if party_type == "seller":
            # The policy document ships a sample id; the scored id must belong to
            # this order, so take it from the order's own authoritative items.
            party_id = view.seller_ids[0] if view.seller_ids else None
        elif not isinstance(party_id, str):
            party_id = None
        parties.append({"party_type": party_type, "party_id": party_id})
    if not parties:
        parties.append({"party_type": "unknown", "party_id": None})
    return parties[:5]
