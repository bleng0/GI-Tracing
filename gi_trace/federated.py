"""Cross-engine read (Li & Gu MSI pattern, restricted to the batch_id key).

SQL and MongoDB are joined only on the indexed batch_id, never on arbitrary
predicates, which avoids the recursive cross-engine join cost of Nest-G.
"""
from __future__ import annotations

import sqlite3

from .sql_store import get_batch


def shipment_dossier(conn: sqlite3.Connection, coll, batch_id: str) -> dict | None:
    batch = get_batch(conn, batch_id)
    if batch is None:
        return None
    orders = [
        dict(r)
        for r in conn.execute(
            """
            SELECT o.*, t.rate_pct AS tariff_rate_pct
            FROM orders o
            LEFT JOIN tariff_schedule t
              ON t.hs_code = ? AND t.origin = ? AND t.dest = o.dest_jurisdiction
            WHERE o.batch_id = ?
            """,
            (batch["hs_code"], batch["origin"], batch_id),
        )
    ]
    certificates = list(coll.find({"batch_id": batch_id}, {"_id": 0}))
    return {"batch": batch, "orders": orders, "certificates": certificates}
