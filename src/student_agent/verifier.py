"""Cross-field verification and confidence calibration.

The verifier is the last actor before finalize. It repairs the few inconsistencies
it can repair deterministically and lowers confidence for everything else, so a
shaky case never ships an over-confident answer.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .evidence import AuthoritativeView, parse_money

_NO_REFUND_STATUSES = {"no_action"}


def verify(
    output: dict[str, Any], view: AuthoritativeView, cited_refs: list[str]
) -> list[str]:
    """Mutate `output` into a self-consistent state and return the applied codes."""
    notes: list[str] = []
    assessment = output["assessment"]
    money = output["financial_resolution"]
    refund = parse_money(money["recommended_refund_brl"]) or Decimal("0")

    if refund < 0:
        money["recommended_refund_brl"] = 0.0
        refund = Decimal("0")
        notes.append("NEGATIVE_REFUND_CLAMPED")

    # A no_action verdict may not carry money, and money always needs an action.
    if assessment["case_status"] in _NO_REFUND_STATUSES and refund > 0:
        money["recommended_refund_brl"] = 0.0
        money["refund_lines"] = []
        refund = Decimal("0")
        notes.append("NO_ACTION_REFUND_DROPPED")
    if refund > 0 and assessment["case_status"] != "action_required":
        assessment["case_status"] = "action_required"
        notes.append("REFUND_FORCED_ACTION_REQUIRED")

    line_total = sum(
        (parse_money(line["amount_brl"]) or Decimal("0") for line in money["refund_lines"]),
        Decimal("0"),
    )
    if line_total != refund:
        money["refund_lines"] = (
            [
                {
                    "reason_code": output["resolution_actions"][0],
                    "amount_brl": float(refund),
                    "entity_id": view.order_id or None,
                }
            ]
            if refund > 0
            else []
        )
        notes.append("REFUND_LINES_REBALANCED")

    # A seller can only be blamed with a seller id that belongs to this order.
    for party in output["root_cause_analysis"]["responsible_parties"]:
        if party["party_type"] != "seller":
            continue
        if party["party_id"] not in view.seller_ids:
            party["party_id"] = view.seller_ids[0] if view.seller_ids else None
            notes.append("SELLER_IDENTITY_REBOUND")
        if party["party_id"] is None:
            party["party_type"] = "unknown"
            notes.append("SELLER_IDENTITY_UNRESOLVED")

    if not cited_refs:
        notes.append("NO_EVIDENCE_CITED")

    confidence = float(assessment["confidence"])
    if "NO_EVIDENCE_CITED" in notes:
        confidence = min(confidence, 0.35)
    if any(note.startswith("SELLER_IDENTITY") for note in notes):
        confidence = min(confidence, 0.70)
    if len(output["data_conflicts"]) >= 4:
        confidence = min(confidence, 0.80)
    assessment["confidence"] = round(max(0.0, min(1.0, confidence)), 2)

    _align_claims(output, refund, view)
    return notes


def _align_claims(output: dict[str, Any], refund: Decimal, view: AuthoritativeView) -> None:
    """Keep every claim verdict consistent with the case-level verdict."""
    for claim in output.get("claim_assessments", []):
        claim["confidence"] = round(max(0.0, min(1.0, float(claim["confidence"]))), 2)
        claim["evidence_refs"] = [
            ref for ref in claim["evidence_refs"] if ref in output["evidence_refs"]
        ]
    del refund, view
