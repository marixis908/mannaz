# AGENTS.md - Mannaz

## Tryb niezalezny Codexa

Biezacy pakiet zadania jest briefem. Konflikt z README.md lub docs/decyzje.md
oznacza STOP i raport. Nie zmieniaj globalnego D:\codex\AGENTS.md.

- Pracuj tylko w worktree wskazanym w pakiecie; cwd = worktree.
- Galaz codex/<slug>. Commit tylko na tej galezi i w tym worktree (D1).
- Push tylko tej galezi, za zgoda ownera na pojedyncza komende (D2).
  Repo publiczne: push jest publikacja. Bez main, force-push, usuwania galezi
  zdalnych, merge i PR. Merge do main robi CC po recenzji.
- D5: tylko kod i testy czyste. Bez dostepu do data/, _incoming/, out/,
  backups/, .env i .env.*; bez testow db i cyklu run_p3.
  Wyjatek wymaga jawnego zapisu w pakiecie zadania.
- D10: Python = C:\Users\MariuszBrysik\projects\Mannaz\.venv\Scripts\python.exe.
  PYTHONPATH=<worktree>\src tylko na czas procesu, potem przywroc poprzednia
  wartosc. Bez pip install i zmian zaleznosci.
- D11: trailer commita: Co-authored-by: Codex <noreply@openai.com>.
- Z7: odmowa narzedzia lub sandboxa = STOP i zgloszenie, bez ponawiania.
- Nie recenzuj wlasnej pracy: wynik recenzuje CC.
- Raport obowiazkowy: docs/codex/_szablon-raportu.md.
  Pakiet: tmp/codex/<slug>.md; raport: tmp/codex/<slug>-raport.md.
- Polski w dokumentach roboczych; pliki dla agentow i skrypty ASCII.

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
odmowa narzedzia, konflikt z regulami repo: STOP i raport do ownera.
Nie rozszerzaj sam zakresu ani uprawnien.

## Pliki tymczasowe testow

Pomiar wdrozenia 2026-10-06: pliki tymczasowe utworzone przez sandbox moga byc
nieczytelne poza nim. Testy czyste uruchamiaj z
--basetemp=<worktree>\tmp\codex\pytest-temp oraz -p no:cacheprovider.
Ustaw PYTHONDONTWRITEBYTECODE=1 i PYTHONPATH=<worktree>\src tylko dla procesu.
codex-task-close.ps1 -Usun usuwa pytest-temp przed git worktree remove;
blad usuwania musi byc jawnie zgloszony, nie moze byc cicho pominiety.
Wyjatki od STOP po odmowie wymagaja jawnego zapisu w pakiecie. W tym wdrozeniu
ZG2 obejmuje zaplanowane kontrole ujemne D-K1/D-K2 oraz odmowy odczytu/usuniecia
plikow sandboxa w tmp/codex/. Zapisz surowy wynik, nie ponawiaj; inne odmowy = STOP.

## Auto-gc i maintenance

W sandboxie gc.auto=0 i maintenance.auto=false, tylko przez zmienne procesu
GIT_CONFIG_COUNT / GIT_CONFIG_KEY_* / GIT_CONFIG_VALUE_*. Funkcja codex-mannaz
zapisuje poprzednie wartosci i przywraca je w finally, bez zmian git config.
Nie udostepniaj korzenia .git ani packed-refs.lock.
Owner/CC okresowo wykonuja git gc / git pack-refs poza sandboxem.
Odmowa packed-refs.lock w tym wdrozeniu to wynik pomiaru na mocy ZG2;
inne nieplanowane odmowy nadal oznaczaja STOP bez ponawiania.
