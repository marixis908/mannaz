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

`--date` (D składu) i `--risk-date` (D ryzyka) są OPCJONALNE (rew. 2). Domyślnie
D = `mannaz.risk.resolve_default_risk_date` (B-36), D ryzyka = `max(risk_date) <= D`
(nagłówek zaznacza "domyslne B-36" albo "podane"). Brak daty D wg B-36 i brak
`--date`: HTML jest renderowany, każda sekcja to `brak pomiaru: brak daty D wg B-36
(brak kompletnej daty w cenach i KONTRAKTOWY)`, kod wyjścia 0. Brak oceny ryzyka
<= D: S3 i S4 = `brak pomiaru: brak oceny ryzyka <= D`. `--account-values-dir`
(domyślnie `mannaz.cycle.DEFAULT_MANUAL_DIR`, ta sama co cykl) = kotwice M78 dla
modułu satelity. Kody wyjścia: `0` sukces, `2` STOP — waluta nieobsłużona, `3`
STOP — dane starsze niż 5 sesji, `1` inny błąd. Stdout: wyłącznie liczności,
daty, sha256, PASS/FAIL (repo publiczne — zero kwot/wag/cen/tickerów/numerów
rachunków w logach).

## Sekcje rewizji 2 (brief D2)

Zasada: dashboard czyta (tabele, widoki, funkcje `mannaz.*`), nie liczy metryk;
generator tylko agreguje (sumy) i przewalutowuje przy agregacji (NBP A).
Sekcje S1, S3-S9 są renderowane statycznie przez `model.render_*_html`.

- S1 nagłówek: D składu, data oceny ryzyka, kompletność cen n/N i KONTRAKTOWY,
  `code_sha()`, liczba `source_runs` i max(fetched_at).
- S3/S4 ryzyko poziomu 1 per emitent i stopy z zapadką (`risk_daily`,
  `risk.level1_by_name`, `cycle.load_buy_below_stop_events`). Reguła Q1: skład
  na D, ryzyko z oceny <= D; pozycja otwarta/zmieniona po dacie oceny =
  `brak pomiaru: pozycja nowsza niz ocena ryzyka <data>`; pozycja zamknięta po
  dacie oceny nie jest pokazana w wierszach. Agregaty (L1 per nazwa, poziom 3,
  tematy) = odczyt z oceny ("stan oceny ryzyka <data>"), bez przeliczania.
- S5a rejestr 1 (`satellite_mandate_violations/_events`, registry = 1); naruszenie
  = alert, bez sugestii ruchu kapitału.
- S5b/S9 wynik satelity i rozkład akcyjny/walutowy z
  `mannaz.satellite.run_satellite_stage(write=False, out_dir=None)` na to samo D;
  status danych (zweryfikowane/niezweryfikowane) i przyczyna przy liczbach.
  Wyjątek modułu = `brak pomiaru: modul satelity: <klasa>`.
- S6 (B-89) i S8 (B-90) = `brak pomiaru` ze stopką "Co zasili".
- S7 archetyp (`archetype_assignments` aktywny na D) i temat (bieżący, B-20).

Konwencja liczb (zasada 4): każdy element liczbowy ma atrybut
`data-conv="waluta|warstwa|zrodlo|data"` (cztery niepuste pola). Sekcje statyczne
mają go w źródle HTML (klasa `num`); sekcje rew. 1 (JS) dostają go w runtime z
`data-conv-default` na `<section>`. Każda sekcja ma stopkę `data-role="conv-footer"`,
sekcje bez źródła także `data-role="co-zasili"`.

## Testy

```powershell
C:\Users\MariuszBrysik\projects\Mannaz\.venv\Scripts\python.exe -m pytest tests\test_dashboard.py -q
```
