-- Migration 8 (item 7.2, #33): what kind of claim a claim is, so the router
-- can ask for a mechanism or a measurement by name instead of guessing.

ALTER TABLE claims ADD COLUMN kind TEXT NOT NULL DEFAULT 'finding'
    CHECK (kind IN ('finding', 'mechanism', 'measurement'));
