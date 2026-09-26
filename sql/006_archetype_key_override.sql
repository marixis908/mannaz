-- D2: split "klucz proponuje / rejestracja decyduje" (§14.1 v2 + adnotacja,
-- brief CC-P6 pkt 1.1 #2, krok D2). Stosowany PO sql/seed_archetypes_v2.sql.
-- Idempotentny: bezpieczny do wielokrotnego uruchomienia, także po ponownym
-- zastosowaniu seed_archetypes_v2.sql (który resetuje `archetype` z powrotem
-- do wyniku klucza dla ACMR/MKSI/IFX poprzez ON CONFLICT DO UPDATE — ten
-- skrypt odtwarza wtedy tę samą rozbieżność 3, bez nadpisywania już zapisanego
-- `archetype_key`).

BEGIN;

-- Schemat: `archetype_key` = wynik klucza v2 (co "proponuje" klucz),
-- `archetype` (istniejąca kolumna) = rejestracja §12, czyli to co "decyduje".
-- `archetype_override_reason` = uzasadnienie rozbieżności archetype != archetype_key.
ALTER TABLE archetype_assignments ADD COLUMN IF NOT EXISTS archetype_key TEXT;
ALTER TABLE archetype_assignments ADD COLUMN IF NOT EXISTS archetype_override_reason TEXT;

-- D2(a): poza_14 -> A0 dla instrumentów satelity (kontrakt/ETF/certyfikat w
-- satelicie = A0, "poza §14" tylko dla rdzenia — brief CC-P6 pkt 1.1 #3).
UPDATE archetype_assignments aa
SET archetype = 'A0'
FROM instruments i
WHERE aa.instrument_id = i.id
  AND aa.key_version = 'v2'
  AND aa.archetype = 'poza_14'
  AND i.is_core = false;

-- D2(b) krok 1: wypełnij archetype_key wynikiem klucza v2 (obecna wartość
-- `archetype` PO kroku (a) powyżej, PRZED nadpisaniem A9 poniżej) dla
-- wszystkich wierszy v2. Warunek "archetype_key IS NULL" czyni krok
-- niedziałającym przy ponownym uruchomieniu (nie nadpisuje już ustalonego
-- wyniku klucza wartością A9 z rejestracji).
UPDATE archetype_assignments
SET archetype_key = archetype
WHERE key_version = 'v2'
  AND archetype_key IS NULL;

-- D2(b) krok 2: rejestracja §12 decyduje inaczej niż klucz dla ACMR, MKSI,
-- IFX -> A9, "cykliczny półprzewodnik" (brief CC-P6 pkt 1.1 #2).
UPDATE archetype_assignments aa
SET archetype = 'A9',
    archetype_override_reason = 'cykliczny półprzewodnik — bramka book-to-bill / zamówienia (decyzja nadzorcy 2026-09-26)'
FROM instruments i
WHERE aa.instrument_id = i.id
  AND aa.key_version = 'v2'
  AND i.broker_ticker IN ('ACMR', 'MKSI', 'IFX');

COMMIT;
