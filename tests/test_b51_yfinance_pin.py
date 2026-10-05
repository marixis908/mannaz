"""B-51: przypięcie wersji `yfinance` i strażnik sygnatury (bez sieci); B-53: spójność
requirements.txt z constraints.txt.

Wersje przypina constraints.txt (B-53); ten test zostaje strażnikiem
semantyki przy zmianie wersji (decyzja 2026-10-05). `fetch_ohlc` (src/mannaz/prices.py) opiera obsługę
wyjątków (C2a z B-48) na parametrze `raise_errors` oraz na zachowaniu
yfinance 1.7.0; zmiana wersji wymaga ponownej weryfikacji tej obsługi."""

from __future__ import annotations

import inspect

import yfinance


def test_price_history_accepts_raise_errors():
    from yfinance.scrapers.history import PriceHistory

    assert "raise_errors" in inspect.signature(PriceHistory.history).parameters, (
        "fetch_ohlc (src/mannaz/prices.py ~146) przekazuje raise_errors=True do Ticker.history; "
        "Ticker.history (yfinance/base.py:128) przyjmuje **kwargs, wiec sprawdzamy funkcje docelowa "
        "PriceHistory.history (yfinance/scrapers/history.py:105-109); B-51"
    )


def test_yfinance_version_pinned():
    assert yfinance.__version__ == "1.7.0", (
        f"yfinance {yfinance.__version__} != 1.7.0 (B-51): po zmianie wersji zweryfikuj ponownie "
        "obsluge wyjatkow w fetch_ohlc (C2a z B-48), potem zaktualizuj przypiecie w tym tescie"
    )


def test_b53_direct_requirements_pinned_in_constraints():
    """B-53: każda zależność bezpośrednia z requirements.txt ma wersję w
    constraints.txt, a yfinance jest tam przypięty do tej samej 1.7.0."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent

    def names(path):
        out = {}
        for line in (root / path).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            m = re.match(r"([A-Za-z0-9_.-]+)(\[[^\]]*\])?\s*(==\s*(\S+))?", line)
            out[re.sub(r"[-_.]+", "-", m.group(1)).lower()] = m.group(4)
        return out

    direct = names("requirements.txt")
    pinned = names("constraints.txt")
    missing = sorted(n for n in direct if not pinned.get(n))
    assert not missing, f"brak wersji w constraints.txt dla: {missing}"
    assert pinned["yfinance"] == "1.7.0"
