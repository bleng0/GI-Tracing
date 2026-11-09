"""JSON-Tiles-style materialised compliance view (mechanism 1).

Scans every certificate in MongoDB, flattens nested objects to dotted paths
(arrays are left out), and copies each path that appears in at least
`threshold` of the documents into the SQL table `compliance_view`, with an
index per column and ANALYZE statistics for the query planner. Rarer,
craft-specific paths and all arrays stay only in MongoDB.

`threshold` is the share of documents containing a path (as in JSON Tiles),
not "the top 80 % of keys".
"""
from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict


def flatten(doc: dict, prefix: str = "") -> dict:
    out = {}
    for key, value in doc.items():
        if key == "_id":
            continue
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten(value, f"{path}."))
        elif not isinstance(value, list):
            out[path] = value
    return out


def _sql_type(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (bool, int)):
        return "INTEGER"
    if isinstance(value, float):
        return "REAL"
    return "TEXT"


def _column_type(types: set) -> str:
    if len(types) == 1:
        return next(iter(types))
    if types == {"INTEGER", "REAL"}:
        return "REAL"
    return "TEXT"


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def materialize_compliance_view(conn: sqlite3.Connection, coll, threshold: float = 0.8) -> dict[str, float]:
    """Rebuilds compliance_view; returns {extracted path: frequency}."""
    docs = list(coll.find())
    if not docs:
        raise ValueError("no certificates to materialise")

    counts: Counter = Counter()
    types: dict[str, set] = defaultdict(set)
    flat_docs = []
    for doc in docs:
        flat = flatten(doc)
        flat_docs.append((str(doc["_id"]), flat))
        counts.update(flat.keys())
        for path, value in flat.items():
            t = _sql_type(value)
            if t:
                types[path].add(t)

    freq = {path: n / len(docs) for path, n in counts.items()}
    hot = sorted(path for path, f in freq.items() if f >= threshold)
    if "batch_id" not in hot:
        raise ValueError("batch_id must be present in the certificates to join with SQL")

    columns = ", ".join(f"{_ident(p)} {_column_type(types[p]) if types[p] else 'TEXT'}" for p in hot)
    conn.execute("DROP TABLE IF EXISTS compliance_view")
    conn.execute(f"CREATE TABLE compliance_view (cert_id TEXT PRIMARY KEY, {columns})")
    for i, path in enumerate(hot):
        conn.execute(f"CREATE INDEX ix_compliance_view_{i} ON compliance_view ({_ident(path)})")
    placeholders = ", ".join("?" * (len(hot) + 1))
    conn.executemany(
        f"INSERT INTO compliance_view VALUES ({placeholders})",
        [[cert_id] + [flat.get(p) for p in hot] for cert_id, flat in flat_docs],
    )
    conn.execute("ANALYZE compliance_view")
    conn.commit()
    return {p: freq[p] for p in hot}
