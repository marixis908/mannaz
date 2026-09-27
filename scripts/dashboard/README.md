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

## Uruchomienie (PowerShell, z dowolnego katalogu)

```powershell
& C:\Users\MariuszBrysik\projects\Mannaz\.venv\Scripts\python.exe C:\Users\MariuszBrysik\projects\Mannaz\scripts\dashboard\generate.py --date <D> --risk-date <D> --out C:\Users\MariuszBrysik\projects\Mannaz\tmp\dashboard\out\dashboard-<D>.html
```

`<D>` = data w formacie `RRRR-MM-DD` (np. `2026-09-25`) — podstawić przed uruchomieniem.
Wynik zawsze do katalogu ignorowanego (`tmp\` albo `out\`) — nigdy do indeksu git.

`--date` (D składu) i `--risk-date` (D ryzyka) są WYMAGANE, bez wartości
domyślnych. Domyślnie D ryzyka = D; inna data tylko gdy test pochodzenia
`risk_daily` na D da FAIL/NIEROZSTRZYGNIETY (stdout pokazuje wynik per test). Kody wyjścia: `0` sukces, `2` STOP — waluta nieobsłużona, `3`
STOP — dane starsze niż 5 sesji, `1` inny błąd. Stdout: wyłącznie liczności,
daty, sha256, PASS/FAIL (repo publiczne — zero kwot/wag/cen/tickerów/numerów
rachunków w logach).

## Testy

```powershell
C:\Users\MariuszBrysik\projects\Mannaz\.venv\Scripts\python.exe -m pytest tests\test_dashboard.py -q
```
