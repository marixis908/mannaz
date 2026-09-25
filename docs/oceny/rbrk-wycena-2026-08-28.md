# RBRK — test wyceny po Q2 FY27 (28.08.2026)

Artefakt: https://claude.ai/code/artifact/96114256-40a4-4550-a5e4-73962ea92f68

Pozycja: waga 3,42% · archetyp A3 (SaaS GAAP-ujemna) · mnożnik główny EV/ARR · EV/NTM Sales.
Jakość danych: **B** (jedno źródło ceny). Konsensus: flaga `low`, bez roli sygnałowej (M28).

## 1. Podsumowanie w formacie M71

1. **Trafienie** — przychód 427,3 mln vs ok. 396 mln konsensusu (+7,9%); non-GAAP EPS 0,20 vs 0,04. Raport w terminie. Dziesiąte z rzędu przebicie wszystkich guidowanych metryk.
2. **Prognoza** — podniesiona: ARR FY27 1 880–1 885 mln (+29% r/r), przychód 1 685–1 693 mln, FCF 323–333 mln, EPS 0,47–0,53. YTD net new ARR w górę o ok. 45 mln (>10%). Strata i Rubrik Agent Cloud **poza** guidance.
3. **KPI archetypu** — ARR +33%; NRR >119%; **SBC 23,9% przychodu**; rozwodnienie +5,75% r/r; marża wkładu ARR 14,0% (+460 pb).
4. **Efekt na wycenę** — EV/sprzedaż FY27 13,4× → 12,2×. Mianownik w górę, licznik w dół: **spadek to czysta kompresja mnożnika**, nie pogorszenie liczb. Peerzy bez ruchu.
5. **Test tezy** — OK. Obserwacja: implikowane H2 net new ARR +7% r/r wobec +26% w H1.
6. **Akcja** — brak natychmiastowej. VALUE → EXPENSIVE, ale sygnał RVS niewiarygodny (patrz §4). Do przeglądu miesięcznego.

## 2. Poziom wyceny

EV liczone jako 217,6 mln akcji (206,097 bazowych + 11,532 ekwiwalentów) + 1 132,9 dług zamienny − 1 747,6 gotówka i inwestycje krótkoterminowe.

| Kurs | EV | EV/S FY27 | EV/ARR dziś | EV/ARR wyjściowe | EV/FCF FY27 |
|---|---|---|---|---|---|
| 107,02 (zamkn. 27.08) | 22,68 mld | 13,4× | 13,7× | 12,1× | 69× |
| **97,12 (po wynikach)** | **20,52 mld** | **12,2×** | **12,4×** | **10,9×** | **63×** |
| 90,00 | 18,97 mld | 11,2× | 11,4× | 10,1× | 58× |
| 80,00 | 16,80 mld | 9,9× | 10,1× | 8,9× | 51× |

Zmienna towarzysząca (M5): wzrost przychodu +38%, ARR +33%, marża brutto non-GAAP 81%, marża FCF 15%. Rule of 40 = 53 (przychód + FCF) / 47 (ARR + marża wkładu).

## 3. Test Motley Fool — ile sprzedaży 2036 płacimy

Metoda z noty „Sell Some CrowdStrike" (Gardner, 27.08.2026): CRWD przy >40× sprzedaży ≈ **7× sprzedaży oczekiwanej na 2036** → trym 15%.

| Scenariusz | Przychód FY37 | EV / sprzedaż 2036 |
|---|---|---|
| Byczy 25%/15% | 10,37 mld | **2,0×** |
| Bazowy 22%/12% | 8,04 mld | **2,6×** |
| Niedźwiedzi 18%/8% | 5,68 mld | **3,6×** |
| CRWD dziś | — | ok. 7× |

**Wniosek:** w kategoriach Foola Rubrik nie dotyka progu „bardzo wymagającej" wyceny. Nawet scenariusz niedźwiedzi daje połowę tego, przy czym Fool tnie CRWD.

Reverse DCF na marży docelowej (model absolutny dla A3): przy oczekiwanym zwrocie 10% rocznie i wyjściu na 25× FCF, Rubrik musi w FY37 generować **2,13 mld FCF** = ok. 20% przychodu (scen. byczy) / 26% (bazowy). CRWD osiąga dziś ~30%. Mieści się w tym, co branża potrafi — bez zapasu na rozczarowanie.

## 4. RVS — grupa peer wymaga naprawy [Z]

Forward P/S, stan 28.08.2026: CRWD 35,2 · NET 33,7 · **RBRK 11,9** · ZS 8,1 · S 6,2 · NTNX 5,9.

- RVS vs mediana (8,08) = **1,47** (przy 107,02 było 1,62)
- RVS vs średnia harmoniczna (9,74) = **1,22**

**Dwa defekty do P-05:**

1. **CYBR nie istnieje** — delisting 11.02.2026, przejęcie przez Palo Alto Networks. Ta sama klasa błędu co PARA w wierszu NFLX. Naturalny następca: **PANW**.
2. **Rozstęp 5,9×–35,2× to nie jedna grupa.** Dwa reżimy: premium AI (CRWD, NET) i dojrzała wartość (ZS, S, NTNX). Mediana z sześciu obserwacji na dwóch skupieniach przeskakuje między reżimami przy dowolnej zmianie składu — zgodnie z M32 to dowód źle zdefiniowanej grupy. Rozjazd mediana/harmoniczna (1,47 vs 1,22) mierzy właśnie to.

**Do czasu naprawy RVS dla RBRK nie powinien mieć roli sygnałowej.** Instrument działa efektywnie w trybie bliskim absolutnemu (M35): test dekady i reverse DCF są tu narzędziem głównym, nie pomocniczym.

## 5. Jakość gotówki — SBC jako metryka pierwszego rzędu

| Pozycja (mln USD) | TTM | Q2 anual. | FY27 guide |
|---|---|---|---|
| FCF | 304 | 263 | 328 |
| SBC | 343 | 409 | ~400 |
| **FCF − SBC** | **−39** | **−146** | **≈ −75** |
| SBC / przychód | 22,2% | 23,9% | ~23% |
| Rozwodnienie r/r | +5,75% | — | — |

SBC TTM = 320,7 opex + 18,5 koszt subskrypcji + 3,7 w amortyzacji oprogramowania. Różnica między stratą GAAP 61,8 mln a zyskiem non-GAAP 44,7 mln w kwartale to w całości SBC.

**To jest realne ryzyko wyceny w tej pozycji**, nie 12× sprzedaży. 63× FCF liczone jest od gotówki, która jeszcze nie należy do akcjonariusza; przy rozwodnieniu 5,75% rocznie kurs musi rosnąć o tyle, żeby wartość na akcję stała w miejscu. Domknięcie luki wymaga mniej więcej podwojenia przychodu przy SBC utrzymanym nominalnie.

## 6. Mostki (analiza odchyleń)

**Przychód Q2 FY27 vs Q2 FY26:** 309,9 → 427,3 (+117,4, +37,9%)
- subskrypcja bez material rights **+119,7** (+102% odchylenia)
- material rights, wygasanie **−9,5** (−8%)
- przychody pozostałe **+7,2** (+6%)
- po normalizacji o material rights: **+43%**

**ARR:** ARR wyjściowe FY26 = 1 459 (odtworzone z „1 880–1 885, +29% r/r")
- Q2 FY27 net new **96** (+35% r/r)
- H1 FY27 net new **202** (+26% r/r)
- H2 FY27 implikowane **222** (**+7% r/r**) — jedyny niepokojący wiersz raportu i jednocześnie najsłabszy dowód: 10 kwartałów przebić sugeruje poduszkę, nie prognozę. Adjusted net new Cloud ARR rośnie ok. 20%.

## 7. Kryteria falsyfikacji

**Teza pęka, jeśli:** net new ARR spada r/r dwa kwartały z rzędu · NRR < 115% · SBC nie spada jako % przychodu przez cztery kwartały mimo wzrostu · rozwodnienie > 7% rocznie.

**Teza się wzmacnia, jeśli:** Identity Resilience dostaje osobną linię ARR · Rubrik Agent Cloud wchodzi do guidance · marża wkładu ARR utrzymuje +460 pb r/r · ARR non-cloud utrzymuje ok. 10%.

## 8. Konkluzja

Kurs jest wymagający w tym sensie, że samo dowożenie przestało wystarczać — potrzebne są podniesienia guidance, a nie ich wykonanie; dokładnie to pokazał spadek o 9% po podniesionej prognozie. Ale w kategoriach Motley Fool (40× sprzedaży, 7× dekady) Rubrik progu nie dotyka: 12× i 2–3,6× dekady przy szybszym wzroście. Stosując regułę Foola konsekwentnie, przycięcie należy się CRWD, nie RBRK.

**Stan: VALUE = EXPENSIVE · QUALITY = IMPROVING · decyzja = HOLD.** Brak podstaw do sprzedaży; brak podstaw do dokładania przed naprawą grupy peer.

---
Źródła: raport bieżący Rubrik Q2 FY2027 (27.08.2026); Stock Advisor „Sell Some CrowdStrike" (27.08.2026); StockAnalysis (mnożniki peerów, 28.08.2026); DNA Rynków. Mnożniki policzone samodzielnie z danych źródłowych. Nie jest doradztwem inwestycyjnym.
