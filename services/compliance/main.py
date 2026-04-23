"""Compliance Service: AML/CTF screening via Kafka consumer.

Consumes transfer.created events and runs:
  - Sanctions screening (simulated blocklist)
  - Transaction velocity checks
  - Structuring detection
  - High-risk corridor flags

Results are published to compliance.result topic.
Also exposes a health endpoint for orchestration.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/app")

from fastapi import FastAPI
from sqlalchemy import text

from shared.database import get_session, SessionLocal
from shared.events import compliance_result as make_compliance_event
from shared.kafka_client import consume_with_dlq
from shared.outbox import insert_outbox_event

app = FastAPI(title="Compliance Service")
logger = logging.getLogger(__name__)

HIGH_RISK_CORRIDORS = {("NG", "GB"), ("PH", "US")}
MAX_TRANSFERS_PER_HOUR = 10
REPORTING_THRESHOLD_USD = 3000.0
STRUCTURING_FLOOR_USD = 2500.0

_velocity: dict[str, list[datetime]] = defaultdict(list)


def screen_transfer(event: dict) -> None:
    """Run all compliance checks on a transfer event."""
    transfer_id = event.get("transfer_id", "")
    sender_id = event.get("sender_id", "")
    receiver_id = event.get("receiver_id", "")
    send_amount = float(event.get("send_amount", 0))

    with get_session() as db:
        # Get client details
        sender = db.execute(
            text("SELECT country_code, full_name FROM clients WHERE id = :id"),
            {"id": sender_id},
        ).fetchone()
        receiver = db.execute(
            text("SELECT country_code, full_name FROM clients WHERE id = :id"),
            {"id": receiver_id},
        ).fetchone()

        if not sender or not receiver:
            logger.warning("Client not found for transfer %s", transfer_id)
            return

        flags: list[str] = []
        risk_score = 0

        # Sanctions check
        for name in (sender.full_name, receiver.full_name):
            if "BLOCKED" in (name or "").upper():
                flags.append(f"sanctions_match: {name}")
                risk_score += 100

        # High-risk corridor
        pair = (sender.country_code, receiver.country_code)
        if pair in HIGH_RISK_CORRIDORS:
            flags.append(f"high_risk_corridor: {pair[0]}->{pair[1]}")
            risk_score += 25

        # Structuring detection
        if STRUCTURING_FLOOR_USD <= send_amount < REPORTING_THRESHOLD_USD:
            flags.append(f"potential_structuring: ${send_amount:.2f}")
            risk_score += 30

        # Velocity check
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=1)
        _velocity[sender_id] = [
            t for t in _velocity[sender_id] if t > cutoff
        ]
        if len(_velocity[sender_id]) >= MAX_TRANSFERS_PER_HOUR:
            flags.append(f"velocity: {len(_velocity[sender_id])} txns/hr")
            risk_score += 35
        _velocity[sender_id].append(now)

        # Large transaction
        if send_amount >= REPORTING_THRESHOLD_USD:
            flags.append(f"large_transaction: ${send_amount:.2f}")
            risk_score += 15

        risk_score = min(risk_score, 100)
        result = "block" if risk_score >= 75 else "flag" if risk_score >= 50 else "pass"
        details = "; ".join(flags) if flags else "clean"

        db.execute(
            text("""
                INSERT INTO compliance_events
                    (transfer_id, check_type, result, risk_score, details,
                     request_id)
                VALUES (:tid, 'full_screening', :res, :rs, :det, :rid)
            """),
            {
                "tid": transfer_id, "res": result,
                "rs": risk_score, "det": details,
                "rid": event.get("event_id"),
            },
        )

        insert_outbox_event(
            db, "compliance.result",
            make_compliance_event(transfer_id, result, risk_score, flags),
            key=transfer_id,
        )

        logger.info(
            "Screened transfer %s: result=%s risk=%d flags=%s",
            transfer_id, result, risk_score, flags,
        )


def _consumer_loop() -> None:
    """Background Kafka consumer loop."""
    logger.info("Compliance consumer starting...")
    time.sleep(15)  # Wait for Kafka to be ready
    try:
        consume_with_dlq(
            topics=["transfer.created"],
            group_id="compliance-screening",
            handler=screen_transfer,
            db_factory=SessionLocal,
        )
    except Exception:
        logger.exception("Compliance consumer crashed")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "compliance"}


@app.on_event("startup")
async def startup() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    thread = threading.Thread(target=_consumer_loop, daemon=True)
    thread.start()


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    thread = threading.Thread(target=_consumer_loop, daemon=True)
    thread.start()
    uvicorn.run(app, host="0.0.0.0", port=8004)
