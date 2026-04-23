"""Deterministic settlement state machine.

Settlement lifecycle:
  PENDING → APPROVED → SIGNED → BROADCASTED → CONFIRMED

Each transition is validated. Invalid transitions raise HTTP 409.
Every transition is recorded in settlement_status_history with
full audit metadata (request_id, actor_id, actor_service).
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from shared.context import get_context

SETTLEMENT_TRANSITIONS: dict[str, set[str]] = {
    "pending":     {"approved", "failed"},
    "approved":    {"signed", "failed"},
    "signed":      {"broadcasted", "failed"},
    "broadcasted": {"confirmed", "failed"},
    "confirmed":   set(),
    "failed":      set(),
}

TRANSFER_TRANSITIONS: dict[str, set[str]] = {
    "pending":          {"compliance_check", "failed"},
    "compliance_check": {"processing", "failed"},
    "processing":       {"settled", "failed"},
    "settled":          set(),
    "failed":           {"refunded"},
    "refunded":         set(),
}


def validate_settlement_transition(current: str, target: str) -> None:
    """Validate a settlement status transition."""
    allowed = SETTLEMENT_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise HTTPException(
            409,
            f"Invalid settlement transition: {current} -> {target}. "
            f"Allowed: {allowed or 'none (terminal state)'}",
        )


def validate_transfer_transition(current: str, target: str) -> None:
    """Validate a transfer status transition."""
    allowed = TRANSFER_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise HTTPException(
            409,
            f"Invalid transfer transition: {current} -> {target}. "
            f"Allowed: {allowed or 'none (terminal state)'}",
        )


def record_settlement_transition(
    db: Session,
    settlement_id: str,
    old_status: str | None,
    new_status: str,
    reason: str = "",
) -> None:
    """Append to settlement_status_history (immutable audit trail)."""
    ctx = get_context()
    db.execute(
        text("""
            INSERT INTO settlement_status_history
                (settlement_id, old_status, new_status, reason,
                 request_id, actor_id, actor_service)
            VALUES (:sid, :old, :new, :reason, :rid, :aid, :asvc)
        """),
        {
            "sid": settlement_id,
            "old": old_status, "new": new_status,
            "reason": reason,
            "rid": str(ctx.request_id) if ctx.request_id else None,
            "aid": ctx.actor_id, "asvc": ctx.actor_service,
        },
    )


def record_transfer_transition(
    db: Session,
    transfer_id: str,
    old_status: str | None,
    new_status: str,
    reason: str = "",
) -> None:
    """Append to transfer_status_history (immutable audit trail)."""
    ctx = get_context()
    db.execute(
        text("""
            INSERT INTO transfer_status_history
                (transfer_id, old_status, new_status, reason,
                 request_id, actor_id, actor_service)
            VALUES (:tid, :old, :new, :reason, :rid, :aid, :asvc)
        """),
        {
            "tid": transfer_id,
            "old": old_status, "new": new_status,
            "reason": reason,
            "rid": str(ctx.request_id) if ctx.request_id else None,
            "aid": ctx.actor_id, "asvc": ctx.actor_service,
        },
    )
