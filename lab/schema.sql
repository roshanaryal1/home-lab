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

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
-- Durability pragmas (synchronous, fullfsync) are per connection, so
-- they are set in TaskQueue._apply_durability, not here.

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
    -- 1, 2, 3... per task, one per claim. With the lease id it makes up
    -- the LeaseToken every worker call must present (item 1.3, #55).
    generation INTEGER NOT NULL DEFAULT 0,
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
