"""Forward P/E - pilotaz (brief CC forward-pe rew. 1, par. 5). Funkcje czyste, bez sieci."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

MEASURE_NAME = "P/E NTM — przybliżenie z konsensusu rocznego"

# prog operacyjny, do kalibracji
RATIO_MIN_GAAP_SHARE = 0.1
# prog operacyjny
DIVERGENCE_FLAG = 0.10

REVISION_TOLERANCE_DAYS = 7
DIVERGENCE_MIN_BASE = 0.05

STATUS_NO_REVISION = "brak porównywalnej rewizji"
STATUS_RATIO_UNDEFINED = "iloraz nieokreślony"
STATUS_DIVERGENCE_NA = "baza bliska zeru / zmiana znaku"
STATUS_AGE_UNKNOWN = "wiek konsensusu nieznany"
STATUS_PERIOD_TIE = "remis dopasowania okresu"

POSITION_STATUSES = ("dane", "brak danych", "wyłączenie metodologiczne", "gotówka")


# ---------------------------------------------------------------------------
# F1 - EPS NTM
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NtmEstimate:
    eps: float
    w: float
    measure: str


def _year_earlier(d: date) -> date:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:  # 29.02 -> 28.02
        return d.replace(year=d.year - 1, day=28)


def eps_ntm(d: date, fy1_end: date, fy1_eps: float, fy2_eps: float) -> NtmEstimate:
    fy0_end = _year_earlier(fy1_end)
    w = (fy1_end - d).days / (fy1_end - fy0_end).days
    w = min(1.0, max(0.0, w))
    return NtmEstimate(eps=w * fy1_eps + (1 - w) * fy2_eps, w=w, measure=MEASURE_NAME)


# ---------------------------------------------------------------------------
# F2 - rewizja po dacie konca okresu
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PeriodEstimate:
    label: str
    period_end: date
    eps_avg: float


@dataclass(frozen=True)
class Snapshot:
    taken_on: date
    periods: tuple[PeriodEstimate, ...]


@dataclass(frozen=True)
class Revision:
    rel: float | None
    abs_change: float | None
    days_between: int
    status: str | None


def _match_period(snap: Snapshot, period_end: date) -> PeriodEstimate | str | None:
    cands = [
        (abs((p.period_end - period_end).days), p)
        for p in snap.periods
        if abs((p.period_end - period_end).days) <= REVISION_TOLERANCE_DAYS
    ]
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    if len(cands) > 1 and cands[0][0] == cands[1][0]:
        return STATUS_PERIOD_TIE
    return cands[0][1]


def revision_fixed(snapshot_a: Snapshot, snapshot_b: Snapshot, period_end: date) -> Revision:
    days = (snapshot_b.taken_on - snapshot_a.taken_on).days
    pa = _match_period(snapshot_a, period_end)
    pb = _match_period(snapshot_b, period_end)
    if pa == STATUS_PERIOD_TIE or pb == STATUS_PERIOD_TIE:
        return Revision(rel=None, abs_change=None, days_between=days, status=STATUS_PERIOD_TIE)
    if pa is None or pb is None:
        return Revision(rel=None, abs_change=None, days_between=days, status=STATUS_NO_REVISION)
    rel = pb.eps_avg / pa.eps_avg - 1 if pa.eps_avg != 0 else None
    return Revision(rel=rel, abs_change=pb.eps_avg - pa.eps_avg, days_between=days, status=None)


# ---------------------------------------------------------------------------
# F3 - wzrost TTM -> NTM
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Growth:
    status: str
    pct: float | None
    delta: float


def growth_status(eps_ttm: float, eps_ntm: float) -> Growth:
    delta = eps_ntm - eps_ttm
    if eps_ttm == 0 or eps_ntm == 0:
        return Growth("zerowa baza — wzrost procentowy nieokreślony", None, delta)
    if eps_ttm > 0 and eps_ntm > 0:
        return Growth("wzrost procentowy", (eps_ntm / eps_ttm - 1) * 100, delta)
    if eps_ttm < 0 and eps_ntm > 0:
        return Growth("oczekiwane przejście ze straty do zysku", None, delta)
    if eps_ttm > 0 and eps_ntm < 0:
        return Growth("oczekiwane przejście z zysku do straty", None, delta)
    # oba < 0
    if delta > 0:
        return Growth("oczekiwane zmniejszenie straty", None, delta)
    if delta < 0:
        return Growth("oczekiwane pogłębienie straty", None, delta)
    # poza spec: delta == 0 przy obu ujemnych
    return Growth("bez zmiany straty", None, delta)


# ---------------------------------------------------------------------------
# F4 - luka adjusted vs GAAP
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GaapGap:
    eps_adj: float
    eps_gaap: float
    diff: float
    ratio: float | None
    ratio_status: str | None


def gaap_gap(eps_adj: float, eps_gaap: float) -> GaapGap:
    diff = eps_adj - eps_gaap
    if eps_gaap > 0 and eps_gaap >= RATIO_MIN_GAAP_SHARE * abs(eps_adj):
        return GaapGap(eps_adj, eps_gaap, diff, eps_adj / eps_gaap, None)
    return GaapGap(eps_adj, eps_gaap, diff, None, STATUS_RATIO_UNDEFINED)


def pe_ratio(price: float, eps: float) -> float | None:
    if eps <= 0:
        return None
    return price / eps


# ---------------------------------------------------------------------------
# F5 - agregacja portfela
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Position:
    weight: float
    status: str
    ep: float | None  # ulamek (0.05 = 5%)

    def __post_init__(self) -> None:
        if self.status not in POSITION_STATUSES:
            raise ValueError(f"nieznany status pozycji: {self.status!r}")
        if self.status == "dane" and self.ep is None:
            raise ValueError("status 'dane' wymaga ep")
        if self.status != "dane" and self.ep is not None:
            raise ValueError("status inny niz 'dane' wymaga ep=None (brak danych nie zastepujemy zerem)")


EQUITY_LABEL = "P/E części akcyjnej"
UNCOVERED_STATUSES = ("brak danych", "wyłączenie metodologiczne")


@dataclass(frozen=True)
class Aggregate:
    """Decyzja planisty FPE r1 (pkt 4): gotowka w rozbiciu wag, poza mianownikiem
    wyceny; pokrycie liczone wzgledem wagi niegotowkowej."""

    weights: dict[str, float]
    cash_share: float
    coverage_data: float | None
    coverage_positive: float | None
    ep_covered: float | None
    pe_covered_positive: float | None
    pe_equity: float | None
    pe_equity_status: str | None
    pe_equity_label: str = EQUITY_LABEL


def aggregate(positions: list[Position]) -> Aggregate:
    weights = {
        "E/P>0": 0.0,
        "E/P<0": 0.0,
        "E/P=0": 0.0,
        "brak danych": 0.0,
        "wyłączenie metodologiczne": 0.0,
        "gotówka": 0.0,
    }
    data = [p for p in positions if p.status == "dane"]
    for p in positions:
        if p.status == "dane":
            assert p.ep is not None
            key = "E/P>0" if p.ep > 0 else ("E/P<0" if p.ep < 0 else "E/P=0")
        else:
            key = p.status
        weights[key] += p.weight

    total = sum(p.weight for p in positions)
    cash = weights["gotówka"]
    non_cash = total - cash
    w_data = sum(p.weight for p in data)
    pos = [p for p in data if p.ep > 0]  # type: ignore[operator]
    w_pos = sum(p.weight for p in pos)

    # brak danych i wylaczenie metodologiczne = niepokryte; mianownik bez gotowki.
    # Procent pokrycia tylko do wyswietlenia - o statusie decyduja kategorie pozycji
    # (decyzja planisty FPE r1b pkt 1).
    coverage_data = w_data / non_cash if non_cash > 0 else None
    uncovered = any(p.status in UNCOVERED_STATUSES and p.weight > 0 for p in positions)
    coverage_positive = w_pos / non_cash if non_cash > 0 else None

    ep_covered = (
        sum(p.weight * p.ep for p in data) / w_data  # type: ignore[operator]
        if data and w_data != 0
        else None
    )
    pe_covered_positive = (
        w_pos / sum(p.weight * p.ep for p in pos)  # type: ignore[operator]
        if pos
        else None
    )

    if uncovered or ep_covered is None:
        pe_equity, status = None, "nieinterpretowalne (braki danych)"
    elif ep_covered <= 0:
        pe_equity, status = None, "nieinterpretowalne (E/P ≤ 0)"
    else:
        pe_equity, status = 1 / ep_covered, None

    return Aggregate(
        weights=weights,
        cash_share=cash / total if total > 0 else 0.0,
        coverage_data=coverage_data,
        coverage_positive=coverage_positive,
        ep_covered=ep_covered,
        pe_covered_positive=pe_covered_positive,
        pe_equity=pe_equity,
        pe_equity_status=status,
    )


# ---------------------------------------------------------------------------
# F6 - rozbieznosc zrodel
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Divergence:
    rel: float | None
    flag: bool | None
    status: str | None


def divergence(primary: float, check: float) -> Divergence:
    if abs(primary) < DIVERGENCE_MIN_BASE or primary * check < 0:
        return Divergence(None, None, STATUS_DIVERGENCE_NA)
    rel = abs(check - primary) / abs(primary)
    return Divergence(rel, rel > DIVERGENCE_FLAG, None)


# ---------------------------------------------------------------------------
# F7 - swiezosc konsensusu
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Freshness:
    status: str | None
    age_days: int | None


def freshness(consensus_date: date | None, today: date) -> Freshness:
    if consensus_date is None:
        return Freshness(STATUS_AGE_UNKNOWN, None)
    return Freshness(None, (today - consensus_date).days)
