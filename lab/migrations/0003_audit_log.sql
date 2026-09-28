-- Migration 3 (item 3.2, #65): an audit log that cannot be quietly rewritten.
--
-- Three changes:
--
-- 1. `events` no longer references `tasks`, and `leases` and `approvals`
--    no longer cascade. Deleting a task used to delete its history with
--    it. Now a task with history cannot be deleted at all (the foreign
--    keys from leases and approvals refuse), and events, which have no
--    key to fail on, outlive any task row.
-- 2. `events` gains `prev_hash` and `hash`, a SHA-256 chain written by
--    lab.audit.append_event. Rows from before this migration keep NULL
--    in both: they predate the chain and are not covered by it.
-- 3. UPDATE and DELETE on `events` are refused by trigger.
--
-- Triggers stop a careless or buggy writer, not someone who can drop the
-- trigger. The chain plus a signed checkpoint kept outside the lab's
-- reach is what catches a rewrite (lab.audit, `lab audit`).

CREATE TABLE events_new (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT,              -- deliberately not a foreign key
    kind       TEXT NOT NULL,
    from_state TEXT,
    to_state   TEXT,
    detail     TEXT,              -- JSON
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    prev_hash  TEXT,              -- NULL only for rows that predate the chain
    hash       TEXT
);

INSERT INTO events_new (id, task_id, kind, from_state, to_state, detail, created_at)
SELECT id, task_id, kind, from_state, to_state, detail, created_at FROM events;

DROP TABLE events;
ALTER TABLE events_new RENAME TO events;

CREATE INDEX idx_events_task ON events (task_id, id);
CREATE INDEX idx_events_kind ON events (kind, id);

CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only');
END;

CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only');
END;

CREATE TABLE leases_new (
    id         TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL REFERENCES tasks(id),
    owner      TEXT NOT NULL,
    holder     TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0,
    acquired_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    released_at TEXT
);

INSERT INTO leases_new
    (id, task_id, owner, holder, generation, acquired_at, expires_at, released_at)
SELECT id, task_id, owner, holder, generation, acquired_at, expires_at, released_at
FROM leases;

DROP TABLE leases;
ALTER TABLE leases_new RENAME TO leases;

CREATE UNIQUE INDEX idx_leases_one_live_per_task
    ON leases (task_id) WHERE released_at IS NULL;
CREATE INDEX idx_leases_expiry
    ON leases (expires_at) WHERE released_at IS NULL;

CREATE TABLE approvals_new (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    reason      TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'granted', 'denied', 'expired')),
    action_hash TEXT NOT NULL,
    intent      TEXT,
    consumed_at TEXT,
    expires_at  TEXT NOT NULL,
    requested_at TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at  TEXT,
    decided_by  TEXT
);

INSERT INTO approvals_new
    (id, task_id, reason, state, action_hash, intent, consumed_at, expires_at,
     requested_at, decided_at, decided_by)
SELECT id, task_id, reason, state, action_hash, intent, consumed_at, expires_at,
       requested_at, decided_at, decided_by
FROM approvals;

DROP TABLE approvals;
ALTER TABLE approvals_new RENAME TO approvals;

CREATE INDEX idx_approvals_lookup ON approvals (task_id, action_hash, state);
CREATE INDEX idx_approvals_pending ON approvals (state, requested_at);
