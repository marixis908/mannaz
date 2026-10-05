"""B-51: przypięcie wersji `yfinance` i strażnik sygnatury (bez sieci).

Repo nie ma pliku zależności (requirements/pyproject), więc przypięcie
egzekwuje ten test. `fetch_ohlc` (src/mannaz/prices.py) opiera obsługę
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
