-- Migration 10 (item 8.6, #86): durable receipts for credentialed sends.
--
-- The row is written as 'reserved' BEFORE anything is sent (write-ahead), so
-- a crash or a lost response leaves evidence of exactly what was attempted:
-- destination, path, the hash of the body, and the idempotency key the
-- provider also saw. It becomes 'confirmed' with the provider's own id when
-- the response arrives, or when a reconciliation finds it on the provider's
-- side. The body itself is not kept here; only its hash.

CREATE TABLE publications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL,
    connector       TEXT NOT NULL,
    host            TEXT NOT NULL,
    method          TEXT NOT NULL,
    path            TEXT NOT NULL,
    body_sha256     TEXT NOT NULL CHECK (length(body_sha256) = 64),
    params_sha256   TEXT NOT NULL CHECK (length(params_sha256) = 64),
    idempotency_key TEXT NOT NULL UNIQUE,
    state           TEXT NOT NULL DEFAULT 'reserved'
                    CHECK (state IN ('reserved', 'confirmed', 'not_found')),
    status_code     INTEGER,
    provider_id     TEXT,
    response_sha256 TEXT,
    confirmed_via   TEXT CHECK (confirmed_via IN ('response', 'reconciliation')),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now'))
);
CREATE INDEX idx_publications_task ON publications (task_id);
CREATE INDEX idx_publications_open ON publications (state) WHERE state = 'reserved';
