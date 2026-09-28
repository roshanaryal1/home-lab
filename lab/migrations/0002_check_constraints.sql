-- Migration 2 (item 3.1, #64): CHECK constraints on the task budgets and
-- on the size and shape of what a task carries.
--
-- SQLite cannot add a CHECK to an existing table, so `tasks` is rebuilt
-- the documented way: create the new table, copy every row, drop the old
-- one, rename, recreate the indexes. The runner has foreign keys off for
-- the duration and checks them before it commits. If an old row breaks
-- one of the new rules the copy fails and the whole migration rolls back,
-- leaving the database exactly as it was.
--
-- Limits: payload 64 KiB, result 1 MiB, measured in bytes not characters.
-- lab.queue enforces the same numbers first so callers get a clear error
-- (PayloadTooLarge) instead of a constraint failure.

CREATE TABLE tasks_new (
    id            TEXT PRIMARY KEY,
    parent_id     TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    title         TEXT NOT NULL,
    payload       TEXT NOT NULL DEFAULT '{}'
                  CHECK (json_valid(payload)
                         AND length(CAST(payload AS BLOB)) <= 65536),
    state         TEXT NOT NULL DEFAULT 'queued'
                  CHECK (state IN ('queued', 'leased', 'running',
                                   'awaiting_approval',
                                   'succeeded', 'failed',
                                   'interrupted', 'cancelled')),
    priority      INTEGER NOT NULL DEFAULT 100,
    agent_kind    TEXT,
    weight        TEXT NOT NULL DEFAULT 'light'
                  CHECK (weight IN ('heavy', 'light')),
    capability_tier TEXT NOT NULL DEFAULT 'autonomous'
                  CHECK (capability_tier IN
                         ('autonomous', 'notify', 'approve', 'never')),
    idempotent    INTEGER NOT NULL DEFAULT 0 CHECK (idempotent IN (0, 1)),
    attempts      INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    executions    INTEGER NOT NULL DEFAULT 0
                  CHECK (executions >= 0 AND executions <= attempts),
    max_attempts  INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
    last_error    TEXT,
    result        TEXT
                  CHECK (result IS NULL
                         OR (json_valid(result)
                             AND length(CAST(result AS BLOB)) <= 1048576)),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    available_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO tasks_new
    (id, parent_id, title, payload, state, priority, agent_kind, weight,
     capability_tier, idempotent, attempts, executions, max_attempts,
     last_error, result, created_at, updated_at, available_at)
SELECT id, parent_id, title, payload, state, priority, agent_kind, weight,
       capability_tier, idempotent, attempts, executions, max_attempts,
       last_error, result, created_at, updated_at, available_at
FROM tasks;

DROP TABLE tasks;
ALTER TABLE tasks_new RENAME TO tasks;

CREATE INDEX idx_tasks_runnable
    ON tasks (state, weight, available_at, priority, created_at);

CREATE INDEX idx_tasks_parent ON tasks (parent_id);
