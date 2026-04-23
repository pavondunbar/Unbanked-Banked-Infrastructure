"""Idempotency layer for API endpoints and message consumers.

Three mechanisms:
  1. Transfer-specific: idempotency_key column on transfers table.
  2. General-purpose: idempotency_store table for any write endpoint
     (wallet funding, etc.). Stores the full JSON response.
  3. Kafka consumers: processed_events table to skip
     already-processed messages (see kafka_client.py).
"""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.orm import Session


def check_transfer_idempotency(
    db: Session,
    idempotency_key: str,
) -> dict | None:
    """Check if a transfer with this idempotency key already exists.

    Returns the existing transfer as a dict, or None if not found.
    """
    row = db.execute(
        text("""
            SELECT t.id, t.corridor, t.send_amount, t.send_currency,
                   t.receive_amount, t.receive_currency, t.status,
                   t.blockchain_tx_hash, t.settlement_ref,
                   t.fx_rate, t.fee_amount,
                   s.id AS settlement_id
            FROM transfers t
            LEFT JOIN settlements s ON s.transfer_id = t.id
            WHERE t.idempotency_key = :key
        """),
        {"key": idempotency_key},
    ).fetchone()

    if not row:
        return None

    return {
        "id": str(row.id),
        "corridor": row.corridor,
        "send_amount": float(row.send_amount),
        "send_currency": row.send_currency,
        "receive_amount": float(row.receive_amount),
        "receive_currency": row.receive_currency,
        "fx_rate": float(row.fx_rate),
        "fee_amount": float(row.fee_amount),
        "status": row.status,
        "blockchain_tx_hash": row.blockchain_tx_hash,
        "settlement_ref": row.settlement_ref,
        "settlement_id": str(row.settlement_id) if row.settlement_id else None,
        "idempotent_hit": True,
    }


def check_idempotency(
    db: Session,
    idempotency_key: str,
) -> dict | None:
    """General-purpose idempotency check against idempotency_store.

    Returns the cached response dict if the key exists, else None.
    """
    row = db.execute(
        text("""
            SELECT response FROM idempotency_store
            WHERE idempotency_key = :key
        """),
        {"key": idempotency_key},
    ).fetchone()
    if not row:
        return None
    resp = row.response
    if isinstance(resp, str):
        resp = json.loads(resp)
    resp["idempotent_hit"] = True
    return resp


def store_idempotency(
    db: Session,
    idempotency_key: str,
    endpoint: str,
    response: dict,
) -> None:
    """Store a response in the idempotency_store for future replays."""
    db.execute(
        text("""
            INSERT INTO idempotency_store
                (idempotency_key, endpoint, response)
            VALUES (:key, :ep, CAST(:resp AS jsonb))
            ON CONFLICT (idempotency_key) DO NOTHING
        """),
        {
            "key": idempotency_key,
            "ep": endpoint,
            "resp": json.dumps(response, default=str),
        },
    )


def check_settlement_idempotency(
    db: Session,
    transfer_id: str,
) -> dict | None:
    """Check if a settlement already exists for this transfer.

    Prevents duplicate settlement creation.
    """
    row = db.execute(
        text("""
            SELECT id, status, blockchain_tx_hash, mpc_signature
            FROM settlements
            WHERE transfer_id = :tid
        """),
        {"tid": transfer_id},
    ).fetchone()

    if not row:
        return None

    return {
        "id": str(row.id),
        "status": row.status,
        "blockchain_tx_hash": row.blockchain_tx_hash,
        "mpc_signature": row.mpc_signature,
        "idempotent_hit": True,
    }
