"""Konsensus Yahoo: bajty HTTP, archiwum append-only i parser M93.

Modul nie wybiera katalogu archiwum, nie otwiera bazy i nie commituje
transakcji. Wywolujacy odpowiada za mapowanie instrumentow i transakcje.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re

import yfinance
from yfinance.exceptions import YFRateLimitError

PINNED_VERSION = "1.7.0"
SOURCE = "yahoo_quotesummary"
MODULES = ("earningsTrend", "earningsHistory", "defaultKeyStatistics", "calendarEvents", "price")
NO_CONSENSUS = "brak zweryfikowanego konsensusu"
NO_PERIOD_END = "brak daty okresu"
BAD_PERIOD_END = "bledna data okresu"
PERIOD_UNCONFIRMED = "data okresu niepotwierdzona niezaleznie"
EPS_CURRENCY_UNKNOWN = "waluta EPS nieznana"
CURRENCY_MISMATCH = "waluta EPS rozna od waluty notowania - niepotwierdzona (P-15)"


@dataclass(frozen=True)
class FetchResult:
    source_symbol: str
    fetched_at_utc: datetime
    source: str
    modules: tuple[str, ...]
    query_params: dict
    url: str
    library: str
    library_version: str
    http_status: int | None = None
    content_type: str | None = None
    content_encoding: str | None = None
    body: bytes | None = None
    error: str | None = None


def fetch_quote_summary(symbol, *, http_get=None, now_utc=None) -> FetchResult:
    """Pobierz response.content bez dekodowania ani ponownej serializacji.

    query_params to doslowne parametry przekazane do YfData.get; cookie
    i crumb sa obslugiwane wewnatrz yfinance, nie sa danymi konsensusu.
    """
    # Wartosci awaryjne, gdy budowa pol wspolnych rzuci: czas biezacy UTC,
    # query_params={}, url="" (tylko dla tego, czego nie dalo sie zbudowac).
    taken_at = datetime.now(timezone.utc)
    params, url, version = {}, "", ""
    common = None
    try:
        if now_utc is not None:
            if now_utc.tzinfo is None or now_utc.utcoffset() is None:
                raise ValueError("now_utc musi miec strefe czasowa")
            taken_at = now_utc.astimezone(timezone.utc)
        version = yfinance.__version__
        from yfinance.config import YfConfig
        from yfinance.const import _BASE_URL_

        params = {
            "modules": ",".join(MODULES), "corsDomain": "finance.yahoo.com",
            "formatted": "false", "symbol": symbol,
            "lang": YfConfig.locale.lang, "region": YfConfig.locale.region,
        }
        # yfinance 1.7.0: _QUOTE_SUMMARY_URL_ + "/" + symbol (quote.py:43, :659), bez kodowania.
        url = f"{_BASE_URL_}/v10/finance/quoteSummary/{symbol}"
        common = dict(
            source_symbol=symbol, fetched_at_utc=taken_at, source=SOURCE,
            modules=MODULES, query_params=params, url=url,
            library="yfinance", library_version=version,
        )
        if version != PINNED_VERSION:
            return FetchResult(**common, error=f"yfinance {version} != {PINNED_VERSION}")
        if http_get is None:
            from yfinance.data import YfData

            http_get = YfData().get
        response = http_get(common["url"], params=params.copy(), timeout=30)
        body = response.content
        if not isinstance(body, bytes):
            raise TypeError("response.content nie jest bytes")
        status = response.status_code
        return FetchResult(
            **common, body=body, http_status=status,
            content_type=response.headers.get("Content-Type"),
            content_encoding=response.headers.get("Content-Encoding"),
            error=None if status == 200 else f"HTTP {status}",
        )
    except Exception as exc:
        if common is None:
            common = dict(
                source_symbol=symbol, fetched_at_utc=taken_at, source=SOURCE,
                modules=MODULES, query_params=params, url=url,
                library="yfinance", library_version=version,
            )
        if isinstance(exc, YFRateLimitError):
            return FetchResult(
                **common,
                error="YFRateLimitError: HTTP 429 wg yfinance 1.7.0 "
                      "data.py:505-506; body niedostepne w bibliotece",
            )
        return FetchResult(**common, error=f"{type(exc).__name__}: {exc}")


@dataclass(frozen=True)
class ArchiveEntry:
    fetch: FetchResult
    raw_ref: str | None = None
    raw_sha256: str | None = None
    byte_count: int | None = None
    meta_ref: str | None = None
    error: str | None = None  # blad archiwizacji; blad HTTP jest w fetch.error


def archive_snapshot(root: Path, fr: FetchResult) -> ArchiveEntry:
    """Zapis wylaczny; czesciowe pliki po bledzie zostaja do audytu."""
    raw_ref = raw_sha256 = byte_count = meta_ref = None
    try:
        safe_symbol = re.sub(r"[^A-Za-z0-9._-]", "_", fr.source_symbol)
        # Puste nazwy i skladowe '.'/'..' nie moga zmienic katalogu zapisu.
        if safe_symbol in ("", ".", ".."):
            safe_symbol = "_" + safe_symbol.replace(".", "_")
        if fr.source != SOURCE:
            raise ValueError("nieobslugiwane zrodlo archiwum")
        if fr.fetched_at_utc.utcoffset() is None:
            raise ValueError("fetched_at_utc musi miec strefe czasowa")
        stamp = fr.fetched_at_utc.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        relative = Path(fr.source) / safe_symbol / stamp
        body_path = root / relative.with_suffix(".body")
        meta_path = root / relative.with_suffix(".meta.json")
        if body_path.exists() or meta_path.exists():
            raise FileExistsError("snapshot juz istnieje")
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        if fr.body is not None:
            with body_path.open("xb") as stream:
                stream.write(fr.body)
            written = body_path.read_bytes()
            raw_sha256 = hashlib.sha256(written).hexdigest()
            if raw_sha256 != hashlib.sha256(fr.body).hexdigest():
                raise ValueError("sha256 po zapisie niezgodny z body")
            raw_ref = relative.with_suffix(".body").as_posix()
            byte_count = len(written)
        meta = {key: value for key, value in asdict(fr).items() if key != "body"}
        meta["fetched_at_utc"] = fr.fetched_at_utc.astimezone(timezone.utc).isoformat()
        meta.update(raw_ref=raw_ref, raw_sha256=raw_sha256, byte_count=byte_count,
                    safe_symbol=safe_symbol)
        with meta_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(meta, stream, sort_keys=True, ensure_ascii=True)
            stream.write("\n")
        meta_ref = relative.with_suffix(".meta.json").as_posix()
        return ArchiveEntry(fr, raw_ref, raw_sha256, byte_count, meta_ref)
    except (OSError, ValueError, TypeError) as exc:
        return ArchiveEntry(fr, raw_ref, raw_sha256, byte_count, meta_ref,
                            f"{type(exc).__name__}: {exc}")


@dataclass(frozen=True)
class Estimate:
    period: str
    period_type: str
    period_end: date | None
    fiscal_year: int | None
    eps_avg: float | None
    eps_low: float | None
    eps_high: float | None
    n_analysts_eps: int | None
    rev_avg: float | None
    rev_low: float | None
    rev_high: float | None
    n_analysts_rev: int | None
    eps_currency: str | None
    rev_currency: str | None
    eps_status: str | None
    rev_status: str | None
    period_end_check: str
    currency_check: str | None
    status: str | None


@dataclass(frozen=True)
class ParsedSnapshot:
    rows: list[Estimate] = field(default_factory=list)
    price_currency: str | None = None
    status: str | None = None
    skipped_labels: list = field(default_factory=list)


def _raw(value):
    return value.get("raw") if isinstance(value, dict) else value


def _fiscal_year(period_end):
    """Rok kalendarzowy konca okresu: konwencja Mannaz, nie etykieta emitenta."""
    return period_end.year if period_end is not None else None


def _metric(data):
    n = _raw(data.get("numberOfAnalysts"))
    covered = isinstance(n, (int, float)) and n > 0
    values = tuple(_raw(data.get(key)) for key in ("avg", "low", "high"))
    return (*values, n, None) if covered else (None, None, None, n, NO_CONSENSUS)


def parse_quote_summary(body: bytes) -> ParsedSnapshot:
    """Odczyt per okres i metryka; braki nigdy nie sa zastepowane zerem."""
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeError, TypeError) as exc:
        return ParsedSnapshot(status=f"bledny JSON: {type(exc).__name__}")
    if not isinstance(doc, dict):
        return ParsedSnapshot(status="nieprawidlowy format odpowiedzi")
    for envelope in ("quoteSummary", "finance"):
        section = doc.get(envelope)
        if isinstance(section, dict) and section.get("error") is not None:
            return ParsedSnapshot(status=f"{envelope}.error: {section['error']}")
    summary = doc.get("quoteSummary")
    results = summary.get("result") if isinstance(summary, dict) else None
    if not results:
        return ParsedSnapshot(status="brak wyniku dostawcy")
    if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
        return ParsedSnapshot(status="nieprawidlowy format result")
    if len(results) != 1:
        return ParsedSnapshot(status="niejednoznaczny result dostawcy")
    result = results[0]
    price_currency = _raw((result.get("price") or {}).get("currency"))
    trends = (result.get("earningsTrend") or {}).get("trend") or []
    rows, skipped = [], []
    for trend in trends:
        label = trend.get("period")
        if label not in ("0q", "+1q", "0y", "+1y"):
            skipped.append(label)
            continue
        end = _raw(trend.get("endDate"))
        row_status = NO_PERIOD_END if end is None else None
        try:
            end = date.fromisoformat(end) if end is not None else None
        except (ValueError, TypeError):
            end, row_status = None, BAD_PERIOD_END
        eps, rev = trend.get("earningsEstimate") or {}, trend.get("revenueEstimate") or {}
        ea, el, eh, en, es = _metric(eps)
        ra, rl, rh, rn, rs = _metric(rev)
        ec, rc = _raw(eps.get("earningsCurrency")), _raw(rev.get("revenueCurrency"))
        check = EPS_CURRENCY_UNKNOWN if ec is None else (
            CURRENCY_MISMATCH if price_currency is not None and ec != price_currency else None)
        rows.append(Estimate(
            label, "quarter" if label.endswith("q") else "year", end, _fiscal_year(end),
            ea, el, eh, en, ra, rl, rh, rn, ec, rc, es, rs,
            PERIOD_UNCONFIRMED, check, row_status,
        ))
    # Status snapshotu nie myli przyczyn: bledna data ma pierwszenstwo nad brakiem.
    if any(row.status == BAD_PERIOD_END for row in rows):
        status = BAD_PERIOD_END
    elif any(row.status == NO_PERIOD_END for row in rows):
        status = NO_PERIOD_END
    else:
        status = None
    if not rows:
        status = "brak okresow konsensusu"
    return ParsedSnapshot(rows, price_currency, status, skipped)


@dataclass(frozen=True)
class InsertSummary:
    snapshot_id: int | None
    inserted_rows: int
    archive_only_count: int = 0
    archive_only_symbols: tuple[str, ...] = ()


def insert_snapshot(cur, instrument_id, entry: ArchiveEntry, parsed: ParsedSnapshot, code_sha):
    """INSERT bez commit; niezmapowany symbol zwraca licznik 'tylko archiwum'."""
    if entry.error is not None:
        raise ValueError(f"blad archiwizacji: {entry.error}")
    if instrument_id is None:
        return InsertSummary(None, 0, 1, (entry.fetch.source_symbol,))
    from psycopg.types.json import Jsonb

    fr = entry.fetch
    cur.execute(
        "INSERT INTO estimate_snapshots "
        "(instrument_id, source, source_symbol, fetched_at_utc, modules, query_params, "
        "library, library_version, http_status, error, raw_ref, raw_sha256, byte_count, "
        "target_mean, price_currency, quality_flag, parse_status, skipped_labels, code_sha) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "RETURNING id",
        (instrument_id, fr.source, fr.source_symbol, fr.fetched_at_utc, list(fr.modules),
         Jsonb(fr.query_params), fr.library, fr.library_version, fr.http_status, fr.error,
         entry.raw_ref, entry.raw_sha256, entry.byte_count, None, parsed.price_currency,
         "low", parsed.status, [str(label) for label in parsed.skipped_labels], code_sha),
    )
    snapshot_id = cur.fetchone()[0]
    for row in parsed.rows:
        fields = asdict(row)
        columns = ", ".join(fields)
        placeholders = ", ".join(["%s"] * (len(fields) + 1))
        cur.execute(f"INSERT INTO estimates (snapshot_id, {columns}) VALUES ({placeholders})",
                    (snapshot_id, *fields.values()))
    return InsertSummary(snapshot_id, len(parsed.rows))
