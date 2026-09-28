-- Migration 5 (item 4.2, #69): where a task's input came from.
--
-- Rows that predate this migration have no known origin, so they are
-- marked unknown and tainted: fail closed. `tainted` is one-way through
-- lineage: a child of a tainted task is tainted (lab.origin.resolve).

ALTER TABLE tasks ADD COLUMN origin_type TEXT NOT NULL DEFAULT 'unknown'
    CHECK (origin_type IN ('operator', 'event', 'web', 'document', 'task',
                           'memory', 'model', 'unknown'));
ALTER TABLE tasks ADD COLUMN origin_id TEXT;
ALTER TABLE tasks ADD COLUMN origin_sha256 TEXT
    CHECK (origin_sha256 IS NULL OR length(origin_sha256) = 64);
ALTER TABLE tasks ADD COLUMN acquired_at TEXT;
ALTER TABLE tasks ADD COLUMN sensitivity TEXT NOT NULL DEFAULT 'internal'
    CHECK (sensitivity IN ('public', 'internal', 'secret'));
ALTER TABLE tasks ADD COLUMN delegated_by TEXT;
ALTER TABLE tasks ADD COLUMN tainted INTEGER NOT NULL DEFAULT 1
    CHECK (tainted IN (0, 1));
