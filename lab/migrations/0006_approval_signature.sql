-- Migration 6 (item 4.5, #70): approvals can carry an operator signature.
--
-- When the supervisor is given the operator's public key it honours only
-- granted approvals whose signature verifies (lab.operator). Rows written
-- by anyone without the private key are ignored, whatever their state.

ALTER TABLE approvals ADD COLUMN signature TEXT;
