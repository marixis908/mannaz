# RBRK — pasmo historyczne wyceny i test Cymcyka (09.09.2026)

Aktualizacja do `claude/rbrk-wycena-2026-08-28.md`. Artefakt (ta sama strona, nowa wersja): https://claude.ai/code/artifact/96114256-40a4-4550-a5e4-73962ea92f68

Kurs 08.09.2026: **91,63 USD** (−15% od szczytu 52T 107,91; +117% od dołka 52T 42,25; +0,8% r/r).
EV wg konwencji z poprzedniej notatki (217,6 mln akcji, dług zamienny 1 132,9, gotówka 1 747,6): **19,33 mld USD**.

| Mnożnik | Wartość |
|---|---|
| EV/sprzedaż TTM (1 542) | **12,53×** |
| EV/sprzedaż FY27 (1 689) | **11,44×** |
| EV/ARR wyjściowe FY27 (1 882,5) | **10,27×** |
| EV/FCF FY27 (328) | **59×** |

## 1. Historia wyceny od IPO (kwiecień 2024)

EV/sprzedaż TTM na koniec kwartałów obrotowych, źródło: StockAnalysis za S&P Global Market Intelligence. Metodologia jednolita, bez wiedzy ex post.

| Data | EV (mln) | EV/S TTM |
|---|---|---|
| 2024-04-30 | 6 389 | 9,40× |
| 2024-07-31 | 6 400 | 8,73× |
| 2024-10-31 | 7 211 | 8,97× |
| 2025-01-31 | 13 267 | **14,97×** (szczyt) |
| 2025-10-31 | 14 508 | 12,12× |
| 2026-01-31 | 10 761 | 8,18× |
| 2026-04-30 | 10 437 | **7,33×** (dołek) |
| 2026-07-31 | 14 291 | 9,27× |
| **08.09.2026** | **19 327** | **12,53×** |

**Średnia = 9,87× · mediana = 9,12× · pasmo 7,33–14,97×.**

Dziś: **+27% powyżej średniej, +37% powyżej mediany, −16% poniżej szczytu.** Górna ćwiartka własnego pasma, ale nie rekord.

Braki danych [Z]: w źródle nie ma snapshotów za kwiecień i lipiec 2025. Snapshoty kwartalne nie łapią ekstremów wewnątrzkwartalnych — 27.08.2026 mnożnik dotknął ok. 13,5×.

**Kursy implikowane przez powrót mnożnika:**

| Mnożnik EV/S TTM | Kurs |
|---|---|
| 14,97× (szczyt sty-2025) | 108,89 |
| **12,53× (dziś)** | **91,63** |
| 9,87× (średnia od IPO) | **72,76** |
| 9,12× (mediana) | 67,44 |
| 7,33× (dołek kwi-2026) | 54,76 |

## 2. Wideo Piotra Cymcyka / DNA Rynków (8.09.2026)

„Rynek oszalał na punkcie spółek cyberbezpieczeństwa. Ja mówię: STOP" — 19:39, transkrypcja odczytana.

**Oś czasu narracji sektora, którą buduje:** kwiecień 2026 — premiera Claude Mythos, panika, wyprzedaż sektora w przekonaniu, że AI zastąpi te firmy → maj–czerwiec — instytucjonalizacja zagrożenia (rozporządzenie Białego Domu 2.06, oświadczenie Five Eyes) → lipiec — materializacja (agent OpenAI) → sierpień — potwierdzenie w rekordowych wynikach. Narracja odwróciła się o 180° w kilka tygodni.

**Jego zarzut dotyczy cen, nie biznesów:**

| Spółka | Mnożnik dziś | Własna średnia | Rerating | Wzrost przychodu |
|---|---|---|---|---|
| PANW | 22× fwd EV/S | ok. 11× | +100% | +34% |
| CRWD | 32× EV/S | ok. 20× | +60% | +26% |
| FTNT | jak w szczycie 2021 | — | +100% YTD | +26% |
| **RBRK** | **12,5× EV/S TTM** | **9,9×** | **+27%** | **+38%** |

(Mnożniki peerów za wideo, nieweryfikowane niezależnie. RBRK policzony samodzielnie.)

**Rubrik jest w tej stawce najmniej przereratowany i najszybciej rosnący.** O Rubriku autor mówi tylko opisowo (model biznesowy „plan na moment, w którym mur padnie", dwa filary: Security Cloud + Agent Cloud, „najszybciej rosnąca spółka w zestawieniu"); mnożnika RBRK nie podaje. Deklaruje, że miał w portfelach CRWD, RBRK i S, ale „teraz się na to nie rzuca".

**Jego trzy warunki powrotu do sektora, przyłożone do RBRK:**

1. Mnożniki wracają do własnych średnich → dla RBRK ok. **73 USD** — **NIE**
2. Net new ARR liderów rośnie po 50% → RBRK +35% w Q2, implikowane +7% w H2 — **NIE**
3. Narracja stygnie → **NIE**

Zero z trzech. To argument przeciwko powiększaniu pozycji, nie za jej sprzedażą.

## 3. Test Cymcyka zastosowany do RBRK (ostrzejszy niż test dekady)

Jego rachunek dla CRWD: 3 lata wzrostu po 26% bez potknięcia + powrót mnożnika 32→20 = ok. +25% łącznie, **~8% rocznie**.

Ten sam rachunek dla RBRK, **z rozwodnieniem 5,75% rocznie**, baza przychodu FY27 = 1 689 mln, gotówka netto 615 mln:

| Wzrost przychodu | mnożnik → 8,5× fwd | → 9,9× | mnożnik zostaje 11,4× |
|---|---|---|---|
| 30% rocznie | +10,9% | +16,4% | +22,1% |
| **25% rocznie** | **+6,8%** | +12,0% | +17,5% |
| 20% rocznie | +2,6% | +7,7% | +12,8% |

**Wniosek:** w horyzoncie trzech lat, przy 25% wzrostu i powrocie mnożnika do własnej średniej, Rubrik daje ok. **7% rocznie** — mniej więcej tyle co szeroki rynek, przy ryzyku pojedynczej spółki. Cała nadwyżka wisi na tym, żeby wzrost był bliżej 30% niż 20% **oraz** żeby mnożnik nie wracał do średniej.

**Rozwodnienie zjada 5,7 pp rocznie z każdego scenariusza** — więcej niż jakikolwiek pojedynczy czynnik operacyjny. Test dekady z notatki z 28.08 (2,4× sprzedaży 2036) jest z natury łagodny, bo ignoruje rozwodnienie i zakłada, że cierpliwość jest darmowa. Ten rachunek jest uczciwszy.

## 4. Trzy linijki, trzy odczyty — wszystkie prawdziwe

- **Wobec peerów** — tani: 11,4× przy wzroście 38%, gdy CRWD płaci 32× przy 26%.
- **Wobec metody Motley Fool** — daleko od progu: 2,4× sprzedaży 2036 wobec 7× u CRWD.
- **Wobec własnej historii** — drogi: 12,5× przy średniej 9,9× i paśmie 7,3–15,0×.

Trzecia linijka jest tu najmocniejsza, bo jako jedyna opiera się na danych o tej spółce, a nie na porównaniu z sąsiadami, którzy sami przereratowali się o 60–100%.

## 5. Decyzja bez zmian

**VALUE = EXPENSIVE (wobec własnej historii, nie wobec peerów) · QUALITY = IMPROVING · decyzja = HOLD.**

Próg zainteresowania dokładaniem: powrót mnożnika do własnej średniej (**ok. 73 USD**) albo dowód, że net new ARR w H2 łamie implikowane +7%. Do przeglądu miesięcznego.

---
Źródła: StockAnalysis / S&P Global Market Intelligence (historia mnożników, kurs 08.09.2026); wideo Piotra Cymcyka „Rynek oszalał na punkcie spółek cyberbezpieczeństwa. Ja mówię: STOP", DNA Rynków, 8.09.2026 (transkrypcja); raport Rubrik Q2 FY2027. Nie jest doradztwem inwestycyjnym.
