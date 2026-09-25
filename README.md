# Mannaz

System monitoringu portfela satelitarnego. Metoda (model analityczny, architektura, dane,
zakres pierwszej implementacji) jest opisana w dokumencie projektowym — to on jest źródłem
prawdy, nie ten plik.

## Gdzie co leży

| ścieżka | zawartość |
|---|---|
| `docs/projekt-systemu-monitoringu.md` | dokument projektowy (rewizja 2) — źródło prawdy metody |
| `docs/decyzje.md` | dziennik decyzji: data, decyzja, kto, uzasadnienie |
| `docs/backlog.md` | backlog; zamknięcie pozycji zmienia `status`, wiersz zostaje |
| `docs/oceny/` | notatki z ocen poszczególnych walorów |
| `scripts/pine/` | skrypty Pine (TradingView) — eksport OHLC z TradingView: us_a (SPY + 16 spółek), us_b (QQQ + 16 spółek), us_c (SPY + RRX i benchmarki zastępcze), eu_2 (ASML + BESI/ADYEN/IFX); CSU osobnym eksportem wykresu TSX:CSU |

Od pierwszego commita to repo jest oryginałem dokumentu projektowego. Kopia w projekcie
Claude jest jego pochodną i przy rozbieżności przegrywa.

## Dane poza gitem

W repo nie ma i nie będzie danych finansowych: historii transakcji, migawek z kwotami,
eksportów z brokera ani kopii bazy. `.gitignore` blokuje katalogi `data/`, `_incoming/`,
`backups/`, `tmp/` oraz pliki `*.csv`, `*.xlsx`, `*.xls`, `*.parquet`, `*.dump`, `*.sql.gz`
i `.env*`. Plik z danymi trzymaj w `data/` — nigdy nie dodawaj go przez `git add -f`.
