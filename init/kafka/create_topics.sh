#!/bin/bash
set -euo pipefail

KAFKA_BIN="/opt/bitnami/kafka/bin"
BOOTSTRAP="kafka:29092"

echo "Waiting for Kafka to be ready..."
sleep 10

topics=(
    "transfer.created"
    "transfer.settled"
    "transfer.failed"
    "settlement.created"
    "settlement.approved"
    "settlement.signed"
    "settlement.broadcasted"
    "settlement.confirmed"
    "compliance.screening"
    "compliance.result"
    "fx.rate.updated"
    "audit.trail"
    "reconciliation.alert"
    "dlq.transfer"
    "dlq.settlement"
    "dlq.compliance"
)

for topic in "${topics[@]}"; do
    echo "Creating topic: ${topic}"
    "$KAFKA_BIN/kafka-topics.sh" \
        --bootstrap-server "$BOOTSTRAP" \
        --create \
        --if-not-exists \
        --topic "$topic" \
        --partitions 3 \
        --replication-factor 1 \
        --config retention.ms=604800000
done

echo "All topics created."
