"""Settlement Service: deterministic state machine + background worker.

State machine: PENDING → APPROVED → SIGNED → BROADCASTED → CONFIRMED

- PENDING → APPROVED:     Admin approves the settlement
- APPROVED → SIGNED:      Signer provides MPC signature (separation of duties)
- SIGNED → BROADCASTED:   Background worker broadcasts to chain
- BROADCASTED → CONFIRMED: Background worker confirms on-chain receipt

The settlement worker processes the queue in priority order,
using SELECT FOR UPDATE SKIP LOCKED for safe horizontal scaling.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import uuid
sys.path.insert(0, "/app")

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from shared.blockchain_sim import record_fiat_rail, record_on_chain
from shared.context import extract_context, get_context
from shared.database import get_db, get_session
from shared.events import settlement_status_changed, transfer_settled
from shared.outbox import insert_outbox_event
from shared.rbac import check_separation_of_duties
from shared.state_machine import (
    record_settlement_transition,
    validate_settlement_transition,
)

app = FastAPI(title="Settlement Service")
logger = logging.getLogger(__name__)

SIGNING_GATEWAY_URL = os.environ.get(
    "SIGNING_GATEWAY_URL", "http://signing-gateway:8005",
)

# ── Request schemas ───────────────────────────────────────

class ApproveRequest(BaseModel):
    settlement_id: str
    approved_by: str

class SignRequest(BaseModel):
    settlement_id: str
    signed_by: str


# ── Endpoints ─────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "settlement"}


@app.get("/settlements/{settlement_id}")
async def get_settlement(
    settlement_id: str,
    db: Session = Depends(get_db),
) -> dict:
    row = db.execute(
        text("SELECT * FROM settlements WHERE id = :id"),
        {"id": settlement_id},
    ).fetchone()
    if not row:
        raise HTTPException(404, "Settlement not found")
    return _row_to_dict(row)


@app.post("/settlements/approve")
async def approve(
    body: ApproveRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Approve a settlement (admin role required)."""
    ctx = extract_context(request)
    row = db.execute(
        text("SELECT * FROM settlements WHERE id = :id FOR UPDATE"),
        {"id": body.settlement_id},
    ).fetchone()

    if not row:
        raise HTTPException(404, "Settlement not found")

    # Idempotent: if already at or past 'approved', return current state
    if row.status in ("approved", "signed", "broadcasted", "confirmed"):
        db.rollback()
        return {
            "settlement_id": body.settlement_id,
            "status": row.status,
            "idempotent_hit": True,
        }

    validate_settlement_transition(row.status, "approved")

    db.execute(
        text("""
            UPDATE settlements
            SET status = 'approved', approved_by = :by, approved_at = now()
            WHERE id = :id
        """),
        {"by": body.approved_by, "id": body.settlement_id},
    )
    record_settlement_transition(
        db, body.settlement_id, row.status, "approved",
        f"Approved by {body.approved_by}",
    )
    insert_outbox_event(db, "settlement.approved", settlement_status_changed(
        settlement_id=body.settlement_id,
        transfer_id=str(row.transfer_id),
        old_status=row.status,
        new_status="approved",
        actor_id=body.approved_by,
    ), key=body.settlement_id)

    db.commit()
    return {"settlement_id": body.settlement_id, "status": "approved"}


@app.post("/settlements/sign")
async def sign(
    body: SignRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Sign a settlement (signer role, must differ from approver)."""
    ctx = extract_context(request)
    row = db.execute(
        text("SELECT * FROM settlements WHERE id = :id FOR UPDATE"),
        {"id": body.settlement_id},
    ).fetchone()

    if not row:
        raise HTTPException(404, "Settlement not found")

    # Idempotent: if already at or past 'signed', return current state
    if row.status in ("signed", "broadcasted", "confirmed"):
        db.rollback()
        return {
            "settlement_id": body.settlement_id,
            "status": row.status,
            "mpc_signature": row.mpc_signature,
            "idempotent_hit": True,
        }

    validate_settlement_transition(row.status, "signed")
    check_separation_of_duties(row.approved_by, body.signed_by)

    # Call signing gateway for MPC threshold signature
    async with httpx.AsyncClient(timeout=15) as client:
        payload = {
            "settlement_id": body.settlement_id,
            "transfer_id": str(row.transfer_id),
            "signed_by": body.signed_by,
        }
        resp = await client.post(
            f"{SIGNING_GATEWAY_URL}/sign", json=payload,
        )
        if resp.status_code != 200:
            raise HTTPException(503, f"MPC signing failed: {resp.text}")
        sig_result = resp.json()

    import json as _json
    db.execute(
        text("""
            UPDATE settlements
            SET status = 'signed', signed_by = :by, signed_at = now(),
                mpc_signature = :sig,
                mpc_partials = CAST(:partials AS jsonb)
            WHERE id = :id
        """),
        {
            "by": body.signed_by, "id": body.settlement_id,
            "sig": sig_result["combined_signature"],
            "partials": _json.dumps(sig_result.get("node_details", [])),
        },
    )
    record_settlement_transition(
        db, body.settlement_id, row.status, "signed",
        f"Signed by {body.signed_by}, MPC quorum: "
        f"{sig_result['partials_received']}/{sig_result['total_nodes']}",
    )
    insert_outbox_event(db, "settlement.signed", settlement_status_changed(
        settlement_id=body.settlement_id,
        transfer_id=str(row.transfer_id),
        old_status=row.status,
        new_status="signed",
        actor_id=body.signed_by,
    ), key=body.settlement_id)

    db.commit()
    return {
        "settlement_id": body.settlement_id,
        "status": "signed",
        "mpc_signature": sig_result["combined_signature"],
        "quorum": f"{sig_result['partials_received']}/{sig_result['total_nodes']}",
    }


# ── Background Worker ─────────────────────────────────────

def _process_signed_settlements() -> None:
    """Process signed settlements: broadcast to chain."""
    with get_session() as db:
        rows = db.execute(text("""
            SELECT s.id, s.transfer_id, s.mpc_signature
            FROM settlements s
            WHERE s.status = 'signed'
            ORDER BY s.priority, s.created_at
            LIMIT 10
            FOR UPDATE SKIP LOCKED
        """)).fetchall()

        for row in rows:
            sid = str(row.id)
            tid = str(row.transfer_id)

            # Simulate blockchain broadcast
            receipt = record_on_chain({
                "settlement_id": sid,
                "transfer_id": tid,
                "mpc_signature": row.mpc_signature,
            })

            db.execute(
                text("""
                    UPDATE settlements
                    SET status = 'broadcasted',
                        blockchain_tx_hash = :txh,
                        block_number = :bn,
                        network = :net, gas_used = :gas
                    WHERE id = :id
                """),
                {
                    "txh": receipt["tx_hash"],
                    "bn": receipt["block_number"],
                    "net": receipt["network"],
                    "gas": receipt["gas_used"],
                    "id": sid,
                },
            )
            record_settlement_transition(
                db, sid, "signed", "broadcasted",
                f"TX: {receipt['tx_hash'][:20]}...",
            )
            insert_outbox_event(
                db, "settlement.broadcasted",
                settlement_status_changed(
                    sid, tid, "signed", "broadcasted",
                ),
                key=sid,
            )
            logger.info("Broadcasted settlement %s tx=%s", sid, receipt["tx_hash"][:20])


def _confirm_broadcasted_settlements() -> None:
    """Confirm broadcasted settlements and finalize transfers.

    Hybrid finality: produces BOTH a blockchain receipt (token leg,
    already stored at broadcast) AND a traditional rail receipt (fiat leg).
    """
    with get_session() as db:
        rows = db.execute(text("""
            SELECT s.id, s.transfer_id, s.blockchain_tx_hash,
                   t.corridor, t.send_currency, t.receive_currency
            FROM settlements s
            JOIN transfers t ON t.id = s.transfer_id
            WHERE s.status = 'broadcasted'
            ORDER BY s.created_at
            LIMIT 10
            FOR UPDATE OF s SKIP LOCKED
        """)).fetchall()

        for row in rows:
            sid = str(row.id)
            tid = str(row.transfer_id)
            settlement_ref = f"STL-{uuid.uuid4().hex[:8].upper()}"

            # Fiat leg: generate traditional rail receipt
            fiat_receipt = record_fiat_rail(
                corridor=row.corridor,
                send_currency=row.send_currency,
                receive_currency=row.receive_currency,
                payload={
                    "settlement_id": sid,
                    "transfer_id": tid,
                    "blockchain_tx_hash": row.blockchain_tx_hash,
                },
            )

            # Confirm settlement with both legs
            db.execute(
                text("""
                    UPDATE settlements
                    SET status = 'confirmed', confirmed_at = now(),
                        fiat_rail = :rail, fiat_reference = :fref
                    WHERE id = :id
                """),
                {
                    "id": sid,
                    "rail": fiat_receipt["rail"],
                    "fref": fiat_receipt["reference"],
                },
            )
            record_settlement_transition(
                db, sid, "broadcasted", "confirmed",
                f"Hybrid finality: chain + {fiat_receipt['rail']} "
                f"({fiat_receipt['reference']})",
            )

            # Finalize transfer
            db.execute(
                text("""
                    UPDATE transfers
                    SET status = 'settled',
                        blockchain_tx_hash = :txh,
                        settlement_ref = :ref,
                        settled_at = now()
                    WHERE id = :tid
                """),
                {"txh": row.blockchain_tx_hash, "ref": settlement_ref,
                 "tid": tid},
            )

            from shared.state_machine import record_transfer_transition
            record_transfer_transition(
                db, tid, "processing", "settled",
                f"Settlement confirmed: {settlement_ref}",
            )

            insert_outbox_event(
                db, "transfer.settled",
                transfer_settled(tid, sid, row.blockchain_tx_hash, settlement_ref),
                key=tid,
            )
            insert_outbox_event(
                db, "settlement.confirmed",
                settlement_status_changed(sid, tid, "broadcasted", "confirmed"),
                key=sid,
            )
            logger.info(
                "Confirmed settlement %s ref=%s fiat=%s",
                sid, settlement_ref, fiat_receipt["reference"],
            )


def _worker_loop() -> None:
    """Background worker loop: process signed → broadcast → confirm."""
    logger.info("Settlement worker started")
    while True:
        try:
            _process_signed_settlements()
            _confirm_broadcasted_settlements()
        except Exception:
            logger.exception("Settlement worker error")
        time.sleep(1)


@app.on_event("startup")
async def startup() -> None:
    logging.basicConfig(level=logging.INFO)
    thread = threading.Thread(target=_worker_loop, daemon=True)
    thread.start()


def _row_to_dict(row) -> dict:
    return {
        "id": str(row.id),
        "transfer_id": str(row.transfer_id),
        "status": row.status,
        "priority": row.priority,
        "approved_by": row.approved_by,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "signed_by": row.signed_by,
        "signed_at": row.signed_at.isoformat() if row.signed_at else None,
        "mpc_signature": row.mpc_signature,
        # Token leg (blockchain)
        "blockchain_tx_hash": row.blockchain_tx_hash,
        "block_number": row.block_number,
        "network": row.network,
        # Fiat leg (traditional rail)
        "fiat_rail": row.fiat_rail,
        "fiat_reference": row.fiat_reference,
        "confirmed_at": row.confirmed_at.isoformat() if row.confirmed_at else None,
    }
