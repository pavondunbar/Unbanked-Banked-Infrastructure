-- ============================================================
-- UNBANKED: Cross-Border Stablecoin Infrastructure
-- PostgreSQL Schema — Append-Only Double-Entry Ledger
-- ============================================================

-- Enums
CREATE TYPE client_type    AS ENUM ('unbanked', 'banked');
CREATE TYPE kyc_tier       AS ENUM ('none', 'basic', 'enhanced', 'full');
CREATE TYPE corridor_type  AS ENUM ('unbanked_to_unbanked', 'banked_to_unbanked', 'unbanked_to_banked');
CREATE TYPE transfer_status AS ENUM ('pending', 'compliance_check', 'processing', 'settled', 'failed', 'refunded');
CREATE TYPE settlement_status AS ENUM ('pending', 'approved', 'signed', 'broadcasted', 'confirmed');
CREATE TYPE journal_direction AS ENUM ('debit', 'credit');
CREATE TYPE compliance_result AS ENUM ('pass', 'flag', 'block');

-- ── Clients ────────────────────────────────────────────────
CREATE TABLE clients (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name     TEXT        NOT NULL,
    client_type   client_type NOT NULL,
    phone         TEXT UNIQUE NOT NULL,
    country_code  TEXT        NOT NULL,
    kyc_tier      kyc_tier    NOT NULL DEFAULT 'none',
    bank_name     TEXT,
    bank_account  TEXT,
    bank_routing  TEXT,
    active        BOOLEAN     NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Wallets (one per client per currency) ──────────────────
CREATE TABLE wallets (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id   UUID NOT NULL REFERENCES clients(id),
    currency    TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(client_id, currency)
);

-- ── Agent Network (cash-in / cash-out locations) ───────────
CREATE TABLE agents (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT        NOT NULL,
    country_code    TEXT        NOT NULL,
    region          TEXT        NOT NULL,
    currencies      TEXT        NOT NULL,
    float_limit_usd NUMERIC(18,2) NOT NULL DEFAULT 50000.00,
    active          BOOLEAN     NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── IMMUTABLE DOUBLE-ENTRY LEDGER ──────────────────────────
-- Append-only. No UPDATE or DELETE permitted (enforced by rules below).
-- Balances are DERIVED via SUM(credits) - SUM(debits), never stored.
CREATE TABLE journal_entries (
    id              BIGSERIAL PRIMARY KEY,
    entry_ref       TEXT              NOT NULL,
    wallet_id       TEXT              NOT NULL,
    direction       journal_direction NOT NULL,
    amount          NUMERIC(18,2)     NOT NULL CHECK (amount > 0),
    currency        TEXT              NOT NULL,
    coa_code        TEXT              NOT NULL,
    description     TEXT,
    transfer_id     UUID,
    settlement_id   UUID,
    request_id      UUID,
    actor_id        TEXT,
    actor_service   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Immutability enforcement: silently reject UPDATE/DELETE
CREATE RULE journal_no_update AS ON UPDATE TO journal_entries DO INSTEAD NOTHING;
CREATE RULE journal_no_delete AS ON DELETE TO journal_entries DO INSTEAD NOTHING;

-- ── Transfers ──────────────────────────────────────────────
CREATE TABLE transfers (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    corridor          corridor_type   NOT NULL,
    sender_id         UUID            NOT NULL REFERENCES clients(id),
    receiver_id       UUID            NOT NULL REFERENCES clients(id),
    send_amount       NUMERIC(18,2)   NOT NULL,
    send_currency     TEXT            NOT NULL,
    receive_amount    NUMERIC(18,2)   NOT NULL,
    receive_currency  TEXT            NOT NULL,
    fx_rate           NUMERIC(18,8)   NOT NULL,
    fee_amount        NUMERIC(18,2)   NOT NULL DEFAULT 0,
    fee_currency      TEXT            NOT NULL,
    status            transfer_status NOT NULL DEFAULT 'pending',
    blockchain_tx_hash TEXT,
    settlement_ref    TEXT,
    agent_in_id       UUID REFERENCES agents(id),
    agent_out_id      UUID REFERENCES agents(id),
    idempotency_key   TEXT UNIQUE,
    request_id        UUID,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at        TIMESTAMPTZ
);

-- ── Settlement State Machine ───────────────────────────────
-- PENDING → APPROVED → SIGNED → BROADCASTED → CONFIRMED
-- Hybrid finality: every settlement produces BOTH a blockchain receipt
-- (token leg) AND a traditional rail receipt (fiat leg).
CREATE TABLE settlements (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transfer_id         UUID              NOT NULL REFERENCES transfers(id),
    status              settlement_status NOT NULL DEFAULT 'pending',
    priority            INTEGER           NOT NULL DEFAULT 2,
    approved_by         TEXT,
    approved_at         TIMESTAMPTZ,
    signed_by           TEXT,
    signed_at           TIMESTAMPTZ,
    mpc_signature       TEXT,
    mpc_partials        JSONB,
    -- Token leg (blockchain)
    blockchain_tx_hash  TEXT,
    block_number        BIGINT,
    block_hash          TEXT,
    network             TEXT DEFAULT 'stablecoin-l2',
    gas_used            INTEGER,
    -- Fiat leg (traditional rail)
    fiat_rail           TEXT,
    fiat_reference      TEXT,
    confirmed_at        TIMESTAMPTZ,
    request_id          UUID,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Append-Only Status History (audit trail) ───────────────
CREATE TABLE transfer_status_history (
    id              BIGSERIAL PRIMARY KEY,
    transfer_id     UUID NOT NULL REFERENCES transfers(id),
    old_status      TEXT,
    new_status      TEXT NOT NULL,
    reason          TEXT,
    request_id      UUID,
    actor_id        TEXT,
    actor_service   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE settlement_status_history (
    id              BIGSERIAL PRIMARY KEY,
    settlement_id   UUID NOT NULL REFERENCES settlements(id),
    old_status      TEXT,
    new_status      TEXT NOT NULL,
    reason          TEXT,
    request_id      UUID,
    actor_id        TEXT,
    actor_service   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Compliance Events ──────────────────────────────────────
CREATE TABLE compliance_events (
    id              BIGSERIAL PRIMARY KEY,
    transfer_id     UUID              NOT NULL REFERENCES transfers(id),
    check_type      TEXT              NOT NULL,
    result          compliance_result NOT NULL,
    risk_score      INTEGER           NOT NULL DEFAULT 0,
    details         TEXT,
    request_id      UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── FX Rates ───────────────────────────────────────────────
CREATE TABLE fx_rates (
    id              BIGSERIAL PRIMARY KEY,
    base_currency   TEXT          NOT NULL,
    quote_currency  TEXT          NOT NULL,
    mid_rate        NUMERIC(18,8) NOT NULL,
    spread_bps      NUMERIC(8,2)  NOT NULL DEFAULT 50.0,
    active          BOOLEAN       NOT NULL DEFAULT true,
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- ── Transactional Outbox ───────────────────────────────────
CREATE TABLE outbox_events (
    id              BIGSERIAL   PRIMARY KEY,
    event_id        UUID        NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    topic           TEXT        NOT NULL,
    key             TEXT,
    payload         JSONB       NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at    TIMESTAMPTZ
);

-- ── Idempotency / Deduplication ────────────────────────────
CREATE TABLE processed_events (
    event_id        UUID PRIMARY KEY,
    consumer_group  TEXT        NOT NULL,
    processed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- General-purpose idempotency store for write endpoints.
-- Maps an idempotency_key to the cached JSON response so replays
-- return the original result without re-executing side effects.
CREATE TABLE idempotency_store (
    idempotency_key TEXT PRIMARY KEY,
    endpoint        TEXT        NOT NULL,
    response        JSONB       NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── RBAC API Keys ──────────────────────────────────────────
CREATE TABLE api_keys (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key_hash    TEXT    UNIQUE NOT NULL,
    role        TEXT    NOT NULL CHECK (role IN (
                    'admin', 'operator', 'signer', 'auditor', 'system'
                )),
    description TEXT,
    active      BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Reconciliation Runs ────────────────────────────────────
CREATE TABLE reconciliation_runs (
    id              BIGSERIAL PRIMARY KEY,
    run_type        TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'running',
    total_wallets   INTEGER,
    mismatches      INTEGER DEFAULT 0,
    details         JSONB,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);

-- ── Indexes ────────────────────────────────────────────────
CREATE INDEX idx_journal_wallet_currency ON journal_entries(wallet_id, currency);
CREATE INDEX idx_journal_entry_ref       ON journal_entries(entry_ref);
CREATE INDEX idx_journal_created         ON journal_entries(created_at);
CREATE INDEX idx_journal_transfer        ON journal_entries(transfer_id);
CREATE INDEX idx_journal_settlement      ON journal_entries(settlement_id);
CREATE INDEX idx_transfers_status        ON transfers(status, created_at);
CREATE INDEX idx_transfers_sender        ON transfers(sender_id);
CREATE INDEX idx_transfers_receiver      ON transfers(receiver_id);
CREATE INDEX idx_transfers_idempotency   ON transfers(idempotency_key);
CREATE INDEX idx_settlements_status      ON settlements(status, priority, created_at);
CREATE INDEX idx_settlements_transfer    ON settlements(transfer_id);
CREATE INDEX idx_outbox_unpublished      ON outbox_events(created_at) WHERE published_at IS NULL;
CREATE INDEX idx_fx_rates_active         ON fx_rates(base_currency, quote_currency) WHERE active = true;
CREATE INDEX idx_compliance_transfer     ON compliance_events(transfer_id);
CREATE INDEX idx_status_hist_transfer    ON transfer_status_history(transfer_id);
CREATE INDEX idx_status_hist_settlement  ON settlement_status_history(settlement_id);

-- ============================================================
-- SEED DATA
-- ============================================================

-- System accounts (platform-owned)
INSERT INTO clients (id, full_name, client_type, phone, country_code, kyc_tier)
VALUES
    ('00000000-0000-0000-0000-000000000001', 'Platform Omnibus Reserve', 'banked',
     '+0-000-000-0001', 'XX', 'full'),
    ('00000000-0000-0000-0000-000000000002', 'FX Liquidity Pool', 'banked',
     '+0-000-000-0002', 'XX', 'full'),
    ('00000000-0000-0000-0000-000000000003', 'Fee Collection', 'banked',
     '+0-000-000-0003', 'XX', 'full');

INSERT INTO wallets (id, client_id, currency) VALUES
    ('00000000-0000-0000-0000-000000000010', '00000000-0000-0000-0000-000000000001', 'MULTI'),
    ('00000000-0000-0000-0000-000000000011', '00000000-0000-0000-0000-000000000002', 'MULTI'),
    ('00000000-0000-0000-0000-000000000012', '00000000-0000-0000-0000-000000000003', 'MULTI');

-- RBAC keys (SHA-256 of plaintext)
INSERT INTO api_keys (key_hash, role, description) VALUES
    (encode(sha256('admin-key-001'::bytea), 'hex'),    'admin',    'Demo admin key'),
    (encode(sha256('operator-key-001'::bytea), 'hex'), 'operator', 'Demo operator key'),
    (encode(sha256('signer-key-001'::bytea), 'hex'),   'signer',   'Demo signer key'),
    (encode(sha256('auditor-key-001'::bytea), 'hex'),  'auditor',  'Demo auditor key'),
    (encode(sha256('system-key-001'::bytea), 'hex'),   'system',   'Internal system key');

-- FX rates (mid-market vs USD)
INSERT INTO fx_rates (base_currency, quote_currency, mid_rate, spread_bps) VALUES
    ('USD', 'NGN', 1550.0,    50), ('NGN', 'USD', 0.000645,  50),
    ('USD', 'KES', 153.0,     50), ('KES', 'USD', 0.006536,  50),
    ('USD', 'GHS', 15.4,      50), ('GHS', 'USD', 0.064935,  50),
    ('USD', 'PHP', 56.2,      50), ('PHP', 'USD', 0.017794,  50),
    ('USD', 'INR', 83.5,      50), ('INR', 'USD', 0.011976,  50),
    ('USD', 'MXN', 17.1,      50), ('MXN', 'USD', 0.058480,  50),
    ('USD', 'BRL', 4.97,      50), ('BRL', 'USD', 0.201207,  50),
    ('USD', 'EUR', 0.92,      50), ('EUR', 'USD', 1.086957,  50),
    ('USD', 'GBP', 0.79,      50), ('GBP', 'USD', 1.265823,  50),
    ('NGN', 'KES', 0.098710,  50), ('KES', 'NGN', 10.13072,  50),
    ('GHS', 'GBP', 0.051299,  50), ('GBP', 'GHS', 19.49367,  50),
    ('GHS', 'NGN', 100.6494,  50), ('NGN', 'GHS', 0.009935,  50),
    ('PHP', 'KES', 2.722420,  50), ('KES', 'PHP', 0.367330,  50),
    ('EUR', 'GBP', 0.858696,  50), ('GBP', 'EUR', 1.164557,  50);
