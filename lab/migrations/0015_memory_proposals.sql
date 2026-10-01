-- Migration 15 (#253): the agent proposes a memory, the owner decides.
--
-- Numbered 15, not 14: migration 14 (chat) is in an open change and lands
-- first.
--
-- A proposal is a separate table, not a state of `memories`, so the
-- full-text index and every query over `memories` cannot see one. It has
-- no expiry, so it never becomes curated memory by waiting. It leaves
-- 'pending' only through an owner decision: 'accepted' carries the
-- operator signature that was verified, and the curated memory it became;
-- 'rejected' carries who and why.
--
-- `tainted` is copied from the proposing task's row when the proposal is
-- made, so the owner sees it before deciding.

CREATE TABLE memory_proposals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id          TEXT NOT NULL,
    text             TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 4000),
    text_sha256      TEXT NOT NULL CHECK (length(text_sha256) = 64),
    source_id        TEXT NOT NULL CHECK (length(source_id) BETWEEN 1 AND 500),
    source_sha256    TEXT CHECK (source_sha256 IS NULL OR length(source_sha256) = 64),
    reason           TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
    tainted          INTEGER NOT NULL CHECK (tainted IN (0, 1)),
    state            TEXT NOT NULL DEFAULT 'pending'
                     CHECK (state IN ('pending', 'accepted', 'rejected')),
    proposed_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
    decided_at       TEXT,
    decided_by       TEXT,
    decision_reason  TEXT,
    signature        TEXT,
    memory_id        INTEGER REFERENCES memories(id),
    CHECK (state != 'accepted' OR (signature IS NOT NULL AND decided_by IS NOT NULL)),
    CHECK (state != 'rejected' OR (decided_by IS NOT NULL AND decision_reason IS NOT NULL))
);
CREATE UNIQUE INDEX idx_memory_proposals_task_text ON memory_proposals (task_id, text_sha256);
CREATE INDEX idx_memory_proposals_state ON memory_proposals (state, id);

-- A curated memory that came from a proposal points back at it.
ALTER TABLE memories ADD COLUMN proposal_id INTEGER REFERENCES memory_proposals(id);
