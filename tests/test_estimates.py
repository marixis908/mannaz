"""E1-E12, E15, E16:syntetyczne bajty, bez sieci i bazy."""

from dataclasses import replace
from datetime import date, datetime, timezone
import hashlib
import json
import socket
from types import SimpleNamespace

import pytest
import yfinance

from mannaz import estimates as est

NOW = datetime(2026, 10, 18, 8, 3, 16, 123456, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("E15: polaczenie sieciowe zablokowane")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    # Klient curl_cffi omija socket.connect: blokada tez na granicy YfData.get.
    from yfinance.data import YfData

    monkeypatch.setattr(YfData, "get", blocked)


def response(body, status=200):
    return SimpleNamespace(content=body, status_code=status, headers={
        "Content-Type": "application/json; charset=utf-8", "Content-Encoding": "gzip",
    })


def fetch(body=b'{ "z": 1, "a": 2 }\n', symbol="SYN/TH:ETIC", status=200):
    return est.fetch_quote_summary(symbol, now_utc=NOW,
                                   http_get=lambda *a, **kw: response(body, status))


def trend(label="0y", end="2027-08-31", eps_n=5, rev_n=4):
    return {
        "period": label, "endDate": end,
        "earningsEstimate": {
            "avg": {"raw": 2, "fmt": None}, "low": {"raw": 1}, "high": 3,
            "numberOfAnalysts": {"raw": eps_n}, "earningsCurrency": "USD",
        },
        "revenueEstimate": {
            "avg": {"raw": 100}, "low": {"raw": 90}, "high": {"raw": 110},
            "numberOfAnalysts": {"raw": rev_n},
        },
        "epsTrend": {"90daysAgo": 9}, "epsRevisions": {"upLast7days": 3},
    }


def payload(*trends, currency="USD"):
    return json.dumps({"quoteSummary": {"result": [{
        "price": {"currency": currency}, "earningsTrend": {"trend": list(trends)},
    }], "error": None}}).encode("utf-8")


def test_e15_positive_control_network_block():
    with socket.socket() as sock:
        with pytest.raises(AssertionError, match="E15"):
            sock.connect(("127.0.0.1", 9))


def test_e15_positive_control_yfdata_get_block():
    from yfinance.data import YfData

    with pytest.raises(AssertionError, match="E15"):
        YfData().get("https://example.invalid/", params={}, timeout=1)


def test_e16_broken_yfinance_config_returns_error(monkeypatch):
    import sys

    import yfinance.config  # noqa: F401  (laduje modul do sys.modules)

    class Broken:
        @property
        def locale(self):
            raise RuntimeError("syntetyczna zepsuta konfiguracja")

    monkeypatch.setattr(sys.modules["yfinance.config"], "YfConfig", Broken())
    # Kontrolka dodatnia: podmiana dziala na module czytanym przez estimates.py.
    from yfinance.config import YfConfig

    assert isinstance(YfConfig, Broken)
    calls = []
    fr = est.fetch_quote_summary("SYN", now_utc=NOW, http_get=lambda *a, **kw: calls.append(a))
    assert calls == [] and fr.body is None and fr.http_status is None
    assert "RuntimeError" in fr.error and "zepsuta konfiguracja" in fr.error
    assert fr.source_symbol == "SYN" and fr.fetched_at_utc == NOW
    assert fr.library_version == "1.7.0"  # wersja odczytana przed bledem locale
    assert fr.query_params == {}
    assert fr.url == ""


def test_e16_naive_now_utc_returns_error():
    naive = datetime(2026, 10, 18, 8, 3, 16)
    fr = est.fetch_quote_summary("SYN", now_utc=naive, http_get=lambda *a, **kw: response(b"x"))
    assert fr.body is None and "ValueError" in fr.error
    assert fr.fetched_at_utc.utcoffset() is not None


def test_e1_url_literal_without_symbol_encoding():
    fr = fetch()
    assert fr.url == "https://query2.finance.yahoo.com/v10/finance/quoteSummary/SYN/TH:ETIC"


def test_e1_bytes_and_metadata(tmp_path):
    body = b' { "z" : 1, "a": "\xc4\x85" }\r\n'
    fr = fetch(body)
    entry = est.archive_snapshot(tmp_path, fr)
    assert entry.error is None
    assert (tmp_path / entry.raw_ref).read_bytes() == body
    assert entry.raw_sha256 == hashlib.sha256(body).hexdigest()
    assert entry.byte_count == len(body)
    assert entry.raw_ref == (
        "yahoo_quotesummary/SYN_TH_ETIC/20261018T080316123456Z.body")
    text = (tmp_path / entry.meta_ref).read_text(encoding="utf-8")
    meta = json.loads(text)
    assert list(meta) == sorted(meta)
    assert "body" not in meta
    assert meta["source_symbol"] == "SYN/TH:ETIC"
    assert meta["safe_symbol"] == "SYN_TH_ETIC"
    assert meta["query_params"] == fr.query_params
    assert meta["modules"] == list(est.MODULES)
    assert meta["url"] == fr.url
    assert meta["content_type"] == "application/json; charset=utf-8"
    assert meta["content_encoding"] == "gzip"
    assert meta["byte_count"] == len(body)
    assert meta["raw_sha256"] == entry.raw_sha256
    assert meta["fetched_at_utc"] == NOW.isoformat()


def test_e2_collision_preserves_both_files(tmp_path):
    first = est.archive_snapshot(tmp_path, fetch())
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (tmp_path / first.raw_ref).parent.iterdir()}
    second = est.archive_snapshot(tmp_path, fetch(b"inne bajty"))
    assert "FileExistsError" in second.error
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
             for p in (tmp_path / first.raw_ref).parent.iterdir()}
    assert before == after


def test_e3_network_failure_metadata_only(tmp_path):
    def fail(*a, **kw):
        raise TimeoutError("syntetyczny timeout")

    fr = est.fetch_quote_summary("SYN", http_get=fail, now_utc=NOW)
    assert fr.body is None and fr.http_status is None
    assert "TimeoutError" in fr.error
    entry = est.archive_snapshot(tmp_path, fr)
    assert entry.error is None and entry.raw_ref is None and entry.raw_sha256 is None
    assert json.loads((tmp_path / entry.meta_ref).read_bytes())["error"] == fr.error
    assert list(tmp_path.rglob("*.meta.json"))  # kontrolka zasiegu przed zerem .body
    assert not list(tmp_path.rglob("*.body"))


def test_e3b_rate_limit_metadata_only_and_other_exception(tmp_path):
    from yfinance.exceptions import YFRateLimitError

    def rate_limit(*args, **kwargs):
        raise YFRateLimitError()

    fr = est.fetch_quote_summary("SYN", http_get=rate_limit, now_utc=NOW)
    expected = ("YFRateLimitError: HTTP 429 wg yfinance 1.7.0 "
                "data.py:505-506; body niedostepne w bibliotece")
    assert fr.body is None and fr.http_status is None and fr.error == expected
    entry = est.archive_snapshot(tmp_path, fr)
    assert entry.error is None and entry.raw_ref is None and entry.raw_sha256 is None
    meta = json.loads((tmp_path / entry.meta_ref).read_bytes())
    assert meta["error"] == expected and meta["http_status"] is None
    assert list(tmp_path.rglob("*.meta.json"))
    assert not list(tmp_path.rglob("*.body"))

    def other_error(*args, **kwargs):
        raise RuntimeError("syntetyczny blad")

    other = est.fetch_quote_summary("OTHER", http_get=other_error, now_utc=NOW)
    assert other.body is None and other.http_status is None
    assert other.error == "RuntimeError: syntetyczny blad"
    assert "429" not in other.error


def test_e4_http_404_body_preserved(tmp_path):
    fr = fetch(b'{"finance":{"error":{"code":"Not Found"}}}', status=404)
    assert fr.http_status == 404 and fr.error == "HTTP 404"
    entry = est.archive_snapshot(tmp_path, fr)
    assert (tmp_path / entry.raw_ref).read_bytes() == fr.body
    assert json.loads((tmp_path / entry.meta_ref).read_bytes())["error"] == "HTTP 404"


def test_e5_version_fail_closed(monkeypatch):
    monkeypatch.setattr(yfinance, "__version__", "1.7.1")
    calls = []
    fr = est.fetch_quote_summary("SYN", now_utc=NOW, http_get=lambda *a, **kw: calls.append(a))
    assert calls == [] and fr.body is None
    assert "1.7.1" in fr.error and "1.7.0" in fr.error


def test_e6_cpr_shape_four_periods():
    trends = []
    for label in ("0q", "+1q", "0y", "+1y"):
        t = trend(label, end=None)
        t["earningsEstimate"] = {}
        t["revenueEstimate"] = {k: {"raw": 0, "fmt": None}
                                for k in ("avg", "low", "high", "numberOfAnalysts")}
        trends.append(t)
    parsed = est.parse_quote_summary(payload(*trends, currency="PLN"))
    assert len(parsed.rows) == 4
    for row in parsed.rows:
        assert (row.eps_avg, row.eps_low, row.eps_high) == (None, None, None)
        assert (row.rev_avg, row.rev_low, row.rev_high) == (None, None, None)
        assert row.eps_status == row.rev_status == est.NO_CONSENSUS
        assert row.n_analysts_eps is None and row.n_analysts_rev == 0


@pytest.mark.parametrize("eps_n,rev_n", [(5, 0), (None, 4)])
def test_e7_coverage_separate(eps_n, rev_n):
    rows = est.parse_quote_summary(payload(trend(eps_n=eps_n, rev_n=rev_n),
                                          trend("+1y"))).rows
    row = rows[0]
    assert row.eps_avg == (2 if eps_n else None)
    assert row.rev_avg == (100 if rev_n else None)
    assert row.eps_status == (None if eps_n else est.NO_CONSENSUS)
    assert row.rev_status == (None if rev_n else est.NO_CONSENSUS)
    assert rows[1].eps_avg == 2 and rows[1].rev_avg == 100


def test_e8_zero_with_coverage():
    t = trend(eps_n=3)
    t["earningsEstimate"]["avg"] = {"raw": 0, "fmt": None}
    row = est.parse_quote_summary(payload(t)).rows[0]
    assert row.eps_avg == 0 and row.eps_status is None
    assert row.n_analysts_eps == 3


def test_e9_csu_currency_no_conversion():
    parsed = est.parse_quote_summary(payload(trend(), currency="CAD"))
    row = parsed.rows[0]
    assert parsed.price_currency == "CAD" and row.eps_currency == "USD"
    assert row.currency_check == est.CURRENCY_MISMATCH
    assert row.eps_avg == 2 and row.rev_currency is None


def test_e10_skipped_labels_and_period_types():
    parsed = est.parse_quote_summary(payload(*(trend(x) for x in ("0q", "+1q", "0y", "+1y", "+5y"))))
    assert len(parsed.rows) == 4 and parsed.skipped_labels == ["+5y"]
    assert [r.period_type for r in parsed.rows] == ["quarter", "quarter", "year", "year"]
    assert all(r.period_end_check == est.PERIOD_UNCONFIRMED for r in parsed.rows)


def test_e11_missing_date():
    parsed = est.parse_quote_summary(payload(trend(end=None)))
    row = parsed.rows[0]
    assert row.period_end is None and row.fiscal_year is None
    assert row.status == parsed.status == "brak daty okresu"


def test_bad_date_format_keeps_row_and_neighbours():
    parsed = est.parse_quote_summary(payload(
        trend("0q", end="2026-12-31"), trend("+1q", end="nie-data"), trend("0y", end="2027-08-31")))
    good_before, bad, good_after = parsed.rows
    assert bad.period == "+1q" and bad.period_end is None and bad.fiscal_year is None
    assert bad.status == "bledna data okresu"
    assert (bad.eps_avg, bad.n_analysts_eps, bad.rev_avg, bad.eps_currency) == (2, 5, 100, "USD")
    assert good_before.period_end == date(2026, 12, 31) and good_before.status is None
    assert good_after.period_end == date(2027, 8, 31) and good_after.fiscal_year == 2027
    assert good_after.status is None and good_after.eps_avg == 2
    assert parsed.status == "bledna data okresu"


def test_snapshot_status_bad_date_wins_over_missing_date():
    parsed = est.parse_quote_summary(payload(trend("0q", end=None), trend("+1q", end="zle")))
    assert [r.status for r in parsed.rows] == ["brak daty okresu", "bledna data okresu"]
    assert parsed.status == "bledna data okresu"


@pytest.mark.parametrize("body", [
    b"{", b'\xff', b'{"quoteSummary":{"result":null,"error":{"code":"Not Found"}}}',
    b'{"finance":{"error":{"code":"Not Found"}}}',
    b'{"quoteSummary":{"result":null}}', b'{"quoteSummary":{"result":[]}}',
])
def test_e12_provider_errors(body):
    parsed = est.parse_quote_summary(body)
    assert parsed.status and parsed.rows == []


@pytest.mark.parametrize("end", ["2027-08-31", "2027-01-31"])
def test_fiscal_year_mannaz_convention(end):
    row = est.parse_quote_summary(payload(trend(end=end))).rows[0]
    assert row.period_end == date.fromisoformat(end) and row.fiscal_year == 2027


def test_missing_metric_values_and_explicit_revenue_currency():
    t = trend()
    t["earningsEstimate"].pop("avg")
    t["earningsEstimate"]["low"] = {}
    t["earningsEstimate"].pop("earningsCurrency")
    t["revenueEstimate"]["revenueCurrency"] = "EUR"
    row = est.parse_quote_summary(payload(t)).rows[0]
    assert row.eps_avg is None and row.eps_low is None and row.eps_high == 3
    assert row.eps_currency is None and row.currency_check == est.EPS_CURRENCY_UNKNOWN
    assert row.rev_currency == "EUR"


def test_default_get_injected_at_yfdata_boundary(monkeypatch):
    from yfinance.data import YfData

    calls = []

    def get(self, url, params=None, timeout=30):
        calls.append((url, params.copy(), timeout))
        return response(b"doslowne bajty")

    monkeypatch.setattr(YfData, "get", get)
    fr = est.fetch_quote_summary("SYN", now_utc=NOW)
    assert fr.error is None and fr.body == b"doslowne bajty"
    assert calls == [(fr.url, fr.query_params, 30)]
    assert fr.query_params["modules"] == ",".join(est.MODULES)
    assert fr.query_params["formatted"] == "false"


def test_archive_hash_verification(tmp_path, monkeypatch):
    from pathlib import Path

    original = Path.read_bytes

    def corrupted(path):
        body = original(path)
        return body + b"!" if path.suffix == ".body" else body

    monkeypatch.setattr(Path, "read_bytes", corrupted)
    entry = est.archive_snapshot(tmp_path, fetch())
    assert "sha256" in entry.error


def test_unmapped_summary_without_cursor(tmp_path):
    entry = est.archive_snapshot(tmp_path, fetch())
    summary = est.insert_snapshot(None, None, entry, est.ParsedSnapshot(), "synthetic")
    assert summary.snapshot_id is None and summary.inserted_rows == 0
    assert summary.archive_only_count == 1
    assert summary.archive_only_symbols == (entry.fetch.source_symbol,)


def test_archive_failure_blocks_insert(tmp_path):
    entry = replace(est.archive_snapshot(tmp_path, fetch()), error="syntetyczny blad")
    with pytest.raises(ValueError, match="blad archiwizacji"):
        est.insert_snapshot(None, 1, entry, est.ParsedSnapshot(), "synthetic")
