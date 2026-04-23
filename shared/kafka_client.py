"""Kafka producer and consumer with DLQ support.

Producer: Thread-safe singleton, idempotent, acks=all.
Consumer: Auto-retry with exponential backoff, routes to DLQ after 3 failures.
Deduplication via processed_events table.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, Callable

from kafka import KafkaConsumer, KafkaProducer
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "localhost:9092")
MAX_RETRIES = 3

_producer: KafkaProducer | None = None


def get_producer() -> KafkaProducer:
    """Get or create a singleton Kafka producer."""
    global _producer
    if _producer is None:
        _producer = KafkaProducer(
            bootstrap_servers=BOOTSTRAP,
            value_serializer=lambda v: json.dumps(v).encode(),
            key_serializer=lambda k: k.encode() if k else None,
            acks="all",
            enable_idempotence=True,
            retries=5,
            compression_type="lz4",
            request_timeout_ms=10000,
        )
    return _producer


def publish(topic: str, payload: dict, key: str | None = None) -> None:
    """Publish a message to a Kafka topic."""
    producer = get_producer()
    producer.send(topic, value=payload, key=key)
    producer.flush(timeout=5)


def is_duplicate(db: Session, event_id: str, consumer_group: str) -> bool:
    """Check if an event has already been processed (idempotency)."""
    row = db.execute(
        text("""
            SELECT 1 FROM processed_events
            WHERE event_id = :eid AND consumer_group = :cg
        """),
        {"eid": event_id, "cg": consumer_group},
    ).fetchone()
    return row is not None


def mark_processed(db: Session, event_id: str, consumer_group: str) -> None:
    """Mark an event as processed for deduplication."""
    db.execute(
        text("""
            INSERT INTO processed_events (event_id, consumer_group)
            VALUES (:eid, :cg)
            ON CONFLICT (event_id) DO NOTHING
        """),
        {"eid": event_id, "cg": consumer_group},
    )


def consume_with_dlq(
    topics: list[str],
    group_id: str,
    handler: Callable[[dict], None],
    db_factory: Callable[[], Session],
) -> None:
    """Consume messages with retry logic and DLQ routing.

    Messages that fail 3 times are sent to a dead letter queue.
    Successfully processed messages are recorded for deduplication.
    """
    consumer = KafkaConsumer(
        *topics,
        bootstrap_servers=BOOTSTRAP,
        group_id=group_id,
        value_deserializer=lambda v: json.loads(v.decode()),
        auto_offset_reset="earliest",
        enable_auto_commit=False,
        max_poll_interval_ms=300000,
    )

    logger.info("Consumer started: topics=%s group=%s", topics, group_id)

    for message in consumer:
        event_id = (message.value or {}).get("event_id", str(uuid.uuid4()))
        db = db_factory()

        try:
            if is_duplicate(db, event_id, group_id):
                logger.debug("Skipping duplicate: %s", event_id)
                consumer.commit()
                continue

            retries = 0
            while retries < MAX_RETRIES:
                try:
                    handler(message.value)
                    mark_processed(db, event_id, group_id)
                    db.commit()
                    break
                except Exception:
                    retries += 1
                    logger.warning(
                        "Retry %d/%d for event %s on %s",
                        retries, MAX_RETRIES, event_id, message.topic,
                    )
                    time.sleep(min(2 ** retries, 30))
            else:
                dlq_topic = f"dlq.{message.topic.split('.')[0]}"
                logger.error(
                    "DLQ routing: event %s -> %s", event_id, dlq_topic,
                )
                publish(dlq_topic, {
                    "original_topic": message.topic,
                    "event_id": event_id,
                    "payload": message.value,
                    "failure_count": MAX_RETRIES,
                })
                mark_processed(db, event_id, group_id)
                db.commit()

            consumer.commit()

        except Exception:
            logger.exception("Fatal error processing %s", event_id)
            db.rollback()
        finally:
            db.close()
