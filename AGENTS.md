# AGENTS.md - Mannaz

## Tryb niezalezny Codexa

Biezacy pakiet zadania jest briefem. Konflikt z README.md lub docs/decyzje.md
oznacza STOP i raport. Nie zmieniaj globalnego D:\codex\AGENTS.md.

- Pracuj tylko w worktree wskazanym w pakiecie; cwd = worktree.
- Galaz codex/<slug>. Commit tylko na tej galezi i w tym worktree (D1).
- Push tylko tej galezi, za zgoda ownera na pojedyncza komende (D2).
  Komenda: git push origin codex/<slug>, bez -u (zmienia .git/config).
  Repo publiczne: push jest publikacja. Bez main, force-push, usuwania galezi
  zdalnych, merge i PR. Merge do main robi CC po recenzji.
- D5: tylko kod i testy czyste. Bez dostepu do data/, _incoming/, out/,
  backups/, .env i .env.*; bez testow db i cyklu run_p3.
  Wyjatek wymaga jawnego zapisu w pakiecie zadania.
- D10: Python = C:\Users\MariuszBrysik\projects\Mannaz\.venv\Scripts\python.exe.
  PYTHONPATH=<worktree>\src tylko na czas procesu, potem przywroc poprzednia
  wartosc. Bez pip install i zmian zaleznosci.
- D11: trailer commita: Co-authored-by: Codex <noreply@openai.com>.
- Z7: kazda odmowa poza regula packed-refs.lock ponizej = STOP i zgloszenie,
  bez ponawiania, chyba ze pakiet jawnie przewiduje inny wyjatek.
- Nie recenzuj wlasnej pracy: wynik recenzuje CC.
- Raport obowiazkowy: docs/codex/_szablon-raportu.md.
  Pakiet: tmp/codex/<slug>.md; raport: tmp/codex/<slug>-raport.md.
- Polski w dokumentach roboczych; pliki dla agentow i skrypty ASCII.

## Recenzja - tylko na jawne zlecenie

Domyslnie Codex w Mannaz wykonuje zadania (tryb niezalezny wyzej).
Recenzje robi tylko, gdy pakiet ja jawnie zleca i podaje delte (commit lub
galaz) oraz pytanie. CC zleca recenzje tylko, gdy wynik moze zmienic kod lub
decyzje: zmiana w src/, sql/, scripts/ lub tests/ przed merge do main albo
brief przed wykonaniem. Bez recenzji: docs/backlog.md, dzienniki, raporty
i dokumenty bez zmiany zachowania kodu (decyzje.md, 2026-10-08).

- Tylko odczyt kodu: bez zmian plikow poza raportem recenzji i pytest-temp,
  bez commitow i push. Wolno testy czyste (D5).
- Nigdy wlasnej galezi codex/<slug> (zasada "Nie recenzuj wlasnej pracy").
- MAJOR tylko z realna sciezka: wejscie -> plik:linia -> skutek, gdzie skutek
  to bledny kapital, ryzyko lub stop bez DATA FAILURE, pozycja wypadajaca po
  cichu (decyzje.md, 2026-09-27) albo naruszenie R1-R9.
- Inny skutek albo brak sciezki: najwyzej MINOR, bez sciezki z "[bez sciezki]".
- Kazde znalezisko niesie minimalna poprawke; bez poprawki nie zglaszaj.
- Bez nowych warstw, testow i przypiec bez MAJOR, ktory usuwaja.
- Nie zglaszaj pozycji z docs/backlog.md, chyba ze zmiana pogarsza skutek.
- Brak MAJOR jest poprawnym wynikiem. MINOR najwyzej 5; styl pomijaj.
- Druga runda tylko po poprawce MAJOR i tylko na tej poprawce.
- Raport: tmp/codex/<slug>-recenzja.md: ZAKRES, MAJOR, MINOR, GRANICA
  (sprawdzone / zalozone / niesprawdzone), WERDYKT approve/odrzucenie.

## Reguly projektu - identyfikatory i zrodla

- R1: README.md; docs/decyzje.md, 2026-09-25, dane poza gitem; zakaz git add -f.
- R2: docs/decyzje.md, 2026-09-25 (wieczor), repo publiczne / push = publikacja.
- R3: tests/test_repo_no_account_numbers.py.
- R4: docs/decyzje.md, 2026-09-26, Regula agentow.
- R5: docs/decyzje.md, naglowek dziennika; dzienniki docs/ tylko dopisywane.
- R6: scripts/dashboard/generate.py, stdout/logi: licznosci/daty/sha256/PASS-FAIL.
- R7: src/mannaz/db.py, nigdy nie logowac wartosci z .env.
- R8: README.md, zrodlo prawdy = docs/projekt-systemu-monitoringu.md.
- R9: README.md, requirements.txt, constraints.txt, yfinance==1.7.0.

## Warunki STOP

Plik poza lista dozwolona, potrzeba danych lub bazy, zmiana zaleznosci,
odmowa inna niz zaakceptowana regula packed-refs.lock, konflikt z regulami repo:
STOP i raport do ownera.
Nie rozszerzaj sam zakresu ani uprawnien.

## Pliki tymczasowe testow

Pomiar wdrozenia 2026-10-06: pliki tymczasowe utworzone przez sandbox moga byc
nieczytelne poza nim. Testy czyste uruchamiaj z
--basetemp=<worktree>\tmp\codex\pytest-temp oraz -p no:cacheprovider.
Ustaw PYTHONDONTWRITEBYTECODE=1 i PYTHONPATH=<worktree>\src tylko dla procesu.
codex-task-close.ps1 -Usun usuwa pytest-temp przed git worktree remove;
blad usuwania musi byc jawnie zgloszony, nie moze byc cicho pominiety.
Wyjatki od STOP poza stala regula packed-refs.lock wymagaja jawnego zapisu
w pakiecie. ZG2 moze obejmowac zaplanowane kontrole ujemne oraz odmowy
odczytu/usuniecia plikow sandboxa w tmp/codex/, jezeli pakiet je przewiduje.
Zapisz surowy wynik, nie ponawiaj; kazda inna odmowa = STOP.

## Auto-gc i maintenance

W sandboxie gc.auto=0 i maintenance.auto=false, tylko przez zmienne procesu
GIT_CONFIG_COUNT / GIT_CONFIG_KEY_* / GIT_CONFIG_VALUE_*. Funkcja codex-mannaz
zapisuje poprzednie wartosci i przywraca je w finally, bez zmian git config.
Nie udostepniaj korzenia .git ani packed-refs.lock.
Owner/CC okresowo wykonuja git gc / git pack-refs poza sandboxem.
Stala regula: odmowa packed-refs.lock przy commicie z kodem 0 nie jest STOP,
jezeli git log -1 --oneline pokazuje nowy commit. Zapisz odmowe i wynik
w raporcie zadania. Kazda inna odmowa = STOP, bez ponawiania.
Blad wystepuje podczas aktualizacji ref, przed podsumowaniem commita.
Auto-gc wykluczone pomiarem gc.auto=0 i maintenance.auto=false widocznymi
dla Git; przyczyna packed-refs.lock pozostaje niewyjasniona.
