# Dashboard portfela satelity (brief CC-D, D4)

Generator lokalnego, samodzielnego dashboardu HTML (bez sieci, bez zapisów do
bazy). Kontrakt danych — autorytatywny, rozstrzyga wątpliwości:
`tmp/dashboard/kontrakt-danych.md`.

## Pliki

- `generate.py` — CLI + wczytanie danych z bazy (read-only).
- `model.py` — czyste funkcje (wycena, konsolidacja, wagi, wynik (a), testy
  pochodzenia ryzyka, agregacja ryzyka, formatowanie PL) — testowane w
  `tests/test_dashboard.py`.
- `template.html` — szablon strony (CSS+JS inline, jeden punkt wstrzyknięcia
  JSON: `__DASHBOARD_DATA_JSON__`).

## Uruchomienie (PowerShell, z katalogu worktree)

```powershell
C:\Users\MariuszBrysik\projects\Mannaz\.venv\Scripts\python.exe scripts\dashboard\generate.py --date 2026-09-25 --risk-date 2026-09-24 --out C:\Users\MariuszBrysik\projects\Mannaz\tmp\dashboard\out\dev\dashboard-dev.html
```

`--date` (D składu) i `--risk-date` (D ryzyka) są WYMAGANE, bez wartości
domyślnych. Kody wyjścia: `0` sukces, `2` STOP — waluta nieobsłużona, `3`
STOP — dane starsze niż 5 sesji, `1` inny błąd. Stdout: wyłącznie liczności,
daty, sha256, PASS/FAIL (repo publiczne — zero kwot/wag/cen/tickerów/numerów
rachunków w logach).

## Testy

```powershell
C:\Users\MariuszBrysik\projects\Mannaz\.venv\Scripts\python.exe -m pytest tests\test_dashboard.py -q
```
