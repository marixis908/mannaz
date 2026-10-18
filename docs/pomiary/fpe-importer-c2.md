# Importer konsensusu — przebiegi C1 i C2 (wyciąg)

Data przebiegów: 2026-10-18 · Wykonawca: sesja CC · Commit kodu: `a5a07c9` (importer konsensusu Yahoo,
migracja `sql/020_estimates.sql`) · Odwołania: T48, T49, M93, P-15; podstawa rewizji 4.23 dokumentu projektowego.

Surowe odpowiedzi dostawcy i pełne raporty przebiegów są poza gitem (katalogi ignorowane). Ten wyciąg zawiera
liczby, wyniki i ograniczenia dowodu potrzebne do odczytania rew. 4.23. Źródło liczb: raporty C1 i C2 oraz
zapis przebiegu C2 z 2026-10-18, chyba że wskazano inaczej.

## C1 — migracja na bazie lokalnej

| Kontrola | Wynik (raport C1, 2026-10-18) |
|---|---|
| Cel | lokalna baza PostgreSQL 16 w kontenerze, adres pętli zwrotnej; hasła nie odczytywano |
| Stan przed | `estimate_snapshots` i `estimates` nieobecne; kontrolka dodatnia: `instruments` obecna |
| Migracja 020 | kod wyjścia 0; obie tabele po 21 kolumn; 4 wyzwalacze tylko do dopisywania (UPDATE/DELETE per wiersz, TRUNCATE per polecenie) |
| Testy db | `tests/test_estimates_db.py`: predykcja 12, wynik 12 passed, 0 failed, 0 skipped; liczniki tabel przed i po bez zmian |
| Idempotencja | drugie zastosowanie 020: kod wyjścia 0; sha256 zrzutu schematu (59 linii) przed = po, 0 różnic |

Ograniczenie C1: niesprawdzone uprawnienia (GRANT) na nowych tabelach i inne bazy.

## C2 — pierwszy przebieg na żywo, 7 symboli pilotażu

Symbole jak w pilotażu r1 (`docs/pomiary/forward-pe-pilot-r1.md`): RBRK, CSU.TO, IFX.DE, MU, MSFT, CPR.WA,
XXQZ.WA. Jeden przebieg, odstęp 2 s między symbolami, bez ponawiania.

| Pozycja | Wynik (raport C2, 2026-10-18) |
|---|---|
| Odpowiedzi HTTP | 6 × 200, 1 × 404 (XXQZ.WA) |
| Snapshoty w archiwum | 7/7 |
| W bazie | 5 snapshotów: RBRK, CSU.TO, IFX.DE, MSFT, CPR.WA |
| Tylko archiwum | 2: MU i XXQZ.WA jako kontrolki; MU poza `instruments` (decyzja ownera), XXQZ.WA symbol nieistniejący (404, odpowiedź zachowana, błąd `HTTP 404`, 0 wierszy) |
| Wiersze `estimates` | 20 (5 snapshotów × 4 okresy: 0q, +1q, 0y, +1y) |
| Snapshoty w bazie | quality_flag `low` 5/5, skipped_labels puste 5/5, error pusty 5/5 |
| CPR.WA (M93) | eps_status i rev_status `brak zweryfikowanego konsensusu` 4/4; currency_check `waluta EPS nieznana` 4/4; parse_status snapshotu i status wierszy `brak daty okresu` (4/4) |
| CSU.TO (P-15) | currency_check `waluta EPS rozna od waluty notowania - niepotwierdzona (P-15)` 4/4 (EPS w USD, cena w CAD) |
| `revenueEstimate.revenueCurrency` | obecny w 24/24 okresach (6 odpowiedzi z wynikiem × 4); kontrolka dodatnia `earningsCurrency` 4/4 w tych samych odpowiedziach; XXQZ.WA 0/0 |
| rev_currency w bazie | RBRK USD, CSU.TO USD, IFX.DE EUR, MSFT USD, CPR.WA brak |
| Kopia archiwum na dysku D: (T49) | wyłącznie dopisująca; sha256 kopii = sha256 źródła = sha256 w metadanych: 7/7 |
| HTTP 429 | nie wystąpił; ścieżka nieudanego pobrania przy 429 (T48) niesprawdzona na żywo |

### Predykcje wobec wyniku

| Pozycja | Predykcja | Wynik | Trafiona |
|---|---|---|---|
| Snapshoty | 7 | 7 | tak |
| W bazie | 5 | 5, te same instrumenty | tak |
| Tylko archiwum | 2 (MU, XXQZ.WA) | 2 (MU, XXQZ.WA) | tak |
| XXQZ.WA | 404, odpowiedź zachowana, 0 wierszy | zgodnie | tak |
| CPR.WA (M93) | statusy braku konsensusu 4/4, `brak daty okresu` | zgodnie 4/4 | tak |
| CSU.TO (P-15) | currency_check niepotwierdzona 4/4 | 4/4 | tak |
| `revenueCurrency` | nie istnieje | istnieje (24/24) | **nie** |

Predykcje: 6/7 trafione; jawnie nietrafiona: obecność `revenueCurrency` (stąd odnotowanie w §8.2, rew. 4.23).

## Ograniczenia dowodu

- Jeden przebieg, w niedzielę (2026-10-18), bez sesji giełdowej; powtarzalność w dniu sesyjnym otwarta (P-16).
- 7 symboli pilotażu, w bazie 5; poprawność merytoryczna wartości konsensusu niesprawdzona (P-14, P-15 otwarte).
- Zachowanie przy HTTP 429 niesprawdzone na żywo.
- Kopia archiwum na tym samym komputerze: nie chroni przed awarią sprzętu ani utratą komputera (T49).
