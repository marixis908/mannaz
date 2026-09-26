"""Zapewnia, że `pytest` uruchomiony z katalogu M widzi pakiet mannaz z src/
bez potrzeby instalacji ani ustawiania PYTHONPATH ręcznie."""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
