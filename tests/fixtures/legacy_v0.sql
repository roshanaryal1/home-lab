-- Fixture: the schema as it was before versioned migrations, before the
-- leases.generation, approvals.intent and tasks.executions columns, and
-- with user_version 0. Filled with rows in every table so an upgrade can
-- be checked for lost data. Built from migrations/0001_baseline.sql.
-- Do not "fix" this file: it stands for a database already in the field.

-- Autonomous lab: task queue and audit schema.
--
-- Implements step 1 of the build order in the companion study's
-- reference architecture (analysis/consensus/reference-architecture.md
-- in roshanaryal1/llm-architects), sections 9 (queue and recovery),
-- 4 (agent model) and 10 (security and sandboxing).
--
-- Design constraints taken from that spec, not invented here:
--   * SQLite, WAL-backed. Redis is a future scaling option, not a
--     day-one dependency.
--   * Task states: queued, leased, running, succeeded, failed,
--     interrupted, cancelled.
--   * Leases expire. On supervisor restart, stale running tasks become
--     interrupted and are requeued according to retry policy.
--   * Destructive actions must never be replayed blindly after
--     recovery, hence the explicit idempotent flag.
--   * Capability tiers: autonomous, notify, approve, never.

-- Migration 1 of the versioned schema (item 3.1, #64). Never edit a
-- migration that has shipped: add a new numbered file instead. PRAGMAs
-- are per connection and cannot run inside a migration's transaction, so
-- TaskQueue sets them (journal_mode, durability) around the runner.

-- ---------------------------------------------------------------- agents

CREATE TABLE IF NOT EXISTS agents (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL,          -- coder | researcher | reviewer | ...
    capability_tier TEXT NOT NULL
                    CHECK (capability_tier IN
                           ('autonomous', 'notify', 'approve', 'never')),
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT
);

-- ----------------------------------------------------------------- tasks

CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    parent_id     TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    title         TEXT NOT NULL,
    payload       TEXT NOT NULL DEFAULT '{}',   -- JSON
    state         TEXT NOT NULL DEFAULT 'queued'
                  CHECK (state IN ('queued', 'leased', 'running',
                                   'awaiting_approval',
                                   'succeeded', 'failed',
                                   'interrupted', 'cancelled')),
    priority      INTEGER NOT NULL DEFAULT 100, -- lower runs first
    agent_kind    TEXT,                         -- routing hint, nullable
    -- Weight decides which worker pool may lease this task. It is a column
    -- rather than a payload key because the dispatch loop has to filter on
    -- it: a worker must be able to ask for work it has capacity to run,
    -- instead of leasing blindly and discovering the weight afterwards.
    weight        TEXT NOT NULL DEFAULT 'light'
                  CHECK (weight IN ('heavy', 'light')),
    -- What authority this task needs. Declared on the task for now; once
    -- the execution broker exists (#10) the tier will be derived from the
    -- tools actually called, and a task will never be able to under-declare.
    capability_tier TEXT NOT NULL DEFAULT 'autonomous'
                  CHECK (capability_tier IN
                         ('autonomous', 'notify', 'approve', 'never')),
    -- Only idempotent tasks may be auto-requeued after an interrupted
    -- run. Anything with side effects must be re-approved by a human.
    idempotent    INTEGER NOT NULL DEFAULT 0 CHECK (idempotent IN (0, 1)),
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    last_error    TEXT,
    result        TEXT,                         -- JSON
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    available_at  TEXT NOT NULL DEFAULT (datetime('now'))  -- retry backoff
);

-- The hot path is "find the next runnable task", so index exactly that.
CREATE INDEX IF NOT EXISTS idx_tasks_runnable
    ON tasks (state, weight, available_at, priority, created_at);

CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks (parent_id);

-- ---------------------------------------------------------------- leases
--
-- A lease is separate from the task row so that an expired lease is a
-- fact we can observe and audit, rather than a field we silently
-- overwrite. One live lease per task is enforced by the unique index.

CREATE TABLE IF NOT EXISTS leases (
    id         TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    -- `owner` is a stable identity (e.g. supervisor@hostname), the same
    -- across a process restart on purpose: recover() uses it to let a
    -- restarted supervisor reclaim its own crashed work by name. It is
    -- NOT a fencing token, and two live TaskQueue instances sharing an
    -- owner name are indistinguishable by this column alone. `holder`
    -- is the fencing token: unique per TaskQueue construction, so a
    -- fresh instance that never itself leased a task, or a stale
    -- instance whose lease was reclaimed out from under it, cannot
    -- pass as the one that actually holds this lease. Issue #47.
    owner      TEXT NOT NULL,
    holder     TEXT NOT NULL,
    acquired_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    released_at TEXT                    -- NULL while live
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_leases_one_live_per_task
    ON leases (task_id) WHERE released_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_leases_expiry
    ON leases (expires_at) WHERE released_at IS NULL;

-- ------------------------------------------------------------- approvals
--
-- Gate for the "approve" capability tier: network-sensitive,
-- publication, account, deletion or infrastructure changes.

CREATE TABLE IF NOT EXISTS approvals (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    reason      TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'granted', 'denied', 'expired')),
    -- An approval authorises ONE action, not a capability. The hash binds
    -- it to the exact normalised parameters that were shown to the human,
    -- so approving "email alice about X" cannot be reused to email bob.
    action_hash TEXT NOT NULL,
    -- Single use. Set the moment the approval is spent, so a replay of the
    -- same token finds it already consumed.
    consumed_at TEXT,
    expires_at  TEXT NOT NULL,
    requested_at TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at  TEXT,
    decided_by  TEXT
);

CREATE INDEX IF NOT EXISTS idx_approvals_lookup
    ON approvals (task_id, action_hash, state);

CREATE INDEX IF NOT EXISTS idx_approvals_pending
    ON approvals (state, requested_at);

-- ---------------------------------------------------------------- events
--
-- Append-only audit log. Every state transition writes one row. This is
-- the spec's "audit events" responsibility and the thing that makes a
-- crash postmortem possible.

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,     -- created | leased | started | ...
    from_state TEXT,
    to_state   TEXT,
    detail     TEXT,              -- JSON
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_task ON events (task_id, id);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events (kind, id);

-- ------------------------------------------------------------ operations
--
-- Journal of operations that are not safe to repeat (item 1.7, #56). The
-- row is committed as 'executing' before the operation starts, so a
-- crash leaves evidence that it may have happened. See lab/journal.py.

CREATE TABLE IF NOT EXISTS operations (
    id            TEXT PRIMARY KEY,   -- sha256(task, tool, params, seq)
    task_id       TEXT NOT NULL REFERENCES tasks(id),
    tool          TEXT NOT NULL,
    params_sha256 TEXT NOT NULL,
    seq           INTEGER NOT NULL,   -- nth identical call within a run
    state         TEXT NOT NULL
                  CHECK (state IN ('executing', 'confirmed', 'failed', 'uncertain')),
    result        TEXT,               -- JSON, for confirmed
    error         TEXT,
    resolved_by   TEXT,               -- set when a person reconciled it
    started_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
    finished_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_operations_unresolved
    ON operations (state, task_id) WHERE state IN ('executing', 'uncertain');

-- ------------------------------------------------------------------ data

INSERT INTO agents (id, name, kind, capability_tier, notes)
VALUES ('a1', 'reviewer-1', 'reviewer', 'notify', 'kept');

INSERT INTO tasks (id, parent_id, title, payload, state, priority, attempts,
                   max_attempts, idempotent, result, last_error)
VALUES
  ('t-queued', NULL, 'still queued', '{"n": 1}', 'queued', 100, 0, 3, 1, NULL, NULL),
  ('t-running', NULL, 'was running', '{"n": 2}', 'running', 50, 1, 3, 1, NULL, NULL),
  ('t-child', 't-queued', 'a child', '{}', 'queued', 100, 0, 3, 0, NULL, NULL),
  ('t-approval', NULL, 'waiting on a human', '{"n": 3}', 'awaiting_approval', 100, 1, 3, 0, NULL, NULL),
  ('t-done', NULL, 'finished', '{"n": 4}', 'succeeded', 100, 1, 3, 0, '{"ok": true}', NULL),
  ('t-failed', NULL, 'gave up', '{}', 'failed', 100, 3, 3, 0, NULL, 'boom');

INSERT INTO leases (id, task_id, owner, holder, expires_at, released_at)
VALUES
  ('l-live', 't-running', 'supervisor-old', 'holder-old', '2999-01-01 00:00:00', NULL),
  ('l-done', 't-done', 'supervisor-old', 'holder-old', '2026-01-01 00:00:00', '2026-01-01 00:00:05');

INSERT INTO approvals (id, task_id, reason, state, action_hash, expires_at)
VALUES ('ap-1', 't-approval', 'send an email', 'pending', 'abc123', '2999-01-01 00:00:00');

INSERT INTO events (task_id, kind, from_state, to_state, detail)
VALUES
  ('t-queued', 'created', NULL, 'queued', '{"title": "still queued"}'),
  ('t-running', 'created', NULL, 'queued', NULL),
  ('t-running', 'leased', 'queued', 'leased', NULL),
  ('t-running', 'started', 'leased', 'running', NULL);

INSERT INTO operations (id, task_id, tool, params_sha256, seq, state)
VALUES ('op-1', 't-running', 'send_email', 'deadbeef', 0, 'executing');
