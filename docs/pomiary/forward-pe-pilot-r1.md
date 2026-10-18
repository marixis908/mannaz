# Pilotaż forward-pe r1 — ocena założeń P/E NTM (wyciąg)

Data pilotażu: 2026-10-18 · Wykonawca: sesja CC · Weryfikator: Codex (tylko odczyt) · Commit kodu: `70d0343`
(`src/mannaz/forward_pe.py`, `tests/test_forward_pe.py`) · Podstawa rewizji 4.22 dokumentu projektowego (§16.5, M90–M94, T47, T48, P-13–P-17, O-52, O-53).

Surowe odpowiedzi dostawców (Yahoo quoteSummary, SEC EDGAR) i pełny raport pilotażu są poza gitem (katalog
ignorowany). Ten wyciąg zastępuje dostęp do ignorowanego raportu: zawiera liczby, werdykty i ograniczenia
dowodów potrzebne do odczytania rew. 4.22.

## Instrumenty i źródła

| Symbol Yahoo | Rola w pilotażu |
|---|---|
| RBRK | portfel, EPS GAAP < 0 |
| CSU.TO | portfel, cena w CAD |
| IFX.DE | portfel, cena w EUR, FY świeżo zamknięty |
| MU | kontrola: rok 52/53-tygodniowy, świeży przeskok roku |
| MSFT | kontrola dodatnia: głębokie pokrycie |
| CPR.WA | kontrola ujemna: brak konsensusu |
| XXQZ.WA | kontrola ujemna: symbol nieistniejący |

- Konsensus: Yahoo quoteSummary (moduły `earningsTrend`, `earningsHistory`, `defaultKeyStatistics`,
  `calendarEvents`, `price`), biblioteka yfinance 1.7.0 (wewnętrzne `YfData.get`), Python 3.14.4.
  Pobrania: 2026-10-18 08:03:16–08:03:18 UTC i 08:13:38–08:13:39 UTC.
- Źródła niezależne: SEC EDGAR (`submissions`: `fiscalYearEnd`, lista 10-K; XBRL `us-gaap/EarningsPerShareDiluted`,
  fakty 10-Q), pobrane SEC 2026-10-18 08:04 UTC, nagłówek User-Agent `Mannaz forward-pe pilot <email>`;
  TradingView `get_financial_history` (`fy`, `fq`), TradingView 2026-10-18.

## Założenia Z1–Z9

Wartości EPS w walucie wskazanej przez dostawcę na akcję; daty okresów w formacie dostawcy. Źródło każdej
liczby: Yahoo quoteSummary z 2026-10-18 08:03 UTC, chyba że w wierszu wskazano inaczej.

| Z | Data · zakres | Metoda | Wynik | Werdykt | Ograniczenie dowodu |
|---|---|---|---|---|---|
| Z1 — data okresu w odpowiedzi | 2026-10-18 · 7 symboli (5 z konsensusem) | odczyt `earningsTrend.trend[*].endDate` | pole `endDate` na najwyższym poziomie dla 0q, +1q, 0y, +1y u 5/5 spółek; 0y / +1y: RBRK 2027-01-31 / 2028-01-31, CSU.TO 2026-12-31 / 2027-12-31, IFX.DE 2026-09-30 / 2027-09-30, MU 2027-08-31 / 2028-08-31, MSFT 2027-06-30 / 2028-06-30; CPR.WA `endDate` = null | PASS | `trend` ma 4 pozycje (0q, +1q, 0y, +1y) u 6/6 symboli z wynikiem, a nie przewidywane > 4 |
| Z2 — poprawność daty okresu | 2026-10-18 · RBRK, MU, MSFT (SEC); CSU.TO, IFX.DE (TV) | MMDD z SEC `fiscalYearEnd`; rok = pierwszy FY po ostatnim 10-K; tolerancja ±7 dni | RBRK: SEC 0131, koniec okresu ostatniego 10-K 2026-01-31, Yahoo 0y 2027-01-31, różnica 0 dni; MU: SEC 0903, koniec okresu ostatniego 10-K 2026-09-03, Yahoo 2027-08-31, różnica 2–3 dni (52 tyg.: 2027-09-02; literalnie 2027-09-03); MSFT: SEC 0630, koniec okresu ostatniego 10-K 2026-06-30, Yahoo 2027-06-30, różnica 0 dni | NIEROZSTRZYGNIĘTE (3/5 PASS) | CSU.TO, IFX.DE bez niezależnej daty: TradingView zwraca etykiety okresów („2025”, „Q3 2025”), bez dat końca. Dowód pomocniczy (wnioskowanie, nie data; TV, mln w walucie raportowej TV, waluta nie podana w odpowiedzi): IFX zysk netto FY2025 1015 = Q4'24 + Q1–Q3'25 (246 + 232 + 305 + 232), a Q1–Q4'25 = 1025 → koniec FY 30.09; CSU FY2025 715,41 wobec Q1–Q4'25 715,26 (0,02%) → rok kalendarzowy |
| Z3 — kolejność i etykiety | 2026-10-18 · 6 symboli z wynikiem | odczyt `trend[*].period`; dopasowanie w kodzie po dacie końca okresu | [0q, +1q, 0y, +1y] u 6/6; F2 dopasowuje okres wyłącznie po `period_end` | PASS | — |
| Z4 — waluta EPS | 2026-10-18 · 5 spółek | `earningsEstimate.earningsCurrency` (0y, +1y) wobec `price.currency` | CSU.TO: USD przy cenie w CAD; IFX.DE: EUR / EUR; RBRK, MU, MSFT: USD / USD; CPR.WA: null, cena PLN | PASS (etykiety) | prawdziwość etykiety USD dla CSU.TO nieweryfikowana (P-15); `defaultKeyStatistics.trailingEps` bez pola waluty |
| Z5 — baza EPS | 2026-10-18 · RBRK, MU | `earningsHistory.epsActual` (Yahoo) wobec SEC XBRL GAAP diluted, fakty 10-Q 88–91 dni, tylko Q1–Q3; równe ±0,01 → GAAP; > 5% w ≥ 2 kwartałach → inna niż GAAP | RBRK (USD/akcję, Yahoo / SEC): 2025-10-31 0,10 / −0,32 (131,25%); 2026-04-30 0,16 / −0,21 (176,19%); 2026-07-31 0,20 / −0,30 (166,67%). MU: 2025-11 4,78 / 4,60 (3,91%); 2026-02 12,20 / 12,07 (1,08%); 2026-05 25,11 / 24,67 (1,78%) | PASS (RBRK inna niż GAAP) | status wywnioskowany; MU nierozstrzygnięte (różnice 1–4%, reguła nie rozstrzyga); wyłączenie SBC nieustalone (P-17) |
| Z6 — semantyka `epsTrend` | 2026-10-18 · MU | `0y.epsTrend.90daysAgo` wobec (a) `0y.current` i (b) zrealizowanego EPS zamkniętego FY (suma 4 kwartałów `earningsHistory`) | MU 0y wskazuje FY kończący się 2027-08-31 (FY2026 zamknięty 2026-09-03 i zaraportowany, 10-K złożony 2026-10-09) — test ważny. 90daysAgo 150,86; (a) 176,55; (b) 4,78 + 12,20 + 25,11 + 33,42 = 75,51 (USD/akcję); odległości 25,69 i 75,35 | PASS — historia dotyczy okresu wskazanego dziś | n = 1 (MU); status „porównywalność okresu niepotwierdzona” utrzymany do P-13 |
| Z7 — świeżość konsensusu | 2026-10-18 · 5 spółek | rekurencyjny przegląd pól z datą/czasem | brak daty aktualizacji konsensusu (są tylko daty okresów, wyników, dywidend, notowań); `numberOfAnalysts` 0y / +1y: RBRK 27 / 27, CSU.TO 7 / 6, IFX.DE 22 / 22, MU 35 / 36, MSFT 34 / 33 | PASS → status „wiek konsensusu nieznany” | — |
| Z8 — powtarzalność | 2026-10-18 08:03 i 08:13 UTC · 7 symboli | dwa pobrania w odstępie 621–622 s; porównanie sha256 i wartości | `raw_sha256` identyczny 7/7; 0 różnych ścieżek JSON; `earningsEstimate.avg` identyczne dla 0q / +1q / 0y / +1y u 5/5 | PASS | słaby dowód — niedziela, rynki zamknięte (P-16) |
| Z9 — kontrole ujemne | 2026-10-18 · CPR.WA, XXQZ.WA | odczyt pól konsensusu i statusu HTTP | CPR.WA: HTTP 200, pola EPS `{}` (puste), ale `revenueEstimate.avg/low/high/numberOfAnalysts` = `raw: 0` przy `fmt: null` w 4/4 okresach i w obu pobraniach; XXQZ.WA: HTTP 404, błąd „Quote not found for symbol: XXQZ.WA”, `result` null | FAIL | zera w przychodach przy zerowej liczbie analityków; reguła: M93 (brak lub zero analityków = brak zweryfikowanego konsensusu dla metryki i okresu) |

## Werdykt bramki — PASS

Przypisanie prognoz 0y i +1y do konkretnej daty końca roku obrotowego udowodnione na RBRK (31.01), MSFT (30.06)
i MU (rok 52/53-tygodniowy, ok. 03.09): trzy różne końce roku, niezależne źródło SEC EDGAR, różnice 0–3 dni;
+1y = 0y + 1 rok u 5/5 spółek. 0y oznacza pierwszy rok obrotowy bez opublikowanego raportu rocznego
(IFX.DE: FY zamknięty 2026-09-30, publikacja wyników 2026-11-10 → 0y = FY2026). Zastrzeżenie: spółki spoza SEC
(CSU.TO, IFX.DE) bez niezależnej daty (P-14).

## Testy syntetyczne S1–S10

To są testy funkcji obliczeniowych na danych syntetycznych, nie pomiar danych rynkowych. Funkcje F1–F7
(P/E NTM, rewizja dla stałego okresu, status wzrostu, luka adjusted/GAAP, agregat portfela, rozbieżność źródeł,
świeżość konsensusu) są czyste, bez sieci i bazy.

Wynik: `15 passed` (S1–S10, test zakazu zastępowania braku zerem, 2 testy F5), tolerancja 1e-6 (procenty 1e-4 pp).

Decyzje planisty w trakcie pilotażu:
- r1: S7b — oczekiwane P/E części = 200/9 (22,222222) z tolerancją 1e-6 (tabela briefu była zaokrąglona do
  22,222; wcześniejsze poluzowanie tolerancji do 1e-3 cofnięte); F5 — gotówka w rozbiciu wag, poza mianownikiem
  wyceny, etykieta „P/E części akcyjnej” z udziałem gotówki (nowy test S7c); nowy test S10 (D po końcu FY1 → w = 0).
- r1b: status F5 wynika z kategorii pozycji (istnieje pozycja z wagą > 0 w „brak danych” lub „wyłączenie
  metodologiczne”), nie z sum wag; bez tolerancji pokrycia.

Weryfikacja Codex: runda 1 — 23/27 zgodne, 3 niezgodne (S7b, F5, Z9), 1 bez weryfikacji (Z2); runda 1b — 3/4
zgodne (F5 niezgodne, kontrprzykład z wagą braku 0,0000000005); runda 1c — F5 7/7 zgodne.

## Ryzyka

- Wewnętrzne API yfinance (`YfData.get`, URL quoteSummary) może się zmienić bez ostrzeżenia; wersja przypięta 1.7.0.
- Format dostawcy: mimo `formatted=false` liczby przychodzą jako `{raw, fmt}`; brak to raz `{}` (EPS), raz
  `raw: 0` przy `fmt: null` (przychody) — parser musi to rozróżniać (M93).
- Archiwum pilotażu zapisywało sparsowany JSON, a sha256 liczono z bajtów odpowiedzi HTTP — hasha nie da się
  odtworzyć z zapisu. Kontrakt T48 wymaga zapisu rzeczywistych bajtów.
- Brak niezależnego źródła daty końca FY dla spółek spoza SEC (P-14).
- Dowody o małej mocy: Z6 n = 1, Z8 w niedzielę (P-13, P-16).
- Rok 52/53-tygodniowy: dostawca podaje koniec miesiąca (MU 2027-08-31 wobec ok. 2027-09-02) — dopasowanie wymaga tolerancji (T47).
Poprawka 2026-10-18 (rew. 4.23): rozroznienie konca okresu objetego 10-K i daty jego zlozenia (Z2, Z6); liczby bez zmian.
