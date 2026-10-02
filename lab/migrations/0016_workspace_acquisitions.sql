-- Migration 16 (ADR 0008): where every repository in a workspace came from.
--
-- A repository enters a task's workspace only through workspace.acquire, from
-- an operator-signed source, at an exact revision. One row per acquisition
-- answers: which repository (the source entry, its path, the digest of the
-- signed entry and who signed it), which revision (the commit and its tree),
-- in which workspace and directory, under which task. Rows are inserted by the
-- broker and never updated.

CREATE TABLE workspace_acquisitions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id        TEXT NOT NULL,
    source         TEXT NOT NULL CHECK (length(source) BETWEEN 1 AND 64),
    source_path    TEXT NOT NULL CHECK (length(source_path) >= 1),
    source_sha256  TEXT NOT NULL CHECK (length(source_sha256) = 64),
    signed_by      TEXT NOT NULL CHECK (length(signed_by) >= 1),
    revision       TEXT NOT NULL CHECK (length(revision) = 40),
    tree           TEXT NOT NULL CHECK (length(tree) = 40),
    directory      TEXT NOT NULL CHECK (length(directory) BETWEEN 1 AND 64),
    workspace      TEXT NOT NULL CHECK (length(workspace) >= 1),
    acquired_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now'))
);
CREATE INDEX idx_workspace_acquisitions_task ON workspace_acquisitions (task_id, id);
