"""Admin query workbench: the thin layer between the route and the two backends.

The SQL side lives in sql_workbench.py, the document side extends query.py. This
module only validates the mode, measures elapsed time and shapes the result for
the table, the JSON view and the two exports.
"""
from __future__ import annotations

import json
import sqlite3
import time

from . import query, sql_workbench

MODES = ("sql", "nosql")
OUTPUTS = ("table", "csv", "json")

SQL_SAMPLES = [
    {"title": "Recent batches",
     "code": "SELECT batch_id, product, weight_kg\nFROM batches\nORDER BY batch_id\nLIMIT 200"},
    {"title": "Cooperatives by jurisdiction",
     "code": "SELECT jurisdiction, COUNT(*) AS coops\nFROM cooperatives\nGROUP BY jurisdiction"},
    {"title": "Orders with batch origin",
     "code": "SELECT o.order_id, o.batch_id, c.jurisdiction AS origin, o.dest_jurisdiction\n"
             "FROM orders o\nJOIN batches b ON b.batch_id = o.batch_id\n"
             "JOIN cooperatives c ON c.coop_id = b.coop_id\nLIMIT 200"},
]

NOSQL_SAMPLES = [
    {"title": "Verified certificates", "op": "find", "collection": "certificates",
     "code": '{ "compliance_status": "verified" }'},
    {"title": "Documents for one batch", "op": "find", "collection": "documents",
     "code": '{ "batch_id": "HEMP-NP-0001" }'},
    {"title": "Nepal ledger blocks", "op": "find", "collection": "ledger_blocks",
     "code": '{ "jurisdiction": "NP" }'},
    {"title": "Certificate counts", "op": "aggregate", "collection": "certificates",
     "code": '[{ "$group": { "_id": "$cert_type", "count": { "$sum": 1 } } }]'},
    {"title": "Checkpoints", "op": "find", "collection": "ledger_heads", "code": "{ }"},
]

NOSQL_COLLECTIONS = (
    {"name": "certificates", "detail": "signed compliance certificates"},
    {"name": "documents", "detail": "PDF twins and their registration data"},
    {"name": "ledger_blocks", "detail": "blocks, with jurisdiction"},
    {"name": "ledger_heads", "detail": "signed checkpoints, with jurisdiction"},
)


# The SQL backend's refusal type is the one the route shows to the admin; the
# document backend raises query.QueryError. Both are caught in app.workbench_page.
WorkbenchError = sql_workbench.WorkbenchError


def defaults() -> dict:
    return {"mode": "sql", "code": SQL_SAMPLES[0]["code"], "op": "find",
            "collection": "certificates", "limit": str(query.MAX_LIMIT), "output": "table"}


def form_from(request_form) -> dict:
    """Keep what was submitted so a refused query does not clear the editor."""
    base = defaults()
    for key in base:
        base[key] = request_form.get(key, base[key])
    if base["mode"] not in MODES:
        base["mode"] = "sql"
    if base["limit"] == "":
        base["limit"] = str(query.MAX_LIMIT)
    return base


def schema(data_dir) -> list[dict]:
    """Sidebar metadata; an unreadable database simply gives an empty sidebar."""
    try:
        return sql_workbench.schema(sql_workbench.db_path(data_dir))
    except (WorkbenchError, OSError, sqlite3.Error):
        return []


def run(data_dir, world, form) -> dict:
    """Return columns, display rows, raw rows, count, milliseconds, truncation
    and the JSON view. Raises WorkbenchError or query.QueryError on refusal."""
    mode = form.get("mode", "sql")
    if mode not in MODES:
        raise WorkbenchError("choose SQL or NoSQL")
    output = form.get("output", "table")
    if output not in OUTPUTS:
        raise WorkbenchError("unknown output format")
    started = time.perf_counter()
    if mode == "sql":
        columns, raw_rows, truncated = sql_workbench.run(
            sql_workbench.db_path(data_dir), form.get("code", ""))
    else:
        if world is None:
            raise WorkbenchError("the document store is not open yet; reload in a moment")
        q = query.parse_workbench(form)
        raw_rows = query.run_workbench(world, q)
        columns, raw_rows = query.table(raw_rows)
        truncated = len(raw_rows) >= query.MAX_LIMIT
    rows = [[query.cell_text(value) for value in row] for row in raw_rows]
    ms = int((time.perf_counter() - started) * 1000)
    payload = [dict(zip(columns, row)) for row in raw_rows]
    return {
        "columns": columns, "rows": rows, "raw_rows": raw_rows,
        "count": len(rows), "ms": ms, "truncated": truncated,
        "json": json.dumps(payload, ensure_ascii=False, indent=2, default=str),
    }


def to_csv(result: dict) -> str:
    return query.to_csv(result["columns"], result["raw_rows"])


def to_json(result: dict) -> str:
    return result["json"] + "\n"
