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

Zależności: `pip install -r requirements.txt -c constraints.txt` w `.venv` (Python 3.14).
`requirements.txt` — zależności bezpośrednie; `constraints.txt` — pełna lista wersji
(`pip freeze` z lokalnego `.venv`, B-53), aktualizowana razem ze zmianą wersji w `.venv`. `yfinance` przypięty
do 1.7.0 w `requirements.txt` i testem `tests/test_b51_yfinance_pin.py` (B-51); zmiana wersji
wymaga ponownej weryfikacji obsługi wyjątków w `fetch_ohlc`.

## Praca w sesjach Claude Code w chmurze

Sesje na claude.ai/code pracują na tym repo bez danych finansowych. Hook
`.claude/hooks/session-start.sh` (zarejestrowany w `.claude/settings.json`, działa tylko
w chmurze, lokalnie nic nie robi) tworzy `.venv` na Pythonie 3.14 przez `uv`, instaluje
`requirements.txt` i ustawia `PYTHONPATH=src`. Postgresa w chmurze nie ma, więc testy
bazodanowe (marker `db`) są pomijane, a `python -m pytest` sprawdza tylko testy czyste.

Testy bazodanowe i cykl `run_p3` uruchamia owner lokalnie, na własnej bazie, przed merge
zmian z sesji chmurowej. Sesja chmurowa pracuje na gałęzi `claude/…` i oddaje pracę jako PR.
Do sesji chmurowej nie wgrywa się `.env`, eksportów z brokera ani zrzutów bazy.
