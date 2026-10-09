-- Migration 17 (#361, feature 5): owner-signed schedules.
--
-- A schedule may start work on a calendar rule, but it may never approve
-- work. Each row is the owner's spec, signed with the operator key over
-- every field below up to the signature. A removed schedule keeps its row
-- with removed_at set, so its nonce cannot be used again. Only the firing
-- columns (last_fired_at, last_task_id, next_due_at) change after insert.

CREATE TABLE schedules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 64),
    rule            TEXT NOT NULL CHECK (length(rule) BETWEEN 1 AND 64),
    tz              TEXT NOT NULL CHECK (length(tz) BETWEEN 1 AND 64),
    agent_kind      TEXT NOT NULL CHECK (length(agent_kind) BETWEEN 1 AND 64),
    title           TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 500),
    payload         TEXT NOT NULL CHECK (json_valid(payload)),
    weight          TEXT NOT NULL CHECK (weight IN ('heavy', 'light')),
    capability_tier TEXT NOT NULL
                    CHECK (capability_tier IN ('autonomous', 'notify', 'approve')),
    created_by      TEXT NOT NULL CHECK (length(created_by) >= 1),
    created_at      TEXT NOT NULL,
    nonce           TEXT NOT NULL UNIQUE CHECK (length(nonce) = 32),
    signature       TEXT,
    removed_at      TEXT,
    removed_by      TEXT,
    last_fired_at   TEXT,
    last_task_id    TEXT,
    next_due_at     TEXT NOT NULL
);

-- One live schedule per name. A removed one frees its name.
CREATE UNIQUE INDEX idx_schedules_live_name ON schedules (name) WHERE removed_at IS NULL;

CREATE INDEX idx_schedules_due ON schedules (next_due_at) WHERE removed_at IS NULL;
