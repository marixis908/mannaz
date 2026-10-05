# Mannaz

System monitoringu portfela satelitarnego. Metoda (model analityczny, architektura, dane,
zakres pierwszej implementacji) jest opisana w dokumencie projektowym — to on jest źródłem
prawdy, nie ten plik.

## Gdzie co leży

| ścieżka | zawartość |
|---|---|
| `docs/projekt-systemu-monitoringu.md` | dokument projektowy (rewizja 4) — źródło prawdy metody |
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

## Uruchomienie cyklu

```
powershell -ExecutionPolicy Bypass -File C:\Users\MariuszBrysik\projects\Mannaz\scripts\cycle.ps1
```

Skrypt `scripts/cycle.ps1` uruchamia `python -m mannaz.run_p3 cycle` interpreterem z `.venv`
repo głównego. Argumenty skryptu są przekazywane do `run_p3 cycle` (np. `--help`, `--today`),
a kod wyjścia skryptu jest kodem wyjścia Pythona; brak `.venv` → komunikat i kod 1.
`PYTHONPATH=<repo>\src` jest ustawiany tylko na czas uruchomionego procesu (potem wraca
poprzednia wartość). Pakietu `mannaz` nie instalujemy w `.venv`: sesje robocze pracują
w worktree, a instalacja wiązałaby venv repo głównego z jednym drzewem.
