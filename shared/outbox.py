"""Transactional outbox pattern.

Events are written to the outbox_events table within the same
database transaction as the business operation. A separate
outbox-publisher service polls the table and publishes to Kafka.
This guarantees at-least-once delivery without coupling business
logic to Kafka availability.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


def _serialize(obj: Any) -> str:
    """JSON-serialize with datetime handling."""
    def default(o):
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, uuid.UUID):
            return str(o)
        raise TypeError(f"Not serializable: {type(o)}")
    return json.dumps(obj, default=default)


def insert_outbox_event(
    db: Session,
    topic: str,
    payload: dict,
    key: str | None = None,
) -> str:
    """Write an event to the outbox table (within current transaction).

    Returns the event_id for tracking.
    """
    event_id = str(uuid.uuid4())
    db.execute(
        text("""
            INSERT INTO outbox_events (event_id, topic, key, payload)
            VALUES (:eid, :topic, :key, CAST(:payload AS jsonb))
        """),
        {
            "eid": event_id,
            "topic": topic,
            "key": key,
            "payload": _serialize(payload),
        },
    )
    return event_id
