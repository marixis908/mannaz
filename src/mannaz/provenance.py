"""Pochodzenie zapisów `source_runs` i `risk_daily` (B-23, B-54, sql/015).

- `code_sha()` — SHA commita kodu, który wykonał przebieg (`git rev-parse HEAD`
  w katalogu repo/worktree), z sufiksem `-dirty`, gdy śledzone pliki mają
  niezatwierdzone zmiany; None, gdy git jest niedostępny (zapis bez SHA, nigdy
  zgadywanie). Liczony raz na proces.
- `current_trigger()` — co uruchomiło przebieg: 'cycle' (`run_cycle`) albo
  'cli' (pojedyncze polecenie `run_p3`); None poza tymi punktami wejścia
  (np. testy). Ustawiany dekoratorem `triggered_by` na punktach wejścia,
  żeby nie przeciągać parametru przez funkcje etapów i ich atrapy w testach.
"""

from __future__ import annotations

import functools
import subprocess
from contextvars import ContextVar
from pathlib import Path

TRIGGERS = ("cycle", "cli")

_REPO_DIR = Path(__file__).resolve().parent.parent.parent
_trigger: ContextVar[str | None] = ContextVar("mannaz_trigger", default=None)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(_REPO_DIR), *args],
        capture_output=True, text=True, check=True, timeout=10,
    ).stdout.strip()


@functools.cache
def code_sha() -> str | None:
    try:
        sha = _git("rev-parse", "HEAD")
        dirty = _git("status", "--porcelain", "--untracked-files=no")
    except (OSError, subprocess.SubprocessError):
        return None
    if not sha:
        return None
    return f"{sha}-dirty" if dirty else sha


def current_trigger() -> str | None:
    return _trigger.get()


def triggered_by(trigger: str):
    """Dekorator punktu wejścia: na czas wywołania `current_trigger()` == trigger.
    Zagnieżdżenie (cykl wywołany z `run_p3 cycle`) — wygrywa wewnętrzny."""
    if trigger not in TRIGGERS:
        raise ValueError(f"nieznany wyzwalacz: {trigger!r}")

    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            token = _trigger.set(trigger)
            try:
                return fn(*args, **kwargs)
            finally:
                _trigger.reset(token)

        return wrapper

    return decorate
