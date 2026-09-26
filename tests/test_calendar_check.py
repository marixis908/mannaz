"""Testy strażnika kalendarza — używa prawdziwych kalendarzy `exchange_calendars`
(dane statyczne dostarczane z pakietem, bez sieci) na syntetycznych datach.
Brief CC-P, P3.4."""

from datetime import date

from mannaz.calendar_check import EXCHANGE_TO_CALENDAR_CODE, compare_dates_to_calendar


def test_xwar_calendar_code_mapped_for_gpw():
    assert EXCHANGE_TO_CALENDAR_CODE["GPW"] == "XWAR"


def test_all_expected_exchanges_have_calendar_codes():
    for exch in ("GPW", "NASDAQ", "NYSE", "XETRA", "AMSTERDAM", "TSX"):
        assert exch in EXCHANGE_TO_CALENDAR_CODE


def test_compare_dates_matching_full_session_set_has_zero_diff():
    # 2024-01-02..2024-01-05 na XWAR: wt-pt, wszystkie sesje handlowe (brak
    # świąt w tym oknie w Polsce).
    price_dates = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)]
    diff_count, missing, extra = compare_dates_to_calendar(price_dates, "XWAR")
    assert diff_count == 0
    assert missing == []
    assert extra == []


def test_compare_dates_missing_a_trading_session_is_detected():
    # Pomijamy 2024-01-04 (czwartek, sesja XWAR) -> dziura w danych.
    price_dates = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 5)]
    diff_count, missing, extra = compare_dates_to_calendar(price_dates, "XWAR")
    assert diff_count == 1
    assert missing == [date(2024, 1, 4)]
    assert extra == []


def test_compare_dates_price_on_weekend_is_detected_as_extra():
    # 2024-01-06 to sobota — żaden kalendarz giełdowy nie uznaje jej za sesję.
    price_dates = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 6)]
    diff_count, missing, extra = compare_dates_to_calendar(price_dates, "XWAR")
    assert date(2024, 1, 6) in extra
    assert diff_count >= 1


def test_compare_dates_empty_input_is_zero_diff_no_crash():
    diff_count, missing, extra = compare_dates_to_calendar([], "XWAR")
    assert diff_count == 0
    assert missing == []
    assert extra == []
