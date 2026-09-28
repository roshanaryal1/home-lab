-- Migration 11 (item 8.7, #87): skills as versioned artifacts with lineage.
--
-- A skill can widen what an agent does, so it is never edited in place.
-- Each submission is a new immutable version whose files live in the
-- content-addressed artifact store. A version starts as a candidate; only
-- an operator-signed promotion makes it active; a known-good mark and a
-- one-step rollback give a safe place to return to.

CREATE TABLE skill_versions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    version        INTEGER NOT NULL,
    state          TEXT NOT NULL DEFAULT 'candidate'
                   CHECK (state IN ('candidate', 'active', 'superseded', 'rolled_back',
                                    'rejected')),
    tier           TEXT NOT NULL CHECK (tier IN ('autonomous', 'notify', 'approve', 'never')),
    declared_tier  TEXT,                        -- what SKILL.md claimed, kept for the record
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    manifest       TEXT NOT NULL CHECK (json_valid(manifest)),   -- path -> [sha256, mode]
    has_scripts    INTEGER NOT NULL DEFAULT 0 CHECK (has_scripts IN (0, 1)),
    parent_id      INTEGER REFERENCES skill_versions(id),        -- the active one it derives from
    derived_from   TEXT,                        -- a task id, an eval record hash, a note
    submitted_by   TEXT NOT NULL,
    submitted_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
    promoted_by    TEXT,
    promoted_at    TEXT,
    known_good     INTEGER NOT NULL DEFAULT 0 CHECK (known_good IN (0, 1)),
    known_good_by  TEXT,
    known_good_evidence TEXT,
    UNIQUE (name, version)
);
CREATE UNIQUE INDEX idx_skill_one_active ON skill_versions (name) WHERE state = 'active';
CREATE INDEX idx_skill_name ON skill_versions (name, version);
