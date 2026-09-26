# Backlog

`status`: otwarte / zamknięte / odrzucone. Zamknięcie lub odrzucenie zmienia `status`
i uzupełnia datę — wiersz zostaje.

| id | pozycja | status | otwarte | zamknięte | uwagi |
|---|---|---|---|---|---|
| B-01 | Skrypty Pine (TradingView) w scripts/pine/ | otwarte | 2026-09-25 |  | us_b, us_c, eu_2 w d92b9f5; us_a w 15acbb3; strażnik US-B w 5ee8dbc; zamknięcie po teście strażnika w TradingView (owner) |
| B-02 | Rewizja 3 dokumentu projektowego: §28 P-07 → zamknięty (firewall frank-web aktywny, 22/80/443, pomiar 2026-09-25); P-09 → pomiar zapasu RAM zamiast decyzji o upgrade (VPS to już KVM 2); §5.5 zdanie o KVM 1 rozstrzygnięte | zamknięte | 2026-09-25 | 2026-09-25 | 32ddd50 |
| B-03 | Odświeżenie kopii dokumentu projektowego w projekcie Claude po każdej zmianie w repo | otwarte | 2026-09-25 |  | kopia jest pochodną, sama się nie synchronizuje |
| B-04 | Magazyn danych: „docelowo w NocoDB” (decyzja 25.09) vs §6/§7.3 (NocoDB tylko podgląd) i §5.5 (wspólna instancja Postgresa vs osobny kontener) | zamknięte | 2026-09-25 | 2026-09-25 | osobny kontener PostgreSQL; decyzja w docs/decyzje.md |
| B-05 | Konwencja „raw” yfinance: `auto_adjust=False` bywa retroaktywnie skorygowany o splity, także bez rekordu splitu (SPYI.DE ~7 EUR vs broker ~170 EUR) — do dokumentu §9/§10 | otwarte | 2026-09-26 |  | **priorytet**; rew. 5; w kodzie: close_raw odtwarzany ze zdarzeń, detektor splitów z ilorazu ceny zostaje |
| B-06 | Semantyka zapadki Chandeliera na dzień zero | otwarte | 2026-09-26 |  | rozwiązane erratum §19.3 w rew. 4 (zapadka od inicjalizacji); przegląd w shadow mode |
| B-07 | σ w OSTRZEŻENIU (§19.1): odchylenie populacyjne czy z próby | otwarte | 2026-09-26 |  | kod: `statistics.pstdev` |
| B-08 | Migawka pozycji brokera jako warunek prawdziwej kontroli FIFO (P2.6) | otwarte | 2026-09-26 |  | bez migawki kontrola jest kołowa |
| B-09 | MDV + FMDVZ26 jako jedna nazwa w budżecie poziomu 1 (§19.2) | otwarte | 2026-09-26 |  | decyzja ownera 2026-09-26; rew. 5; kod bez zmian |
| B-10 | Budżet poziomu 2 na korelacjach zamiast tematów (M50) | otwarte | 2026-09-26 |  | propozycja nadzorcy |
| B-11 | VRC → temat software; archetyp CBF (klucz v2: A2, opis ownera: A4) | otwarte | 2026-09-26 |  | czeka na ownera |
| B-12 | Kapitał satelity: wartość rachunku KONTRAKTOWY (środki + wynik zmienny) zgodnie z §19.4 | otwarte | 2026-09-26 |  | [N] kod dziś jej nie dolicza |
| B-13 | Ryzyko kontraktów poza heat i poza budżetem tematów w `risk.py` — sprzeczne z §19.4 | otwarte | 2026-09-26 |  | pomiar D=2026-09-24: heat satelity 3,251 % bez kontraktów vs 3,997 % z kontraktami |
| B-14 | Poza zakresem przekroju B/R/K (brief CC-P): pipeline cykliczny F0–F6, RVS, alerty, reverse DCF, baza na VPS | otwarte | 2026-09-26 |  | osobne briefy |
| B-15 | NU: średnia brokera wyższa o 0,06 USD/szt. od kosztu rezydualnego FIFO | otwarte | 2026-09-26 |  | [N]; hipoteza: prowizje/opłaty wliczone przez brokera w koszt jednego lotu; test: przeliczenie FIFO NU z opłatami |
| B-16 | Porównanie FIFO z eksportem pozycji brokera jako moduł w `src\` (dziś skrypt `tmp\przekroj\compare_b2.py`, poza gitem) | otwarte | 2026-09-26 |  | warunek powtarzalnej kontroli P2.6 (B-08) |
