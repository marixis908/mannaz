# Pakiet zadania Codexa: <slug>

## Cel

<konkretny wynik>

## Srodowisko

- Worktree: D:\codex\worktrees\mannaz\<slug>
- Galaz: codex/<slug>
- Cwd: worktree
- Python: C:\Users\MariuszBrysik\projects\Mannaz\.venv\Scripts\python.exe
- PYTHONPATH: <worktree>\src, tylko dla procesu

## Pliki dozwolone

<zamknieta lista sciezek lub globow wzgledem worktree>

## Pliki zakazane

data/, _incoming/, out/, backups/, .env, .env.*, oraz wszystkie pliki spoza
listy dozwolonej. Bez testow db i cyklu run_p3.

## Zasoby spoza repo

Domyslnie brak. Kazdy wyjatek: pelna sciezka, cel oraz tryb R albo RW.
Interpreter z Srodowiska: R, tylko testy czyste. Metadane Git glownego
repo: RW tylko .git/objects, .git/refs/heads/codex, .git/logs/refs/heads/codex
i .git/worktrees/<slug>; bez calego .git, config, hooks i refs/heads/main.
<inne wyjatki: domyslnie brak>

## Kryteria akceptacji jako komendy

<komenda, cwd, oczekiwany kod i wynik; tylko testy czyste>
Przyklad: & 'C:\Users\MariuszBrysik\projects\Mannaz\.venv\Scripts\python.exe' -m pytest -q -m "not db" -p no:cacheprovider --basetemp=<worktree>\tmp\codex\pytest-temp
PYTHONPATH ustaw i przywroc w try/finally. Bez pip install.

## Predykcje

<przed pomiarem: wartosc, mianownik, jednostka i konwencja>
Przed pomiarem mogacym dac zero wykonaj dodatnia kontrolke tego samego typu.

## Warunki STOP

Plik spoza listy dozwolonej; potrzeba danych/bazy; zmiana zaleznosci;
odmowa narzedzia/sandboxa (bez ponawiania); konflikt z regulami repo;
przekroczenie limitu czasu. Zglos ownerowi, nie rozszerzaj zakresu.

## Limit czasu

<czas i moment rozpoczecia>

## Commit i push

Commit tylko na codex/<slug>. Trailer z D11.
Push domyslnie NIE; wymaga zgody ownera na pojedyncza komende.
Bez main, merge, PR, force-push i usuwania galezi zdalnych.

## Format raportu

docs/codex/_szablon-raportu.md
Raport zadania: tmp/codex/<slug>-raport.md.
