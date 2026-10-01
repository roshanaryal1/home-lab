-- Migration 14 (#239): chat through the broker.
--
-- 1. A task can come from the paired chat. `origin_type` gains 'chat'.
--    SQLite cannot widen a CHECK in place, so `tasks` is rebuilt the way
--    migration 2 did it: new table, copy every row, drop, rename, indexes.
--    A chat task is tainted like any input that is not the operator's own
--    at the terminal (lab.origin): the text is data, never a grant.
-- 2. `chat_state` holds the next Telegram update id to ask for. It is
--    advanced in the same transaction that records the update, so a
--    message is handled once even when the poller dies halfway.
-- 3. `chat_updates` is one row per update handled: who sent it, what was
--    done, the task it created, and the last task state the chat was told
--    about. It also feeds the per-chat rate limit.

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
    available_at  TEXT NOT NULL DEFAULT (datetime('now')),
    origin_type   TEXT NOT NULL DEFAULT 'unknown'
                  CHECK (origin_type IN ('operator', 'event', 'web', 'document',
                                         'task', 'memory', 'model', 'chat',
                                         'unknown')),
    origin_id     TEXT,
    origin_sha256 TEXT
                  CHECK (origin_sha256 IS NULL OR length(origin_sha256) = 64),
    acquired_at   TEXT,
    sensitivity   TEXT NOT NULL DEFAULT 'internal'
                  CHECK (sensitivity IN ('public', 'internal', 'secret')),
    delegated_by  TEXT,
    tainted       INTEGER NOT NULL DEFAULT 1 CHECK (tainted IN (0, 1))
);

INSERT INTO tasks_new
    (id, parent_id, title, payload, state, priority, agent_kind, weight,
     capability_tier, idempotent, attempts, executions, max_attempts,
     last_error, result, created_at, updated_at, available_at,
     origin_type, origin_id, origin_sha256, acquired_at, sensitivity,
     delegated_by, tainted)
SELECT id, parent_id, title, payload, state, priority, agent_kind, weight,
       capability_tier, idempotent, attempts, executions, max_attempts,
       last_error, result, created_at, updated_at, available_at,
       origin_type, origin_id, origin_sha256, acquired_at, sensitivity,
       delegated_by, tainted
FROM tasks;

DROP TABLE tasks;
ALTER TABLE tasks_new RENAME TO tasks;

CREATE INDEX idx_tasks_runnable
    ON tasks (state, weight, available_at, priority, created_at);

CREATE INDEX idx_tasks_parent ON tasks (parent_id);

CREATE INDEX idx_tasks_origin ON tasks (origin_type, origin_id);

CREATE TABLE chat_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    next_update_id  INTEGER NOT NULL DEFAULT 0 CHECK (next_update_id >= 0)
);

INSERT INTO chat_state (id) VALUES (1);

CREATE TABLE chat_updates (
    update_id       INTEGER PRIMARY KEY CHECK (update_id >= 0),
    chat_id         INTEGER NOT NULL,
    action          TEXT NOT NULL,
    task_id         TEXT REFERENCES tasks(id),
    received_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
    notified_state  TEXT
);

CREATE INDEX idx_chat_updates_chat ON chat_updates (chat_id, received_at);

CREATE INDEX idx_chat_updates_task ON chat_updates (task_id);
