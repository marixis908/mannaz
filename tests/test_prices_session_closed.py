"""B-48 (T46) — testy jednostkowe `session_closed` (zero bazy, zero sieci) i
linii HEARTBEAT o pominiętych świecach sesji niezamkniętej."""

from datetime import date, datetime, timedelta, timezone

import pytest

from mannaz import prices
from mannaz.cycle import ReportState, _render_heartbeat_section
from mannaz.prices import SESSION_CLOSE_GRACE, session_closed

UTC = timezone.utc


def test_grace_is_30_minutes():
    assert SESSION_CLOSE_GRACE == timedelta(minutes=30)


# --- prawdziwy exchange_calendars (XWAR, 2026-10-05) -------------------------
def test_real_calendar_xwar_2026_boundaries():
    d = date(2026, 10, 5)
    close = prices._session_close_utc("XWAR", d)
    assert close is not None
    assert close == datetime(2026, 10, 5, 15, 0, tzinfo=UTC)  # zweryfikowane z biblioteki
    assert session_closed("XWAR", d, close + timedelta(minutes=29)) is False
    assert session_closed("XWAR", d, close + timedelta(minutes=30)) is True
    assert session_closed("XWAR", d, close + timedelta(minutes=31)) is True
    assert session_closed("XWAR", d, close - timedelta(minutes=1)) is False


# --- lookup monkeypatchowany, daty 1990 --------------------------------------
@pytest.fixture
def fake_close(monkeypatch):
    close = datetime(1990, 1, 3, 16, 0, tzinfo=UTC)
    monkeypatch.setattr(prices, "_session_close_utc", lambda code, d: close if d == date(1990, 1, 3) else None)
    return close


def test_calendar_boundaries_1990(fake_close):
    d = date(1990, 1, 3)
    assert session_closed("X", d, fake_close - timedelta(minutes=1)) is False  # kontrolka dodatnia
    assert session_closed("X", d, fake_close + timedelta(minutes=29, seconds=59)) is False
    assert session_closed("X", d, fake_close + timedelta(minutes=30)) is True  # granica
    assert session_closed("X", d, fake_close + timedelta(minutes=31)) is True  # kontrolka ujemna


def test_no_calendar_rule():
    now = datetime(1990, 1, 3, 0, 1, tzinfo=UTC)
    assert session_closed(None, date(1990, 1, 3), now) is False
    assert session_closed(None, date(1990, 1, 2), now) is True


def test_non_session_date_falls_back_to_date_rule(fake_close):
    now = datetime(1990, 1, 7, 0, 1, tzinfo=UTC)
    assert session_closed("X", date(1990, 1, 6), now) is True  # lookup None, 6 < 7
    assert session_closed("X", date(1990, 1, 7), now) is False


def test_out_of_range_and_unknown_calendar_fall_back():
    now = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    assert session_closed("XWAR", date(1990, 1, 3), now) is True  # poza zakresem biblioteki
    assert session_closed("NIE_MA_TAKIEGO", date(2026, 10, 5), now) is False


def test_naive_now_rejected():
    with pytest.raises(ValueError):
        session_closed(None, date(1990, 1, 3), datetime(1990, 1, 4))


# --- heartbeat ---------------------------------------------------------------
def _state(**kw) -> ReportState:
    return ReportState(d=date(1990, 3, 9), run_started_at=datetime(1990, 3, 10, 8, tzinfo=UTC), **kw)


def test_heartbeat_line_with_counts():
    lines = _render_heartbeat_section(_state(unclosed_session_rows=3, unclosed_session_instruments=2))
    assert "- świece niezamkniętej sesji pominięte: 3 (instrumentów: 2)" in lines


def test_heartbeat_line_zero_when_stage_not_run():
    lines = _render_heartbeat_section(_state())
    assert "- świece niezamkniętej sesji pominięte: 0 (instrumentów: 0)" in lines
