# Dziennik decyzji

Nowe wpisy na dole. Decyzji się nie usuwa — zmianę zapisuje się jako nowy wpis, który
wskazuje, co zastępuje.

| data | decyzja | kto | uzasadnienie |
|---|---|---|---|
| 2026-09-06 | Mannaz stoi na własnym stacku, w osobnym repo | owner | dokument projektowy §6 |
| 2026-09-25 | Repo na prywatnym koncie `marixis908`, repo prywatne | owner | — |
| 2026-09-25 | Dane finansowe poza gitem od pierwszego commita: historie transakcji, migawki z kwotami, eksporty z brokera, kopie bazy | owner | — |
| 2026-09-25 | Lekka struktura repo, bez maszynerii z `frank-daily` (bramka rejestru, `state.json`, regeneracja stanu) | owner | — |
| 2026-09-25 | Od pierwszego commita repo jest źródłem prawdy dokumentu projektowego; plik w projekcie Claude jest jego kopią | owner | dokument nie miał innego oryginału niż plik w projekcie Claude |
| 2026-09-25 | Magazyn danych Mannaza: osobny kontener PostgreSQL, nie wspólna instancja z wewnętrznym Postgresem NocoDB Franka; rola NocoDB wobec danych Mannaza bez zmian (§6, O-02) | owner | blast radius (§6 pkt 2); ~300 MB RAM pomijalne na KVM 2; zastępuje rekomendację §5.5 rew. 2 |
| 2026-09-25 | Strażnik kalendarza (NYSE) w skrypcie Pine US-B | owner | ta sama klasa błędu co eksport pliku C z wykresu skryptu B |
| 2026-09-25 | Rewizja 3 dokumentu projektowego: P-07 zamknięty, P-09 przeformułowany, §5.5 | owner | pomiary 2026-09-25; commit 32ddd50 |
| 2026-09-25 | Przekrój B/R/K (baza z historii transakcji, ryzyko §19 na żywej książce, klasyfikacja §14.1) przed pipeline'em cyklicznym F0–F6 | owner | brief CC-P; RVS, alerty, reverse DCF i baza na VPS poza zakresem |
| 2026-09-26 | Kontrakty terminowe (opcja 1): ekspozycja = liczba × mnożnik × kurs bazy; ATR, stop i REGIME na bazie; kontrakty w budżetach ryzyka 1–3; nominał poza kapitałem satelity, wchodzi wartość rachunku KONTRAKTOWY | nadzorca, owner | §19.4 rew. 4; mnożniki PGE 1000, CDR 100, MDV 100 |
| 2026-09-26 | Rewizja 4 nakładana na rew. 3.1; wiersz 3 checklisty §12 i O-12 bajtowo bez zmian; nowy wpis rejestru O = O-45 (S&P/Kensho) | nadzorca | zastępuje warunek „żaden hunk nie dotyka §12 ani rejestru O” (STOP-6) |
| 2026-09-26 | Klucz §14.1 v2 z adnotacją kalibracji 17/20; klucz proponuje, rejestracja decyduje (M76); ACMR, MKSI, IFX = A9 z uzasadnieniem „cykliczny półprzewodnik — bramka book-to-bill / zamówienia” | nadzorca, owner | bez v3 (ryzyko dopasowania klucza do pliku K) |
| 2026-09-26 | Progi klucza T34–T37: kapitalizacja 30 mld USD / 5 mld PLN, recurring 70 %, capex/przychód 10 %, segment dominujący 60 %; EBIT = Operating Income GAAP | nadzorca | przegląd przy następnej kalibracji klucza |
| 2026-09-26 | Kontrakt, ETF i certyfikat w satelicie → A0; „poza §14” tylko rdzeń | nadzorca | sprzeczność w pliku K §4 pkt 2 vs pkt 4 |
| 2026-09-26 | Zapadka Chandeliera „nigdy w dół” biegnie od dnia inicjalizacji systemu; erratum §19.3 | nadzorca | kontrola wsteczna 24.09: 17 pod Chandelierem, heat 3,458 % |
| 2026-09-26 | Konwencja „raw” yfinance do dokumentu w rew. 5; detektor splitów zostaje w kodzie | nadzorca | backlog B-05 |
| 2026-09-26 | Plik K pozostaje zamrożonym artefaktem audytu; errata w raporcie P6 | nadzorca | — |
| 2026-09-26 | Reguła agentów: po odmowie narzędzia lub klasyfikatora — STOP i zgłoszenie, bez ponawiania | nadzorca, owner | ponowienie `DELETE` po odmowie w P4 |
| 2026-09-26 | Tematy budżetu poziomu 2: 14 tematów; ORCL i IFX → semi/AI-infra, UBER → platformy-US, TSLA → auto/autonomia; VRC → software | owner | sql/seed_themes.sql |
| 2026-09-26 | MDV + FMDVZ26 jako jedna nazwa w poziomie 1 — do rew. 5; CBF i VRC to dwie nazwy | owner | backlog B-09; CBF sprzedał duży pakiet VRC |
| 2026-09-26 | CBF = A2 (klucz v2 przyjęty); ponowna kalibracja po danych FY2026 bez konsolidacji Vercom | owner | archetype_override_reason pozostaje pusty |
| 2026-09-26 | GMT: akcje z preIPO wprowadzone bilansem otwarcia (ilość i koszt podane przez ownera); SHO → CBF, parytet połączenia 0,2281:1 | owner | ilości i koszty tylko w data/manual (poza gitem) |
| 2026-09-26 | Rozjazd „42 pozycje ZAGRANICZNY” z czatu 24.09 zamknięty jako błąd po stronie czatu; FIFO = broker jest pomiarem | owner | B2: ilości 39/39 |
| 2026-09-25 (wieczór) | Repo `marixis908/mannaz` **publiczne**; dane finansowe poza gitem od pierwszego commita | owner | uchyla wpis 2026-09-25 „repo prywatne”; pomiar 2026-09-26: raw.githubusercontent.com/…/README.md → 200 bez uwierzytelnienia |
