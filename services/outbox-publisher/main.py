"""Outbox Publisher: polls outbox_events and publishes to Kafka.

Runs as an async loop, not a web server. Uses SELECT FOR UPDATE
SKIP LOCKED for safe horizontal scaling. Guarantees at-least-once
delivery by only marking published_at after successful Kafka send.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

sys.path.insert(0, "/app")

from sqlalchemy import text

from shared.database import get_session
from shared.kafka_client import get_producer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("outbox-publisher")

POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "0.5"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "100"))


def publish_batch() -> int:
    """Poll and publish a batch of outbox events. Returns count published."""
    published = 0
    producer = get_producer()

    with get_session() as db:
        rows = db.execute(
            text("""
                SELECT id, event_id, topic, key, payload
                FROM outbox_events
                WHERE published_at IS NULL
                ORDER BY created_at
                LIMIT :batch
                FOR UPDATE SKIP LOCKED
            """),
            {"batch": BATCH_SIZE},
        ).fetchall()

        for row in rows:
            payload = row.payload
            if isinstance(payload, str):
                payload = json.loads(payload)

            producer.send(
                row.topic,
                value=payload,
                key=row.key,
            )

            db.execute(
                text("""
                    UPDATE outbox_events
                    SET published_at = now()
                    WHERE id = :id
                """),
                {"id": row.id},
            )
            published += 1

        if published:
            producer.flush(timeout=10)
            db.commit()

    return published


def main() -> None:
    logger.info(
        "Outbox publisher started (interval=%.1fs, batch=%d)",
        POLL_INTERVAL, BATCH_SIZE,
    )

    while True:
        try:
            count = publish_batch()
            if count:
                logger.info("Published %d events", count)
        except Exception:
            logger.exception("Error in publish cycle")
            time.sleep(5)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
