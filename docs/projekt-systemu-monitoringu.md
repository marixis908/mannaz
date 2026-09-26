# Mannaz — system monitoringu portfela satelitarnego

**Rewizja 4 · 2026-09-26**

---

## 0. O tym dokumencie

### 0.1 Przeznaczenie

Dokument ma trzy funkcje jednocześnie i jest tak zbudowany, żeby każda dała się wykonać osobno:

1. **Podstawa oceny merytorycznej** — czy model analityczny jest poprawny (części IV i V).
2. **Podstawa oceny technicznej** — czy architektura i dane są wykonalne (części II, III, VI).
3. **Specyfikacja do implementacji** — każde założenie ponumerowane i falsyfikowalne, każda decyzja z uzasadnieniem i alternatywą, którą odrzucono (część VII).

Dokument opisuje **wersję docelową**. Zakres pierwszej implementacji jest węższy i opisany w §25.

### 0.2 Historia rewizji

| Rew. | Data | Zakres zmiany |
|---|---|---|
| 1.0 | 2026-08-27 | Projekt pierwotny: 9 archetypów wyceny, grupy peer, 7 wskaźników technicznych, reguły Turtle, kadencja |
| 1.1 | 2026-08-27 | Po recenzji zewnętrznej: sygnał główny RVS zamiast reszty z regresji; wersja minimalna; kwarantanna; egzekucja transzowa; dostawa dzielona po krytyczności |
| 2.0 | 2026-08-27 | Wielorynkowość i wielowalutowość; reverse DCF i MEROI; polityka alokacji; kadencja tygodniowa zamiast dobowej; weryfikacja Hostingera; pełny rejestr opcji odrzuconych; cztery zmienne stanu; korekty po recenzji Fable |
| 3.0 | 2026-09-25 | Stan infrastruktury po pomiarach: P-07 zamknięty, P-09 przeformułowany; §5.5 — osobny kontener PostgreSQL dla Mannaza zamiast wspólnej instancji z NocoDB; oryginał dokumentu w repo `mannaz` |
| 3.1 | 2026-09-25 | Stooq po pomiarze: dostęp skryptowy zablokowany JS proof-of-work, ścieżka `get_apikey` nie wydaje klucza; Stooq przechodzi do roli źródła ręcznego (§9.2, §9.3, §12, §25.2, §25.3, O-12, R-01, P-02, nowy P-12) |
| **4** | **2026-09-26** | **Klucz przypisania archetypu §14.1 (kalibracja 17/20), archetyp A0, reguła „klucz proponuje, rejestracja decyduje" (§12, M76), progi klucza T34–T37, instrumenty pochodne §19.4, erratum §19.3 (zapadka Chandeliera od inicjalizacji), O-45** |

### 0.3 Oznaczenia

| Znacznik | Znaczenie |
|---|---|
| **[Z]** | Zweryfikowane w źródle pierwotnym — dokumentacja dostawcy, oficjalna publikacja, żywe wywołanie API |
| **[Z2]** | Zweryfikowane w źródle wtórnym — agregator, forum, kod trzeciej strony |
| **[N]** | Niezweryfikowane — hipoteza do sprawdzenia, oznaczona pomiarem w §28 |
| **[S]** | Synteza własna — wniosek z zestawienia faktów, nie ustalenie źródłowe |
| **M**nn / **T**nn | Numerowane założenie metodologiczne / techniczne |
| **O**nn | Opcja odrzucona, rejestr w §26 |
| **P**nn | Pomiar do wykonania, rejestr w §28 |

**Zasada nadrzędna dokumentu:** żadna liczba nie występuje bez zadeklarowanej konwencji (warstwa, jednostka, waluta, źródło, data). Liczba bez konwencji nie jest pomiarem.

---

# CZĘŚĆ I — ZAKRES I ZAŁOŻENIA

## 1. Cel systemu i granice

### 1.1 Co system robi

Mierzy stan pozycji spekulacyjnych w czterech niezależnych wymiarach i na tej podstawie proponuje działania:

```
DANE ──┬── FUNDAMENTY ──────→ QUALITY  (IMPROVING / STABLE / DETERIORATING / BROKEN / UNKNOWN)
       ├── WYCENA ──────────→ VALUE    (VERY_CHEAP / CHEAP / FAIR / EXPENSIVE / VERY_EXPENSIVE / UNKNOWN)
       ├── CENA ────────────→ TREND    (UPTREND / NEUTRAL / DOWNTREND / UNKNOWN)
       └── CENA + PORTFEL ──→ RISK     (LOW / NORMAL / HIGH / CRITICAL / UNKNOWN)
                                  │
                                  ▼
                          SILNIK DECYZYJNY
                                  │
              ┌───────────┬───────┴───────┬───────────┐
              ▼           ▼               ▼           ▼
            ADD         HOLD           TRIM         EXIT
```

Cztery zmienne są **rozłączne z założenia**. „Tania", „dobry biznes", „w trendzie wzrostowym" i „należy sprzedać" to cztery różne pytania i system, który zwija je w jeden bool, przy błędnym sygnale nie pozwala ustalić, które wejście zawiodło.

### 1.2 Czego system nie robi

- **Nie przewiduje kursu.** Mierzy pozycję relatywną i stan reżimu.
- **Nie wykonuje transakcji.** Zlecenia składa właściciel; twarde stopy leżą u brokera jako zlecenia oczekujące (§22.3).
- **Nie zastępuje czytania raportów.** KPI, które faktycznie falsyfikują tezy, nie istnieją w żadnym maszynowym źródle (§9.5).
- **Nie obejmuje rdzenia portfela.** Satelita jest tu zamkniętym światem; decyzja o wielkości satelity względem rdzenia leży poza zakresem.
- **Nie jest doradztwem inwestycyjnym.** Wszystkie progi są punktami startowymi do kalibracji na własnych danych.

### 1.3 Granica sukcesu

System uznaje się za działający, gdy przez kwartał spełnia jednocześnie: mniej niż 15 alertów miesięcznie, zero cichych awarii (każda luka danych ma stan i jest raportowana), i właściciel czyta artefakt okresowy oraz reaguje na alerty. Jeśli po kwartale artefakt nie jest czytany — system zostaje wyłączony, a nie rozbudowany.

## 2. Portfel: stan, charakterystyka, kierunek

### 2.1 Stan zmierzony (zrzut 2026-08-27, część USD)

| Miara | Wartość |
|---|---|
| Pozycji | 31 |
| Wartość (suma `liczba × kurs`) | 306 899,53 USD |
| Wartość wg nagłówka zrzutu | 306 902,03 USD (różnica 2,50 z zaokrągleń kursów) |
| Koszt nabycia (z zaokrąglonych średnich) | 284 970,90 USD |
| Wynik | +21 928,63 USD · +7,70% |
| Pozycji na minusie | 14 / 31 |
| HHI wag | 0,0465 → **21,5 efektywnej pozycji wg wag** |
| Udział TOP5 / TOP10 | 35,5% / 57,5% |
| Pozycji poniżej 2% wagi | 13, razem 17,6% portfela |
| Pozycji poniżej 1,5% wagi | 6, razem ok. 5,5% portfela |

**Defekt danych wejściowych [Z]:** dla NU, GRAB i DLO iloczyn `(kurs − średnia) × liczba` nie zgadza się z raportowanym Z/S (GRAB: −1275,00 vs −1260,60). Kolumna „średnia cena" jest zaokrąglona do dwóch miejsc — realna średnia GRAB to ok. 4,0452. **Koszt nabycia musi pochodzić z historii transakcji, nigdy ze zrzutu portfela.**

**Splity [Z]:** kursy w zrzucie są split-adjusted (ServiceNow po podziale 5:1 zatwierdzonym w 2025, Netflix po 10:1 z listopada 2025). Dane XBRL z SEC są „as reported" i splitów nie korygują — historyczne EPS i liczba akcji wymagają własnej korekty (T14).

### 2.2 Kierunek rozwoju — to jest zmiana wobec rewizji 1

Portfel **nie jest** i nie będzie portfelem wyłącznie amerykańskim:

- **GPW, notowania w PLN** — spółki polskie wchodzą do tego samego systemu.
- **Inne giełdy światowe** w walutach lokalnych — potencjalnie Xetra (EUR), Amsterdam (EUR), SIX (CHF), LSE (GBp), HKEX (HKD), TSE (JPY), Sztokholm (SEK).
- **Dodawanie kolejnych spółek jest operacją rutynową**, nie zmianą kodu — procedura w §12.

To wywraca trzy rozstrzygnięcia z poprzednich rewizji, opisane w §26 jako O-11, O-12 i O-13.

### 2.3 Konsekwencja: waluta bazowa

**M1.** Walutą bazową systemu jest **PLN**. Uzasadnienie: właściciel rozlicza się w PLN, NBP publikuje kursy wszystkich potrzebnych walut wprost do PLN (bez krzyżowania przez EUR, czyli bez propagacji błędu zaokrągleń na dwóch krokach), a kurs NBP jest zgodny z rozliczeniem podatkowym.

**M2.** Każdy instrument ma **walutę notowania** niezależną od waluty bazowej. Wszystkie wskaźniki cenowe (ATR, SMA, momentum, stop) liczone są **w walucie notowania**. Przewalutowanie następuje wyłącznie na poziomie agregacji portfelowej i sizingu.

**Uzasadnienie M2 [S]:** liczenie ATR na szeregu przewalutowanym oznacza, że ruch kursu walutowego generuje fałszywe naruszenia Chandelier Exit i stopu 2N — czyli alert o złamaniu reguły ryzyka, którego rynek nie wygenerował. To błąd cichy: alert wygląda normalnie i nie da się go odróżnić od prawdziwego.

**M3.** Wynik portfela jest zawsze rozkładany na **komponent akcyjny i walutowy**. Ocena selekcji spółek na podstawie ruchu, który z selekcją nie ma nic wspólnego, jest bezwartościowa. Przy ekspozycji rzędu 307 tys. USD ruch USD/PLN o 5% to ok. 15 tys. USD zmiany wartości bez żadnego ruchu cen akcji.

## 3. Założenia metodologiczne

Numerowane, żeby dało się je zakwestionować pojedynczo. Każde jest falsyfikowalne albo jest decyzją, i to jest zaznaczone.

### 3.1 O wycenie

**M4.** Mnożnik jest dobierany do **archetypu biznesu**, nie stosowany jednolicie. P/E, forward P/E i P/S są niewłaściwe dla większości pozycji spekulacyjnych. *Podstawa: Liu, Nissim, Thomas (2002) — hierarchia dokładności forward earnings > earnings > cash flow ≈ book value > sales, rozstęp międzykwartylowy błędu 0,317 vs 0,738.*

**M5.** Każdy mnożnik występuje **w parze ze zmienną towarzyszącą** (marża, wzrost albo ROIC). Mnożnik bez zmiennej towarzyszącej nie niesie informacji.

**M6.** Agregacja mnożników w grupie peer: **mediana** (dla RVS) albo **średnia harmoniczna** (dla porównań poziomu). **Nigdy średnia arytmetyczna** — u Bakera i Rubacka zawyżenie sięgało +38,8%, przy harmonicznej maksymalnie 7%.

**M7.** Mnożnik jest zdefiniowany **wyłącznie przy dodatnim mianowniku**. Spółka z ujemnym EBIT wypada z policzalnych i nie wchodzi do mediany. Zmiana składu policzalnych między okresami jest **zdarzeniem do zalogowania**, nie milczącym przeliczeniem (§16.4).

**M8.** Statystyka relatywna: z-score na **logarytmie** mnożnika albo w wariancie odpornym `(x − mediana)/(1,4826 × MAD)`. Przy `n < 8` **nie liczy się z-score w ogóle** — tylko percentyl. Winsoryzacja 5/95 przed statystyką.

**M9.** SBC jest **kosztem, nie korektą**. Nie liczy się „P/E ex-SBC". Osobno monitorowane jest tempo rozwodnienia (%/rok wzrostu akcji rozwodnionych) jako metryka pierwszego rzędu dla archetypów SaaS.

### 3.2 O sygnale

**M10.** Sygnał główny nie stoi na poziomie mnożnika, tylko na **relacji do grupy porównanej z własną historią tej relacji**:
```
RVS    = mnożnik_spółki / mediana_mnożnika_peer
sygnał = ln(RVS_dziś) − ln(mediana RVS z okna referencyjnego)
```
*Uzasadnienie: relacja kasuje przeszacowanie całego sektora i wymaga tylko mediany, więc działa przy pięciu peerach tak samo jak przy piętnastu.*

**M11.** Regresja mnożnika na fundamentach **nie występuje w systemie w żadnej roli**. Przy `n = 8` krytyczne R² przy p = 0,05 wynosi ok. 0,50; próg 0,15 odpowiada p ≈ 0,34, czyli wpuszcza modele nieodróżnialne od szumu. Nawet w roli „wyjaśniającej" model modulowałby sygnały, czyli losowo wyciszał prawdziwe okazje.

**M12.** Korekta o jakość ma formę **stanu porządkowego (QUALITY), nie ciągłej korekty mnożnika**. Jakość mierzona jako **kierunek zmian** (rewizje, trend marży) jest odporna przy małym n; jakość mierzona jako **poziom** (ROIC vs peers) wymagałaby regresji — i dlatego się jej nie robi.

**M13.** Własna historia RVS jest **wejściem do rankingu conviction, nie twardą bramką**. Spółka, która strukturalnie poprawiła jakość i dlatego jej mnożnik trwale wzrósł, nie może być z tego powodu odrzucana.

**M14.** Sygnał wymaga **histerezy**: próg wejścia i wyjścia są różne (−0,25 wejście, −0,10 wyjście). Bez tego sygnał oscylujący wokół jednego progu generuje alert w każdym przebiegu.

**M15.** **Bramka jakości jest specyficzna dla archetypu.** Jedna uniwersalna bramka („rewizje NTM ≥ 0 ∧ marża brutto nie spada ∧ runway > 8 kwartałów") jest bramką SaaS-ową i zastosowana do banku, kopalni bitcoinów czy spółki farmaceutycznej popełnia ten sam błąd kategorii, który M4 zarzuca mnożnikom.

**M16.** Bramka ma **logikę trójwartościową**: PASS / FAIL / **UNKNOWN**. Zlanie UNKNOWN z FAIL daje spam pułapkowy; zlanie z PASS każe kupować pułapki. Obie ścieżki wyglądają wiarygodnie i żadna nie rzuca wyjątku.

### 3.3 O technice

**M17.** Zestaw wskaźników jest **minimalny i nieredundantny**. SMA-crossover, MACD, ROC i nachylenie średniej mierzą to samo; RSI, Stochastic, CCI i Williams %R mierzą to samo. Po jednym z rodziny.

**M18.** Wskaźniki dzielą się na cztery grupy odpowiadające na różne pytania i **nie głosują jako niezależne**: TREND (reżim), MOMENTUM (siła relatywna), RISK (zmienność), PORTFOLIO (koncentracja).

**M19.** Wskaźniki wewnątrzrynkowe liczone są **na własnym kalendarzu rynku, bez wyrównywania**. Forward-fill przez święta wstawia sztuczną sesję o zerowym zwrocie, co **zaniża ATR i σ**, poluzowuje stop i zawyża wielkość pozycji — błąd idzie w stronę większego ryzyka.

**M20.** Momentum przekrojowe kotwiczone jest na **datach kalendarzowych, nie na liczbie barów**. Liczba sesji w roku różni się między giełdami (NYSE ~252, GPW ~250, HKEX ~247, TSE ~245), więc stałe `close[252]` porównuje w jednym rankingu różne okna czasowe.

**M21.** Korelacja i PCA liczone są na **zwrotach tygodniowych**, nie dziennych. Zamknięcia giełd rozjeżdżają się o do 15 godzin (Tokio 07:00 CET vs NYSE 22:00 CET); asynchroniczność **systematycznie zaniża korelację**, co daje **zawyżoną efektywną liczbę zakładów i fałszywe poczucie dywersyfikacji**.

**M22.** Smart Money Concepts **nie wchodzi do silnika decyzyjnego**. Test mechaniczny na 2,55 mln barów, 54 warianty parametrów: 0/54 rentownych po koszcie 0,5 pipsa. Brak recenzowanej walidacji dla order blocks, BOS i CHoCH. Struktura swing przy `len=50` na D1 potwierdza się po ~2,5 miesiąca. Licencja skryptu LuxAlgo: CC BY-NC-SA 4.0 — port kodu 1:1 to utwór pochodny; sama logika (koncepcje publiczne) nie podlega prawu autorskiemu.

### 3.4 O ryzyku

**M23.** Twarde stopy są funkcją zmienności, nie procentu ceny: Chandelier Exit `max(high,22) − 3×ATR22` i stop 2N `entry − 2×ATR20`.

**M24.** Wielkość pozycji jest funkcją zmienności: `(kapitał × 0,01) / (2 × ATR20_w_walucie_notowania)`, przewalutowane na PLN po kursie z §10.

**M25.** Ryzyko ma **budżet na trzech poziomach**: pojedyncza nazwa ≤ 1% kapitału, temat/czynnik ≤ 3%, suma otwartych ryzyk ≤ 15%. Przy 31 pozycjach po 1% suma to 31% — a stopy pękną razem, bo portfel ma dominujący czynnik.

**M26.** Słabość relatywna **nie jest samodzielnym wyjściem** przy `VALUE = CHEAP`. System, którego sygnał kupna brzmi „tania względem peerów", a reguła wyjścia strzela na słabości relatywnej, kupuje to, co jednocześnie sprzedaje.

**M27.** Zakaz dokładania poniżej 0,70 × maksimum 52-tygodniowe jest **blokadą, nie ostrzeżeniem** — z nazwanym override'em zapisywanym w dzienniku decyzji. Reguła nie ma mieć racji co do spółki; ma mieć rację co do człowieka. Koszt fałszywej blokady to nieodebrany zysk na posiadanej pozycji; koszt fałszywego pozwolenia to kapitał w spółce, która dalej spada.

### 3.5 O wycenie absolutnej

**M28.** Reverse DCF **nie generuje alertów**. Uzasadnienie empiryczne: implied cost of capital oparte na modelu statystycznym predykuje przekrojowe zwroty (spread decylowy 10,62–12,03%, t = 3,77–5,39), ale oparte **na prognozach analityków — nie predykuje** (3,91–4,86%, wszystkie t < 1,40). Konsensus w tym systemie pochodzi z yfinance z flagą jakości `low`, czyli z gorszej gałęzi.

**M29.** Wartość rezydualna w reverse DCF to **perpetuity bez wzrostu** (`NOPAT/WACC`). Exit multiple jest cyrkularny — mnożnik wyjścia bierze się od peerów, a pytanie brzmi, czy cena jest wymagająca; przy przewartościowanym sektorze model nigdy nie powie „drogo".

**M30.** Dla spółek finansowych reverse DCF **nie ma zastosowania** — dług jest surowcem, WACC niedefiniowalne, reinwestycji nie da się zmierzyć. Zamiast tego forma zamknięta na excess return.

**M31.** Wymagalność założeń raportowana jest jako **percentyl wobec base rates**, nie jako liczba bezwzględna. Utrzymanie CAGR przychodów >20% przez 5 lat udało się 6,0% spółek, przez 10 lat — 2,8%.

## 4. Założenia techniczne

### 4.1 O danych

**T1.** Tabele **obserwacji** są append-only, egzekwowane **triggerem w bazie**, nie konwencją. Reguła bez egzekucji nie jest kontrolą.

**T2.** Tabele **metryk pochodnych** są przeliczalne, nie append-only. Zmiana definicji EV/GP nie może wymuszać przebudowy fundamentów. Warstwy: `raw → canonical → derived → states → decisions`.

**T3.** Każdy wiersz obserwacji niesie **prowenienecję**: źródło, znacznik czasu pobrania, waluta, konwencja korekty. Wiersz wpisany ręcznie ma dokładnie te same pola co pobrany automatycznie.

**T4.** Rozróżniane są trzy znaczniki czasu: `period_end` (czego dotyczy), `filed_at` (kiedy złożono), **`available_at`** (kiedy informacja była realnie dostępna). Bez trzeciego replay historyczny ma look-ahead.

**T5.** Każdy ticker w każdym przebiegu ma **stan**: `OK / DEGRADED / STALE / FAILED`. Poniżej progu pokrycia system **nie publikuje sygnału portfelowego**. Stan `brak danych → NaN → fillna(0) → brak alertu` ma być architektonicznie niemożliwy.

**T6.** Konwencja korekty cenowej jest **mapą wskaźnik → szereg**, nie jednym wyborem globalnym:

| Zastosowanie | Szereg |
|---|---|
| Stopy, ATR, Chandelier, poziomy cenowe | **split-adjusted, bez dywidend** |
| Momentum, RS, benchmarki, atrybucja wyniku | **total return** |

*Uzasadnienie [S]: szereg split-only systematycznie karze płatników dywidendy w rankingu momentum — a ten ranking bramkuje dokładanie. W portfelu dotyczy to MSFT, ORCL, APH, KKR, NVO i META.*

**T7.** Waluta instrumentu jest **twardo asertowana przy każdym pobraniu** wobec oczekiwanej waluty giełdy. Yahoo miesza jednostki — dokumentacja yfinance mówi wprost: *„Sometimes Yahoo mixes up currencies e.g. $/cents or £/pence. So some prices are 100x wrong"*, a błędy bywają rozsiane losowo albo w blokach. Skutek dla systemu nie jest wycenowy, tylko **rozmiarowy**: błąd 100× w ATR przechodzi wprost w sizing i stop.

**T8.** Sanity-check rzędu wielkości: `|log return| > 4` między sesjami → **kwarantanna szeregu**, nie alert cenowy.

**T9.** Kolejność i agregaty liczone są **w SQL albo lokalnie po pobraniu**, nigdy po stronie klienta UI. Zmierzone wcześniej: klient MCP NocoDB cicho gubi `sort` — `Id desc` zwracało rosnąco, URL pokazywał `sort=[object Object]`, zero błędu.

**T10.** Przy każdej migracji: **licznik rekordów per tabela** i checksum per ticker. Bilans bajtowy mierzy ILE, nie GDZIE.

**T11.** Przed każdym pomiarem mogącym zwrócić zero — **kontrolka dodatnia na tym samym typie wejścia**. Zielona kontrolka na innych danych jest kontrolką wadliwą, nawet gdy świeci.

**T12.** Cache dyskowy z TTL 12–24 h dla źródeł bez SLA. **Circuit breaker**: zero wierszy albo same NaN → nie zapisuj, zaloguj do `ingest_errors`, użyj poprzedniego dnia z flagą `is_stale`.

**T13.** Throttling: SEC ≤ 10 req/s (twardy, z nagłówkiem User-Agent zawierającym kontakt), yfinance ~1 req/s z jitterem (token bucket), NBP z chunkowaniem po 93 dni.

**T14.** Korekta splitów dla danych fundamentalnych **as reported** żyje w jednej funkcji, wołanej w jednym miejscu. Mnożnik przed i po splicie bez tej korekty to dwa różne byty.

### 4.2 O obliczeniach

**T15.** Ceny przechowywane jako `numeric`, nie `float8`. Precyzja finansowa.

**T16.** Wskaźniki wymagają **rozgrzewki**: minimum 300 barów dla wskaźników z wygładzaniem Wildera (RSI, ATR, ADX), 252 dla SMA200 i momentum 12-1. Wartości liczone na krótszej historii nie są publikowane — mają stan UNKNOWN.

**T17.** Metryki pochodne są **zawsze liczone, nigdy wpisywane**. Arkusz ręczny przechowuje **mianowniki** (net debt, liczba akcji, GP, EBIT, sales, TBV), nie mnożniki.

*Uzasadnienie T17 [S] — to jest naprawa błędu z rewizji 1:* mnożnik przepisany ze screenera ma wpieczoną cenę z dnia przepisania. Wyprzedaż −30% — dokładnie zdarzenie, które ma odpalić sygnał — nie rusza RVS aż do następnego wpisu kwartalnego. System pokazałby wtedy liczbę świeżą z wyglądu i martwą w środku.

**T18.** Runway liczony jest z **zabezpieczeniem znaku**: przy `burn ≤ 0` runway wynosi `∞`, nie liczbę ujemną. Naiwne `cash/burn` dla spółki FCF-dodatniej daje runway ujemny, warunek `runway > 8Q` fałszywy, i **META oraz MSFT dostają etykietę PUŁAPKA** — czyli najważniejszy komunikat systemu jest spamowany przez błąd znaku.

**T19.** Okno porównania marży jest **zadeklarowane** (YoY, nie QoQ). Przy sezonowości porównanie QoQ flaguje każdy pierwszy kwartał.

**T20.** Solver reverse DCF: `scipy.optimize.brentq` z bracketingiem `[-0.50, 1.00]`, **skanem siatki 200 punktów** i liczeniem zmian znaku. Więcej niż jedna zmiana → flaga `multiple_roots`, brak zmiany → flaga `no_solution` z kierunkiem. Brak rozwiązania jest **najsilniejszym możliwym sygnałem**, nie awarią.

**T21.** „Potwierdzenie sygnału" ma zdefiniowaną semantykę: **dwa kolejne przebiegi tygodniowe** (nie dwa warunki, nie dwa kwartały).

---

# CZĘŚĆ II — INFRASTRUKTURA

## 5. Hostinger: co jest realnie dostępne

Weryfikacja 2026-08-27 w dokumentacji Hostingera.

### 5.1 Hosting współdzielony — odpada jednoznacznie

| Pytanie | Odpowiedź | Status |
|---|---|---|
| PostgreSQL? | **NIE.** Cytat: *„Since PostgreSQL requires a larger amount of system resources and memory… VPS Hosting is the supported option"* | [Z] |
| Python? | **NIE, w ogóle.** Cytat: *„Root access is required… Python is supported exclusively on VPS Hosting"* — nie ma nawet CGI | [Z] |
| MySQL | tak, limit **3 GB na bazę** na wszystkich planach Web | [Z] |
| Cron | Single 2 zadania, Premium+ bez limitu; **minimalna granulacja nieudokumentowana** | [Z] / [N] |

Brak Pythona i brak Postgresa zamykają temat niezależnie od reszty limitów. **Hosting współdzielony nie jest opcją.**

### 5.2 VPS (KVM) — jedyna droga

| Plan | vCPU | RAM | NVMe | Transfer | Promo (24 mies.) | Odnowienie |
|---|---|---|---|---|---|---|
| KVM 1 | 1 | 4 GB | 50 GB | 4 TB | 23,99 zł/mies. | 51,99 zł |
| **KVM 2** | **2** | **8 GB** | **100 GB** | 8 TB | **34,99 zł/mies.** | **64,99 zł** |
| KVM 4 | 4 | 16 GB | 200 GB | 16 TB | 46,99 zł/mies. | 119,99 zł |

[Z] Pełny root we wszystkich planach. Szablon „Ubuntu 24.04 with Docker" ma preinstalowane `docker-ce` i `docker-compose`. W hPanel jest Docker Manager. Inodes na VPS: bez limitu.

**[Z] Backupy:** cotygodniowe automatyczne w cenie, retencja do czterech kopii. Dzienne = płatny upgrade (**cena niezweryfikowana**). **Snapshot: tylko jeden naraz, nadpisywany, automatycznie kasowany po dobie.** Godziny wykonywania backupu nie da się ustawić.

> **[S] To nie jest ochrona wystarczająca dla bazy finansowej.** Tygodniowy obraz maszyny oznacza w najgorszym razie utratę siedmiu dni danych, a snapshot to checkpoint na czas jednej operacji, nie kopia zapasowa. Własny `pg_dump` do zewnętrznej lokalizacji w cronie, codziennie — przy tej wielkości bazy dump trwa sekundy.

**[Z] Firewall:** zarządzany z hPanel, filtruje przed dotarciem do serwera, działa **niezależnie od** ufw/iptables (ruch musi przejść obie warstwy). Domyślnie wszystkie porty zamknięte; **pusty ruleset zablokuje cały ruch po przypisaniu**. [Z] Pomiar 2026-09-25 (P-07, zamknięty): VPS ma przypisaną grupę `frank-web`, aktywną — przepuszcza TCP 22/80/443, resztę odrzuca. Zachowanie dla IPv6 niezmierzone.

**Port 5432 nie będzie otwierany.** Usługa i baza siedzą na tej samej maszynie — połączenie przez `localhost` albo sieć wewnętrzną Dockera. Zero ekspozycji.

**[Z] Porty wychodzące:** zablokowany **25 (SMTP)** i port 0. Reszta otwarta. Alerty mailowe (jeśli kiedykolwiek) przez API na 443 albo SMTP 587/465, nigdy przez lokalny MTA.

**[Z] Transfer:** po przekroczeniu limitu **dławienie do 10 Mbps do końca miesiąca**, bez opłat i bez zawieszenia. [S] Przy odpytywaniu API giełdowych to rzędy megabajtów wobec 4–8 TB limitu — ryzyko zerowe.

### 5.3 Najważniejsze ryzyko: automatyczne dławienie CPU

**[Z]** Cytat z dokumentacji: *„Internal systems identify if a VPS sustains high CPU usage for longer period of time"* → *„the CPU capacity of the VPS is decreased automatically by **25% per hour**"* → po przekroczeniu limitów *„the system considers the VPS potentially compromised"*. Limit można zdjąć z panelu, ale **tylko raz w tygodniu**. Konkretny próg czasowy **nie jest podany** [N].

**[S] To jest główny argument przeciw KVM 1 w tym scenariuszu.** Przy jednym vCPU dzielonym przez n8n, NocoDB, Postgres i usługę Pythonową wystarczy jeden cięższy przebieg pandas nakładający się na workflow n8n, żeby utrzymać wysokie CPU. Kaskadowe dławienie −25%/godz. na maszynie jednordzeniowej degraduje się bardzo szybko, a odblokowanie jest raz na tydzień. **Skutek spadłby na produkcyjnego agenta, nie na eksperyment.**

Mitygacja niezależna od planu: ciężkie przeliczenia pod `nice`, rozłożenie cronów w czasie (nie o pełnych godzinach razem z n8n), `cpus:` i `mem_limit:` w docker-compose dla kontenera Mannaza.

### 5.4 Baza: PostgreSQL, bez rozszerzeń specjalistycznych

**Szacunek rozmiaru po 3 latach [S]** (252 sesje rocznie, ~200 instrumentów):

| Zbiór | Wiersze | Rozmiar z indeksami |
|---|---|---|
| OHLC dzienne | ~151 000 | ~25 MB |
| Fundamenty kwartalne | ~2 400 | ~3 MB |
| Snapshoty konsensusu tygodniowe | ~31 000 | ~8 MB |
| FX (8 par × 3 lata) | ~6 300 | ~1 MB |
| Metryki pochodne | ~150–300 tys. | ~40–60 MB |
| **Razem** | | **~80–150 MB** |

**To jest sedno wyboru bazy:** nawet przy hojnych założeniach nie przekroczysz 1 GB w trzy lata. Cały zbiór zmieści się w pamięci podręcznej Postgresa. **Dobór bazy nie jest decyzją wydajnościową, tylko operacyjną i integracyjną.**

| Rozszerzenie / opcja | Decyzja | Powód |
|---|---|---|
| `pg_stat_statements` | **włączyć od razu** | zero kosztu, ratuje przy diagnozie |
| `postgres_fdw` | opcjonalnie | podgląd danych między bazami bez duplikacji |
| `pg_cron` | opcjonalnie | zadanie żyje z bazą, przetrwa restart kontenera aplikacji |
| **TimescaleDB** | **nie** | [Z] Community Edition jest darmowa do self-hostingu (licencja TSL), hypertables są też w wersji Apache 2 — ale przewaga zaczyna się od dziesiątek milionów wierszy. Przy 150 tys. dokładasz zależność i migracje wersji za zero zysku |
| `pg_partman` | **nie** | partycjonowanie ma sens od dziesiątek milionów wierszy |
| `pgvector` | **nie** | nie dotyczy OHLC |
| **DuckDB jako baza główna** | **nie** | [Z] jeden proces może pisać; w trybie read-only wielu czyta, ale nikt nie pisze. Pipeline jest z definicji wieloprocesowy — to dyskwalifikuje. Sensowny jako **biblioteka analityczna** do backtestów i ciężkich agregacji ad hoc |
| **SQLite jako baza główna docelowo** | **nie** | ten sam problem jednego pisarza; słabsza obsługa dat i stref czasowych. **Ale jako baza wersji minimalnej — tak** (§25), przy identycznym schemacie |
| **Managed database u Hostingera** | **nie istnieje** | [Z] Strona `hostinger.com/vps/docker/postgresql` to szablon Dockera na zwykłym VPS, nie DBaaS. Brak HA, replikacji, automatycznych upgrade'ów |

### 5.5 Rekomendacja infrastrukturalna

**Rozstrzygnięte w rew. 3: VPS to KVM 2 (2 vCPU, 8 GB RAM, 100 GB) — upgrade nie jest potrzebny.** Szacunek RAM czterech usług (n8n ~300–500 MB, NocoDB ~300–500 MB, Postgres ~300–400 MB, Python ~150–300 MB, OS ~300–400 MB = ok. 1,5–2,1 GB) mieści się z zapasem, a argument z dławienia przy jednym vCPU nie dotyczy. Zapas przy kontenerze Mannaza mierzy P-09. KVM 4 nie jest potrzebny.

Konfiguracja: **osobny kontener PostgreSQL 17/18 dla Mannaza, własna rola**, `shared_buffers` 256–512 MB, `cpus:` i `mem_limit:` w docker-compose. **Nie** wspólna instancja z wewnętrznym Postgresem NocoDB Franka (`nocodb-fjht-nocodb-db-1`): oszczędność ~300 MB RAM jest przy 8 GB pomijalna, a wspólna instancja to wspólny cykl życia, restartów i backupu z produkcją Franka — ten sam blast radius, który wyklucza §6 pkt 2. Decyzja ownera 2026-09-25 (zastępuje rekomendację rew. 2).

## 6. Wybór stacku

**Decyzja: osobne repo `mannaz` w Pythonie, własna baza, na tym samym VPS, w kontenerze z limitem pamięci i CPU.**

Cztery argumenty, w kolejności wagi:

1. **Archiwum point-in-time jest rdzeniem wartości systemu** i wymaga migracji, testów i wymuszonej niezmienności. Logi egzekucji n8n z retencją tego nie dają.
2. **Blast radius.** Frank to żywy agent produkcyjny w trakcie sprintu; następna zaplanowana sesja wymaga jawnie dostępu do n8n i NocoDB.
3. **Charakter obliczeń.** Regresje, mediany harmoniczne, solver Brenta i macierze kowariancji w węzłach Code n8n są nietestowalne.
4. **Zmierzone defekty warstwy NocoDB** — ciche gubienie `sort`, typ JSON kolumny `Summary`, rozbieżność między tym, co zwraca REST, a tym, co węzeł podaje następnemu w tej samej egzekucji.

**NocoDB pozostaje — jako podgląd tylko do odczytu nad Postgresem Mannaza.** Sprzężenie jednokierunkowe i słabe: NocoDB pada → tracisz widok, nie dane.

> **Twarda reguła:** NocoDB służy do **oglądania wierszy — nigdy do sortowania, agregowania ani karmienia kolejnego kroku**. „Top 10 po RVS" odczytane z ekranu NocoDB jest niewiarygodne.

## 7. Reuse z projektu Frank

### 7.1 Doświadczenia pozytywne — bierzemy

| Element | Sposób przeniesienia |
|---|---|
| **Wzorzec wrappera crona** z `/opt/frank-monitor`: `started_at`, `duration_sec`, `host`, budżet czasowy, trigger przy przekroczeniu progu | **kopiujemy, nie współdzielimy** |
| **Wzorzec bramek**: `Assert-Gate`, tally per result, kontrolka dodatnia obok ujemnej, mutanty rozdzielające bramkę długości od bramki tożsamości | wzorzec przenosimy do kryteriów akceptacji (§25) |
| **Rejestr anty-wzorców (F)** i dyscyplina „przed dopisaniem nowej pozycji przeczytaj sąsiednie" | analogiczny rejestr od pierwszego dnia |
| **Metoda weryfikacji**: predykcja liczbowa przed pomiarem bez hedge'y, kontrolka dodatnia, census jako zbiór a nie licznik, deklarowanie konwencji przy każdej liczbie | wchodzi jako M i T w §3–4 |
| **Zmierzona anomalia czasowa**: przebiegi o 06:00 UTC trwają ~91–97 s, wymuszony o 09:56 UTC — 25,1 s, mechanizm nieznany | **ostrzeżenie, nie założenie** — budżet czasowy mierzyć o docelowej godzinie (P-08) |
| **Doświadczenie z kalibracją progów**: w Franku koniunkcja trigger ∧ filtr wolumenowy ∧ struktura dała zero sygnałów przez jedenaście tygodni przy każdym warunku wyglądającym rozsądnie osobno | **bezpośrednia przyczyna M13 i M16** — zbyt koniunkcyjne bramki produkują ciszę nieodróżnialną od braku okazji |

### 7.2 Wady Franka, których nie powtarzamy

To jest najcenniejsza część reuse — obserwacje z 90-pozycyjnego backlogu, które są **przyczyną** konkretnych decyzji w tym dokumencie:

| Obserwacja z Franka | Konsekwencja dla Mannaza |
|---|---|
| **Backlog urósł 67 → 90 w kilka tygodni**, przyrost w 100% skorelowany z sesjami recon, **zero dodanych pozycji produktowych**, proporcja 27% produkt / 73% maszyneria i otoczenie | Twardy limit: **każda sesja recon musi zamknąć co najmniej tyle pozycji, ile otwiera.** Rejestr z polem `status` od początku (w Franku go nie ma — zamknięcie = usunięcie wpisu, co nie zostawia śladu w SSOT) |
| **Pomiar skuteczności sygnałów nie istnieje nigdzie jako zaplanowana praca** — grep po trafność/hit rate/backtest/TP-SL/PnL w całej roadmapie dał zero | **Dziennik decyzji i benchmark zastępczy są w zakresie wersji minimalnej**, nie w fazie 2 |
| **`Decisions` nie jest zasilana od 2026-04-29**; `Create decision` wykonał się 0/12 razy; persystowane są tylko werdykty BUY, telemetria nie widzi reszty | **Persystencja wszystkich stanów, nie tylko tych, które generują akcję.** Stan HOLD jest tak samo zapisywany jak ADD |
| **`relay.sent:false` na wszystkich rekordach** — ścieżka alertu Slack niećwiczona od 2026-06-22 | Ścieżka alertowa jest **ćwiczona od pierwszego dnia** i ma dead-man switch |
| **`BL-FRANKVERSION-NOT-A-VERSION`** — 9365 różnych wartości pola wersji na 9365 rekordów, więc nie da się przypisać snapshotów do wersji promptu | Każdy zapis metryki pochodnej niesie **wersję kodu, który ją policzył** (git SHA), nie znacznik czasu |
| **`CandleSignalW1` pusta 9365/9365** przy `CandleSignalD1` wypełnionej w 100% | Kolumna, która nigdy nie została zapisana, jest **defektem wykrywanym przez bramkę pokrycia**, nie odkrywanym po roku |
| **Zrzuty egzekucji ~101 MB niosą definicję workflow** — obecność nazwy węzła nie dowodzi wykonania | Logi wykonania są **strukturalne i małe**; fakt wykonania kroku jest osobnym rekordem |
| **Anti-pattern „UI Save overwrite"** — otwarte UI podczas operacji PUT nadpisuje zmiany | Brak UI z prawem zapisu w ścieżce krytycznej (§6) |
| **Reguła „regen ostatni" powoduje, że runda nie może zapisać tego, czego się w niej nauczyła** — siedem wystąpień jednego dnia, przez co parking rośnie szybciej, niż się go opróżnia | Dokumentacja stanu generowana **przed** zamknięciem rundy, nie po |
| **`docs/log.md` bez wpisów od 2026-07-20 przy commitach do 2026-08-05** | Log jest generowany z historii, nie pisany ręcznie |

### 7.3 Czego nie współdzielimy i dlaczego

**Feed cenowy z n8n — nie.** Trzy powody:

1. **Różna konwencja korekty.** Frank robi Price Action, VSA i Fibonacciego na D1 — ta analiza potrzebuje ceny, którą ludzie faktycznie widzieli. Mannaz potrzebuje total return do momentum i split-only do stopów (T6). **Te same tickery pod różnymi konwencjami** — reuse feedu wstrzykuje konwencję Franka w ranking momentum Mannaza, czyli błąd cichy wprowadzony architekturą.
2. **Małe przecięcie.** Frank: 113 tickerów GPW + US + krypto. Mannaz: ~180 instrumentów z ośmiu giełd.
3. **[N] Nie jest zweryfikowane, jakie w ogóle jest źródło cen Franka.** Notatki projektowe rejestrują pipeline, ID workflow, tabele NocoDB i 61 kolumn `TA_Snapshots`, ale **nie nazywają źródła cen**. Pomiar P-01.

**NocoDB jako magazyn — nie.** Brak możliwości wyegzekwowania append-only, brak migracji, wspólny cykl backupu z danymi produkcyjnymi, defekty na ścieżce odczytu i zapisu.

**n8n jako transport alertów regułowych — nie.** Każdy hop to miejsce, w którym alert zostaje policzony poprawnie i nie dociera, a brak alertu jest nieodróżnialny od braku zdarzenia.

**n8n do narracji kwartalnych — tak.** Tolerancyjne na opóźnienie, a n8n daje realną przewagę: gotowe wpięcie modelu językowego i szablonowanie.

### 7.4 Reuse w drugą stronę

Dwa elementy Mannaza są odpowiedzią na otwarte pozycje Franka i powinny powstać najpierw tutaj, gdzie stawka jest niższa:

- **Sprawdzona ścieżka alertowa Slack** → zamyka `BL-RELAY-PATH-UNEXERCISED`.
- **Dziennik decyzji z predykcją przed pomiarem** → instrument dla `BL-SIGNAL-EFFECTIVENESS-UNMEASURED`.

## 8. Model danych

### 8.1 Warstwy

```
raw_observations     append-only, trigger blokujący UPDATE/DELETE
    │                surowe odpowiedzi źródeł, z prowenienecją
    ▼
canonical_*          znormalizowane: jedna taksonomia, jedna waluta, jedna konwencja
    │                (prices, fundamentals, estimates, fx)
    ▼
derived_metrics      PRZELICZALNE, wersjonowane git SHA kodu liczącego
    │                (multiples, rvs, technicals, reverse_dcf)
    ▼
states               VALUE / QUALITY / TREND / RISK per instrument per data
    │
    ▼
decisions            propozycje systemu + decyzje właściciela + wynik
```

### 8.2 Tabele

| Tabela | Ziarno | Kluczowe pola | Zapis |
|---|---|---|---|
| `instruments` | instrument | ticker_local, isin, exchange (MIC), **currency**, archetype, base_ccy_reporting, first_listed | ręczny |
| `positions` | instrument × data | qty, **cost_basis z historii transakcji**, target_weight, weight_band, conviction_floor, thesis_id | ręczny/import |
| `transactions` | zdarzenie | data, instrument, qty, cena, prowizja, **kurs FX z datą i typem fixingu** | ręczny/import |
| `prices_eod` | instrument × data × źródło | ohlcv, **currency**, **adjustment_convention**, source, fetched_at, is_stale | append |
| `corporate_actions` | instrument × data | typ (split/dywidenda/prawo poboru/scalenie), współczynnik, ex_date, source | append |
| `fx_rates` | para × data × źródło | kurs, **fixing_id** (nbp_a / ecb_ref), source, fetched_at | append |
| `fundamentals` | instrument × okres × **filed_at** | revenue, gross_profit, ebit, ebitda, fcf, sbc, shares_diluted, tbv, nopat, capex, wc, net_debt, **taxonomy**, **currency**, form_type, **available_at** | append |
| `estimates` | instrument × data pobrania | eps_ntm, rev_ntm, target_mean, **currency**, quality_flag | append |
| `peer_groups` | grupa | instrument, layer (core/statistical), **valid_from**, **valid_to**, **reason** | wersjonowany |
| `derived_metrics` | instrument × data × metryka | wartość, **code_version (git SHA)**, inputs_hash | przeliczalne |
| `states` | instrument × data | value_state, quality_state, trend_state, risk_state, **data_quality**, powody | przeliczalne |
| `thesis` | instrument | teza, 2–3 **kryteria falsyfikacji**, horyzont, benchmark zastępczy, data zapisu | ręczny |
| `decisions` | zdarzenie | propozycja systemu, decyzja właściciela, **predykcja i pewność przed**, uzasadnienie, wynik po horyzoncie | ręczny |
| `ingest_errors` | zdarzenie | źródło, instrument, typ błędu, treść, czy zdegradował stan | append |
| `run_log` | przebieg | started_at, duration_sec, host, code_version, pokrycie (n_ok/n_total), werdykt | append |

### 8.3 Indeksy i ograniczenia

- `prices_eod`: unikalny `(instrument_id, date, source, adjustment_convention)`; indeks `(instrument_id, date)`
- `fundamentals`: unikalny `(instrument_id, period_end, filed_at, taxonomy)`
- `fx_rates`: unikalny `(pair, date, fixing_id)`
- `derived_metrics`: unikalny `(instrument_id, date, metric, code_version)`
- Trigger `BEFORE UPDATE OR DELETE` z `RAISE EXCEPTION` na wszystkich tabelach append-only
- Migracje przez Alembic, nigdy ręcznie

---

# CZĘŚĆ III — WARSTWA DANYCH

## 9. Źródła per rynek

### 9.1 Zmiana wobec rewizji 1: Massive odpada

**[Z] Massive (dawniej Polygon.io) obsługuje wyłącznie giełdy USA.** Potwierdzone dwiema stronami dostawcy: cennik opisuje wszystkie plany jako „All US Stocks Tickers", dokumentacja mówi o *„comprehensive suite of **U.S.** stock market data"* i wymienia wyłącznie giełdy amerykańskie. Zero pokrycia międzynarodowego.

Do tego plan darmowy daje **2 lata historii** — za mało na jednoczesne SMA200, momentum 12-1 i trzyletnią medianę RVS.

**Wniosek:** rekomendacja z rundy 3 (Massive jako kręgosłup cenowy) **jest nieważna dla portfela wielorynkowego**. Zostaje jako opcjonalne, wysokiej jakości źródło dla części amerykańskiej — patrz O-11.

### 9.2 Zmiana wobec rewizji 1: Stooq się zdegradował

**[Z2] Od marca 2026 Stooq deklaruje klucz API**; od czerwca 2026 stosuje **site-wide JavaScript proof-of-work**. Według źródeł wtórnych klucz jest darmowy (CAPTCHA, bez rejestracji), a raportowane są awarie kluczy „due to IP or usage volume". Dzienny limit istniał od dawna, konkretna liczba nigdy nie została opublikowana.

**Pomiar 2026-09-25 [Z]** (stooq.pl: PKN; stooq.com: AAPL.US):
- żądanie skryptowe (`curl.exe` z komputera ownera, bez klucza) na `q/d/l/?s=pkn&i=d` zwraca `200 text/html`, 796 znaków — stronę proof-of-work (SHA-256, prefiks 4 zer hex, weryfikacja przez `/__verify`), nie CSV;
- przeglądarka pobiera CSV bez klucza i bez CAPTCHA, także w czystej sesji incognito;
- ścieżka `?get_apikey` nie wyświetla formularza CAPTCHA (formularz istnieje w kodzie strony, ukryty) — klucza nie da się uzyskać.

Automatyczne rozwiązywanie proof-of-work w importerze jest obejściem zabezpieczenia antybotowego serwisu i nie wchodzi w grę.

**Wniosek:** zapis z rewizji 1 „Stooq — fallback na ceny, niezależny od Yahoo" jest **nieaktualny**. Stooq jest **źródłem ręcznym** — pobieranym z przeglądarki do kontroli punktowych — i nie jest dostępny dla importera przy zerowym budżecie.

**Stooq zachowuje jednak jedną unikalną przewagę [Z2]:** parametr `o=` to **maska korekt korporacyjnych** — siedem znaków sterujących osobno splitem, dywidendami, prawami poboru, denominacją. To jedyne darmowe źródło dające tę kontrolę, co przy GPW (gdzie prawa poboru są częste) ma realną wartość. **Kierunek flagi wymaga weryfikacji empirycznej na spółce ze znanym splitem** (P-02, wykonalny ręcznie w przeglądarce) — pomyłka zatruwa cały szereg.

### 9.3 Matryca źródeł

| Warstwa | Źródło | Rola | Status |
|---|---|---|---|
| **Ceny EOD, wszystkie rynki** | **yfinance** | jedyne realne źródło jednolite | [Z] sufiksy |
| Ceny — kontrola | Stooq, pobieranie ręczne z przeglądarki (US, PL, UK, JP, HK, DE) | kontrola punktowa, poza importerem | [Z] pomiar 2026-09-25 (§9.2) |
| Ceny — opcjonalnie USA | Massive Stocks Basic | wysoka jakość, tylko USA | [Z] |
| Kalendarze sesyjne | `exchange_calendars` 4.13.2, Apache-2.0 | 62 kalendarze, w tym XWAR | [Z] |
| **FX podstawa** | **NBP API tabela A** | wszystko → PLN, kurs urzędowy | [Z] |
| **FX kontrola** | Frankfurter `api.frankfurter.dev/v1/` | niezależny, bez klucza, bez limitu | [Z] na żywo |
| Fundamenty USA | SEC EDGAR XBRL | jedyne prawdziwe źródło | [Z] |
| Fundamenty GPW — backfill | filings.xbrl.org, FY2020–FY2023 | jednorazowo | [Z] na żywo |
| Fundamenty GPW — bieżące | **arkusz ręczny, kwartalnie** | — | — |
| Dywidendy GPW | bankier.pl/gielda/dywidendy | kontrola korekty | [Z] historia od 2010 |
| Splity GPW | gpw.pl/komunikaty | kontrola korekty | [Z] |
| ERP | Damodaran `ERPbymonth.xlsx` | **miesięcznie** | [Z] |
| Bety, WACC sektorowy, marże | Damodaran, styczeń | rocznie | [Z] |
| Stopa wolna od ryzyka | FRED `DGS10` + odpowiedniki lokalne | | [Z] |

**Odrzucone po weryfikacji:** Twelve Data (3 giełdy na planie darmowym), EODHD (20 wywołań/dzień, rok historii), Alpha Vantage (25/dzień), FMP (kalendarz i peers płatne, Basic ograniczony do próbki symboli), Tiingo (fundamenty to płatny dodatek), exchangerate.host (100 zapytań/miesiąc), Wise (wymaga tokenu konta), investpy (martwe), FRED jako główne FX (brak dziennego PLN).

### 9.4 Sufiksy giełd [Z] — z oficjalnej listy Yahoo

| Giełda | Sufiks | Opóźnienie | Waluta notowania |
|---|---|---|---|
| GPW Warszawa | `.WA` | 15 min | PLN |
| Deutsche Börse XETRA | `.DE` | 15 min | EUR |
| Euronext Amsterdam | `.AS` | 15 min | EUR |
| SIX Swiss Exchange | `.SW` | 30 min | CHF |
| London Stock Exchange | `.L` | 20 min | **GBp (pensy)** |
| HKEX | `.HK` | 15 min | HKD |
| Tokyo Stock Exchange | `.T` | 20 min | JPY |
| Nasdaq OMX Stockholm | `.ST` | czas rzeczywisty | SEK |

**[Z] Pułapka GBX/GBP jest realna i udokumentowana przez autorów yfinance:** *„Sometimes Yahoo mixes up currencies e.g. $/cents or £/pence. So some prices are 100x wrong"* — z zastrzeżeniem, że błędy bywają rozsiane losowo albo w bloku, *„because Yahoo decided one day to permanently switch currency"*. Potwierdzone na konkretnym przypadku (EasyJet: ceny 612,20 przez trzy dni, potem 5–6).

`repair=True` jest **domyślnie wyłączony** i sam ma fałszywe pozytywy. Zabezpieczenie po stronie systemu — T7 i T8.

**[Z] Ostrzeżenie wprost z dokumentacji yfinance:** *„Only US market data appears perfect"*. To zdanie stoi w dokumentacji projektu, którego używamy jako głównego źródła cen dla wszystkich rynków spoza USA. Nie da się tego obejść przy zerowym budżecie — da się to **zmierzyć i oflagować**.

### 9.5 Fundamenty GPW — najtrudniejszy punkt całego projektu

**Obowiązek raportowania istnieje i jest szeroki [Z]:** ESEF (XHTML + Inline XBRL) obowiązuje emitentów rynku regulowanego UE, czyli cały Główny Rynek GPW, od lat obrotowych rozpoczynających się po 1 stycznia 2020. Od 2020 szczegółowe znaczniki dla RZiS, bilansu i przepływów; od 2022 dodatkowo block tagging not. NewConnect nie jest objęty.

**Ale zasilanie publicznego repozytorium dla Polski jest martwe [Z, zweryfikowane na żywo]:**

| Zapytanie do `filings.xbrl.org/api/filings` | Wynik |
|---|---|
| `filter[country]=PL` | **877 zgłoszeń** |
| `filter[country]=PL&sort=-date_added` | najnowsze **`date_added` = 2024-07-30** |
| `sort=-date_added` (wszystkie kraje) | najnowsze **2026-08-04** |

Indeks żyje. Polski feed nie. Źródłem dla PL jest PAP BIZNES, co zbiega się czasowo z wyłączeniem serwisu Infostrefa.com pod koniec sierpnia 2024 — **korelacja, nie potwierdzona przyczyna**.

Pozostałe kanały:

| Kanał | Status |
|---|---|
| ESPI przez `biznes.pap.pl` | [Z] za **Incapsulą** — automatyzacja to walka z WAF |
| `gpw.pl/komunikaty` | [Z] darmowe, filtrowalne, stabilny URL. **[N] Niezweryfikowane, czy paczka ESEF jest stąd pobieralna** |
| GPW „Dane przetworzone i wskaźniki" | [Z] **płatne** |
| Biznesradar | [Z] RZiS, bilans, przepływy, **historia 2009–2026**, darmowe — ale HTML bez eksportu i bez API, regulamin nie autoryzuje scrapingu |
| KRS Open API | [Z] darmowe, JSON, bez klucza — ale **wyłącznie dane rejestrowe**, bez sprawozdań |
| RDF (przeglądarka dokumentów finansowych KRS) | [Z] bezpłatne przez WWW, **brak publicznego API** |
| ESAP (europejski punkt dostępu) | [Z] **otwarcie 10 lipca 2027** |

**Wniosek [S]: fundamentów GPW nie da się zautomatyzować w sposób, na którym można oprzeć archiwum point-in-time.** Zostaje wpis ręczny — co jest zbieżne z wersją minimalną i nie jest obejściem, tylko właściwą kolejnością.

**Jedno działanie pilne:** jednorazowy backfill z `filings.xbrl.org` za FY2020–FY2023 dla ok. 150 polskich emitentów. Regulamin serwisu wprost zastrzega prawo do wycofania API. To jedyne darmowe, czyste XBRL dla polskich spółek, jakie istnieje, i daje historyczną głębokość mnożników nieodtwarzalną później. Biblioteka `xbrl-filings-api` istnieje i jest gotowa.

## 10. Wielowalutowość, FX i konwencje

### 10.1 Źródła FX

**Podstawa: NBP API, tabela A [Z]**
```
https://api.nbp.pl/api/exchangerates/rates/a/{code}/{from}/{to}/?format=json
```
| Parametr | Wartość |
|---|---|
| Historia | od 2 stycznia 2002 |
| Max okres na zapytanie | **93 dni** — obowiązkowe chunkowanie |
| Limit zapytań | nieujawniony; przekroczenie → `400 Przekroczony limit` |
| HTTPS | **obowiązkowe od 1 sierpnia 2025** |
| Klucz | brak |

**Harmonogram publikacji [Z]:** tabela A — każdy dzień roboczy **11:45–12:15**; tabela B (waluty mniej płynne) — **każda środa**; tabela C (kupno/sprzedaż) — każdy dzień roboczy 7:45–8:15.

> **Pomiar krytyczny (P-03):** skład tabel A i B nie jest zweryfikowany. **Jeśli HKD jest w tabeli B, kurs dostajesz raz w tygodniu**, co dla dziennego przeliczania pozycji z Hongkongu jest dyskwalifikujące. Sprawdzenie to jedno wywołanie: `api.nbp.pl/api/exchangerates/rates/a/hkd/?format=json` — 404 oznacza tabelę B. W tym wypadku Frankfurter staje się podstawą dla HKD, z jawnym zapisem, że dwa szeregi mają różny fixing.

**Kontrola: Frankfurter [Z, zweryfikowane na żywo]**
```
https://api.frankfurter.dev/v1/{from}..{to}?base=EUR&symbols=PLN,USD,CHF,GBP,SEK,DKK,JPY,HKD
```
Bez klucza, bez limitów dziennych ani miesięcznych, darmowe także komercyjnie. Zwraca kalendarz TARGET (weekendy i święta pominięte — to zachowanie ECB, nie błąd). Stary host `api.frankfurter.app` przekierowuje 302 na `.dev/v1/`.

### 10.2 Problem, którego nie da się rozwiązać, tylko zadeklarować

**Żaden fixing nie jest równoczesny z zamknięciem giełdy.**

| Moment | CET (zima) |
|---|---|
| Zamknięcie Tokio | ~07:00 |
| Zamknięcie HKEX | ~09:00 |
| **NBP tabela A — publikacja** | **11:45–12:15** |
| **ECB — publikacja** | **~16:00** |
| Zamknięcie GPW | 17:00 |
| Zamknięcie Xetra / Amsterdam / SIX / Sztokholm | 17:30 |
| Zamknięcie LSE | ~17:30 |
| **Zamknięcie NYSE / Nasdaq** | **22:00** |

Kurs zamknięcia pozycji amerykańskiej przeliczasz kursem sprzed jej sesji — różnica ok. 10 godzin. Dla HKEX i TSE jest odwrotnie i lepiej: zamykają się przed fixingiem.

**T22.** System **wybiera jeden fixing i stosuje go konsekwentnie** do wszystkich rynków i wszystkich okresów; identyfikator fixingu jest zapisywany przy każdym wierszu FX. Mieszanie fixingów między szeregami albo między okresami jest jedynym wariantem, który naprawdę psuje wyniki.

**T23.** Do przeliczeń portfelowych i sizingu używa się **NBP tabeli A z dnia D**. Wskaźniki cenowe (M2) w ogóle nie dotykają FX.

**T24.** Atrybucja wyniku rozdziela komponent akcyjny (zmiana ceny w walucie notowania) od walutowego (zmiana kursu przy stałej cenie) — zgodnie z M3.

## 11. Kalendarze, strefy czasowe, synchronizacja

**T25.** Kalendarze sesyjne z `exchange_calendars` [Z]: wersja 4.13.2 (marzec 2026), licencja **Apache-2.0**, 62 kalendarze. Potwierdzone potrzebne: XWAR, XETR, XAMS, XSWX, XLON, XHKG, XTKS, XSTO, XNYS.

> [Z] Zastrzeżenie dokumentacji: *„All of the exchange calendars are maintained by user contributions"* — brak gwarancji poprawności. **Weryfikacja pozytywna przed użyciem (P-04):** przepuścić rzeczywiste daty sesyjne z pobranych szeregów przez `XWAR.sessions_in_range()` i policzyć różnicę symetryczną. Kalendarz twierdzący, że była sesja, dla której nikt nie ma danych, jest błędny albo dane mają dziurę — i jedno od drugiego trzeba odróżnić.

*Odrzucone: `pandas_market_calendars` — od wersji 2.0 lustrzanie odbija kalendarze z `exchange_calendars`, czyli jest warstwą pośrednią bez własnego źródła prawdy (ten sam wzorzec, który odrzucono przy OpenBB), a jego ostatnie wydanie jest o czternaście miesięcy starsze.*

**T26.** Siatka wspólna, gdy jest potrzebna: **suma kalendarzy, nie przecięcie**. Przecięcie dziewięciu giełd wycina kilkanaście procent dni i tworzy nierówne odstępy, na których potem liczy się zwroty tak, jakby odstępy były równe.

**T27.** Forward-fill maksymalnie 1–2 dni, wyłącznie dla rynku faktycznie zamkniętego, **zawsze z flagą `is_stale`**. Nigdy w przód poza ostatnią realną sesję — to jest mechanizm, przez który system rysuje płaski szereg i cicho przestaje generować alerty.

**T28.** Dead-man switch sprawdza **wiek najświeższego wiersza per rynek**, nie sam fakt przebiegu.

## 12. Procedura rejestracji nowego instrumentu

Dodanie spółki jest operacją rutynową. Checklista — każdy punkt musi być wypełniony przed pierwszym przebiegiem z tym instrumentem:

| # | Pole / czynność | Uwagi |
|---|---|---|
| 1 | `ticker_local`, `isin`, `exchange` (kod MIC) | MIC, nie nazwa potoczna |
| 2 | **`currency`** i asercja wobec waluty giełdy | dla `.L` oczekiwane GBp, nie GBP |
| 3 | Sufiks źródła cenowego per dostawca | Yahoo `.WA` ≠ Stooq (bez sufiksu na stooq.pl); stooq.com: `pkn.pl` nie istnieje (pomiar 2026-09-25), zapis GPW do ustalenia — P-12 |
| 4 | **`archetype`** — klucz §14.1 proponuje, rejestracja decyduje (M76) | zapisywane: `archetype_key` (wynik klucza z odpowiedziami na pytania 1–9), `archetype` (decyzja), `archetype_secondary`, przy rozbieżności `archetype_override_reason`; determinuje mnożnik, bramkę jakości i model absolutny |
| 5 | **Grupa peer core (4–6)** z uzasadnieniem i datą `valid_from` | §15 |
| 6 | **Benchmark zastępczy** — instrument dający tę samą ekspozycję prościej | §21.4 |
| 7 | **Teza + 2–3 kryteria falsyfikacji**, zapisane przed pierwszym sygnałem | §20.4 |
| 8 | `target_weight` i pasmo | §20 |
| 9 | Źródło fundamentów i kadencja (SEC / arkusz / XBRL PL) | |
| 10 | Backfill ≥ 300 sesji + weryfikacja pokrycia | T16 |
| 11 | Kontrolka dodatnia: jedna znana wartość historyczna zgodna z niezależnym odczytem | T11 |
| 12 | Pierwszy przebieg w trybie `shadow` — instrument nie generuje alertów przez 2 tygodnie | |

**T29.** Instrument bez kompletu 1–11 ma stan `UNKNOWN` we wszystkich czterech wymiarach i **nie wchodzi do statystyk portfelowych**.

**M76.** Klucz §14.1 **proponuje** archetyp, rejestracja (pole `archetype`) **decyduje**. Rozbieżność klucz ≠ rejestracja jest dozwolona wyłącznie z flagą i pisemnym uzasadnieniem (`archetype_override_reason`) i jest wykazywana przy każdej kalibracji klucza.

## 13. Jakość danych i degradacja

**T30.** Jakość danych jest **oceną porządkową A/B/C z jawnymi kryteriami i zapisanymi powodami**, nie liczbą 0–100.

| Ocena | Kryterium |
|---|---|
| **A** | Fundamenty z SEC XBRL albo ESEF; cena z dwóch źródeł zgodnych <0,5%; waluta zweryfikowana; okres kwartalny potwierdzony |
| **B** | Fundamenty z jednego źródła bez audytu (yfinance) albo wpis ręczny z prowenienecją; cena z jednego źródła; waluta zweryfikowana |
| **C** | Braki w którymkolwiek z powyższych; dane starsze niż jeden okres raportowy; szereg w kwarantannie |

*Uzasadnienie odrzucenia skali 0–100 [S]: addytywny score z wagami +30/+20/+15 sugeruje precyzję, której konstrukcja nie ma — różnica 61 vs 48 nie znaczy nic. Trzy stopnie z jawnymi kryteriami są uczciwsze i wystarczają do bramkowania.*

**T31.** Sygnał ADD wymaga jakości **A lub B**. Przy jakości C system raportuje stan, ale nie proponuje działania zwiększającego pozycję. Reguły ryzyka działają przy każdej jakości — wyciszać można okazję, nigdy stop.

**T32.** Kryterium akceptacji „dwa niezależne źródła cen" **jest przeformułowane per rynek**, bo dla Amsterdamu, Zurychu i Sztokholmu jest przy zerowym budżecie nieosiągalne (Yahoo je pokrywa, Stooq — [N] niepotwierdzone, reszta nie). Dla tych rynków: jedno źródło z jawną flagą jakości B.

---

# CZĘŚĆ IV — MODEL ANALITYCZNY

## 14. Archetypy wyceny

Każdy instrument satelity ma dokładnie jeden archetyp **główny**, zaproponowany kluczem §14.1 i zatwierdzony przy rejestracji (§12, M76). Przy dwóch segmentach o porównywalnej wadze obowiązuje archetyp segmentu z większym zyskiem brutto, a drugi segment trafia do pola `archetype_secondary` (tylko do bramki jakości, nie do mnożnika). Archetyp determinuje **trzy rzeczy naraz**: mnożnik główny, bramkę jakości i model wyceny absolutnej.

| # | Archetyp | Mnożnik główny | Zmienna towarzysząca | Bramka jakości (M15) | Model absolutny |
|---|---|---|---|---|---|
| A1 | Mega-cap zyskowna | EV/EBIT, fwd P/E | ROIC − WACC | rewizje NTM, marża EBIT YoY, dilution | reverse DCF na CAGR |
| A2 | SaaS zyskowna gotówkowo | EV/NTM Sales | Rule of X | NRR, SBC/przychód, FCF margin, rewizje | reverse DCF na CAGR |
| A3 | SaaS GAAP-ujemna | EV/NTM Sales, EV/ARR | marża brutto + wzrost | ARR growth, NRR, **dilution %/rok**, runway (T18) | reverse DCF na **marży docelowej** |
| A4 | Marketplace / platforma | EV/Gross Profit | wzrost GP | wzrost GP, take-rate, marża wkładu | reverse DCF na CAGR |
| A5 | Fintech pożyczkowy | P/TBV | ROTE | **NCO %**, ROTE, koszt finansowania, współczynnik kapitałowy | **excess return, forma zamknięta** |
| A6 | Płatności / acquiring | EV/Gross Profit | TPV × take-rate | wzrost TPV, take-rate, koncentracja klientów | reverse DCF na CAGR |
| A7 | Zarządzający aktywami | P/FRE + SOTP | wzrost FRE | wzrost FRE, AUM płatne, realizacje carry | SOTP, nie DCF |
| A8 | Krypto-bilansowe | mNAV / EV do NAV | koszt jednostkowy | koszt/BTC, hashrate, **dilution**, wrażliwość na cenę aktywa | **benchmark zastępczy**, nie DCF |
| A9 | Capex-heavy / przemysł | EV/EBIT, EV/NTM Sales | marża brutto, backlog | backlog, book-to-bill, marża brutto, cash burn | reverse DCF na marży |

**Czego nie używać, per archetyp:** A3 i A4 — P/E i EV/EBITDA; A5 — EV/EBITDA i EV/Sales (EV nie ma sensu, gdy dług jest surowcem); A7 — GAAP P/E (carry i konsolidacja czynią go bez treści); A8 — P/E i P/B (po ASU 2023-08 zysk jest funkcją ceny aktywa, nie operacji); A9 — samo EV/EBITDA (ukrywa zużycie majątku).

### 14.1 Klucz przypisania archetypu

Pytania zadaje się w podanej kolejności; pierwsze „tak" rozstrzyga. Dla konglomeratu odpowiedź dotyczy segmentu dominującego (T37). Gdy żaden segment nie dominuje, rozstrzyga segment z większym zyskiem brutto, a drugi trafia do `archetype_secondary`.

1. Kontrakt terminowy, ETF, fundusz albo certyfikat **w satelicie**? → **A0**. Instrumenty rdzenia są poza §14.
2. Aktywa krypto na bilansie są główną ekspozycją? → **A8**.
3. Wynik zależy od marży odsetkowej i strat kredytowych na własnej książce? → **A5**.
4. Przychód to opłaty od zarządzanych aktywów (FRE) i carry? → **A7**.
5. Przychód = wolumen płatności × take-rate, bez własnej książki? → **A6**.
6. Przychód = GMV × take-rate, bez własnego zapasu? → **A4**.
7. Oprogramowanie (SaaS, licencja z maintenance, platforma komunikacyjna CPaaS) z przychodem powtarzalnym co najmniej na progu T35? Do przychodu powtarzalnego wlicza się subskrypcję, ARR i przychód użyciowy na umowach. Subskrypcja treści albo usług niebędących oprogramowaniem nie spełnia tego pytania. → EBIT < 0: **A3**, inaczej **A2**.
8. Kapitalizacja co najmniej na progu T34 i EBIT > 0 w trzech ostatnich latach obrotowych? → **A1**.
9. Capex/przychód powyżej progu T36 **albo** backlog, portfel zamówień, order intake lub book-to-bill raportowane jako KPI? RPO się nie liczy. → **A9**.
10. Żadne → **A0**.

**EBIT** w kluczu to Operating Income według GAAP/MSSF (w yfinance pole `Operating Income`), a nie pole `EBIT` dostawcy.

**A0 — bez modelu wyceny.** Alerty wycenowe wyłączone; technika (§18) i ryzyko (§19) działają.

**Kalibracja 2026-09-26.** Klucz odtwarza 17 z 20 przypadków jednoznacznych z testu na składzie satelity; predykcja ≥ 18/20 chybiona. Rozbieżne: ACMR i MKSI (klucz A0) oraz IFX (klucz A1). Rejestracja nadaje im A9 z uzasadnieniem „cykliczny półprzewodnik — bramka book-to-bill / zamówienia" (M76). Niezależność kalibracji jest częściowa: recenzent stosujący klucz widział tabelę §15.1 z archetypami.

**T34.** Próg kapitalizacji w pytaniu 8: ≥ 30 mld USD dla spółek z USA, Europy i Kanady; ≥ 5 mld PLN dla GPW. [S] Przegląd: następna kalibracja klucza.

**T35.** Próg przychodu powtarzalnego w pytaniu 7: ≥ 70 % przychodu ostatniego roku obrotowego. [S] Przegląd: następna kalibracja klucza.

**T36.** Próg capex w pytaniu 9: capex/przychód > 10 % w ostatnim roku obrotowym (yfinance: `Capital Expenditure` / `Total Revenue`). [S] Przegląd: następna kalibracja klucza.

**T37.** Segment dominujący konglomeratu: > ~60 % przychodu albo zysku. [S] Przegląd: następna kalibracja klucza.

## 15. Grupy peer

**M32.** Dwie warstwy: **core (4–6 spółek, ręcznie, po modelu biznesowym)** i **statystyczna (10–15, algorytmicznie w obrębie sektora)**. Rozjazd sygnału między warstwami jest informacją — znaczy, że grupa jest źle zdefiniowana.

*Podstawa: GICS wypada lepiej niż SIC i NAICS w wyjaśnianiu zmienności mnożników (Bhojraj, Lee, Oler 2003), ale dobór po fundamentach obniża medianę błędu wyceny dla EV/Sales z 40,7% do 25,4%; najlepsza jest hybryda — dobór fundamentalny wewnątrz branży. Optimum liczebności: 6–16.*

**M33.** Grupy peer są **wersjonowane** (`valid_from`, `valid_to`, `reason`). RVS jest funkcją zbioru peerów, więc zmiana zbioru tworzy w szeregu skok nieodróżnialny od ruchu wyceny.

**M34.** RVS liczone jest na **stałym składzie wewnątrz okna porównania**, z jawnym **łączeniem łańcuchowym** na styku wersji. Samo wersjonowanie dokumentuje skok, ale go nie usuwa.

**M35.** Przy mniej niż trzech policzalnych peerach instrument przechodzi w **tryb absolutny**: model z tabeli §14, benchmark zastępczy, monitoring operacyjny — bez sygnału wycenowego relatywnego.

### 15.1 Tabela peer — część USD

Wagi ze zrzutu 2026-08-27.

| Ticker | Waga | Arch. | Mnożnik główny | Core peers | Uwaga |
|---|---|---|---|---|---|
| NOW | 9,00% | A2 | EV/NTM Sales | CRM · WDAY · TEAM · DDOG · SNOW · ADBE | kurs po splicie 5:1 |
| SE | 7,61% | A4 | EV/GP · SOTP | CPNG · MELI · PDD · GRAB · BABA | trzy segmenty, SOTP obowiązkowe |
| META | 7,58% | A1 | EV/EBIT · fwd P/E | GOOGL · PINS · RDDT · SNAP · TTD | rosnący capex → EV/(EBITDA−capex) |
| MELI | 6,37% | A4 | EV/GP · SOTP | AMZN · SE · CPNG ‖ NU · DLO · STNE · PAGS | osobne grupy per segment |
| NU | 4,92% | A5 | P/TBV vs ROTE | ITUB · BBD · INTR · SOFI · KSPI | emitent zagraniczny, brak kwartałów w XBRL |
| MSFT | 4,88% | A1 | EV/EBIT · fwd P/E | GOOGL · AMZN · ORCL · CRM · NOW · ADBE | kotwica ROIC portfela |
| KKR | 4,62% | A7 | P/FRE + SOTP | BX · APO · ARES · CG · TPG · OWL | |
| MARA | 4,57% | A8 | mNAV | RIOT · CLSK · IREN · CIFR · HUT · WULF | **benchmark zastępczy: BTC/IBIT** |
| AMZN | 4,19% | A1 | EV/EBIT · SOTP | MSFT · GOOGL ‖ WMT · MELI ‖ META | trzy grupy ważone zyskiem segmentu |
| BABA | 3,78% | A1 | EV/EBIT, P/E po korekcie o gotówkę | PDD · JD · 0700.HK · BIDU · 3690.HK | FY kończy marzec |
| UBER | 3,74% | A4 | EV/Gross Profit | DASH · LYFT · GRAB · ABNB · DHER.DE | kontaminacja: LYFT to jednorynkowy ridesharing |
| GRAB | 3,54% | A4 | EV/Gross Profit | SE · UBER · DASH · GOTO.JK · LYFT | w XBRL `fy`/`fp` = null → klucz po `end` |
| DLO | 3,45% | A6 | EV/Gross Profit | ADYEN.AS · STNE · PAGS · PAYO · GPN | take-rate spada strukturalnie |
| RBRK | 3,42% | A3 | EV/ARR · EV/NTM Sales | CRWD · ZS · S · NET · NTNX · CYBR | SBC/przychód metryką pierwszego rzędu |
| NVO | 3,12% | A1 | fwd P/E · EV/EBIT | LLY · AZN · SNY · MRK · ROG.SW | **raportuje w DKK** |
| CRCL | 3,08% | A8 | EV / przychód z rezerw netto | COIN · HOOD · GLXY ‖ SCHW · IBKR | **brak peera → tryb absolutny** |
| TSLA | 2,28% | A9 | EV/EBIT · EV/Sales auto | 1211.HK · GM · F · RIVN · LI | mnożnik nisko informacyjny |
| SPOT | 2,27% | A4 | EV/Gross Profit | NFLX · TME · UMG.AS · WMG · DUOL | marża brutto to cała historia |
| AXON | 1,97% | A2 | EV/NTM Sales | MSI · TYL · PLTR · VRSK | rozdzielić hardware od ARR |
| FTAI | 1,97% | A9 | EV/EBITDA · P/E | AER · AL · HEI · TDG · GE | monitoring akrualny uzasadniony |
| NVDA | 1,83% | A1 | fwd P/E · EV/EBIT | AMD · AVGO · TSM · MRVL · MU | PEG z ostrożnością |
| RKLB | 1,61% | A9 | EV/NTM Sales · EV/backlog | LUNR · RDW · ASTS | **brak peera → tryb absolutny** |
| APH | 1,57% | A1 | EV/EBIT | TEL · GLW · LFUS · VRT · CLS | ROIC vs WACC główną zmienną |
| ACMR | 1,56% | A9 | EV/Sales · EV/EBIT | LRCX · AMAT · KLAC · TOELY · 002371.SZ | trwałe dyskonto za Chiny |
| NFLX | 1,56% | A1 | fwd P/E · EV/EBIT | DIS · WBD · SPOT · ROKU · **PSKY** | **korekta: PARA nie istnieje od VIII 2025** (fuzja ze Skydance) |
| ORCL | 1,23% | A1 | EV/(EBITDA−capex) | MSFT · SAP · IBM · CRM · NOW | capex AI zmienił profil |
| TEM | 1,12% | A3 | SOTP: EV/Sales + EV/GP | NTRA · EXAS · GH ‖ VEEV · RXRX | rozdzielić segmenty |
| COIN | 1,05% | A8 | EV/przychód wg strumienia | HOOD · GLXY · CME · CRCL · BLSH | zysk skrajnie procykliczny |
| KLAR | 0,95% | A5 | P/TBV vs ROTE | AFRM · SEZL · XYZ · PYPL · SOFI | pożyczkodawca, nie software |
| MBLY | 0,71% | A9 | EV/Sales | AMBA · APTV · INDI · QCOM · HSAI | dyskonto za kontrolę większościową |
| TTD | 0,44% | A4 | EV/Sales · EV/GP | APP · MGNI · PUBM · CRTO · DV | **test kwasowy filtra pułapek** |

**Defekt zastany [Z]:** PARA nie istnieje od sierpnia 2025 — fuzja Paramount ze Skydance, ticker PSKY. **WBD jest w trakcie zmian strukturalnych [N].** Cała tabela wymaga rewalidacji przed startem (P-05), nie dopiero przy przeglądzie kwartalnym. To dowód, że przeżywalność peerów jest stanem zastanym, nie ryzykiem trzyletnim.

## 16. Sygnał i cztery zmienne stanu

### 16.1 VALUE

```
RVS    = mnożnik_główny / mediana_mnożnika_peer        (stały skład, M34)
sygnał = ln(RVS_dziś) − ln(mediana RVS z okna referencyjnego)
```

**M36.** Okno referencyjne zależy od długości własnej historii:

| Historia RVS | Rola |
|---|---|
| **0–26 tygodni** | **brak historycznego RVS — wyłącznie percentyl w grupie peer.** To jest faktyczna definicja sygnału wycenowego na cały pierwszy rok pracy systemu, nie przypadek brzegowy |
| 26–104 tygodnie | rolling mediana + MAD, sygnał **wyłącznie pomocniczy**, nigdy bramkujący |
| powyżej 104 tygodni | pełny historyczny RVS jako sygnał główny |

*Nazwa „historyczny reżim RVS", nie „mediana 3-letnia" — żeby dało się zmienić okno bez zmiany semantyki systemu.*

**M37.** Bieżący punkt jest **wyłączony z mediany referencyjnej**. Bez tego przedłużająca się prawdziwa taniość wchodzi do własnego okna odniesienia i przesuwa medianę ku sobie; przy próbkowaniu kwartalnym rok taniości to 4 z 12 obserwacji. Histereza wyjścia zostaje wtedy przecięta **dryfem bazy, a nie repricingiem** — pozycja przestaje być tania, choć nic się nie stało.

**M38.** Przy wygaszeniu sygnału logowana jest **przyczyna**: cena, mianownik, zmiana składu peerów, czy dryf bazy.

Progi startowe (do kalibracji, nie ustalenia): VERY_CHEAP < −0,40; CHEAP < −0,25; FAIR w [−0,10, +0,10]; EXPENSIVE > +0,25; VERY_EXPENSIVE > +0,40. Pasmo martwe [−0,10, +0,10]; histereza wejście −0,25 / wyjście −0,10.

### 16.2 QUALITY

Stan porządkowy z bramki archetypowej (§14): **IMPROVING / STABLE / DETERIORATING / BROKEN / UNKNOWN**. Bramka ma logikę trójwartościową (M16), a przy niepoliczalnym członie zwraca UNKNOWN, nie FAIL.

Uzupełnienie ręczne: **kwartalny werdykt per spółka** zapisywany w rejestrze tez — 31 werdyktów przy przeglądzie, który i tak się odbywa. To jest realizacja M12: jakość jako kierunek, oceniana także przez człowieka tam, gdzie dane maszynowe nie sięgają.

### 16.3 TREND i RISK

TREND z grupy wskaźników trendowych (§18): UPTREND, gdy `close > SMA200` ∧ SMA200 rośnie ∧ `close/max(close,252) > 0,85`. DOWNTREND przy odwróceniu. NEUTRAL pośrodku.

RISK: CRITICAL przy naruszeniu twardego stopu albo przekroczeniu portfolio heat; HIGH przy `σ20 > 2 × σ60` albo jakości danych C; NORMAL domyślnie.

### 16.4 Silnik decyzyjny

| VALUE | QUALITY | TREND | RISK | Działanie |
|---|---|---|---|---|
| CHEAP / VERY_CHEAP | IMPROVING / STABLE | UPTREND / NEUTRAL | ≤ NORMAL | **ADD candidate** |
| CHEAP / VERY_CHEAP | DETERIORATING / BROKEN | dowolny | dowolny | **TRAP** — kwarantanna 30 dni |
| FAIR | IMPROVING / STABLE | UPTREND | ≤ NORMAL | **HOLD** |
| EXPENSIVE | STABLE / IMPROVING | UPTREND | ≤ NORMAL | **HOLD, no-add** |
| EXPENSIVE / VERY_EXPENSIVE | DETERIORATING | DOWNTREND | dowolny | **TRIM** |
| dowolny | dowolny | dowolny | **CRITICAL** | **EXIT** niezależnie od wyceny |
| **UNKNOWN w polu wymaganym** | | | | **brak decyzji**, wpis do raportu |

**M39.** Kwarantanna po TRAP: 30 dni bez alertów wycenowych, **zdejmowana wcześniej przez zmianę stanu** (QUALITY wraca do STABLE albo lepiej), nie tylko przez upływ czasu. Pozycje w kwarantannie są **wypisywane w przeglądzie okresowym** — cicha kwarantanna to mechanizm, przez który psująca się pozycja znika z pola widzenia dokładnie wtedy, gdy wymaga decyzji. Reguły ryzyka działają w kwarantannie normalnie.

**M40.** Sygnał ROTACJA (peer atrakcyjniejszy): `sygnał_posiadanej − sygnał_peera > 0,35`, utrzymane dwa przebiegi, peer przechodzi bramkę jakości swojego archetypu, peer nie ma gorszego trendu rewizji. Pasmo martwe: różnica < 0,20 wygasza.

**M41.** Silnik pracuje **symetrycznie** — ta sama regresja i te same progi produkują listę kandydatów do wejścia z grup peer, bez osobnego, luźniejszego procesu.

## 17. Reverse DCF i wymagalność założeń

### 17.1 Po co

Odpowiada na pytanie: **jak wysoko rynek zawiesił poprzeczkę**. Cena jest wejściem, nie wyjściem — implicytna prognoza staje się wielkością wyprowadzoną, nie założoną. To redukcja stopni swobody z sześciu do dwóch (WACC i struktura wartości rezydualnej), nie do zera.

Drugi, ważniejszy zysk: wynik jest **falsyfikowalny wobec danych zewnętrznych** (base rates), podczas gdy „moja wycena = 180 USD" nie jest falsyfikowalna wobec niczego poza samą ceną. To jest dokładnie to, czego potrzebuje rejestr tez.

### 17.2 Silnik projekcji

```
Sales(t)               = Sales(t−1) × (1 + g)
Operating Profit       = Sales × operating margin
NOPAT                  = Operating Profit × (1 − cash tax rate)
Incremental Investment = ΔSales × (working capital rate + fixed capital rate)
FCF                    = NOPAT − Incremental Investment

Corporate value = Σ PV(FCF) + PV(wartość rezydualna)
Equity value    = Corporate value + gotówka nadwyżkowa − dług
```

**M42.** Wartość rezydualna: **perpetuity bez wzrostu** `NOPAT/WACC` (M29). Wariant fade z korelacjami sektorowymi raportowany obok jako „realistyczny", nigdy zamiast.

**M43.** Rozwiązywana jest **dokładnie jedna zmienna**, dobrana wg archetypu:

| Zmienna | Archetyp | Kalibracja wyniku |
|---|---|---|
| CAGR przychodów | A1, A2, A4, A6 | base rates wg bucketu przychodowego |
| Marża docelowa w roku N | A3, A9 | marże dojrzałych peerów |
| Okres prognozy (CAP) | uzupełniająco | benchmark 5–20 lat |
| **MEROI** | A7, A8, brak peerów | benchmark = WACC, **bez danych zewnętrznych** |
| **ROTE implikowane** | **A5** | forma zamknięta, bez solvera |

### 17.3 MEROI — metryka dla spółek bez peerów

`PV(ΔNOPAT skapitalizowane przy MEROI) = PV(inwestycji przy koszcie kapitału)`, rozwiązywane względem MEROI. To IRR, nie stopa dyskontowa.

**[S] Dlaczego to jest właściwa odpowiedź dla CRCL, RKLB, KKR i spółek wielosegmentowych:** MEROI jest bezjednostkowe i porównywalne **bez peerów**, bo benchmark (koszt kapitału) jest wewnętrzny dla spółki. Implikowany CAGR wymaga rozkładu base rates dla danej wielkości; MEROI wymaga tylko WACC. Spread `MEROI − WACC` czyta się wprost: „ile ponad koszt kapitału musi zarobić każdy nowy dolar inwestycji, żeby dzisiejsza cena się broniła".

Kalibracja [Z]: Microsoft FY2004–2020 miał MEROI 27,1% (18,1% po kapitalizacji niematerialnych) przy koszcie kapitału własnego 9,5% — spread 17,6 / 8,6 pp. **Microsoft dowiózł.** Kategoria „skrajne" nie znaczy „sprzedawaj", tylko „margines błędu zniknął".

### 17.4 Fintech — forma zamknięta zamiast solvera

Dla A5 (NU, KLAR) reverse DCF nie ma zastosowania (M30). Zamiast tego:

```
P/TBV = 1 + (ROTE_impl − Ke) / (Ke − g)
   ⟹   ROTE_impl = Ke + (P/TBV − 1) × (Ke − g)
```

Rozwiązanie w formie zamkniętej, z trzech liczb, które i tak są zbierane. Wynik „rynek wycenia NU na trwałe ROTE 28%" jest wprost porównywalny z ROTE raportowanym i z ROTE peerów. **Lepszy sygnał absolutny niż jakikolwiek reverse DCF i tańszy w implementacji.**

### 17.5 Ocena wymagalności — base rates

Rozkłady CAGR przychodów [Z] (Mauboussin, *The Base Rate Book*, ~1000 największych spółek, 1950–2015):

| Horyzont | Średnia | Mediana | σ | **Udział z CAGR > 20%** |
|---|---|---|---|---|
| 3 lata | 8,1% | 5,4% | 18,7% | — |
| **5 lat** | 6,9% | 5,2% | 12,3% | **6,0%** |
| **10 lat** | 5,8% | 4,9% | 8,0% | **2,8%** |

Wg wielkości przychodów, 3 lata [Z] — pokazuje monotoniczny spadek ze skalą:

| Przychody | Średnia | Mediana | σ |
|---|---|---|---|
| 0–325 mln | 21,2% | 11,7% | 40,0% |
| 700 mln–1,25 mld | 9,2% | 6,8% | 13,5% |
| 3–4,5 mld | 6,4% | 5,0% | 11,1% |
| 12–25 mld | 3,8% | 3,0% | 12,3% |
| >25 mld | 2,4% | 2,2% | 10,9% |
| >50 mld | 1,2% | 1,5% | 10,3% |

**Persystencja wzrostu jest niska [Z]:** korelacja rok-do-roku dla tempa wzrostu przychodów 0,30; dla okresów 3-letnich 0,17; 5-letnich 0,19. Wśród spółek rosnących 20%+ przez poprzednie 3 lata — nieco ponad 30% utrzymało tempo, blisko 70% rosło wolniej, **22% zanotowało wzrost ujemny**.

**M44.** Dla spółek technologicznych i ochrony zdrowia powyżej 5 mld przychodów używa się **tabeli sektorowej z aktualizacji 2021** (Russell 3000, 1984–2020), nie rozkładu ogólnego 1950–2015. Prawy ogon rozkładu wzrostu rozszerza się w sektorach intensywnych w niematerialne; użycie rozkładu ogólnego systematycznie **zawyża percentyl**, czyli uznaje założenia za bardziej wymagające niż są.

**M45.** Raportowane jest **przesunięcie percentyla**, nie sam poziom: *„rynek wycenia 82. percentyl rozkładu spółek o przychodach 12–25 mld; ta spółka w ostatnich pięciu latach była w 61."* Przesunięcie jest odporne na błąd doboru bucketu, bo błąd znosi się po obu stronach.

Kategoryzacja startowa:

| Kategoria | Percentyl | lub CAP | lub MEROI − WACC |
|---|---|---|---|
| Skromne | <50 | <5 lat | <3 pp |
| Umiarkowane | 50–75 | 5–10 lat | 3–8 pp |
| Wymagające | 75–90 | 10–20 lat | 8–15 pp |
| Skrajne | >90 | >20 lat | >15 pp |

**M46.** Percentyl base rate, CAP i MEROI **nie są niezależne** — wywodzą się z tego samego równania. Nie wolno ich uśredniać ani składać w jeden score; to policzyłoby tę samą informację trzy razy. Raportowane obok siebie jako trzy rzuty tej samej wielkości w różnych jednostkach.

### 17.6 Ograniczenia

**M47.** Reverse DCF **nie generuje alertów** (M28). Miejsce wyniku: kolumna kontekstowa w artefakcie okresowym i materiał do przeglądu miesięcznego.

**[S] Nie znaleziono żadnego recenzowanego badania testującego reverse DCF ani Expectations Investing jako strategię.** Znaleziono natomiast trzy dowody dla konstrukcji pojęciowo bliskich, z których jeden jest wprost ostrzeżeniem: implied cost of capital **oparte na modelu przekrojowym** daje spread decylowy 10,62–12,03% przy t = 3,77–5,39, ale **oparte na prognozach analityków — 3,91–4,86% przy t < 1,40, czyli nieistotne**. Nasz konsensus pochodzi z yfinance z flagą jakości `low`.

**M48.** Wrażliwość: zamiast punktowego WACC raportowany jest **przedział dla WACC ∈ {8%, 10%, 12%}**. Przy spółkach przedzyskowych ponad 90% wartości leży w ogonie, więc precyzja do 0,01% CAGR jest fikcyjna wobec błędu WACC rzędu ±200 bp.

**M49.** Ryzyko przetrwania jest **osobnym czynnikiem, nie korektą stopy dyskontowej**: `EV = V_going_concern × (1 − p_fail) + V_distress × p_fail`. Standardowy reverse DCF milcząco zakłada `p_fail = 0`, co dla spółek przedzyskowych jest fałszywe.

### 17.7 Kadencja

| Komponent | Kadencja | Powód |
|---|---|---|
| Cena i kapitalizacja | z przebiegiem okresowym | zero kosztu |
| **ERP** | **miesięcznie** | [Z] Damodaran publikuje na początek miesiąca; 4,37% → 4,51% w dwa tygodnie |
| Bety, WACC sektorowy | styczeń | roczny cykl |
| Fundamenty | kwartalnie, po raporcie | |
| **Pełny reverse DCF** | **kwartalnie + przy ruchu ceny >20% od ostatniego przeliczenia** | funkcja jest silnie spłaszczona; ruch 5% zmienia implikowany CAGR poniżej szumu z WACC |

*To jest korekta wobec rewizji 1, która przewidywała odświeżanie zestawów Damodarana raz w styczniu — dotyczy to bet i marż, ale nie ERP.*

## 18. Analiza techniczna

| Grupa | Wskaźnik | Wzór | Podstawa |
|---|---|---|---|
| **TREND** | `close / SMA200` + nachylenie SMA200 | `SMA200 / SMA200[21] − 1` | Faber 2007: obsunięcie S&P −83,7% → −50,0%, Sharpe 0,29 → 0,43. **Przewaga w redukcji ryzyka, nie w zwrocie** (+0,9 pp CAGR) |
| **TREND** | pozycja w kanale 52-tyg. | `close / max(close, 252)` | George & Hwang 2004: 1,13%/mies. skoryg. o ryzyko vs 0,46% klasycznego momentum (ex-styczeń); **zyski nie odwracają się** |
| **TREND** | linia RS vs benchmark | `close / close_benchmark` + nachylenie 63 sesje | benchmark lokalny per rynek, nie SPY dla wszystkich |
| **MOMENTUM** | momentum 12-1 | `close[t−1 mies.] / close[t−12 mies.] − 1`, **kotwiczone datą** (M20) | Jegadeesh & Titman; ranking wewnątrz portfela, nie próg binarny |
| **RISK** | ATR(20) w % ceny | `ATR20 / close`, **w walucie notowania** | baza sizingu i stopów |
| **RISK** | reżim zmienności | `σ20·√252 / σ60·√252` | wczesny detektor |
| **PORTFOLIO** | korelacja + PCA + MCTR | **zwroty tygodniowe**, okno ≥ 120 obserwacji, shrinkage Ledoit-Wolf | M21 |

**Odrzucone:** MACD (redundancja wobec pierwszego; win-rate poniżej 50% w badaniu na DJI/Nasdaq/S&P 2015–2021), RSI z progami 30/70 (oscylatory pozostają wykupione w silnym trendzie; działa w konsolidacji, czyli tam, gdzie nie handlujemy), ADX (opóźnienie, 150 barów rozgrzewki, brak recenzowanego potwierdzenia), OBV i anchored VWAP (zero recenzowanych badań), Connors RSI (horyzont 2–5 dni), pełne SMC (M22).

**Dopuszczalne uzupełnienie:** RSI **wyłącznie w interpretacji Cardwella** — nie progi 30/70, tylko „czy RSI trzyma się w paśmie 40–90 (reżim byka), czy przełamał 40 w dół".

**Z SMC bierzemy trzy deterministyczne elementy jako kontekst, bez wpływu na decyzję:** Fair Value Gap (`low > high[2] ∧ close[1] > high[2]`, próg = 2 × średnia bezwzględna zmiana %), poziomy MTF (poprzednie ekstrema tygodnia i miesiąca), premium/discount (tożsame ze wskaźnikiem pozycji w kanale — nie implementować dwa razy).

**T33.** Macierz korelacji 31×31 z 60 obserwacji to 496 parametrów z 60 wierszy — PC1 zawyżone z konstrukcji. **Pierwszy pomiar od razu na ≥120 obserwacjach tygodniowych ze shrinkage, albo wcale.**

## 19. Ryzyko i sizing

### 19.1 Hierarchia wyjść

| Prio | Klasa | Reguła |
|---|---|---|
| 1 | **STOP LOSS** | Chandelier `max(high,22) − 3×ATR22` albo stop 2N `entry − 2×ATR20`, nigdy przesuwany w dół |
| 2 | **REGIME EXIT** | `close < SMA200` ∧ SMA200 opada 21 sesji |
| 3 | **THESIS EXIT** | naruszone kryterium falsyfikacji z rejestru tez |
| 4 | **RELATIVE EXIT** | porażka wobec benchmarku zastępczego przez 24 miesiące — **tylko przy VALUE ≠ CHEAP** (M26) |
| 5 | **OSTRZEŻENIE** | `σ20 > 2 × σ60` albo `close/max(close,252) < 0,75` — nie wyjście |

### 19.2 Sizing i budżety

```
wielkość = (kapitał_satelity × 0,01) / (2 × ATR20_w_walucie_notowania)
         → przewalutowane na PLN po kursie NBP A z dnia D

Poziom 1: ryzyko pojedynczej nazwy   ≤ 1%  kapitału satelity
Poziom 2: ryzyko tematu / czynnika   ≤ 3%
Poziom 3: suma otwartych ryzyk       ≤ 15%
```

**M50.** Po przekroczeniu poziomu 3 system **nie proponuje dobrania — wyłącznie cięcia**. Przy 31 pozycjach po 1% suma to 31% kapitału, a stopy pękną razem, bo portfel ma dominujący czynnik wzrostowy o długim duration.

### 19.3 Dzień zero na żywej książce

**M51.** System startuje na istniejących pozycjach z historycznymi wejściami, a wszystkie reguły są pisane dla wejść nowych. Semantyka inicjalizacji:

- Pozycje **już poniżej** poziomu stopu w dniu pierwszym → stan `RISK = HIGH` i **jednorazowy raport inicjalizacyjny**, nie 31 alertów. Alert właściwy dopiero przy kolejnym przecięciu.
- Stop 2N od historycznego wejścia dla pozycji z dużym zyskiem jest **martwy** — zastępowany przez Chandelier z §19.1 (`max(high,22) − 3×ATR22`), z zapadką „nigdy w dół" biegnącą od dnia inicjalizacji systemu.
- Raport inicjalizacyjny jest **poza limitem 15 alertów miesięcznie** i występuje dokładnie raz.

### 19.4 Instrumenty pochodne

- Ekspozycja kontraktu terminowego = liczba × mnożnik × kurs instrumentu bazowego, w PLN.
- ATR, stop i REGIME liczy się na instrumencie bazowym.
- Ryzyko = (close bazy − stop) × liczba × mnożnik; dla pozycji krótkiej lustrzanie, ze stopem nad ceną.
- Kontrakty wchodzą do budżetów ryzyka poziomów 1–3. Nominał kontraktu nie wchodzi do kapitału satelity; wchodzi wartość rachunku KONTRAKTOWY (środki + wynik zmienny).
- Seria wygasła bez transakcji zamykającej jest zamykana w dniu wygaśnięcia (trzeci piątek miesiąca serii). Archetyp kontraktu to A0 (§14.1, pytanie 1); archetyp bazy jest zapisywany w `archetype_secondary` jako informacja.

---

# CZĘŚĆ V — ZARZĄDZANIE ALOKACJĄ

## 20. Polityka wag

**M52.** Każda pozycja ma **wagę docelową** i **pasmo**, zapisane przy rejestracji (§12) i rewidowane wyłącznie przy przeglądzie kwartalnym — nigdy w reakcji na ruch ceny.

| Klasa konwikcji | Waga docelowa | Pasmo | Uwagi |
|---|---|---|---|
| Rdzeń satelity | 6–9% | ±2 pp | maks. 5 pozycji |
| Standard | 3–5% | ±1,5 pp | |
| Sonda | 1,5–2,5% | ±1 pp | pozycja badawcza, teza niepotwierdzona |
| **Poniżej podłogi** | **< 1,5%** | — | **decyzja: dobrać do minimum albo zamknąć** |

**M53. Podłoga konwikcji.** Pozycja poniżej 1,5% wagi, która nie przechodzi ani filtra reżimowego, ani relatywnego, trafia do decyzji „zamknąć albo dobrać do wagi minimalnej". Nie zostaje w zawieszeniu.

**[S] Uzasadnienie liczbowe ze stanu bieżącego:** 13 pozycji poniżej 2% wagi to razem 17,6% portfela; 6 pozycji poniżej 1,5% to ok. 5,5%. Te pozycje nie mogą już poruszyć wyniku, a konsumują pełny koszt uwagi: dane, peerów, tezę, raport kwartalny, decyzję i rozliczenie podatkowe. **Zastosowanie podłogi ręcznie, przed budową systemu, usuwa z każdego przyszłego problemu nie tylko pozycję, ale i jej grupę peer** — to jedyna dostępna redukcja złożoności o ok. 30% kosztująca jeden wieczór decyzji zamiast tygodni kodu.

## 21. Powody zmiany alokacji

Cztery mechanizmy, **rozłączne z założenia**. Mieszanie ich jest tym samym błędem, co zwijanie czterech stanów w jeden bool.

### 21.1 Naruszenie pasma wagi — mechanizm mechaniczny

**M54.** Wyjście poza pasmo → propozycja przywrócenia do wagi docelowej. Powód jest **czysto portfelowy** i nie mówi nic o spółce. Realizacja przy przeglądzie miesięcznym, nie natychmiast.

Kalibracja: przy paśmie ±1,5 pp i wadze docelowej 4% pozycja musi urosnąć o ok. 38% względem reszty portfela, żeby naruszyć pasmo. To odpowiedni rząd rzadkości.

### 21.2 Osiągnięcie ceny docelowej — mechanizm tezowy

**M55.** „Cena docelowa" **nie jest pojedynczą liczbą wpisaną przy zakupie**. Jest definiowana jako **poziom, przy którym implikowane założenia przekraczają zadeklarowany percentyl base rate**.

Zamiast: *„target 200 USD"*
Zapis: *„trim gdy implikowany CAGR przekroczy 80. percentyl rozkładu dla bucketu przychodowego"*

*Uzasadnienie [S]: cena docelowa wpisana rok temu jest funkcją tego, co wtedy wiedziałeś, i nie aktualizuje się wraz z wynikami spółki. Poprzeczka wyrażona w percentylu aktualizuje się sama — gdy spółka dowozi, ten sam kurs oznacza mniej wymagające założenia.* To jest bezpośrednie zastosowanie §17.

Realizacja: **drabina**, nie decyzja binarna.

| Percentyl implikowany | Działanie |
|---|---|
| < 75 | brak |
| 75–90 | **TRIM 1/3 nadwyżki** ponad wagę docelową |
| > 90 | TRIM do dolnej granicy pasma |
| > 90 **∧** QUALITY = DETERIORATING | TRIM do zera lub do wagi sondy |

### 21.3 Wymagająca wycena — mechanizm relatywny

**M56.** Odrębny od 21.2: tam poprzeczka jest **absolutna** (wobec base rates), tu **relatywna** (wobec peerów). Sygnał EXPENSIVE / VERY_EXPENSIVE z §16 przy TREND ≠ UPTREND → TRIM. Przy TREND = UPTREND → HOLD, no-add.

*Rozdzielenie ma znaczenie: spółka może być droga wobec peerów, choć jej implikowane założenia są skromne (cały sektor tani), i odwrotnie.*

### 21.4 Przegrana z benchmarkiem zastępczym — mechanizm kontrfaktyczny

**M57.** Każda pozycja ma zapisany **benchmark zastępczy** — instrument dający **tę samą ekspozycję prościej i taniej**:

```
MARA
├── peers      : RIOT, CLSK, IREN, CIFR, HUT, WULF
├── underlying : BTC
├── sector     : crypto miners
└── substitute : IBIT          ← test brzmi: "czy MARA uzasadnia dodatkową
                                  złożoność i ryzyko względem IBIT?"
```

Przykłady: NVDA → SMH; COIN → BTC/IBIT; ACMR → SOXX; spółka GPW → mWIG40/sWIG80; cały satelita → QQQ (lub koszyk odzwierciedlający jego skład rynkowy).

**M58.** Miara: excess return 12M i 24M **skorygowany o ryzyko**. Pozycja przegrywająca z własnym benchmarkiem przez 24 miesiące jest kandydatem do **zamiany na ten benchmark**, bez dyskusji o tezie.

**M59.** Ten sam test na poziomie całego satelity. 31 pozycji, godziny pracy i ryzyko idiosynkratyczne muszą wygrać z jednym instrumentem. Jeśli nie wygrywają przez 24 miesiące, **to jest informacja o systemie, nie o rynku** — i przesłanka do jego wyłączenia zgodnie z §1.3.

### 21.5 Czego zmiana alokacji NIE obejmuje

Twarde stopy i wyjścia reżimowe (§19.1, priorytet 1–2) **nie są zmianami alokacji** — są wyjściami z pozycji i mają pierwszeństwo przed każdym mechanizmem z §21.

## 22. Egzekucja

### 22.1 Wejście transzowe

**M60.** Wejście dzieli się na trzy transze: 1/3 po przejściu bramki, 1/3 na zadeklarowanym z góry triggerze technicznym, 1/3 na potwierdzeniu (zamknięcie powyżej ceny pierwszej transzy). **Trigger ma limit 20 sesji** — po jego upływie pozostałe transze wchodzą po rynku.

*Bez limitu „czekam na strefę" jest mechanizmem, przez który nigdy nie kupuje się niczego, co rośnie.*

**M61.** Wartość transzowania **nie zależy od tego, czy trigger ma przewagę** — podział obniża wariancję ceny wejścia niezależnie od jakości sygnału. Warunek: trigger **deklarowany przed wejściem i zapisywany w dzienniku decyzji**, żeby po roku dało się policzyć, czy druga transza wchodziła taniej. Jeśli nie — trigger wypada.

*Kontekst dowodowy: Golden Pocket 0,618–0,7272 i Overbalance 1:1 nie mają recenzowanego potwierdzenia; rozszerzenie reguł Brocka i in. na pełne uniwersum z korektą na data snooping pokazało, że przewaga najlepszych reguł technicznych nie powtórzyła się poza próbą w latach 1987–1996. Dodatkowo audyt silnika Franka wykazał, że tamtejsza formuła Overbalance 1:1 była bez treści — to nie dyskwalifikuje koncepcji, ale każe zweryfikować implementację.*

### 22.2 Wyjście i redukcja

Redukcje wg drabiny z §21.2. Wyjścia reżimowe i tezowe — jednorazowo, bez drabiny.

### 22.3 Twarde stopy leżą u brokera

**M62. To jest rozstrzygnięcie o architekturze, nie o egzekucji.** System **oblicza** poziom stopu i informuje o konieczności aktualizacji zlecenia; **wykonuje broker** jako zlecenie oczekujące.

*Konsekwencja [S]: to usuwa jedyny argument za przebiegiem dobowym. Skrypt uruchamiany raz w tygodniu nie może być mechanizmem stop-lossa, bo luka w poniedziałek zostałaby zauważona w piątek. Zlecenie u brokera działa w każdej sesji, także wtedy gdy VPS padnie, gdy Yahoo zmieni schemat i gdy właściciel jest na urlopie. System jest wtedy tym, czym ma być — instrumentem pomiarowym, nie systemem wykonawczym.*

**M63.** Poziom stopu zmienia się rzadko (Chandelier podąża za maksimum). Aktualizacja zlecenia jest pozycją w przeglądzie okresowym, a przy skoku zmienności — osobnym alertem.

---

# CZĘŚĆ VI — OPERACJE

## 23. Kadencja

### 23.1 Rozstrzygnięcie: system działa na bieżąco, ale nie codziennie

Trzy kadencje są **rozdzielone**, bo mają różne uzasadnienia:

```
POBRANIE      ← jak często dane wchodzą do archiwum
OCENA         ← jak często liczone są stany i sygnały
ALERT         ← kiedy system się odzywa
```

**M64.** Kadencja podstawowa to **tydzień**. Uzasadnienie w trzech krokach:

1. **Ceny są odtwarzalne wstecz.** Dzienne OHLC da się dobrać w każdej chwili z Yahoo albo Stooq — pobranie tygodniowe nie traci ani jednej obserwacji historycznej. To odróżnia ceny od konsensusu, który jest nadpisywany w miejscu.
2. **Wszystkie wskaźniki są wolne.** SMA200, momentum 12-1, pozycja w kanale 52-tygodniowym, RVS z kwartalnych fundamentów — żaden nie zmienia stanu z dnia na dzień w sposób wymagający reakcji.
3. **Twarde stopy nie potrzebują systemu** (M62). Wykonuje je broker.

Ocena dobowa dałaby ok. pięciokrotnie więcej okazji do fałszywego przekroczenia progu przy tej samej informacji.

**M65.** Wyjątek: **strumień konsensusu i kwartałów emitentów zagranicznych pobierany jest częściej niż tygodniowo**, bo jest nadpisywany w miejscu i nieodtwarzalny wstecz. Minimalna wersja: snapshot dwa razy w tygodniu.

### 23.2 Harmonogram

| Kiedy | Co | Kanał |
|---|---|---|
| **2×/tydzień, 04:30 UTC** | snapshot konsensusu i kwartałów FPI (nieodtwarzalne) | cichy zapis |
| **Sobota 07:00 UTC** | **przebieg główny**: ceny wszystkich rynków, FX, wskaźniki, RVS, cztery stany, reguły ryzyka, portfolio heat | **artefakt + alert tylko przy zdarzeniu** |
| **Codziennie, opcjonalnie** | „straż cenowa": pobranie cen + sprawdzenie reguł ryzyka bez pozostałych obliczeń | Slack tylko przy naruszeniu |
| **1. dzień roboczy miesiąca** | przegląd: rotacje, alokacja, pozycje w kwarantannie, podłoga konwikcji, aktualizacja zleceń stop | artefakt |
| **T−3 / T+1 wg kalendarza** | wyniki kwartalne — format 6-liniowy | Slack (T+1) |
| **Kwartalnie** | rewalidacja peerów, reverse DCF, KPI ręczne, werdykt QUALITY, test kryteriów falsyfikacji, PCA/ENB/MCTR | artefakt |
| **Miesięcznie** | odświeżenie ERP Damodarana | cichy zapis |
| **Styczeń** | bety, WACC sektorowy, marże, tabele fade | cichy zapis |
| **Codziennie** | **ping do zewnętrznego monitora** | brak pingu = alarm |

**M66.** Godzina 04:30 UTC omija zmierzoną anomalię obciążenia VPS o 06:00 UTC (§7.1). Przebieg sobotni o 07:00 UTC działa na domkniętym tygodniu wszystkich giełd.

**M67.** Dane sesji D są **odświeżane w oknie D−5** przy każdym przebiegu, a rozbieżności logowane do `ingest_errors`. Dostawcy doprecyzowują sesje i nakładają korekty korporacyjne po fakcie.

### 23.3 Dead-man switch

**M68.** Heartbeat raportowany przez sam system **nie wykrywa najgorszego przypadku** — martwy proces nie wysyła „nie żyję". Wymagany jest **ping wychodzący do zewnętrznego monitora, gdzie alarmuje brak pingu**. Cisza musi być dowodliwa, skoro jest stanem domyślnym.

## 24. Dostawa

**M69.** Trzy klasy komunikatów, rozdzielone kanałem albo priorytetem:

| Klasa | Zawartość | Priorytet |
|---|---|---|
| 🔴 **EVENT** | STOP, TRAP, ROTACJA, **DATA FAILURE** | wymaga działania |
| 🟡 **STATE CHANGE** | zmiana któregokolwiek z czterech stanów | informacyjny |
| 🟢 **HEARTBEAT** | system żyje, pokrycie n/N, alertów 0 | tygodniowy |

**M70. DATA FAILURE ma najwyższy priorytet.** „Spółka złamała regułę" i „API nie odpowiedziało" to dwie różne rzeczy, a druga jest groźniejsza, bo maskuje pierwszą.

Wzór alertu:

```
🔴 MANNAZ · REGUŁA ZŁAMANA · 2026-09-14 · jakość danych: B (yfinance, 1 źródło)

TTD  The Trade Desk        waga 0,44%   poz. −71,4%   ccy USD
├ VALUE   : CHEAP        RVS −0,41 vs reżim historyczny (percentyl 8/5 peerów)
├ QUALITY : DETERIORATING  rewizje NTM −7% kw/kw ×2, marża brutto YoY spada
├ TREND   : DOWNTREND     close 12,80 < SMA200 14,95, SMA200 opada 47 sesji
├ RISK    : HIGH          σ20 = 2,3 × σ60
└ DECYZJA : TRAP → kwarantanna 30 dni, zdejmowana przy powrocie rewizji na plus

   Benchmark zastępczy APP: −12% za 12M vs TTD −64%
   Pozycja poniżej podłogi konwikcji 1,5% → decyzja przy przeglądzie miesięcznym
```

**M71.** Format podsumowania po wynikach — **sześć linii, zawsze te same, w tej samej kolejności**:

1. **Trafienie** — przychód i EPS vs konsensus, z flagą jakości konsensusu **oraz flagą opóźnienia** (raport 5 dni po kalendarzu to informacja o jakości emitenta)
2. **Prognoza** — guidance podniesiony / obniżony / potwierdzony, o ile
3. **KPI archetypu** — 2–3 metryki właściwe dla tego archetypu (§14)
4. **Efekt na wycenę** — mnożnik i sygnał RVS przed/po; odpowiada, czy spółka staniała bo cena, zdrożała bo mianownik, **czy ruszył się peer**
5. **Test tezy** — naruszone kryteria falsyfikacji, albo `OK`
6. **Akcja** — brak / do przeglądu / natychmiastowa reguła ryzyka

## 25. Walidacja i roadmapa

### 25.1 Kolejność: najpierw ręcznie, potem kod

**M72.** Przed napisaniem linijki kodu: **ręczny replay 5 spółek × 3 daty historyczne**, po jednej z problemowych archetypów (np. NOW jako A2, RBRK albo TTD jako A3/A4, NU jako A5, MARA jako A8, RKLB jako A9). Dla każdej kratki ręcznie: VALUE, QUALITY, TREND, RISK, benchmark, decyzja.

**Obowiązkowe pytanie przy każdej kratce: „skąd dokładnie wezmę tę liczbę za darmo i co wpisuję, gdy jej nie ma".** To pytanie jest ważniejsze od samych werdyktów — wygeneruje kontrakt danych za darmo i ujawni sprzeczności definicji, zanim staną się trzema tygodniami kodu.

**M73.** Cztery klasy przypadków testowych, sprawdzające **logikę decyzji, nie wynik historyczny**:

| Klasa | Oczekiwanie |
|---|---|
| A — tania i dobra | ADD |
| B — tania i psująca się | TRAP / HOLD |
| C — droga i dobra | **HOLD, nie SELL** |
| D — droga i psująca się | TRIM / EXIT |

**M74. Zakaz backtestu całego systemu na starcie.** Przy archetypach, progach, siedmiu wskaźnikach, RVS, histerezie, kwarantannie i transzach pełna optymalizacja na historii da maszynę do data-miningu, nie walidację. Etapy:

```
Etap 1  niezmienniki mechaniczne — czy ATR, SMA, RVS, EV, FX, LTM i splity są policzone poprawnie
Etap 2  decision replay na snapshotach historycznych, bez optymalizacji
Etap 3  shadow mode 8–12 tygodni: system mówi, człowiek decyduje, obie decyzje zapisane
Etap 4  dopiero teraz zmiana progów
```

**M75.** Każdy sygnał zapisuje predykcję **przed** pomiarem: znacznik czasu, instrument, cztery stany, pewność, horyzont, oczekiwany wynik, benchmark, kryterium unieważnienia. Przeszły stan nigdy nie jest modyfikowany.

### 25.2 Wersja minimalna

**Zakres:** ceny wszystkich rynków + FX + siedem wskaźników + reguły ryzyka + arkusz mianowników + RVS na percentylu peerów + artefakt tygodniowy + webhook Slack + zewnętrzny dead-man.

**Poza zakresem:** pipeline XBRL, reverse DCF, regresje, PCA, Beneish, FVG, narracja LLM, raporty T+1.

| Element | Wersja minimalna | Wersja pełna |
|---|---|---|
| Ceny EOD | yfinance, wszystkie rynki | + Stooq jako kontrola ręczna (§9.2) |
| FX | NBP tabela A | + Frankfurter jako kontrola |
| 7 wskaźników, 5 reguł ryzyka, sizing | w całości | bez zmian |
| Mianowniki mnożników | **arkusz ręczny, kwartalnie** | pipeline SEC + XBRL PL |
| Sygnał wycenowy | **percentyl w grupie peer** (M36, brak historii) | pełny RVS z reżimem historycznym |
| Reverse DCF | pominięty | kwartalnie, kontekstowo |
| Cztery stany | **tak** — to jest kształt, nie objętość | bez zmian |
| Bramka jakości | tabela per archetyp, wypełniana ręcznie | automat |
| Rejestr tez, dziennik decyzji | plik markdown w repo | tabele |
| Baza | **SQLite, identyczny schemat** | PostgreSQL, migracja jedną komendą |

**Dwa warunki, żeby nie była ślepą uliczką:** identyczny schemat w obu wersjach, oraz **prowenienecja przy wpisie ręcznym** (`source = manual`, `fetched_at`, `currency`, `fixing_id`).

### 25.3 Fazy i kryteria akceptacji

Kryteria sformułowane jako **liczby przewidziane przed pomiarem, bez hedge'y**.

| Faza | Zakres | Kryterium akceptacji | Nakład realny |
|---|---|---|---|
| **F0** | Ręczny replay 5×3; kontrakt danych z `available_at`; definicja czterech stanów; deklaracja konwencji korekt; wersjonowanie peerów w schemacie; modele Pydantic; ok. 20 testów jednostkowych na EV, P/TBV, mNAV, ATR | Replay wykonany, każda kratka ma wskazane źródło albo jawne „brak". Testy przechodzą, w tym **kontrolka dodatnia na znanej wartości** | 3–4 dni |
| **F1** | Repo, baza, schemat, trigger append-only, klienci yfinance + NBP + kalendarze, asercja walut, ingest wszystkich rynków | ≥ 252 sesje dla każdego instrumentu; **asercja waluty przechodzi 100%**; rozbieżność do drugiego źródła <0,5% **tam, gdzie drugie źródło istnieje** (T32); kontrolka dodatnia na instrumencie o znanej liczbie sesji | 2–3 weekendy |
| **F2** | 7 wskaźników, reguły ryzyka, sizing, portfolio heat, raport inicjalizacyjny, Slack, dead-man | Zgodność ATR i RSI z niezależną implementacją **do 4 miejsc**; ≥300 barów rozgrzewki; liczba alertów w raporcie inicjalizacyjnym **przewidziana przed przebiegiem** | 1–2 weekendy |
| **F3** | Arkusz mianowników, RVS na percentylu, cztery stany, silnik decyzyjny, kwarantanna, artefakt tygodniowy | Każdy instrument ma cztery stany albo jawny UNKNOWN z przyczyną; pierwszy pełny cykl tygodniowy bez interwencji | 2 weekendy |
| **F4** | Pipeline SEC XBRL, backfill filings.xbrl.org PL, normalizacja taksonomii i walut | 24 spółki US z pełnymi kwartałami; NVO przeliczone z DKK zgodne z raportem do 1%; ~150 emitentów PL FY2020–FY2023 | **3–4 tygodnie**, nie 2 |
| **F5** | Reverse DCF, MEROI, ROTE implikowane, base rates, PCA/ENB/MCTR, rejestr tez jako tabela | Solver z diagnostyką pierwiastków; przedział WACC zamiast punktu; PCA na ≥120 obserwacjach | 2–3 tygodnie |
| **F6** | Shadow mode | 8–12 tygodni; <15 alertów/mies.; artefakt czytany | — |

**Realny horyzont po godzinach: 5–7 miesięcy do wersji pełnej, 5–8 weekendów do F3.** Nominalne „dwa weekendy" z rewizji 1 było zaniżone — test dwóch źródeł z założenia pali się czerwono do czasu napisania normalizatora konwencji, dochodzi walidacja wskaźników i pierwsze wypełnienie arkusza.

**Gdzie projekt najprawdopodobniej umrze:**
1. **Króliczy dół uzgadniania źródeł w F1.** Mitygacja: start na samym yfinance, Stooq jako ręczna kontrola punktowa, akceptacja „szwy znane i opisane" zamiast twardego progu.
2. **Rytuał kwartalny w drugim i trzecim kwartale**, gdy minie nowość. Mitygacja: stan `STALE` czyni zaniedbany arkusz widocznym zamiast cichej stęchlizny.
3. **Kolejna runda recenzji zamiast kodu.**

---

# CZĘŚĆ VII — REJESTRY

## 26. Opcje odrzucone i uzasadnienia

Rejestr istnieje po to, żeby odrzucone opcje nie wracały co rundę bez nowego argumentu. Kolumna „co by musiało się zmienić" mówi, kiedy decyzję wolno otworzyć ponownie.

### 26.1 Infrastruktura

| # | Opcja | Powód odrzucenia | Co by musiało się zmienić |
|---|---|---|---|
| **O-01** | System jako workflow w istniejącej instancji n8n | Archiwum w logach z retencją; wspólny blast radius z produkcyjnym agentem w trakcie sprintu; regresje i statystyka w węzłach Code bez testów; anti-pattern UI Save overwrite | Nic — decyzja trwała |
| **O-02** | NocoDB jako magazyn danych Mannaza | Brak możliwości wyegzekwowania append-only; brak migracji; wspólny cykl backupu z danymi Franka; zmierzone ciche gubienie `sort` i rozjazd REST vs węzeł | Nic — decyzja trwała |
| **O-03** | Hybryda „silnik Python, transport n8n" dla **wszystkich** komunikatów | Każdy hop to miejsce, gdzie alert liczy się poprawnie i nie dociera; brak alertu nieodróżnialny od braku zdarzenia; ścieżka relay Franka niećwiczona od 2026-06 | Zamknięte częściowo: n8n **przyjęty** dla narracji kwartalnych |
| **O-04** | Zadania cykliczne w Cowork jako architektura docelowa | Brak trwałej bazy → brak archiwum → znika połowa wartości systemu; zmienność wyniku między przebiegami | Dopuszczalne jako prototyp jednorazowy |
| **O-05** | Hosting współdzielony Hostingera | [Z] Brak PostgreSQL i **brak Pythona w ogóle** (nie tylko brak długo działających procesów) | Nic |
| **O-06** | Managed database | [Z] Hostinger nie ma takiego produktu; zewnętrzny dokłada latencję i koszt przy bazie wielkości <1 GB | Skala urosłaby o dwa rzędy |
| **O-07** | TimescaleDB | Przewaga (hypertables, kompresja, continuous aggregates) zaczyna się od dziesiątek milionów wierszy; przy ~150 tys. to zależność i migracje wersji za zero zysku | Wejście danych intraday |
| **O-08** | DuckDB jako baza główna | [Z] Model jednego pisarza: w trybie read-write pisze jeden proces, w read-only nikt nie pisze. Pipeline jest wieloprocesowy (Python + NocoDB + ewentualnie n8n) | Nic — pozostaje jako **biblioteka analityczna** do backtestów |
| **O-09** | SQLite jako baza docelowa | Ten sam problem jednego pisarza; słabsza obsługa dat i stref czasowych | Pozostaje jako **baza wersji minimalnej** |
| **O-10** | KVM 4 | Przy bazie <1 GB i czterech usługach RAM nie jest wąskim gardłem | Wejście danych intraday albo drugi portfel |

### 26.2 Dane

| # | Opcja | Powód odrzucenia | Co by musiało się zmienić |
|---|---|---|---|
| **O-11** | **Massive (Polygon) jako kręgosłup cenowy** | [Z] **Wyłącznie giełdy USA** — potwierdzone dwiema stronami dostawcy. Plan darmowy daje 2 lata historii, za mało na SMA200 + momentum + reżim RVS jednocześnie | Portfel wróciłby do wyłącznie amerykańskiego. Pozostaje **opcjonalnym źródłem wysokiej jakości dla części USA** |
| **O-12** | **Stooq jako źródło importera (bezobsługowy fallback lub kontrola automatyczna)** | [Z] Pomiar 2026-09-25: żądanie skryptowe dostaje stronę JS proof-of-work zamiast CSV; ścieżka `get_apikey` nie wydaje klucza (§9.2) | Oficjalny, działający dostęp skryptowy (klucz albo API). Do tego czasu **źródło ręczne** i jedyne źródło maski korekt `o=` |
| **O-13** | **Rezygnacja z forward P/E i konsensusu** | Amputowałaby EV/NTM Sales dla **NOW (największa pozycja, 9%)**, AXON, RBRK, RKLB, fwd P/E dla megacapów i NVO **oraz pierwszy człon bramki anty-pułapkowej** | Konsensus zostaje, z jawną flagą jakości `low` i **bez roli sygnałowej** (M28) |
| **O-14** | Twelve Data / EODHD / Alpha Vantage / FMP / Tiingo | [Z] Odpowiednio: 3 giełdy na free; 20 wywołań/dzień i rok historii; 25/dzień; kalendarz i peers płatne przy Basic ograniczonym do próbki symboli; fundamenty jako płatny dodatek | Budżet płatny — wtedy porównać tiery pod kątem **pokrycia spoza USA** |
| **O-15** | Płatne API za 19–25 USD/mies. jako rozwiązanie problemu danych | Nie kupuje danych **point-in-time** — plany retailowe podają bieżącą wersję, nie stan z dnia X; prawdziwe PIT kosztuje tysiące. **Archiwum od dnia zero potrzebne tak samo przy 0 jak przy 25 USD** | Decyzja po pierwszym kwartale, na **zmierzonym** czasie wpisu ręcznego |
| **O-16** | OpenBB jako warstwa dostępu | Abstrakcja, nie źródło — pod spodem ten sam yfinance i SEC; zdejmuje utrzymanie wrapperów, ale dokłada warstwę psującą się niezależnie i nie zmniejsza ryzyka zatrucia archiwum | Gdyby ktoś przejął utrzymanie normalizacji wielorynkowej |
| **O-17** | Automatyzacja fundamentów GPW | [Z] filings.xbrl.org bez polskich danych od 2024-07; ESPI za Incapsulą; GPW płatne; Biznesradar bez API i bez zgody regulaminowej; ESAP dopiero 2027-07-10 | **ESAP w lipcu 2027** — wtedy wrócić. Do tego czasu wpis ręczny |
| **O-18** | Własna baza zdarzeń korporacyjnych GPW | [Z] KDPW sprzedaje eksport; praca rzędu tygodni na dane policzone już przez Stooq | Nic — maska `o=` plus kontrola na bankier.pl i gpw.pl |
| **O-19** | `pandas_market_calendars` | Od v2.0 lustrzanie odbija `exchange_calendars`, czyli warstwa pośrednia bez własnego źródła prawdy; wydanie starsze o 14 miesięcy | Nic |
| **O-20** | FRED jako główne źródło FX | [Z] Brak dziennej serii dla złotego; aktualizacja tygodniowa | Zmiana waluty bazowej |
| **O-21** | exchangerate.host / Wise / ExchangeRate-API | [Z] 100 zapytań/miesiąc; wymaga tokenu konta; brak szeregów historycznych | Nic |
| **O-45** | S&P / Kensho (Capital IQ przez MCP) jako źródło danych fundamentalnych | Wymaga płatnego konta; brak konta (decyzja 2026-09-25) | Budżet na dane > 0 |

### 26.3 Model analityczny

| # | Opcja | Powód odrzucenia | Co by musiało się zmienić |
|---|---|---|---|
| **O-22** | P/E, forward P/E i P/S jako jednolita siatka | Lewarowane (liczą equity, nie firm value); P/S najgorszy w hierarchii dokładności; dla A5, A7, A8 wprost niewłaściwe | Nic |
| **O-23** | **Reszta z regresji mnożnika na fundamentach jako sygnał główny** | Przy `n = 8` krytyczne R² ≈ 0,50; próg 0,15 to p ≈ 0,34 | Zastąpione przez RVS |
| **O-24** | **Regresja jako „warstwa druga wyjaśniająca"** | Mimo etykiety **moduluje** sygnały („rozjazd degraduje do wpisu w raporcie"), czyli losowo wycisza prawdziwe okazje na podstawie modelu nieodróżnialnego od szumu | Panel ≥12 peerów z poolingiem w czasie i testem out-of-sample — przy maksymalnej grupie ~16 praktycznie nigdy |
| **O-25** | Ciągła korekta mnożnika o jakość (RQ jako liczba) | Wymagałaby regresji (O-23/O-24). Jakość jako **kierunek zmian** jest odporna przy małym n, jako **poziom** — nie | Jak O-24 |
| **O-26** | „Mnożnik poniżej własnej mediany 3-letniej" jako **twarda bramka** | Zbyt koniunkcyjne: odrzuca spółkę, która strukturalnie poprawiła jakość i dlatego jej mnożnik trwale wzrósł. W wersji minimalnej historii nie ma przez rok → sygnał martwy | Przeniesione do rankingu conviction (M13) |
| **O-27** | Jedna uniwersalna bramka anty-pułapkowa | Bramka SaaS-owa zastosowana do banku, kopalni bitcoinów i farmacji — ten sam błąd kategorii, który M4 zarzuca mnożnikom | Zastąpione bramkami per archetyp (M15) |
| **O-28** | **Pełne Smart Money Concepts** | 0/54 wariantów rentownych na 2,55 mln barów po koszcie 0,5 pipsa; brak recenzowanej walidacji dla OB/BOS/CHoCH; główna biblioteka Pythonowa ma look-ahead (PF 7,32 → 1,82 po naprawie); struktura swing na D1 potwierdza się po ~2,5 miesiąca; licencja CC BY-NC-SA 4.0 | Nic. Zostają FVG, poziomy MTF, premium/discount — **jako kontekst, bez wpływu na decyzję** |
| **O-29** | MACD, RSI 30/70, ADX, OBV, anchored VWAP, Connors RSI | Redundancja albo brak recenzowanego potwierdzenia; szczegóły w §18 | Nic |
| **O-30** | Beneish M-Score w silniku | Przy realnej częstości manipulacji 1–2% i 17,5% fałszywych alarmów większość alertów byłaby fałszywa | Zostaje jako **wyzwalacz przeglądu ręcznego**, poza automatem |
| **O-31** | Reverse DCF jako generator alertów | ICC oparte na konsensusie analityków **nie predykuje** zwrotów (t < 1,40), w przeciwieństwie do opartego na modelu (t = 3,77–5,39); nasz konsensus to yfinance z flagą `low` | Model statystyczny zamiast konsensusu — poza zakresem |
| **O-32** | Exit multiple w wartości rezydualnej | Cyrkularny: mnożnik bierze się od peerów, a pytanie brzmi, czy cena jest wymagająca; przy przewartościowanym sektorze model nigdy nie powie „drogo". Dla CRCL i RKLB nie ma od kogo wziąć | Nic |
| **O-33** | Reverse DCF dla A5 (NU, KLAR) | Dług jest surowcem → WACC niedefiniowalne; reinwestycji nie da się zmierzyć; ograniczenia regulacyjne niewidoczne dla DCF | Zastąpione formą zamkniętą P/TBV ↔ ROTE |
| **O-34** | Score jakości danych 0–100 | Addytywne wagi sugerują precyzję, której konstrukcja nie ma | Ocena A/B/C z jawnymi kryteriami |
| **O-35** | Uśrednianie percentyla, CAP i MEROI w jeden wskaźnik | Nie są niezależne — wywodzą się z tego samego równania; policzyłoby tę samą informację trzy razy | Nic |
| **O-36** | „Nie dokładaj poniżej 30% od maksimum" jako **ostrzeżenie** | Reguła ma funkcję behawioralną, nie informacyjną. Asymetria: koszt fałszywej blokady to nieodebrany zysk, koszt fałszywego pozwolenia to kapitał w spółce, która dalej spada | Zostaje blokadą z **nazwanym override'em w dzienniku** |
| **O-37** | Słabość relatywna jako automatyczne wyjście | Sygnał kupna brzmi „tania względem peerów", więc system kupowałby to, co sprzedaje | Uwarunkowane stanem VALUE (M26) |
| **O-38** | Cena docelowa jako liczba wpisana przy zakupie | Nie aktualizuje się wraz z wynikami spółki | Zastąpione percentylem implikowanych założeń (M55) |

### 26.4 Operacje

| # | Opcja | Powód odrzucenia |
|---|---|---|
| **O-39** | **Przebieg dobowy jako kadencja podstawowa** | Ceny są odtwarzalne wstecz; wskaźniki są wolne; twarde stopy wykonuje broker (M62). Ocena dobowa daje ~5× więcej okazji do fałszywego przekroczenia progu przy tej samej informacji |
| **O-40** | Przesunięcie przebiegu na 05:30 UTC „bo dane potrzebują 2–3 h po zamknięciu USA" | Arytmetycznie błędne: przebieg 04:30 UTC jest **następnego dnia rano**, 7,5–8,5 h po zamknięciu. Realny problem to późniejsze doprecyzowanie sesji — rozwiązany odświeżaniem okna D−5 (M67) |
| **O-41** | Heartbeat raportowany przez system jako dead-man | Martwy proces nie wysyła „nie żyję" |
| **O-42** | Codzienny raport „nic się nie zmieniło" | Uczy ignorowania kanału; kanał ignorowany jest gorszy niż brak kanału |
| **O-43** | Backtest całego systemu przed shadow mode | Przy tej liczbie parametrów produkuje maszynę do data-miningu, nie walidację |
| **O-44** | Współdzielenie feedu cenowego z Frankiem | Różna konwencja korekty (chart analysis vs total return), małe przecięcie tickerów, [N] źródło cen Franka niezweryfikowane |

## 27. Ryzyka i ograniczenia

| # | Ryzyko | Klasa | Mitygacja |
|---|---|---|---|
| R-01 | **yfinance przestaje działać albo zmienia schemat** — nieoficjalne API, ToS „personal use only", zero SLA, a dla portfela wielorynkowego **jedyne jednolite źródło** | krytyczne | Cache z TTL, circuit breaker (T12), Stooq jako ręczna kontrola punktowa tam, gdzie pokrywa (automatycznej drugiej kontroli brak — §9.2), stan `FAILED` zamiast cichego zera |
| R-02 | **Błąd jednostki waluty (GBX/GBP)** przechodzi wprost w sizing i stop — błąd 100× | krytyczne, ciche | Asercja waluty per giełda (T7), sanity-check `\|log return\| > 4` (T8) |
| R-03 | **Zamrożony mnożnik** przy przechowywaniu mnożników zamiast mianowników | krytyczne, ciche | T17 |
| R-04 | **Ujemny runway** przy `burn ≤ 0` wystawia zdrowe megacapy jako pułapkę | wysokie, ciche | T18 |
| R-05 | **Zmiana składu peerów** (delisting, fuzja, sign-flip mianownika, różny sezon wyników) porusza medianę bez zdarzenia po stronie spółki | wysokie, ciche | M7, M33, M34, M38, P-05 |
| R-06 | **Dryf bazy RVS** wygasza sygnał bez repricingu | średnie, ciche | M37, M38 |
| R-07 | **Asynchroniczność zamknięć** zaniża korelację → zawyża efektywną liczbę zakładów → fałszywe poczucie dywersyfikacji | wysokie, ciche | M21, T33 |
| R-08 | **Fixing FX nie pokrywa się z zamknięciem** żadnej giełdy poza HK i TSE | strukturalne | T22–T24: zadeklarować jeden i nie mieszać |
| R-09 | **Dławienie CPU na Hostingerze** −25%/godz., odblokowanie raz w tygodniu, szkoda spada na produkcyjnego Franka | wysokie | KVM 2, `cpus:` i `mem_limit:`, `nice`, rozłożenie cronów |
| R-10 | **Backup tygodniowy** to w najgorszym razie utrata 7 dni archiwum | wysokie | własny `pg_dump` do zewnętrznej lokalizacji, codziennie |
| R-11 | **Rytuał kwartalny zamiera** w drugim–trzecim kwartale | wysokie | stan `STALE` czyni zaniedbanie widocznym |
| R-12 | **Liczba testów**: ~31 instrumentów × ~5 typów sygnału × tygodniowo ≈ 8 000 testów rocznie — część alertów fałszywa z konstrukcji | metodologiczne | Zadeklarować **oczekiwaną liczbę alertów przed uruchomieniem**; odchylenie to defekt kalibracji, nie sygnał rynkowy |
| R-13 | **Relative value działa epizodycznie** — Magic Formula 26%/rok 2004–2007, potem 57% obsunięcia i wyniki poniżej benchmarku po 2010 | metodologiczne | Bramka jakości jako warunek konieczny (Piotroski: sam mnożnik 5,9%/rok, z filtrem jakości 13,4%) |
| R-14 | **Brak recenzowanej walidacji reverse DCF** jako strategii | metodologiczne | M28 — narzędzie opisowe, nie generator sygnałów |
| R-15 | **Archiwum zaczyna się w dniu zero** dla konsensusu i kwartałów FPI (nadpisywane w miejscu). Ceny i fundamenty as-reported **są** odtwarzalne z EDGAR | operacyjne | M65 — snapshot nieodtwarzalnych strumieni dwa razy w tygodniu, od zaraz |
| R-16 | **filings.xbrl.org może wycofać API** — regulamin wprost to zastrzega, a to jedyne darmowe czyste XBRL dla polskich spółek | wysokie, nieodwracalne | Backfill FY2020–FY2023 jako pierwsze działanie (P-06) |

## 28. Pomiary do wykonania i pytania otwarte

Każdy pomiar ma zdefiniowany test i konsekwencję wyniku. Wszystkie read-only.

| # | Pomiar | Test | Co zmienia |
|---|---|---|---|
| **P-01** | Źródło cen Franka i jego konwencja korekty | Odczyt **konfiguracji węzła** w workflow, nie nazwy | Rozstrzyga, czy jakikolwiek reuse feedu jest w ogóle rozważalny (§7.3) |
| **P-02** | Kierunek flagi w masce `o=` Stooq | Ręczne pobranie z przeglądarki spółki ze znanym splitem z `o=0000000` i `o=1111110`, porównanie z ceną przed splitem | Pomyłka zatruwa cały szereg GPW |
| **P-03** | **Czy HKD jest w tabeli A NBP** | `api.nbp.pl/api/exchangerates/rates/a/hkd/?format=json` — 404 oznacza tabelę B | Przy tabeli B kurs raz w tygodniu → Frankfurter jako podstawa dla HKD, z jawnym zapisem różnego fixingu |
| **P-04** | Poprawność kalendarza XWAR | Różnica symetryczna między `XWAR.sessions_in_range()` a datami z pobranych szeregów | Kalendarz twierdzący, że była sesja, dla której nie ma danych, jest błędny albo dane mają dziurę |
| **P-05** | **Rewalidacja całej tabeli peer** | Sprawdzenie istnienia i statusu każdego z ~150 tickerów | PARA już nie istnieje; WBD w trakcie zmian [N] |
| **P-06** | Backfill filings.xbrl.org PL, FY2020–FY2023 | `filter[country]=PL`, pobranie paczek | Dane nieodtwarzalne po wycofaniu API |
| **P-07** | Czy VPS ma przypisaną domyślną grupę firewalla | Panel Hostingera | **Zamknięty 2026-09-25:** grupa `frank-web` przypisana i aktywna (TCP 22/80/443); IPv6 niezmierzone |
| **P-08** | Budżet czasowy przebiegu o docelowej godzinie | Pomiar `duration_sec` o 04:30 i 07:00 UTC, n ≥ 5 | Zmierzona anomalia: 91–97 s o 06:00 vs 25,1 s o 09:56 |
| **P-09** | Zapas RAM i dysku na VPS przy kontenerze Mannaza oraz szczyt Franka | Monitoring hPanel przez tydzień | Wartości `mem_limit:` i `cpus:` kontenera Mannaza (rew. 3: decyzja o upgrade KVM 1 → KVM 2 nieaktualna — VPS to KVM 2) |
| **P-10** | Efektywna liczba zakładów po korelacji | PCA na ≥120 zwrotach tygodniowych, shrinkage | HHI daje 21,5 **wg wag**; po korelacji będzie istotnie niżej. **Zmienia sizing** |
| **P-11** | Realny czas wpisu ręcznego mianowników | Chronometraż pierwszego kwartału | Próg decyzji o płatnym API (O-15) |
| **P-12** | Zapis symboli GPW na stooq.com | Wyszukiwarka symboli stooq.com dla PKN; porównanie szeregu ze stooq.pl | Rozstrzyga wiersz 3 checklisty §12 (pomiar 2026-09-25: `pkn.pl` nie istnieje) |

**Pytania otwarte, należące do właściciela:**

1. **Skład satelity.** Czy 31 pozycji plus GPW to docelowa liczba, czy podłoga konwikcji (M53) ma być zastosowana przed startem? To zmienia rozmiar problemu o ok. 30%.
2. **Waluta raportowania.** M1 zakłada PLN jako bazową. Do potwierdzenia, czy widok „Mannaz USD" ma pozostać osobną perspektywą, czy zostaje wyłącznie ujęcie w PLN.
3. **Benchmark całego satelity.** QQQ? Koszyk odzwierciedlający skład rynkowy? Coś w PLN? Od tego zależy M59 — najostrzejszy test całego przedsięwzięcia.
4. **Cel zmienności portfela.** Poziom 3 budżetu ryzyka (15%) jest punktem startowym; docelowy poziom trzeba **zmierzyć, nie zgadnąć**.
5. **Czy „straż cenowa" dobowa jest potrzebna**, skoro twarde stopy leżą u brokera (M62).

---

*Dokument opisuje projekt narzędzia analitycznego. Nie jest rekomendacją inwestycyjną ani doradztwem. Wszystkie progi są punktami startowymi do kalibracji na własnych danych. Dane rynkowe i wyceny spółek wymagają weryfikacji w momencie użycia.*
