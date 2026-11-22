"""Read-only SQL backend for the admin workbench.

Only ``gi_trace.db`` is opened, through a URI connection in ``mode=ro``, with
``PRAGMA query_only`` on and an authorizer that permits just SELECT, READ and
FUNCTION actions on the prototype's own tables. Nothing here can reach
``users.db``, another file, or a write statement: the text is screened before
SQLite sees it and the authorizer screens what SQLite actually wants to do.

One statement only (no semicolons), at most MAX_LIMIT rows, and the progress
handler stops a query after TIMEOUT_SECONDS.
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

DB_NAME = "gi_trace.db"
MAX_LIMIT = 200
TIMEOUT_SECONDS = 10
PROGRESS_STEPS = 1000
MAX_TEXT = 20_000
# Tables owned by the prototype (sql_store + the materialised tiles view).
TABLES = (
    "artisans", "batch_materials", "batches", "compliance_view", "cooperatives",
    "jurisdiction_rules", "orders", "tariff_schedule",
)
# Words that mean a write, an attached file, or a PRAGMA that could change the
# connection. Checked with word boundaries before the statement is executed;
# the authorizer is the second, authoritative layer.
FORBIDDEN = (
    "ALTER", "ATTACH", "CREATE", "DELETE", "DETACH", "DROP", "INSERT", "PRAGMA",
    "REINDEX", "REPLACE", "TRIGGER", "UPDATE", "VACUUM", "VIEW", "INDEX",
)
_FORBIDDEN_RE = re.compile(r"\b(?:" + "|".join(FORBIDDEN) + r")\b", re.IGNORECASE)
_DENY_FUNCTIONS = {"load_extension", "readfile", "writefile", "eval"}


class WorkbenchError(ValueError):
    """Shown to the admin as is; never contains a file path or connection detail."""


def _actions():
    return (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION)


def _authorizer(action, arg1, arg2, db_name, source):
    if action not in _actions():
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ:
        if arg1 not in TABLES or db_name not in ("main", None, ""):
            return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and arg1 and str(arg1).lower() in _DENY_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def db_path(data_dir) -> Path:
    """The one database the workbench may read, resolved inside the world folder."""
    root = Path(data_dir).resolve()
    path = (root / DB_NAME).resolve()
    if path.parent != root or path.name != DB_NAME:
        raise WorkbenchError("the relational database is not available")
    return path


def parse(text: str) -> str:
    """Validate the submitted SQL. Raises WorkbenchError with a short message."""
    sql = (text or "").strip()
    if not sql:
        raise WorkbenchError("enter a SQL SELECT query")
    if len(sql) > MAX_TEXT:
        raise WorkbenchError(f"the query is longer than {MAX_TEXT} characters")
    if ";" in sql:
        raise WorkbenchError("one statement at a time: remove the semicolon")
    if not re.match(r"^(?:SELECT|WITH)\b", sql, re.IGNORECASE):
        raise WorkbenchError("only SELECT (or WITH ... SELECT) queries are allowed")
    word = _FORBIDDEN_RE.search(sql)
    if word:
        raise WorkbenchError(f"{word.group(0).upper()} is not allowed")
    lower = sql.lower()
    if "users.db" in lower or re.search(r"\busers\b", lower):
        raise WorkbenchError("dashboard accounts are not in the workbench schema")
    if re.search(r"\bsqlite_(?:master|schema|temp_master)\b", lower) or "file:" in lower:
        raise WorkbenchError("the database catalogue and file URIs are not available")
    return sql


def schema(path) -> list[dict]:
    """Table and column names for the sidebar, read before any authorizer is set."""
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        names = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        return [{"name": name,
                 "columns": [row[1] for row in conn.execute(f'PRAGMA table_info("{name}")')]}
                for name in names if name in TABLES]
    finally:
        conn.close()


def run(path, text: str) -> tuple[list[str], list[list], bool]:
    """Return (columns, rows, truncated). Raises WorkbenchError on any refusal."""
    sql = parse(text)
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    started = time.perf_counter()

    def progress():
        return 1 if time.perf_counter() - started > TIMEOUT_SECONDS else 0

    try:
        conn.row_factory = sqlite3.Row
        conn.enable_load_extension(False)
        conn.execute("PRAGMA query_only = ON")
        conn.set_authorizer(_authorizer)
        conn.set_progress_handler(progress, PROGRESS_STEPS)
        try:
            cursor = conn.execute(sql)
        except sqlite3.OperationalError as e:
            if "interrupted" in str(e).lower():
                raise WorkbenchError(f"stopped after {TIMEOUT_SECONDS} s (time limit)") from None
            raise WorkbenchError(f"SQLite refused the query: {e}") from None
        except sqlite3.DatabaseError as e:
            raise WorkbenchError(f"SQLite refused the query: {type(e).__name__}") from None
        columns = [d[0] for d in (cursor.description or [])]
        try:
            rows = cursor.fetchmany(MAX_LIMIT + 1)
        except sqlite3.OperationalError as e:
            raise WorkbenchError(f"SQLite refused the query: {e}") from None
        truncated = len(rows) > MAX_LIMIT
        return columns, [list(row) for row in rows[:MAX_LIMIT]], truncated
    finally:
        conn.set_progress_handler(None, 0)
        conn.set_authorizer(None)
        conn.close()
