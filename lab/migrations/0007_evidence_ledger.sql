-- Migration 7 (item #90): the evidence ledger.
--
-- A research task records what it asked and how it was run. Each
-- conclusion is a claim with its own status, separate from the task's:
-- a task that finished says nothing about whether what it concluded is
-- true. Claims link to snapshots of the sources behind them; the bytes of
-- a snapshot live in the content-addressed artifact store (migration 4).
-- Task references are deliberately not foreign keys, as in migrations 3
-- and 4: the record outlives the task row.

CREATE TABLE research_tasks (
    task_id          TEXT PRIMARY KEY,
    question         TEXT NOT NULL CHECK (length(question) > 0),
    protocol_version TEXT NOT NULL CHECK (length(protocol_version) > 0),
    data_ids         TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(data_ids)),
    code_ids         TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(code_ids)),
    outputs          TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(outputs)),
    validation_checks TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(validation_checks)),
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now'))
);

CREATE TABLE evidence_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    source_id   TEXT NOT NULL,             -- a URL, a file path, a record id
    source_type TEXT NOT NULL,
    sha256      TEXT NOT NULL CHECK (length(sha256) = 64),
    size        INTEGER NOT NULL CHECK (size >= 0),
    acquired_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now'))
);
CREATE INDEX idx_snapshots_task ON evidence_snapshots (task_id);

CREATE TABLE claims (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    text       TEXT NOT NULL CHECK (length(text) > 0),
    status     TEXT NOT NULL DEFAULT 'unverified'
               CHECK (status IN ('unverified', 'supported', 'contradicted', 'verified')),
    status_by  TEXT,                       -- who or what set a verified status
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now'))
);
CREATE INDEX idx_claims_task ON claims (task_id);

CREATE TABLE claim_evidence (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id    INTEGER NOT NULL REFERENCES claims(id),
    snapshot_id INTEGER NOT NULL REFERENCES evidence_snapshots(id),
    relation    TEXT NOT NULL CHECK (relation IN ('supports', 'contradicts')),
    quote       TEXT NOT NULL CHECK (length(quote) > 0),   -- must appear in the snapshot
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
    UNIQUE (claim_id, snapshot_id, relation, quote)
);

-- A draft is reviewable only after a contradiction pass and a missing-
-- evidence list, both recorded here and both newer than the last change.
CREATE TABLE review_passes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id        TEXT NOT NULL,
    ran_by         TEXT NOT NULL,
    ran_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
    last_change_id INTEGER NOT NULL,       -- highest claim_evidence id seen
    claims_seen    INTEGER NOT NULL,
    missing_evidence TEXT NOT NULL CHECK (json_valid(missing_evidence)),
    contradictions   TEXT NOT NULL CHECK (json_valid(contradictions))
);
CREATE INDEX idx_review_task ON review_passes (task_id, id);
