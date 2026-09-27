"""Testy `_resolve_position_price_coverage` (brief CC-U, U2/U5) — rdzeń
"czy ta pozycja ma komplet na D" dzielony przez `run_risk`/
`resolve_default_risk_date`. WYŁĄCZNIE dane syntetyczne, bez bazy/sieci —
`FakeCursor` symuluje odpowiedzi SQL po charakterystycznym fragmencie
zapytania (mały, ustalony zestaw zapytań tego modułu)."""

from datetime import date
from decimal import Decimal

from mannaz.risk import CalendarFacts, IncompleteRiskItem, _resolve_position_price_coverage


class FakeCursor:
    """Fake kursor — bez bazy/sieci. `base_instrument_row`: krotka
    (id, currency, yahoo_symbol, exchange) albo None (brak instrumentu
    bazowego). `price_rows`: [(price_date, high, low, close), ...]."""

    def __init__(self, *, base_instrument_row=None, price_rows=()):
        self._base_instrument_row = base_instrument_row
        self._price_rows = list(price_rows)
        self._last_kind: str | None = None

    def execute(self, sql, params=()):
        if "FROM instruments WHERE yahoo_symbol" in sql:
            self._last_kind = "base_instrument"
        elif "FROM prices_daily" in sql:
            self._last_kind = "price_series"
        else:
            raise AssertionError(f"FakeCursor: nieobslugiwane zapytanie w tym scenariuszu: {sql[:80]!r}")

    def fetchone(self):
        if self._last_kind == "base_instrument":
            return self._base_instrument_row
        raise AssertionError(f"fetchone() nieoczekiwane dla zapytania {self._last_kind!r}")

    def fetchall(self):
        if self._last_kind == "price_series":
            return self._price_rows
        raise AssertionError(f"fetchall() nieoczekiwane dla zapytania {self._last_kind!r}")


def _open_gpw_calendar_facts(calendar_code, as_of):
    assert calendar_code == "XWAR"  # instruments.exchange='GPW' -> EXCHANGE_TO_CALENDAR_CODE['GPW']
    return CalendarFacts(is_session_on_d=True, sessions_before=[])


# ---------------------------------------------------------------------------
# U5 (4): kontrakt bez ceny bazy przy otwartym GPW -> niekompletne (kontrakty
# nie wypadaja po cichu z kapitalu ani ryzyka, decyzja nadzorcy 2026-09-27).
# ---------------------------------------------------------------------------


def test_future_without_base_price_on_open_market_is_incomplete_not_silently_dropped():
    d = date(2026, 9, 24)
    pos = {
        "broker_ticker": "FXYZ26",
        "instrument_type": "future",
        "base_symbol": "XYZ.WA",
        "multiplier": Decimal(100),
    }
    cur = FakeCursor(
        base_instrument_row=(42, "PLN", "XYZ.WA", "GPW"),
        price_rows=[],  # baza istnieje i ma mnoznik, ale brak JAKIEJKOLWIEK ceny <= D
    )

    coverage, incomplete = _resolve_position_price_coverage(cur, pos, d, _open_gpw_calendar_facts)

    assert coverage is None
    assert incomplete == IncompleteRiskItem("FXYZ26", "XYZ.WA", "GPW", "market_open_no_price")


# ---------------------------------------------------------------------------
# Dodatkowo (nie jeden z 6 scenariuszy U5, ale ta sama zasada "nigdy cicho")
# — brak instrumentu bazowego / brak mnoznika kontraktu tez sa niekompletne,
# nie cicho pominiete.
# ---------------------------------------------------------------------------


def test_future_base_instrument_not_found_is_incomplete():
    d = date(2026, 9, 24)
    pos = {
        "broker_ticker": "FABC26",
        "instrument_type": "future",
        "base_symbol": "ABC.WA",
        "multiplier": Decimal(100),
    }
    cur = FakeCursor(base_instrument_row=None)

    coverage, incomplete = _resolve_position_price_coverage(cur, pos, d, _open_gpw_calendar_facts)

    assert coverage is None
    assert incomplete == IncompleteRiskItem("FABC26", "ABC.WA", None, "base_instrument_not_found")


def test_future_multiplier_missing_is_incomplete():
    d = date(2026, 9, 24)
    pos = {
        "broker_ticker": "FDEF26",
        "instrument_type": "future",
        "base_symbol": "DEF.WA",
        "multiplier": None,  # instruments.multiplier IS NULL
    }
    cur = FakeCursor(base_instrument_row=(7, "PLN", "DEF.WA", "GPW"))

    coverage, incomplete = _resolve_position_price_coverage(cur, pos, d, _open_gpw_calendar_facts)

    assert coverage is None
    assert incomplete == IncompleteRiskItem("FDEF26", "DEF.WA", "GPW", "multiplier_missing")
