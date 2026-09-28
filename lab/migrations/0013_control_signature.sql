-- Migration 13 (H3): a resume must be signed by the operator.
--
-- ``generation`` counts changes; the operator signs (generation, 'running').
-- A supervisor that has the operator's public key honours ``running`` only
-- with a signature that verifies for that exact generation, so neither a
-- forged row nor a replayed old signature restarts the lab.

ALTER TABLE control ADD COLUMN generation INTEGER NOT NULL DEFAULT 0;
ALTER TABLE control ADD COLUMN signature TEXT;
