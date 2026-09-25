"""L3A coordinator and specialist agents.

Topology (A2A handoffs, correlated by case_id):

    coordinator
        ├─ order-agent      get_order, get_order_items
        ├─ payment-agent    get_order_payments, get_payment_timeline, get_refund_timeline
        ├─ shipment-agent   get_shipment_summary, get_sellers
        ├─ policy-agent     get_policy
        └─ verifier-agent   no tool access

Each specialist owns a disjoint tool scope, consumes evidence through the MCP
gateway and hands the correlated state forward. Nothing downstream may invent an
`evidence_ref`: refs only ever come from a validated MCP envelope.
"""

from __future__ import annotations

import asyncio

import httpx2

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .evidence import AuthoritativeView, build_view
from .mcp_gateway import EvidenceGateway
from .policy import EVIDENCE_PLAN, decide
from .trace import TraceWriter
from .verifier import verify

# Tool permissions per actor. A specialist may not reach outside its own scope.
AGENT_TOOLS: dict[str, tuple[str, ...]] = {
    "order-agent": ("get_order", "get_order_items"),
    "payment-agent": ("get_order_payments", "get_payment_timeline", "get_refund_timeline"),
    "shipment-agent": ("get_shipment_summary", "get_sellers"),
    "policy-agent": ("get_policy",),
}

# Tools whose absence is a legitimate outcome rather than a failure: a case with
# no refund history simply has no refund timeline to return.
OPTIONAL_TOOLS = frozenset({"get_refund_timeline", "get_sellers", "get_product_context"})

MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 0.5


class ToolPermissionError(RuntimeError):
    pass


@dataclass
class CaseState:
    """The A2A envelope handed between agents. Correlated by case_id."""

    case_id: str
    order_id: str
    policy_version: str
    payloads: dict[str, Any] = field(default_factory=dict)
    refs_by_domain: dict[str, list[str]] = field(default_factory=dict)
    refs_by_tool: dict[str, str] = field(default_factory=dict)
    unavailable: list[str] = field(default_factory=list)

    def refs_for(self, domains: tuple[str, ...]) -> list[str]:
        ordered: list[str] = []
        for domain in domains:
            for ref in self.refs_by_domain.get(domain, []):
                if ref not in ordered:
                    ordered.append(ref)
        return ordered[:30]


class Specialist:
    """One actor with a fixed tool scope, its own retries and its own trace lines."""

    def __init__(
        self, name: str, gateway: EvidenceGateway, trace: TraceWriter, state: CaseState
    ) -> None:
        self.name = name
        self._gateway = gateway
        self._trace = trace
        self._state = state

    async def consume(self, tool_name: str, **arguments: str) -> dict[str, Any] | None:
        if tool_name not in AGENT_TOOLS.get(self.name, ()):
            raise ToolPermissionError(f"{self.name} may not call {tool_name}")

        evidence = await self._call_with_retry(tool_name, **arguments)
        if evidence is None:
            self._state.unavailable.append(tool_name)
            self._trace.emit(
                case_id=self._state.case_id,
                event_type="tool_result_consumed",
                actor=self.name,
                tool_name=tool_name,
                decision_code="EVIDENCE_UNAVAILABLE",
            )
            return None

        ref = evidence["evidence_ref"]
        domain = evidence["domain"]
        self._state.payloads[tool_name] = evidence.get("data")
        self._state.refs_by_tool[tool_name] = ref
        self._state.refs_by_domain.setdefault(domain, []).append(ref)
        self._trace.emit(
            case_id=self._state.case_id,
            event_type="tool_result_consumed",
            actor=self.name,
            tool_name=tool_name,
            evidence_refs=[ref],
            attributes={"domain": domain, "warnings": len(evidence.get("warnings") or [])},
        )
        return evidence

    async def _call_with_retry(self, tool_name: str, **arguments: str) -> dict[str, Any] | None:
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return await self._gateway.call(
                    tool_name, case_id=self._state.case_id, **arguments
                )
            except RuntimeError as exc:
                # Server-side "Error executing tool" is transient under load
                # (observed flapping between healthy and failing windows),
                # so retry with backoff before giving up.
                last_error = exc
                if attempt == MAX_ATTEMPTS:
                    break
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)
            except (TimeoutError, OSError, ValueError, httpx2.TransportError) as exc:
                last_error = exc
                if attempt == MAX_ATTEMPTS:
                    break
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)
        if tool_name in OPTIONAL_TOOLS:
            return None
        raise RuntimeError(f"{self.name} could not consume {tool_name}: {last_error}")


def _handoff(trace: TraceWriter, case_id: str, source: str, target: str, code: str) -> None:
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=source,
        target=target,
        decision_code=code,
    )


def _assign(trace: TraceWriter, case_id: str, target: str, code: str) -> None:
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target=target,
        decision_code=code,
    )


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run the multi-agent investigation for one case and return its output."""
    case_id = str(case["case_id"])
    request = case.get("customer_request") or {}
    state = CaseState(
        case_id=case_id,
        order_id=str(request.get("claimed_order_id") or ""),
        policy_version=str(case.get("policy_version") or ""),
    )

    order_agent = Specialist("order-agent", gateway, trace, state)
    payment_agent = Specialist("payment-agent", gateway, trace, state)
    shipment_agent = Specialist("shipment-agent", gateway, trace, state)
    policy_agent = Specialist("policy-agent", gateway, trace, state)

    _assign(trace, case_id, "order-agent", "SCOPE_ORDER_AND_ITEMS")
    await order_agent.consume("get_order", order_id=state.order_id)
    await order_agent.consume("get_order_items", order_id=state.order_id)
    _handoff(trace, case_id, "order-agent", "payment-agent", "ORDER_SCOPE_RESOLVED")

    _assign(trace, case_id, "payment-agent", "SCOPE_PAYMENT_AND_REFUND")
    await payment_agent.consume("get_order_payments", order_id=state.order_id)
    await payment_agent.consume("get_payment_timeline", order_id=state.order_id)
    await payment_agent.consume("get_refund_timeline", order_id=state.order_id)
    _handoff(trace, case_id, "payment-agent", "shipment-agent", "PAYMENT_SCOPE_RESOLVED")

    _assign(trace, case_id, "shipment-agent", "SCOPE_DELIVERY_AND_SELLER")
    await shipment_agent.consume("get_shipment_summary", order_id=state.order_id)
    await shipment_agent.consume("get_sellers", order_id=state.order_id)
    _handoff(trace, case_id, "shipment-agent", "policy-agent", "DELIVERY_SCOPE_RESOLVED")

    _assign(trace, case_id, "policy-agent", "SCOPE_POLICY_ARBITRATION")
    await policy_agent.consume("get_policy", policy_version=state.policy_version)

    view = build_view(
        order=state.payloads.get("get_order"),
        items=state.payloads.get("get_order_items"),
        payments=state.payloads.get("get_order_payments"),
        payment_timeline=state.payloads.get("get_payment_timeline"),
        refund_timeline=state.payloads.get("get_refund_timeline"),
        shipment=state.payloads.get("get_shipment_summary"),
        sellers=state.payloads.get("get_sellers"),
    )
    decision = decide(view, state.payloads.get("get_policy"))

    cited = state.refs_for(EVIDENCE_PLAN.get(decision.primary_issue, ("order", "policy")))
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        target="verifier-agent",
        decision_code=decision.decision_code,
        evidence_refs=cited[:20],
        attributes={
            "primary_issue": decision.primary_issue,
            "case_status": decision.case_status,
            "recommended_refund_brl": float(decision.refund_brl),
        },
    )
    _handoff(trace, case_id, "policy-agent", "verifier-agent", "POLICY_APPLIED")

    output = _build_output(case, view, decision, cited)
    notes = verify(output, view, cited)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        target="coordinator",
        decision_code=notes[0] if notes else "ALL_INVARIANTS_HELD",
        evidence_refs=output["evidence_refs"][:20],
        attributes={
            "repairs": len(notes),
            "confidence": output["assessment"]["confidence"],
            "data_conflicts": len(output["data_conflicts"]),
            "evidence_unavailable": len(state.unavailable),
        },
    )
    return output


def _build_output(
    case: dict[str, Any],
    view: AuthoritativeView,
    decision: Any,
    cited: list[str],
) -> dict[str, Any]:
    refund = decision.refund_brl
    action = decision.recommended_action
    party_id = next(
        (
            party["party_id"]
            for party in decision.responsible_parties
            if isinstance(party.get("party_id"), str)
        ),
        None,
    )

    refund_lines: list[dict[str, Any]] = []
    if refund > 0:
        refund_lines.append(
            {
                "reason_code": action,
                "amount_brl": float(refund),
                "entity_id": party_id or view.order_id or None,
            }
        )

    conflicts = [
        {
            "field": conflict.field,
            "sources": conflict.sources,
            "selected_source": conflict.selected_source,
            "resolution_code": conflict.resolution_code,
        }
        for conflict in view.conflicts
        if len(conflict.sources) >= 2
    ][:5]

    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": str(case["case_id"]),
        "assessment": {
            "primary_issue": decision.primary_issue,
            "case_status": decision.case_status,
            "confidence": decision.confidence,
        },
        "affected_entities": {
            "order_ids": [view.order_id] if view.order_id else [],
            "item_ids": view.item_ids[:20],
            "seller_ids": view.seller_ids[:20],
            "payment_references": view.payment_references[:20],
            "shipment_ids": [view.order_id] if view.order_id else [],
        },
        "claim_assessments": _claim_assessments(case, view, decision, cited),
        "root_cause_analysis": {
            "ranked_causes": [
                {"cause_code": decision.primary_issue.upper(), "rank": 1},
            ],
            "responsible_parties": decision.responsible_parties,
        },
        "evidence_refs": cited,
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": float(refund),
            "refund_lines": refund_lines,
        },
        "resolution_actions": [action],
    }


def _claim_assessments(
    case: dict[str, Any],
    view: AuthoritativeView,
    decision: Any,
    cited: list[str],
) -> list[dict[str, Any]]:
    claims = (case.get("customer_request") or {}).get("claims") or []
    results: list[dict[str, Any]] = []
    refund = decision.refund_brl

    for claim in claims[:5]:
        if not isinstance(claim, dict):
            continue
        topic = str(claim.get("topic") or "")
        if topic == "requested_full_refund":
            verdict, confidence = _refund_claim_verdict(refund, view.order_total)
        elif decision.primary_issue == "insufficient_evidence":
            verdict, confidence = "insufficient_evidence", decision.confidence
        elif topic == decision.primary_issue:
            verdict, confidence = "supported", decision.confidence
        else:
            verdict, confidence = "unsupported", decision.confidence
        results.append(
            {
                "claim_id": str(claim.get("claim_id") or "")[:64],
                "verdict": verdict,
                "confidence": round(confidence, 2),
                "evidence_refs": cited[:20],
            }
        )
    return results


def _refund_claim_verdict(refund: Decimal, order_total: Decimal) -> tuple[str, float]:
    if refund <= 0:
        return "unsupported", 0.88
    if order_total > 0 and refund >= order_total:
        return "supported", 0.85
    return "partially_supported", 0.85
