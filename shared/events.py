"""Canonical event schemas for Kafka messaging.

All events include event_id and event_time for deduplication
and ordering. Events are published via the transactional outbox.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


def _base_event(event_type: str, **kwargs) -> dict:
    """Build a base event dict with standard fields."""
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "event_time": datetime.now(timezone.utc).isoformat(),
        **kwargs,
    }


def transfer_created(
    transfer_id: str,
    corridor: str,
    sender_id: str,
    receiver_id: str,
    send_amount: float,
    send_currency: str,
    receive_amount: float,
    receive_currency: str,
) -> dict:
    return _base_event(
        "transfer.created",
        transfer_id=transfer_id,
        corridor=corridor,
        sender_id=sender_id,
        receiver_id=receiver_id,
        send_amount=send_amount,
        send_currency=send_currency,
        receive_amount=receive_amount,
        receive_currency=receive_currency,
    )


def transfer_settled(
    transfer_id: str,
    settlement_id: str,
    blockchain_tx_hash: str,
    settlement_ref: str,
) -> dict:
    return _base_event(
        "transfer.settled",
        transfer_id=transfer_id,
        settlement_id=settlement_id,
        blockchain_tx_hash=blockchain_tx_hash,
        settlement_ref=settlement_ref,
    )


def transfer_failed(transfer_id: str, reason: str) -> dict:
    return _base_event(
        "transfer.failed",
        transfer_id=transfer_id,
        reason=reason,
    )


def settlement_status_changed(
    settlement_id: str,
    transfer_id: str,
    old_status: str,
    new_status: str,
    actor_id: str | None = None,
) -> dict:
    return _base_event(
        f"settlement.{new_status}",
        settlement_id=settlement_id,
        transfer_id=transfer_id,
        old_status=old_status,
        new_status=new_status,
        actor_id=actor_id,
    )


def compliance_result(
    transfer_id: str,
    result: str,
    risk_score: int,
    flags: list[str],
) -> dict:
    return _base_event(
        "compliance.result",
        transfer_id=transfer_id,
        result=result,
        risk_score=risk_score,
        flags=flags,
    )


def reconciliation_alert(
    run_id: int,
    mismatches: int,
    details: list[dict],
) -> dict:
    return _base_event(
        "reconciliation.alert",
        run_id=run_id,
        mismatches=mismatches,
        details=details,
    )
