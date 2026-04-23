# UNBANKED/BANKED STABLECOIN INFRASTRUCTURE

> **SANDBOX / EDUCATIONAL USE ONLY -- NOT FOR PRODUCTION**
> This codebase is a reference implementation designed for learning, prototyping, and architectural exploration. It is **not audited, not legally reviewed, and must not be used to manage real funds, process real remittances, or interface with real mobile money or banking rails.** See the [Production Warning](#production-warning) section for full details.

Microservices platform for cross-border stablecoin transfers serving both banked and unbanked clients. Built around an agent network model where cash-in/cash-out operators bridge the last mile for clients without bank accounts -- enabling remittance corridors like Nigeria to Kenya, USA to Philippines, and Ghana to UK.

The system implements a blockchain-grade double-entry ledger with immutability enforcement, transactional outbox event publishing, MPC-based transaction signing, role-based access control with separation of duties, FX conversion across 28 currency pairs, and a trust-boundary network model where only the API gateway is internet-facing.

---

## Table of Contents

- [Architecture](#architecture)
- [Services](#services)
- [Data Model](#data-model)
- [Kafka Topics](#kafka-topics)
- [API Reference](#api-reference)
- [Getting Started](#getting-started)
- [Scripts and Utilities](#scripts-and-utilities)
- [Testing](#testing)
- [Technical Design](#technical-design)
- [Production Warning](#production-warning)
- [License](#license)

---

## Architecture

```
                           Internet
                              |
                    +---------+---------+
                    |   API Gateway     |  :8000 (only exposed port)
                    |  RBAC + rate-limit|
                    +----+-+-+-+--------+
                         | | | |
              DMZ network| | | |internal network
         +---------------+ | | +---------------+
         |          +------+ +------+           |
         v          v               v           v
   +-----------+ +-------+ +----------+ +----------+
   | Transfer  | |  FX   | |Compliance| |  Recon-  |
   |  Engine   | |Engine | | Monitor  | | ciliation|
   |   :8001   | | :8003 | |  :8004   | |  :8007   |
   +-----------+ +-------+ +----------+ +----------+
         |                                    |
         +-----+----+-----+-----+            |
               |          |     |            |
         +-----+---+ +----+----+----+  +-----+----+
         |Postgres | | Kafka   |    |  | Settlement|
         |  :5432  | |  :9092  |    |  |   :8002   |
         +---------+ +----+----+    |  +-----+-----+
                          |         |        |
              +-----------+    +----+----+ signing network
              |                | Outbox  |   |
        +-----+------+        |Publisher|   |
        |  Compliance |        +---------+   |
        |   Consumer  |              +-------+-------+
        +-------------+              |   Signing     |
                                     |   Gateway     |
                                     |    :8005      |
                                     +--+---+---+---+
                                        |   |   |
                                     +--+  ++-  +-+
                                     |MPC||MPC||MPC|
                                     | 1 || 2 || 3 |
                                     +---++---++---+
```

### Network Isolation

| Network | Services | Internet Access |
|---------|----------|-----------------|
| **dmz** | api-gateway | Yes (host-reachable) |
| **internal** | All microservices, postgres, kafka, outbox-publisher | No (`internal: true`) |
| **signing** | signing-gateway, mpc-node-1, mpc-node-2, mpc-node-3 | No (`internal: true`) |

The API gateway bridges dmz and internal. The settlement service bridges internal and signing. All other backend services are unreachable from outside the Docker network. MPC nodes communicate only with the signing gateway.

### Infrastructure

| Component | Image | Purpose |
|-----------|-------|---------|
| PostgreSQL | `postgres:16-alpine` | Persistent storage, immutable double-entry ledger |
| Kafka | `apache/kafka:3.9.0` | Event streaming between services (KRaft mode, no Zookeeper) |

---

## Services

### API Gateway (port 8000)

The sole internet-facing service. Authenticates requests via `X-API-Key` header against SHA-256 hashed keys in the database, resolves the caller's role (admin, operator, signer, auditor, system), enforces rate limits (1,000 requests per 60-second sliding window per API key), and reverse-proxies to internal services. Every request is logged to the `audit.trail` Kafka topic with `X-Request-ID`, actor identity, and elapsed time.

**Route mapping:**

| Path Prefix | Upstream Service | Port |
|-------------|------------------|------|
| `/transfers`, `/clients`, `/wallets`, `/agents`, `/kyc` | transfer-engine | 8001 |
| `/settlements` | settlement | 8002 |
| `/fx`, `/rates`, `/convert` | fx-engine | 8003 |
| `/reconciliation` | reconciliation | 8007 |

`GET /health` aggregates health from all five downstream services, returning `200` with `status: "ok"` if all are healthy or `status: "degraded"` if any are unreachable.

### Transfer Engine (port 8001)

Handles client onboarding, wallet management, agent registration, KYC verification, and the full cross-border transfer pipeline across all three corridors. Every balance-changing operation produces matching debit and credit journal entries (double-entry bookkeeping).

**Transfer pipeline:**
1. Idempotency check (returns cached result if key exists)
2. Load and validate sender/receiver clients
3. Determine corridor from client types (unbanked-to-unbanked, banked-to-unbanked, unbanked-to-banked)
4. FX conversion via fx-engine
5. KYC tier limit check (converted to USD equivalent)
6. Acquire advisory lock on sender wallet
7. Verify sufficient balance (amount + 0.5% fee)
8. Record three journal pairs: sell leg, buy leg, fee collection
9. Create settlement record
10. Publish `transfer.created` event via transactional outbox

**KYC limits (per transaction, USD equivalent):**

| Tier | Limit |
|------|-------|
| none | $0 (blocked) |
| basic | $500 |
| enhanced | $5,000 |
| full | $50,000 |

**System accounts seeded at startup:**

| UUID | Name | Purpose |
|------|------|---------|
| `00000000-...-000000000010` | Platform Omnibus Reserve | Backs all client deposits |
| `00000000-...-000000000011` | FX Liquidity Pool | Intermediary for currency conversion |
| `00000000-...-000000000012` | Fee Collection | Accumulates transfer fees |

### Settlement Service (port 8002)

Manages the settlement state machine and runs a background worker for blockchain broadcast and confirmation. Uses `SELECT FOR UPDATE SKIP LOCKED` for safe horizontal scaling of the worker.

**Approval workflow:**

| Step | Endpoint | Required Role | Constraint |
|------|----------|---------------|------------|
| Approve | `POST /settlements/approve` | admin | Transitions pending to approved |
| Sign | `POST /settlements/sign` | signer | Transitions approved to signed; signer must differ from approver |

**State machine:** `pending -> approved -> signed -> broadcasted -> confirmed`

The background worker (1-second polling) picks up signed settlements, broadcasts them to the simulated L2 chain, and confirms them -- finalizing the parent transfer as `settled`.

### FX Engine (port 8003)

Currency conversion service maintaining 28 live rate pairs across 11 currencies. Rates include configurable bid/ask spreads (default 50 basis points).

**Seeded currency pairs:** USD/NGN, USD/KES, USD/GHS, USD/PHP, USD/INR, USD/MXN, USD/BRL, USD/EUR, USD/GBP, NGN/KES, GHS/GBP, GHS/NGN, PHP/KES, EUR/GBP, and all inverses.

**Rate calculation:**
- Bid: `mid_rate * (1 + spread_bps / 10000)` (selling price)
- Ask: `mid_rate * (1 - spread_bps / 10000)` (buying price)
- Conversion uses the ask rate

### Compliance Monitor (port 8004)

Consumes `transfer.created` events from Kafka and runs rule-based AML/CTF screening. Operates as fire-and-forget -- it never blocks the originating transaction. Deduplicates events via the `processed_events` table.

**AML rules:**

| Rule | Threshold | Risk Score |
|------|-----------|------------|
| Sanctions screening | Client name contains "BLOCKED" | +100 |
| High-risk corridor | NG/GB or PH/US country pair | +25 |
| Structuring detection | $2,500 - $3,000 range | +30 |
| Velocity check | >= 10 transfers/hour from same sender | +35 |
| Large transaction | >= $3,000 USD equivalent | +15 |

Risk scores are cumulative (capped at 100). Result: >= 75 = `block`, >= 50 = `flag`, < 50 = `pass`.

### Signing Gateway (port 8005)

Coordinates MPC signing requests across three independent MPC nodes in the isolated signing network. Fans out the signing payload to all nodes, collects partial signatures, and combines them when the 2-of-3 threshold is met. Returns HTTP 503 if the threshold is not met.

### MPC Nodes (3 instances)

Stateless signing nodes that produce deterministic partial signatures (`SHA-256(node_id:sorted_json_payload)`). Each node runs independently in the isolated signing network with no database access and no internet access.

### Outbox Publisher

Polls the `outbox_events` database table every 500ms for unpublished events and forwards them to Kafka. Uses `FOR UPDATE SKIP LOCKED` for safe horizontal scaling. Publishes with `acks=all` and marks events as published within the same transaction. Processes batches of 100 events per cycle.

### Reconciliation Service (port 8007)

Verifies ledger integrity and enables deterministic state rebuilds from the append-only journal.

**Run types:**

| Type | What It Verifies |
|------|-----------------|
| Balance check | Global debits == credits, no negative non-system balances |
| Transfer verify | Every settled transfer has >= 2 matching journal entries |
| State rebuild | Reconstructs all wallet balances purely from journal_entries |

---

## Data Model

14 tables with PostgreSQL enums, check constraints, foreign keys, and immutability rules.

### Enums

| Enum | Values |
|------|--------|
| `client_type` | unbanked, banked |
| `kyc_tier` | none, basic, enhanced, full |
| `corridor_type` | unbanked_to_unbanked, banked_to_unbanked, unbanked_to_banked |
| `transfer_status` | pending, compliance_check, processing, settled, failed, refunded |
| `settlement_status` | pending, approved, signed, broadcasted, confirmed |
| `journal_direction` | debit, credit |
| `compliance_result` | pass, flag, block |

### Core Tables

```
clients                      wallets
  +- id (UUID PK)             +- id (UUID PK)
  +- full_name                +- client_id FK -> clients
  +- client_type (enum)       +- currency
  +- phone (UNIQUE)           +- created_at
  +- country_code             +- UNIQUE(client_id, currency)
  +- kyc_tier (enum)
  +- bank_name (nullable)     agents
  +- bank_account (nullable)    +- id (UUID PK)
  +- bank_routing (nullable)    +- name, country_code, region
  +- active (default true)      +- currencies
  +- created_at                 +- float_limit_usd NUMERIC(18,2)
                                +- active (default true)

api_keys
  +- id (UUID PK)
  +- key_hash (SHA-256, UNIQUE)
  +- role (admin|operator|signer|auditor|system)
  +- description
  +- active (default true)
```

### Ledger Tables (Immutable)

```
journal_entries (IMMUTABLE -- PostgreSQL rules reject UPDATE and DELETE)
  +- id (BIGSERIAL PK)
  +- entry_ref (JRN-{hex})
  +- wallet_id
  +- direction (debit|credit)
  +- amount NUMERIC(18,2), CHECK > 0
  +- currency
  +- coa_code (chart of accounts)
  +- description
  +- transfer_id FK (nullable)
  +- settlement_id FK (nullable)
  +- request_id, actor_id, actor_service (audit trail)
  +- created_at
```

### Business Tables

```
transfers                    settlements
  +- id (UUID PK)             +- id (UUID PK)
  +- corridor (enum)          +- transfer_id FK -> transfers
  +- sender_id FK             +- status (enum)
  +- receiver_id FK           +- priority (1-3)
  +- send_amount / currency   +- approved_by, approved_at
  +- receive_amount / currency+- signed_by, signed_at
  +- fx_rate NUMERIC(18,8)    +- mpc_signature
  +- fee_amount / fee_currency+- mpc_partials (JSONB)
  +- status (enum)            +- blockchain_tx_hash
  +- blockchain_tx_hash       +- block_number, block_hash
  +- settlement_ref           +- network (default 'stablecoin-l2')
  +- agent_in_id FK (nullable)+- gas_used
  +- agent_out_id FK (nullable)+- confirmed_at
  +- idempotency_key (UNIQUE) +- request_id, created_at
  +- request_id, created_at
  +- settled_at

fx_rates                     compliance_events
  +- base_currency             +- transfer_id FK
  +- quote_currency            +- check_type
  +- mid_rate NUMERIC(18,8)    +- result (pass|flag|block)
  +- spread_bps (default 50)   +- risk_score
  +- active (default true)     +- details
```

### Event Infrastructure Tables

```
outbox_events                processed_events
  +- id (BIGSERIAL PK)        +- event_id (UUID PK)
  +- event_id (UUID, UNIQUE)   +- consumer_group
  +- topic                     +- processed_at
  +- key (nullable)
  +- payload (JSONB)
  +- created_at
  +- published_at (NULL until processed)

reconciliation_runs
  +- id (BIGSERIAL PK)
  +- run_type, status
  +- total_wallets, mismatches
  +- details (JSONB)
  +- started_at, completed_at
```

### Status History Tables (Append-Only)

Two append-only tables track every state transition with `request_id`, `actor_id`, `actor_service`, and timestamp for forensic audit:

| History Table | Parent Entity |
|---------------|---------------|
| `transfer_status_history` | `transfers` |
| `settlement_status_history` | `settlements` |

### Indexes

16 indexes covering: journal lookups by wallet/currency/entry_ref/transfer/settlement, transfer lookups by status/sender/receiver/idempotency_key, settlement lookups by status+priority, unpublished outbox events (partial index), active FX rates (partial index), and status history tables.

---

## Kafka Topics

16 topics provisioned at startup, each with 3 partitions, replication factor 1, 7-day retention.

| Topic | Purpose |
|-------|---------|
| `transfer.created` | New transfer created |
| `transfer.settled` | Transfer finalized on-chain |
| `transfer.failed` | Transfer failure |
| `settlement.created` | Settlement record created |
| `settlement.approved` | Settlement approved by admin |
| `settlement.signed` | MPC signing completed |
| `settlement.broadcasted` | Transaction broadcast to chain |
| `settlement.confirmed` | On-chain confirmation received |
| `compliance.screening` | Compliance screening requests |
| `compliance.result` | Screening results (pass/flag/block) |
| `fx.rate.updated` | FX rate change notifications |
| `audit.trail` | API gateway audit log |
| `reconciliation.alert` | Reconciliation mismatch alerts |
| `dlq.transfer` | Dead letter queue for transfer events |
| `dlq.settlement` | Dead letter queue for settlement events |
| `dlq.compliance` | Dead letter queue for compliance events |

---

## API Reference

All requests go through the API gateway at `http://localhost:8000`. Include the API key as `X-API-Key` header.

**Seeded API keys (demo only):**

| Key | Role | Purpose |
|-----|------|---------|
| `admin-key-001` | admin | Full access |
| `operator-key-001` | operator | Transfers, wallets, clients, agents |
| `signer-key-001` | signer | Sign settlements |
| `auditor-key-001` | auditor | Read-only reconciliation and audit |
| `system-key-001` | system | Internal service-to-service |

### Client Onboarding

```bash
# Create unbanked client
curl -X POST http://localhost:8000/clients \
  -H "X-API-Key: admin-key-001" \
  -H "Content-Type: application/json" \
  -d '{
    "full_name": "Amara Okafor",
    "client_type": "unbanked",
    "phone": "+234-801-234-5678",
    "country_code": "NG"
  }'

# Create banked client
curl -X POST http://localhost:8000/clients \
  -H "X-API-Key: admin-key-001" \
  -H "Content-Type: application/json" \
  -d '{
    "full_name": "Sarah Johnson",
    "client_type": "banked",
    "phone": "+1-555-123-4567",
    "country_code": "US",
    "bank_name": "Chase",
    "bank_account": "****4521",
    "bank_routing": "021000021"
  }'

# KYC verification
curl -X POST http://localhost:8000/kyc/verify \
  -H "X-API-Key: admin-key-001" \
  -H "Content-Type: application/json" \
  -d '{"client_id": "<client-uuid>", "tier": "basic"}'
```

### Wallets and Funding

```bash
# Create wallet
curl -X POST http://localhost:8000/wallets \
  -H "X-API-Key: admin-key-001" \
  -H "Content-Type: application/json" \
  -d '{"client_id": "<client-uuid>", "currency": "NGN"}'

# Fund wallet via cash-in (unbanked -- through agent)
curl -X POST http://localhost:8000/wallets/fund \
  -H "X-API-Key: admin-key-001" \
  -H "Content-Type: application/json" \
  -d '{
    "wallet_id": "<wallet-uuid>",
    "amount": 150000,
    "currency": "NGN",
    "method": "cash_in",
    "agent_id": "<agent-uuid>"
  }'

# Fund wallet via bank deposit (banked)
curl -X POST http://localhost:8000/wallets/fund \
  -H "X-API-Key: admin-key-001" \
  -H "Content-Type: application/json" \
  -d '{
    "wallet_id": "<wallet-uuid>",
    "amount": 1000,
    "currency": "USD",
    "method": "bank_deposit",
    "bank_ref": "CHASE-98765"
  }'

# Query balance (derived from journal)
curl http://localhost:8000/wallets/<wallet-uuid>/balance \
  -H "X-API-Key: admin-key-001"
```

### Agent Network

```bash
# Register cash-in/cash-out agent
curl -X POST http://localhost:8000/agents \
  -H "X-API-Key: admin-key-001" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Lagos MobileMoney Hub",
    "country_code": "NG",
    "region": "Lagos",
    "currencies": "NGN,USD",
    "float_limit_usd": 100000
  }'
```

### Cross-Border Transfers

```bash
# Unbanked-to-unbanked transfer (Nigeria -> Kenya)
curl -X POST http://localhost:8000/transfers \
  -H "X-API-Key: admin-key-001" \
  -H "Content-Type: application/json" \
  -d '{
    "sender_id": "<sender-uuid>",
    "receiver_id": "<receiver-uuid>",
    "amount": 50000,
    "send_currency": "NGN",
    "receive_currency": "KES",
    "sender_wallet_id": "<sender-wallet-uuid>",
    "receiver_wallet_id": "<receiver-wallet-uuid>",
    "agent_in_id": "<nigeria-agent-uuid>",
    "agent_out_id": "<kenya-agent-uuid>",
    "idempotency_key": "TXN-20260422-001"
  }'
```

### Settlement Workflow

```bash
# Approve settlement (requires admin role)
curl -X POST http://localhost:8000/settlements/approve \
  -H "X-API-Key: admin-key-001" \
  -H "Content-Type: application/json" \
  -d '{
    "settlement_id": "<settlement-uuid>",
    "approved_by": "admin-001"
  }'

# Sign settlement (requires signer role, must differ from approver)
curl -X POST http://localhost:8000/settlements/sign \
  -H "X-API-Key: signer-key-001" \
  -H "Content-Type: application/json" \
  -d '{
    "settlement_id": "<settlement-uuid>",
    "signed_by": "signer-001"
  }'

# Query settlement status
curl http://localhost:8000/settlements/<settlement-uuid> \
  -H "X-API-Key: admin-key-001"
```

### FX Rates

```bash
# Get rate for a currency pair
curl http://localhost:8000/rates/USD/NGN \
  -H "X-API-Key: admin-key-001"

# Convert amount
curl http://localhost:8000/convert/USD/NGN/1000 \
  -H "X-API-Key: admin-key-001"

# List all active rates
curl http://localhost:8000/rates \
  -H "X-API-Key: admin-key-001"
```

### Reconciliation

```bash
# Balance reconciliation (verify debits == credits)
curl -X POST http://localhost:8000/reconciliation/balance \
  -H "X-API-Key: auditor-key-001"

# Transfer reconciliation (verify journal entries for settled transfers)
curl -X POST http://localhost:8000/reconciliation/transfers \
  -H "X-API-Key: auditor-key-001"

# Deterministic state rebuild from journal
curl http://localhost:8000/reconciliation/rebuild \
  -H "X-API-Key: auditor-key-001"

# List reconciliation run history
curl http://localhost:8000/reconciliation/runs \
  -H "X-API-Key: auditor-key-001"
```

### Health

```bash
# Gateway health (aggregates all downstream services)
curl http://localhost:8000/health -H "X-API-Key: admin-key-001"
```

---

## Getting Started

### Prerequisites

- Docker and Docker Compose
- 4 GB RAM minimum (Kafka + PostgreSQL + 9 services + 3 MPC nodes)
- Python 3.11+ with `httpx` (for the demo script)

### 1. Start the platform

```bash
make up
```

This builds and starts 13 containers:

| # | Container | Purpose |
|---|-----------|---------|
| 1 | **postgres** | Database with schema, indexes, and seed data |
| 2 | **kafka** | Event broker (KRaft mode, single-node) |
| 3 | **api-gateway** | Reverse proxy, RBAC, rate limiting |
| 4 | **transfer-engine** | Client/wallet/transfer management |
| 5 | **settlement** | Settlement state machine + background worker |
| 6 | **fx-engine** | FX rates and currency conversion |
| 7 | **compliance** | AML/CTF screening (Kafka consumer) |
| 8 | **outbox-publisher** | Transactional outbox to Kafka relay |
| 9 | **reconciliation** | Ledger integrity verification |
| 10 | **signing-gateway** | MPC signing coordinator |
| 11 | **mpc-node-1** | MPC signing node |
| 12 | **mpc-node-2** | MPC signing node |
| 13 | **mpc-node-3** | MPC signing node |

### 2. Verify

```bash
# Check all containers are running
make ps

# Test gateway health
make health

# Expected response:
# {"status": "ok", "services": {"transfer-engine": "ok", "settlement": "ok", ...}}
```

### 3. Run the demo

```bash
make demo
```

The demo exercises the full platform:

1. **Agent Network** -- Registers 4 cash-in/cash-out agents in Nigeria, Kenya, Philippines, and Ghana
2. **Client Onboarding** -- Creates 6 clients (4 unbanked, 2 banked) across 6 countries
3. **KYC Verification** -- Unbanked clients get basic tier ($500/txn), banked clients get full tier ($50,000/txn)
4. **Wallet Funding** -- Funds wallets via cash-in (agent) and bank deposit methods
5. **Corridor 1: Unbanked to Unbanked** -- Amara (Nigeria) sends 50,000 NGN to James (Kenya)
6. **Corridor 2: Banked to Unbanked** -- Sarah (USA, Chase) sends $200 to Maria (Philippines)
7. **Corridor 3: Unbanked to Banked** -- Kwame (Ghana) sends 500 GHS to David (UK, Barclays)
8. **Reconciliation** -- Verifies ledger integrity, transfer consistency, and deterministic state rebuild
9. **Idempotency** -- Replays corridor 1 with the same key, confirms duplicate detection

Each transfer flows through: FX conversion, journal entries (sell + buy + fee legs), settlement creation, admin approval, MPC 2-of-3 signing, blockchain broadcast, on-chain confirmation.

### Teardown

```bash
make clean   # Stop containers and remove volumes
make nuke    # Full reset -- containers, volumes, images, orphans
```

---

## Scripts and Utilities

| Script | Purpose |
|--------|---------|
| `scripts/demo.py` | End-to-end demo: onboarding, funding, 3 corridors, reconciliation, idempotency |

### Makefile Targets

```bash
make help            # Show all available targets
make up              # Build and start all services
make down            # Stop containers (keep data)
make build           # Build all Docker images
make restart         # Restart all services
make logs            # Follow all service logs (tail 50)
make ps              # Container status
make clean           # Stop and remove containers + volumes
make nuke            # Full reset -- containers, volumes, images, orphans
make health          # Check gateway health via API
make integrity       # Verify ledger integrity (debits == credits)
make demo            # Run end-to-end demo
make test            # Run all tests
make test-unit       # Run unit tests only
make test-e2e        # Run e2e tests (requires running stack)
make db-balances     # Show all wallet balances (derived from journal)
make db-ledger       # Show recent journal entries
make db-transfers    # Show recent transfers
make db-settlements  # Show settlements with state machine status
make db-fx           # Show active FX rates
make topics          # List all Kafka topics
make kafka-tail      # Tail settled transfer events
make seed-accounts   # Seed demo client, wallet, and fund via API
make shell-pg        # Open interactive psql shell
make shell-kafka     # Open shell inside Kafka container
```

---

## Testing

```bash
make test
```

18 pure unit tests covering the most critical invariants -- no Docker, database, or network required.

| Test | Coverage |
|------|----------|
| `test_journal_rejects_zero_amount` | Journal entry validation: amount=0 raises ValueError |
| `test_journal_rejects_negative_amount` | Journal entry validation: amount=-50 raises ValueError |
| `test_settlement_valid_full_path` | Settlement state machine: pending -> approved -> signed -> broadcasted -> confirmed |
| `test_settlement_rejects_skip` | Cannot jump from pending directly to signed (HTTP 409) |
| `test_settlement_rejects_backward` | Cannot go from confirmed back to pending (HTTP 409) |
| `test_settlement_terminal_states_are_empty` | confirmed and failed have no outgoing transitions |
| `test_settlement_any_active_state_can_fail` | Every non-terminal state allows transition to failed |
| `test_transfer_settled_is_terminal` | settled -> failed raises HTTP 409 |
| `test_transfer_failed_to_refunded` | failed -> refunded is valid |
| `test_mpc_quorum_2_of_3` | 2 partial signatures meet threshold, combined sig is 64 hex chars |
| `test_mpc_quorum_fails_below_threshold` | 1 partial does not meet threshold=2 |
| `test_mpc_deterministic` | Same node + payload produces identical partial signature |
| `test_mpc_different_nodes_differ` | Different nodes produce different partials |
| `test_blockchain_receipt_fields` | Receipt: 0x-prefixed 66-char tx_hash, block >= 19M, gas = 21000 |
| `test_separation_of_duties_blocks_same_actor` | Same actor approving + signing raises HTTP 403 |
| `test_separation_of_duties_allows_different_actors` | Different actors pass separation check |
| `test_separation_of_duties_allows_none_approver` | None approver with any signer passes |
| `test_api_key_hash_deterministic` | SHA-256 hashing is deterministic, produces 64-char hex |

---

## Technical Design

### Double-Entry Journal System

Every balance-changing operation creates exactly two `journal_entries` records: one debit and one credit, linked by a shared `entry_ref`. Entries reference a chart of accounts that defines the account type:

| COA Code | Purpose |
|----------|---------|
| `CLIENT_WALLET` | Client token holdings |
| `OMNIBUS_RESERVE` | Platform backing reserve |
| `FX_POOL_{currency}` | FX liquidity per currency |
| `FEE_REVENUE` | Collected fees |
| `AGENT_FLOAT` | Agent cash float |

**Balance derivation** (no stored balances): `SUM(credits) - SUM(debits)` computed on every query from the journal. The journal is the authoritative source of truth; the reconciliation engine verifies consistency.

### Immutability Enforcement

PostgreSQL rules prevent mutation of the journal:

```sql
CREATE RULE journal_no_update AS ON UPDATE TO journal_entries DO INSTEAD NOTHING;
CREATE RULE journal_no_delete AS ON DELETE TO journal_entries DO INSTEAD NOTHING;
```

UPDATE and DELETE statements silently return `0 rows affected` rather than raising errors, making the ledger append-only at the database level.

### Settlement State Machine

Deterministic state transitions defined in `shared/state_machine.py`:

**Settlement transitions:**
```
pending -> approved -> signed -> broadcasted -> confirmed
   |          |          |            |
   +-> failed +-> failed +-> failed   +-> failed
```

**Transfer transitions:**
```
pending -> compliance_check -> processing -> settled
   |             |                 |
   +-> failed    +-> failed        +-> failed
         |
         +-> refunded
```

`validate_settlement_transition()` and `validate_transfer_transition()` raise HTTP 409 on invalid transitions. Every transition is recorded in the corresponding status history table with full audit context.

### Role-Based Access Control (RBAC)

Five roles enforce separation of duties:

| Role | Permissions |
|------|-------------|
| `admin` | Full access to all endpoints |
| `operator` | Transfers, wallets, clients, agents, FX |
| `signer` | Sign settlements only |
| `auditor` | Read-only reconciliation and audit |
| `system` | Internal service-to-service |

API keys are stored as SHA-256 hashes. The API gateway resolves keys, validates route-role mappings, and injects `X-Request-ID`, `X-Actor-ID`, and `X-Actor-Service` headers before proxying.

**Separation of duties:** The signer of a settlement must differ from its approver. Attempting to sign a settlement you approved raises HTTP 403.

### MPC Transaction Signing

2-of-3 threshold signing for blockchain settlement:

1. Settlement service sends signing request to signing gateway
2. Gateway fans out to all 3 MPC nodes in the isolated signing network
3. Each node produces a deterministic partial signature: `SHA-256(node_id:sorted_json_payload)`
4. Gateway sorts and combines partials once threshold (2) is met: `SHA-256(partial1:partial2)`
5. Combined signature and partials stored on the settlement record

Simulated (deterministic hashes, not real cryptographic MPC) -- suitable for demonstration purposes.

### Transactional Outbox Pattern

Services write events to the `outbox_events` table atomically within the same database transaction as business operations. The dedicated `outbox-publisher` service polls this table every 500ms, publishes events to Kafka with `acks=all`, and marks them as published. This guarantees at-least-once delivery without distributed transactions.

### Dead Letter Queue

Failed Kafka messages are routed to topic-specific DLQs (`dlq.transfer`, `dlq.settlement`, `dlq.compliance`) after 3 retry attempts with exponential backoff (2^n seconds, max 30s). Consumers deduplicate via the `processed_events` table.

### Reconciliation Engine

Three reconciliation run types implement the replay-recompute-compare-alert cycle:

1. **Balance check** -- Verifies `SUM(debits) == SUM(credits)` globally and checks no non-system wallet has a negative balance
2. **Transfer verify** -- Verifies every settled transfer has at least 2 matching journal entries
3. **State rebuild** -- Reconstructs all wallet balances purely from `journal_entries`, proving the system state is fully derivable from the append-only ledger

All runs are recorded in the `reconciliation_runs` table with status, mismatch count, and details.

### Deterministic State Rebuild

Full system state can be reconstructed from the immutable ledger:

- **Balances**: Replay `journal_entries` to recompute every wallet balance as `SUM(credits) - SUM(debits)`
- **Status history**: All status history tables are append-only; replaying them reconstructs the full lifecycle of every transfer and settlement
- **Audit trail**: `request_id`, `actor_id`, and `actor_service` on every journal entry and status transition enables forensic reconstruction of who did what and when

### Concurrency Control

- **Advisory locks:** `pg_advisory_xact_lock` with deterministic hash of `wallet_id:currency` prevents double-spend
- **Skip-locked queuing:** `SELECT FOR UPDATE SKIP LOCKED` for multi-worker safety (settlement worker, outbox-publisher)
- **Transaction isolation:** `READ COMMITTED` enforced at the session level via `SET SESSION CHARACTERISTICS`

### Idempotency

Transfer creation accepts an `idempotency_key`. If a transfer with the same key already exists, the cached result is returned with `idempotent_hit: true` without re-executing. Kafka consumers deduplicate via the `processed_events` table using `ON CONFLICT DO NOTHING`. This prevents duplicate settlements, double-debit, and payment replay on network retries.

### Kafka Guarantees

- **Producer:** Idempotent (`enable_idempotence=True`), waits for all in-sync replicas (`acks=all`), LZ4 compression, flushes after every publish
- **Consumer:** Manual offset commits only after the handler succeeds. Exponential backoff retries (up to 3 attempts). Failed messages routed to DLQ
- **Outbox pattern:** Events are persisted atomically with business writes, then published asynchronously by the outbox-publisher. Consumers must handle duplicates via `processed_events`

---

## Project Structure

```
UNBANKED/
+-- docker-compose.yml          # 13 containers, 3 trust-boundary networks
+-- Makefile                    # 25 operational targets
+-- pyproject.toml              # Project metadata
+-- README.md
+-- shared/
|   +-- __init__.py
|   +-- database.py             # SQLAlchemy session factory, connection pooling
|   +-- journal.py              # Double-entry journal with balance derivation
|   +-- state_machine.py        # Settlement + transfer state transition validation
|   +-- blockchain_sim.py       # Simulated L2 blockchain + MPC signature aggregation
|   +-- kafka_client.py         # Idempotent producer, DLQ consumer
|   +-- outbox.py               # Transactional outbox insert
|   +-- rbac.py                 # Role-based access control, key hashing
|   +-- context.py              # Request context propagation (audit trail)
|   +-- events.py               # Canonical Kafka event schemas
|   +-- idempotency.py          # Transfer + settlement deduplication
|   +-- reconciliation.py       # Balance reconciliation, state rebuild
+-- services/
|   +-- api-gateway/            # RBAC auth, rate limiting, reverse proxy
|   +-- transfer-engine/        # Clients, wallets, agents, KYC, transfers
|   +-- settlement/             # Approval workflow, MPC signing, background worker
|   +-- fx-engine/              # FX rates and currency conversion (28 pairs)
|   +-- compliance/             # AML screening, Kafka consumer
|   +-- signing-gateway/        # MPC signing coordinator
|   +-- mpc-node/               # MPC signing node (3 instances)
|   +-- outbox-publisher/       # Transactional outbox -> Kafka relay
|   +-- reconciliation/         # Ledger integrity verification
+-- init/
|   +-- kafka/
|   |   +-- create_topics.sh    # 16 topics with retention policies
|   +-- postgres/
|       +-- 01_schema.sql       # Schema, enums, indexes, immutability rules, seed data
+-- scripts/
|   +-- demo.py                 # Full end-to-end demo (3 corridors)
+-- tests/
    +-- __init__.py
    +-- test_core.py            # 18 unit tests (no Docker required)
```

---

## Production Warning

**This project is explicitly NOT suitable for production use.** Cross-border remittance platforms are among the most regulated, operationally complex, and legally sensitive activities in financial services. The following critical components are absent or stubbed:

| Missing Component | Risk if Absent |
|-------------------|----------------|
| Money transmitter license / e-money authorization | Cannot legally process real remittances |
| Real mobile money API integration (M-Pesa, MTN MoMo, GCash) | No actual last-mile delivery to unbanked recipients |
| Real banking rails (SWIFT, FedWire, ACH, SEPA) | Cannot move real fiat funds |
| Real KYC/AML provider integration (Onfido, ComplyAdvantage, Chainalysis) | No actual identity verification or sanctions screening |
| HSM / MPC key management (Thales, Fireblocks) | No real cryptographic signing |
| TLS / mTLS for service-to-service communication | Plaintext internal traffic |
| API key rotation and expiry | Static API keys with no lifecycle management |
| Request signing (HMAC / JWT) | No request integrity verification |
| Per-account transaction limits and velocity controls | No per-participant abuse prevention |
| Comprehensive test suite with mutation testing | Untested edge cases in fund handling |
| Disaster recovery and failover procedures | No tested recovery for settlement outages |
| Regulatory reporting (FinCEN CTR/SAR, FCA, Central Bank reporting) | Post-trade reporting violations |
| Kafka cluster (replication factor > 1) | Single broker -- no fault tolerance |
| Database replication and failover | Single PostgreSQL instance -- no HA |
| Smart contract audit (if blockchain rail is real) | Exploitable vulnerabilities in settlement contracts |
| Cold storage / custody separation | All funds in hot operational accounts |
| Real FX rate feeds (Reuters, Bloomberg, Open Exchange Rates) | Static seed rates with no live updates |
| Agent float management and reconciliation | No real cash-in/cash-out float controls |

> Cross-border remittance platforms require: money transmitter licenses in each operating jurisdiction, central bank approvals, mobile money operator partnerships, banking relationships for fiat rails, AML/KYC compliance infrastructure, and legal agreements with all counterparties. **Do not use this code to process real remittances, manage real funds, or serve real clients.**

---

## License

This project is provided as-is for educational and reference purposes under the MIT License.

---

*Built with ❤️ by Pavon Dunbar -- Cross-border stablecoin infrastructure for the unbanked*
