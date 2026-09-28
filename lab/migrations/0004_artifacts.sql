-- Migration 4 (item 3.3, #66): content-addressed artifacts.
--
-- One row per file a task attempt left in its workspace. The bytes live in
-- the artifact store under their SHA-256 (lab.artifacts); this table is
-- the descriptor: what the file was called, who made it, how big, and
-- what it was derived from. The task reference is deliberately not a
-- foreign key: a receipt must outlive the task row (see migration 3).

CREATE TABLE artifacts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    attempt      INTEGER NOT NULL CHECK (attempt >= 0),
    path         TEXT NOT NULL,          -- workspace-relative, POSIX separators
    sha256       TEXT NOT NULL CHECK (length(sha256) = 64),
    size         INTEGER NOT NULL CHECK (size >= 0),
    media_type   TEXT NOT NULL,
    tool_version TEXT NOT NULL,          -- the lab build that produced the record
    lineage      TEXT NOT NULL DEFAULT '[]'
                 CHECK (json_valid(lineage)),   -- JSON list of input sha256s
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
    UNIQUE (task_id, attempt, path)
);

CREATE INDEX idx_artifacts_task ON artifacts (task_id, attempt);
CREATE INDEX idx_artifacts_sha ON artifacts (sha256);
