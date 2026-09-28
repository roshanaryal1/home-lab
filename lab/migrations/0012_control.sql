-- Migration 12 (item 6.3, #79): the operator's mode switch.
--
-- One row. A supervisor reads it before leasing work, so a person at
-- another terminal can pause, drain or stop the lab without signalling a
-- process. Every change is also written to the hash-chained event log.

CREATE TABLE control (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    mode    TEXT NOT NULL DEFAULT 'running'
            CHECK (mode IN ('running', 'paused', 'draining', 'stopped')),
    reason  TEXT,
    set_by  TEXT,
    set_at  TEXT
);

INSERT INTO control (id) VALUES (1);
