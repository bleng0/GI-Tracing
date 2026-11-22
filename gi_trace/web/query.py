"""Read-only document queries (admin only): the query console and the workbench.

The console does one find() on `certificates` or `documents`; the workbench adds
`find` and read-only `aggregate` on those two plus the ledger collections.

The filter, projection and sort are plain JSON (not Extended JSON: {"$oid": ...} is not
understood). Refused before anything reaches the database:
  - the operators $where, $function, $accumulator, $out and $merge, in any letter case,
    at any depth, as a key or as a string value (so also inside $expr and projections);
  - text over 20 000 characters, nesting deeper than 32 levels, NaN or Infinity, integers
    that do not fit in 64 bits, a key given twice, anything but an object at the top;
  - a limit outside 1..200, a sort value other than 1 or -1.
Time limit: Atlas stops a query after 10 s (maxTimeMS). The offline store (mongomock)
ignores maxTimeMS, so offline a query runs in a separate Python process (query_worker)
that is stopped after 10 s; at most four run at once, others wait up to 10 s for a turn.
"""
from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

COLLECTIONS = ("certificates", "documents")
# The workbench may also look at the ledger collections. The old console keeps
# its two-collection allow-list, so its existing contract does not change.
WORKBENCH_COLLECTIONS = COLLECTIONS + ("ledger_blocks", "ledger_heads")
BANNED = ("$where", "$function", "$accumulator", "$out", "$merge")
# Cross-collection stages and write/admin commands are refused in the workbench
# as well, even though the console never offers an aggregation pipeline.
WORKBENCH_BANNED = BANNED + (
    "$lookup", "$graphLookup", "$unionWith", "$insert", "$update", "$delete",
    "$drop", "$create", "$renameCollection", "$collMod", "$currentOp",
    "$listSessions", "$killSessions", "$planCacheClear", "$planCacheDrop",
)
# Stages with no access to another collection or to the filesystem.
AGGREGATE_STAGES = (
    "$addFields", "$bucket", "$bucketAuto", "$count", "$facet", "$group",
    "$limit", "$match", "$project", "$replaceRoot", "$set", "$skip",
    "$sort", "$sortByCount", "$unset", "$unwind",
)
MAX_STAGES = 20
MAX_LIMIT = 200
DEFAULT_LIMIT = 50
MAX_TIME_MS = 10_000
MAX_TEXT = 20_000
MAX_DEPTH = 32
MAX_SORT_KEYS = 5
OFFLINE_SLOTS = 4
STARTUP_SECONDS = 60  # a cold start of the query process on a slow Windows PC
INT64 = (-2**63, 2**63 - 1)
# Spreadsheet formula starters: = + - @ (OWASP adds tab and CR), their fullwidth forms, checked
# after any leading spaces or line breaks.
CSV_RISKY = ("=", "+", "-", "@", "\t", "\r", "\uff1d", "\uff0b", "\uff0d", "\uff20")

EXAMPLES = [
    {"title": "Rejected certificates", "collection": "certificates",
     "filter": '{"compliance_status": "rejected"}',
     "projection": '{"_id": 0, "batch_id": 1, "cert_type": 1, "signer": 1}',
     "sort": '{"batch_id": 1}', "limit": 200},
    {"title": "Hemp assays over the 0.3 % THC limit", "collection": "certificates",
     "filter": '{"cert_type": "hemp_lab_assay", "assay.thc_pct": {"$gt": 0.3}}',
     "projection": '{"_id": 0, "batch_id": 1, "assay.thc_pct": 1, "compliance_status": 1}',
     "sort": '{"assay.thc_pct": -1, "batch_id": 1}', "limit": 200},
    {"title": "All documents for batch HEMP-NP-0001", "collection": "documents",
     "filter": '{"batch_id": "HEMP-NP-0001"}',
     "projection": '{"_id": 0, "doc_id": 1, "type": 1, "issuer": 1, "issued_at": 1, "ledger": 1}',
     "sort": '{"doc_id": 1}', "limit": 200},
    {"title": "Export clearances anchored on the Nepal ledger", "collection": "documents",
     "filter": '{"type": "export_clearance", "ledger": "NP"}',
     "projection": '{"_id": 0, "doc_id": 1, "batch_id": 1, "issuer": 1, "ledger_block": 1}',
     "sort": '{"doc_id": 1}', "limit": 200},
]


class QueryError(ValueError):
    """Shown to the admin as is; never contains a connection string or a file path."""


@dataclass
class Query:
    collection: str
    filter: dict
    projection: dict | None
    sort: list[tuple[str, int]]
    limit: int


@dataclass
class WorkbenchQuery:
    op: str  # "find" or "aggregate"
    collection: str
    limit: int
    filter: dict | None = None
    projection: dict | None = None
    sort: list[tuple[str, int]] | None = None
    pipeline: list[dict] | None = None


def _no_constants(name):
    raise QueryError(f"{name} is not allowed")


def _pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise QueryError(f"the key {k!r} is given twice")
        out[k] = v
    return out


def _banned(text: str, banned=BANNED) -> str | None:
    t = text.strip().lower()
    for op in banned:
        if t == op or t.startswith(op + "."):
            return op
    return None


def check_value(value, depth: int = 0, banned=BANNED) -> None:
    """Raises QueryError for a banned operator, too deep nesting or an out-of-range number."""
    if depth > MAX_DEPTH:
        raise QueryError(f"nested deeper than {MAX_DEPTH} levels")
    if isinstance(value, dict):
        for k, v in value.items():
            op = _banned(k, banned)
            if op:
                raise QueryError(f"the operator {op} is not allowed")
            check_value(v, depth + 1, banned)
    elif isinstance(value, list):
        for v in value:
            check_value(v, depth + 1, banned)
    elif isinstance(value, str):
        op = _banned(value, banned)
        if op:
            raise QueryError(f"the operator {op} is not allowed")
    elif isinstance(value, int) and not isinstance(value, bool):
        if not INT64[0] <= value <= INT64[1]:
            raise QueryError("a number is too large")


def _load_json(text: str, what: str, banned):
    text = (text or "").strip()
    if len(text) > MAX_TEXT:
        raise QueryError(f"{what} is longer than {MAX_TEXT} characters")
    try:
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_no_constants)
    except QueryError as e:
        raise QueryError(f"{what}: {e}") from None
    except RecursionError:
        raise QueryError(f"{what}: nested deeper than {MAX_DEPTH} levels") from None
    except ValueError as e:
        raise QueryError(f"{what} is not valid JSON ({e})") from None
    try:
        check_value(value, banned=banned)
    except QueryError as e:
        raise QueryError(f"{what}: {e}") from None
    return value


def parse_json(text: str, what: str, empty=None, banned=BANNED) -> dict | None:
    text = (text or "").strip()
    if not text:
        return empty
    value = _load_json(text, what, banned)
    if not isinstance(value, dict):
        raise QueryError(f"{what} must be a JSON object, like {{}}")
    return value


def parse(form, collections=COLLECTIONS, banned=BANNED) -> Query:
    """Builds a Query from the console form (any mapping), or raises QueryError."""
    collection = form.get("collection", "")
    if collection not in collections:
        raise QueryError(f"choose one of {', '.join(collections)}")
    flt = parse_json(form.get("filter", ""), "Filter", empty={}, banned=banned)
    projection = parse_json(form.get("projection", ""), "Projection", banned=banned) or None
    sort_doc = parse_json(form.get("sort", ""), "Sort", empty={}, banned=banned)
    if len(sort_doc) > MAX_SORT_KEYS:
        raise QueryError(f"Sort: at most {MAX_SORT_KEYS} fields")
    sort = []
    for k, v in sort_doc.items():
        if type(v) is not int or v not in (1, -1):
            raise QueryError(f"Sort: {k!r} must be 1 (ascending) or -1 (descending)")
        sort.append((k, v))
    raw = str(form.get("limit", "") or DEFAULT_LIMIT).strip()
    if not (raw.isascii() and raw.isdigit()) or len(raw) > 4 or not 1 <= int(raw) <= MAX_LIMIT:
        raise QueryError(f"Limit must be a whole number from 1 to {MAX_LIMIT}")
    return Query(collection, flt, projection, sort, int(raw))


def parse_workbench(form) -> WorkbenchQuery:
    """Builds the workbench's find/aggregate query. The editor text is the filter
    for `find` and the pipeline for `aggregate`; everything is screened before
    anything reaches MongoDB (or mongomock offline)."""
    op = form.get("op", "find")
    if op not in ("find", "aggregate"):
        raise QueryError("choose find or aggregate")
    collection = form.get("collection", "")
    if collection not in WORKBENCH_COLLECTIONS:
        raise QueryError(f"choose one of {', '.join(WORKBENCH_COLLECTIONS)}")
    raw = str(form.get("limit", "") or DEFAULT_LIMIT).strip()
    if not (raw.isascii() and raw.isdigit()) or len(raw) > 4 or not 1 <= int(raw) <= MAX_LIMIT:
        raise QueryError(f"Limit must be a whole number from 1 to {MAX_LIMIT}")
    limit = int(raw)
    code = form.get("code", "")
    if op == "find":
        q = parse({"collection": collection, "filter": code, "limit": str(limit)},
                  collections=WORKBENCH_COLLECTIONS, banned=WORKBENCH_BANNED)
        return WorkbenchQuery(op, collection, limit, q.filter, q.projection, q.sort)
    text = (code or "").strip()
    if not text:
        raise QueryError("enter an aggregation pipeline, like [{ \"$match\": {} }]")
    pipeline = _load_json(text, "Pipeline", WORKBENCH_BANNED)
    if not isinstance(pipeline, list) or not pipeline:
        raise QueryError("Pipeline must be a non-empty JSON array of stages")
    if len(pipeline) > MAX_STAGES:
        raise QueryError(f"Pipeline: at most {MAX_STAGES} stages")
    stages = []
    for i, stage in enumerate(pipeline):
        if not isinstance(stage, dict) or len(stage) != 1:
            raise QueryError(f"Pipeline stage {i + 1} must be one JSON object")
        name = next(iter(stage))
        if not name.startswith("$") or name not in AGGREGATE_STAGES:
            raise QueryError(f"Pipeline stage {name!r} is not a read-only stage")
        if name == "$facet":
            for value in stage[name].values():
                if not isinstance(value, list):
                    raise QueryError("Pipeline: $facet needs arrays of read-only stages")
                for j, inner in enumerate(value):
                    if not isinstance(inner, dict) or not next(iter(inner), "").startswith("$") \
                            or next(iter(inner)) not in AGGREGATE_STAGES:
                        raise QueryError(f"Pipeline: $facet contains a stage that is not read-only")
        stages.append(stage)
    # Server-side cap even when the admin forgets a $limit stage. User limits
    # smaller than 200 still win, because an extra $limit cannot add rows.
    stages.append({"$limit": limit})
    return WorkbenchQuery(op, collection, limit, pipeline=stages)


def workbench_docs(world, collection: str) -> list[dict]:
    """Documents for one offline query. Ledgers live in files offline, so their
    collections are assembled from the already-verified world, never from a
    user-supplied path."""
    if collection == "ledger_blocks":
        from ..ledger_io import block_to_json
        return [dict(block_to_json(block), jurisdiction=j)
                for j, ledger in sorted(world.ledgers.items()) for block in ledger.blocks]
    if collection == "ledger_heads":
        import json as _json
        heads = []
        directory = Path(world.data_dir) / "ledgers"
        for j in sorted(world.ledgers):
            path = directory / f"{j}.head.json"
            if path.is_file():
                try:
                    head = _json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(head, dict):
                    heads.append(dict(head, jurisdiction=j))
        return heads
    return list(world.store.get_collection(collection).find({}))


# ---------------------------------------------------------------- running

def run(world, q: Query) -> list[dict]:
    if world.offline:
        docs = list(world.store.get_collection(q.collection).find({}))
        return run_isolated(docs, q)
    return _run_atlas(world.store.get_collection(q.collection), q)


def _run_atlas(coll, q: Query) -> list[dict]:
    from bson.errors import BSONError
    from pymongo.errors import ConnectionFailure, ExecutionTimeout, OperationFailure, PyMongoError
    try:
        cursor = coll.find(q.filter, q.projection)
        if q.sort:
            cursor = cursor.sort(q.sort)
        return list(cursor.limit(q.limit).max_time_ms(MAX_TIME_MS))
    except ConnectionFailure:
        raise  # the dashboard switches to the offline copy
    except ExecutionTimeout:
        raise QueryError(f"Atlas stopped the query after {MAX_TIME_MS / 1000:g} s (time limit)") from None
    except OperationFailure as e:
        detail = (e.details or {}).get("errmsg") or str(e)
        raise QueryError(f"Atlas refused the query: {detail}") from None
    except (PyMongoError, BSONError, OverflowError, TypeError, ValueError) as e:
        raise QueryError(f"the query could not be sent: {type(e).__name__}: {e}") from None
    except Exception as e:  # never a 500 page for a query the server could not run
        raise QueryError(f"the query failed: {type(e).__name__}") from None


def _run_aggregate_atlas(coll, q: WorkbenchQuery) -> list[dict]:
    from bson.errors import BSONError
    from pymongo.errors import ConnectionFailure, ExecutionTimeout, OperationFailure, PyMongoError
    try:
        cursor = coll.aggregate(q.pipeline, maxTimeMS=MAX_TIME_MS)
        return list(cursor)
    except ConnectionFailure:
        raise
    except ExecutionTimeout:
        raise QueryError(f"Atlas stopped the query after {MAX_TIME_MS / 1000:g} s (time limit)") from None
    except OperationFailure as e:
        detail = (e.details or {}).get("errmsg") or str(e)
        raise QueryError(f"Atlas refused the query: {detail}") from None
    except (PyMongoError, BSONError, OverflowError, TypeError, ValueError) as e:
        raise QueryError(f"the query could not be sent: {type(e).__name__}: {e}") from None
    except Exception as e:
        raise QueryError(f"the query failed: {type(e).__name__}") from None


def run_workbench(world, q: WorkbenchQuery) -> list[dict]:
    """Read-only find/aggregate, online on the read-only Atlas store or offline in
    the isolated worker. The 200-row cap is enforced in parse_workbench."""
    if world.offline:
        docs = workbench_docs(world, q.collection)
        return run_isolated(docs, q, op=q.op, pipeline=q.pipeline, collection=q.collection)
    coll = world.store.get_collection(q.collection)
    if q.op == "aggregate":
        return _run_aggregate_atlas(coll, q)
    return _run_atlas(coll, q)


_slots = threading.BoundedSemaphore(OFFLINE_SLOTS)
ROOT = Path(__file__).resolve().parents[2]


def run_isolated(docs: list[dict], q: Query, seconds: float | None = None,
                 op: str = "find", pipeline: list[dict] | None = None,
                 collection: str = "query") -> list[dict]:
    """Runs the query on `docs` in a separate Python process (query_worker, mongomock) and
    stops it after `seconds` (default MAX_TIME_MS). A runaway filter or pipeline cannot
    slow the dashboard down. `op`/`pipeline` are the workbench's aggregate option."""
    import bson
    seconds = MAX_TIME_MS / 1000 if seconds is None else seconds
    try:
        request = bson.encode({"docs": docs, "filter": q.filter or {}, "projection": q.projection,
                               "sort": [list(p) for p in (q.sort or [])], "limit": q.limit,
                               "op": op, "pipeline": pipeline or [], "collection": collection})
    except Exception as e:
        raise QueryError(f"the query cannot be encoded: {type(e).__name__}") from None
    if not _slots.acquire(timeout=seconds):
        raise QueryError("too many queries are running; try again in a moment")
    proc = None
    try:
        proc = subprocess.Popen([sys.executable, "-m", "gi_trace.web.query_worker"], cwd=str(ROOT),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))  # no console flash on Windows
        out, ready, done = bytearray(), threading.Event(), threading.Event()

        def feed():
            try:
                proc.stdin.write(request)
                proc.stdin.close()
            except OSError:
                pass  # the worker ended early; its answer (or none) says why

        def drain():
            try:
                first = proc.stdout.read(1)
                ready.set()
                out.extend(first + proc.stdout.read())
            except (OSError, ValueError):
                pass
            finally:
                ready.set()
                done.set()

        threading.Thread(target=feed, daemon=True).start()
        threading.Thread(target=drain, daemon=True).start()
        if not ready.wait(STARTUP_SECONDS):
            raise QueryError("the query process did not start")
        if not done.wait(seconds):
            raise QueryError(f"stopped after {seconds:g} s (time limit)")
        if out[:1] not in (b"R", b"E"):
            raise QueryError("the query process ended without an answer")
        del out[:1]  # the status byte: R (documents loaded) or E (failed before that)
        try:
            reply = bson.decode(bytes(out))
        except Exception:
            raise QueryError("the query process ended without an answer") from None
        if "error" in reply:
            raise QueryError(f"the offline store refused the query: {reply['error']}")
        return reply["rows"]
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            for pipe in (proc.stdin, proc.stdout):
                try:
                    pipe.close()
                except OSError:
                    pass
        _slots.release()


# ---------------------------------------------------------------- output

def _flatten(doc: dict, prefix: str = "") -> dict:
    out = {}
    for key, value in doc.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            out.update(_flatten(value, f"{path}."))
        else:
            out[path] = value
    return out


def table(rows: list[dict]) -> tuple[list[str], list[list]]:
    """Columns in order of first appearance (nested objects as dotted paths) and the values."""
    flat = [_flatten(r) for r in rows]
    columns = list(dict.fromkeys(k for f in flat for k in f))
    return columns, [[f.get(c) for c in columns] for f in flat]


def cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, default=str, ensure_ascii=False)
    return str(value)  # ObjectId, datetime


def csv_cell(value) -> str:
    """Text for one CSV cell. Text starting like a spreadsheet formula gets a leading
    apostrophe so Excel or LibreOffice shows it instead of running it; numbers are left alone."""
    text = cell_text(value)
    if not isinstance(value, (int, float)) and (text.startswith(CSV_RISKY) or text.lstrip().startswith(CSV_RISKY)):
        return "'" + text
    return text


def to_csv(columns: list[str], cells: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow([csv_cell(c) for c in columns])
    for row in cells:
        w.writerow([csv_cell(v) for v in row])
    return buf.getvalue()
