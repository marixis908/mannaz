"""Opening balances declared by the owner for holdings acquired outside the
broker cash history (e.g. pre-IPO allocations).

Source file: data/manual/opening_balances.csv (git-ignored, `;`-separated):
date;rachunek;currency;broker_ticker;qty;total_cost;note[;row_type]

Each row becomes a `transactions` row with row_type 'bilans_otwarcia', which
FIFO treats as a buy dated at `date`. Idempotent via the natural UNIQUE key.
"""
from __future__ import annotations

import csv
import hashlib
from decimal import Decimal
from pathlib import Path

import psycopg

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "manual" / "opening_balances.csv"
ROW_TYPE = "bilans_otwarcia"
# manual row types -> title prefix; FIFO maps the first two to buys, the last to a sale
MANUAL_ROW_TYPES = {
    "bilans_otwarcia": "Bilans otwarcia",
    "zamiana_przyjecie": "Zamiana akcji - przyjecie",
    "zamiana_wydanie": "Zamiana akcji - wydanie",
}
INFLOW_ROW_TYPES = ("bilans_otwarcia", "zamiana_przyjecie")


def load_opening_balances(conn: psycopg.Connection, path: Path = DEFAULT_PATH) -> int:
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    rows = list(csv.DictReader(raw.decode("utf-8-sig").splitlines(), delimiter=";"))
    inserted = 0
    with conn.cursor() as cur:
        for r in rows:
            cur.execute("SELECT id FROM instruments WHERE broker_ticker = %s", (r["broker_ticker"],))
            found = cur.fetchone()
            if found is None:
                raise ValueError(f"unknown instrument: {r['broker_ticker']}")
            qty = Decimal(r["qty"])
            cost = Decimal(r["total_cost"])
            row_type = r.get("row_type") or ROW_TYPE
            if row_type not in MANUAL_ROW_TYPES:
                raise ValueError(f"unsupported row_type: {row_type}")
            title = f"{MANUAL_ROW_TYPES[row_type]}: {r['broker_ticker']} ({r['note']})"
            cur.execute(
                """
                INSERT INTO transactions (transaction_date, rachunek, currency, title_raw, amount,
                    occurrence_no, row_type, instrument_id, qty, price, source_file, source_sha256)
                VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (r["date"], r["rachunek"], r["currency"], title,
                 -cost if row_type in INFLOW_ROW_TYPES else cost, row_type, found[0],
                 qty, cost / qty, "manual:opening_balances.csv", sha),
            )
            inserted += cur.rowcount
    conn.commit()
    return inserted
