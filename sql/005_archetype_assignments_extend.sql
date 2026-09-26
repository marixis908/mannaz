-- P5.1: classification key §14.1 audit trail (answers to questions 1-9).
-- answers: 9 chars in the ORIGINAL question numbering of K §4 pt 2
-- (1 fund/ETF/contract, 2 crypto, 3 credit book, 4 FRE, 5 payments, 6 GMV,
--  7 recurring software, 8 capex/backlog, 9 top decile + EBIT>0 3y);
-- T = yes, N = no, ? = undecided, - = not evaluated (earlier yes decided).
-- Idempotent.
ALTER TABLE archetype_assignments ADD COLUMN IF NOT EXISTS archetype_secondary TEXT;
ALTER TABLE archetype_assignments ADD COLUMN IF NOT EXISTS answers TEXT;
ALTER TABLE archetype_assignments ADD COLUMN IF NOT EXISTS resolution TEXT;
ALTER TABLE archetype_assignments ADD COLUMN IF NOT EXISTS key_version TEXT;
ALTER TABLE archetype_assignments ADD COLUMN IF NOT EXISTS source_tags TEXT;
