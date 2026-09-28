-- Migration 9 (item 8.4, #85): inspectable memory with an FTS5 baseline.
--
-- ADR 0003's rule is the design: a web page, an email or another agent
-- must never silently write curated memory. So there are two kinds:
--   curated   stable facts a person promoted (promoted_by is required)
--   evidence  source-backed notes with a source, a hash and a retrieval date,
--             always untrusted, always expiring
-- Retrieval returns data, never instructions, and only active rows.

CREATE TABLE memories (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    kind              TEXT NOT NULL CHECK (kind IN ('curated', 'evidence')),
    text              TEXT NOT NULL,             -- emptied when deleted
    text_sha256       TEXT NOT NULL CHECK (length(text_sha256) = 64),
    source_id         TEXT NOT NULL,
    source_sha256     TEXT,
    trust             TEXT NOT NULL CHECK (trust IN ('trusted', 'untrusted')),
    state             TEXT NOT NULL DEFAULT 'active'
                      CHECK (state IN ('active', 'revoked', 'deleted')),
    created_by        TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
    expires_at        TEXT,                      -- required for untrusted, checked in code
    embedding_version TEXT,                      -- NULL: the FTS5 baseline, no embedding
    corrected_from    INTEGER REFERENCES memories(id),
    ended_at          TEXT,
    ended_by          TEXT,
    ended_reason      TEXT
);
CREATE INDEX idx_memories_state ON memories (state, expires_at);

-- External-content full-text index over active rows only. Rows leave the
-- index the moment they stop being active, so a revoked or deleted memory
-- cannot be retrieved even by a stale query.
CREATE VIRTUAL TABLE memories_fts USING fts5(text, content='memories', content_rowid='id');

CREATE TRIGGER memories_ai AFTER INSERT ON memories WHEN new.state = 'active'
BEGIN
    INSERT INTO memories_fts (rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER memories_au AFTER UPDATE ON memories
BEGIN
    INSERT INTO memories_fts (memories_fts, rowid, text)
        SELECT 'delete', old.id, old.text WHERE old.state = 'active';
    INSERT INTO memories_fts (rowid, text)
        SELECT new.id, new.text WHERE new.state = 'active';
END;

CREATE TRIGGER memories_ad AFTER DELETE ON memories WHEN old.state = 'active'
BEGIN
    INSERT INTO memories_fts (memories_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

-- Which tasks read which memory, so revoking one can name what it touched.
CREATE TABLE memory_uses (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id INTEGER NOT NULL REFERENCES memories(id),
    task_id   TEXT NOT NULL,
    used_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now'))
);
CREATE INDEX idx_memory_uses ON memory_uses (memory_id);
