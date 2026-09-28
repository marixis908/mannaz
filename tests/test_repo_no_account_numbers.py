"""Strażnik w repo — brief CC-C fix, krok R2: numery rachunków ownera NIE mogą
się pojawić w żadnym pliku śledzonym przez git w tym repo (`git ls-files`).

Wzorzec połączenia jak w `tests/test_cycle_db.py`: `mannaz.db.get_connection()`,
skip gdy baza niedostępna; tylko SELECT (`SELECT DISTINCT rachunek FROM
transactions`) — zero zapisów do bazy.

KRYTYCZNE: żaden test w tym pliku nie drukuje/nie wpisuje realnego numeru
rachunku — ani w treści testu (dane wyłącznie syntetyczne), ani w komunikacie
asercji (komunikaty `find_account_numbers` zawierają typ + ścieżkę + linię,
nigdy sam numer)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from mannaz.db import get_connection


def find_account_numbers(
    numbers_by_type: dict[str, set[str]], files: list[tuple[str, str]]
) -> list[str]:
    """Czysta funkcja: przeszukuje `files` (ścieżka względna, treść) pod kątem
    wystąpień ciągów cyfr z `numbers_by_type` (typ -> zbiór numerów).

    Zwraca listę komunikatów w formacie dokładnie
    "numer rachunku <typ> w <plik>:<linia>" — nigdy sam numer."""
    messages: list[str] = []
    for path, content in files:
        lines = content.split("\n")
        for line_no, line in enumerate(lines, start=1):
            for typ, numbers in numbers_by_type.items():
                for number in numbers:
                    if number in line:
                        messages.append(f"numer rachunku {typ} w {path}:{line_no}")
    return messages


def _numbers_by_type_from_rachunki(rachunki: list[str]) -> dict[str, set[str]]:
    """rachunek w bazie ma postać "<TYP> <numer>" (np. "AKCYJNY 900001") —
    typ = pierwszy token, numer(y) = ciągi cyfr o długości >= 4."""
    numbers_by_type: dict[str, set[str]] = {}
    for rachunek in rachunki:
        if not rachunek:
            continue
        parts = rachunek.split()
        if not parts:
            continue
        typ = parts[0]
        for digits in re.findall(r"\d{4,}", rachunek):
            numbers_by_type.setdefault(typ, set()).add(digits)
    return numbers_by_type


@pytest.fixture
def db_conn():
    try:
        conn = get_connection()
    except Exception as exc:  # brak .env/hasla/serwera -> caly test pomijamy
        pytest.skip(f"mannaz.db.get_connection() niedostepne: {exc}")
        return
    try:
        yield conn
    finally:
        conn.rollback()  # tylko SELECT -> nigdy nic do zatwierdzenia
        conn.close()


def _repo_files() -> list[tuple[str, str]]:
    """`git ls-files -z` w repo, w którym leży ten plik testu; treść czytana
    jako UTF-8 z errors="ignore"; pliki binarne (zawierające bajt NUL) i
    nieistniejące są pomijane."""
    repo_dir = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "ls-files", "-z"],
        capture_output=True,
        check=True,
    )
    rel_paths = [p for p in result.stdout.decode("utf-8", errors="ignore").split("\0") if p]

    files: list[tuple[str, str]] = []
    for rel_path in rel_paths:
        full_path = repo_dir / rel_path
        if not full_path.is_file():
            continue
        raw = full_path.read_bytes()
        if b"\x00" in raw:
            continue  # plik binarny
        content = raw.decode("utf-8", errors="ignore")
        files.append((rel_path, content))
    return files


@pytest.mark.db
def test_no_real_account_numbers_leak_into_repo_files(db_conn):
    conn = db_conn
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT rachunek FROM transactions")
        rachunki = [row[0] for row in cur.fetchall() if row[0]]

    numbers_by_type = _numbers_by_type_from_rachunki(rachunki)
    assert numbers_by_type, "mapa numerow rachunkow jest pusta — test nic nie sprawdza"

    files = _repo_files()
    messages = find_account_numbers(numbers_by_type, files)
    assert messages == [], "\n".join(messages)


@pytest.mark.db
def test_positive_control_find_account_numbers_detects_injected_number(db_conn, tmp_path):
    conn = db_conn
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT rachunek FROM transactions")
        rachunki = [row[0] for row in cur.fetchall() if row[0]]

    numbers_by_type = _numbers_by_type_from_rachunki(rachunki)
    if not numbers_by_type:
        pytest.skip("brak numerow rachunkow w bazie — nic do przetestowania")

    typ, numbers = next(iter(numbers_by_type.items()))
    number = next(iter(numbers))

    target = tmp_path / "injected_account_number.txt"
    target.write_text(f"linia pierwsza bez numeru\n{number}\nlinia trzecia bez numeru\n", encoding="utf-8")
    content = target.read_text(encoding="utf-8")

    messages = find_account_numbers(numbers_by_type, [(str(target), content)])

    assert len(messages) == 1
    (message,) = messages
    assert message.startswith("numer rachunku ")
    assert message.endswith(":2")
    all_numbers = {n for nums in numbers_by_type.values() for n in nums}
    assert not any(n in message for n in all_numbers), "komunikat nie moze zawierac samego numeru"


def test_find_account_numbers_pure_synthetic_data():
    numbers_by_type = {
        "AKCYJNY": {"900001"},
        "KONTRAKTOWY": {"123456"},
    }
    files = [
        ("src/plik_a.py", "pierwsza linia\ndruga linia z 900001 w srodku\ntrzecia linia\n"),
        ("src/plik_b.py", "nic tu nie ma\ninna linia bez numeru\nponizej jest 123456 numer\n"),
        ("src/plik_c.py", "zupelnie bez numerow\ndruga linia\n"),
    ]

    messages = find_account_numbers(numbers_by_type, files)

    assert messages == [
        "numer rachunku AKCYJNY w src/plik_a.py:2",
        "numer rachunku KONTRAKTOWY w src/plik_b.py:3",
    ]
