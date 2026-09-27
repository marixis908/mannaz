"""Zapewnia, że `pytest` uruchomiony z katalogu M widzi pakiet mannaz z src/
bez potrzeby instalacji ani ustawiania PYTHONPATH ręcznie."""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def pytest_configure(config):
    """Rejestracja markera `db` (brief CC-S, testy bazodanowe
    tests/test_risk_pit_db.py) — brak pyproject.toml/pytest.ini w repo, więc
    rejestracja odbywa się tutaj (inaczej pytest zgłasza ostrzeżenie o
    nieznanym markerze)."""
    config.addinivalue_line(
        "markers", "db: testy wymagajace polaczenia z baza (pomijane, gdy mannaz.db.get_connection() zawiedzie)"
    )
