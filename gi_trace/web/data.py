"""What the dashboard pages show, read from an opened world and its SQLite file.
Read only: nothing here writes to the world."""
from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from ..sql_store import get_batch
from ..tiles import flatten

SHIPMENT_STATES = ("COMMITTED", "LOCKED", "ABORTED")


def sql_connect(db_path) -> sqlite3.Connection:
    """Read-only connection, one per request (SQLite connections cannot cross threads)."""
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _status(certs: list[dict]) -> str:
    statuses = {c.get("compliance_status") for c in certs}
    if not certs:
        return "no certificate"
    if statuses == {"verified"}:
        return "verified"
    return "rejected" if "rejected" in statuses else "pending"


def batches(conn, world) -> list[dict]:
    certs = defaultdict(list)
    for c in world.coll.find({}, {"_id": 0, "signature": 0}):
        certs[c.get("batch_id")].append(c)
    rows = []
    for r in conn.execute("""SELECT b.batch_id, b.product, b.created_on, b.weight_kg,
                                    c.jurisdiction AS origin, c.name AS coop_name
                             FROM batches b JOIN cooperatives c ON c.coop_id = b.coop_id
                             ORDER BY b.batch_id"""):
        own = sorted(certs.get(r["batch_id"], []), key=lambda c: str(c.get("cert_type")))
        rows.append(dict(r, status=_status(own),
                         certificates=[(c.get("cert_type"), c.get("compliance_status")) for c in own]))
    return rows


def batch_detail(conn, world, batch_id: str) -> dict | None:
    batch = get_batch(conn, batch_id)
    if batch is None:
        return None
    certs = [sorted(flatten(c).items()) for c in
             sorted(world.coll.find({"batch_id": batch_id}, {"_id": 0, "signature": 0}),
                    key=lambda c: str(c.get("cert_type")))]
    ledger = world.ledgers.get(batch["origin"])
    anchors = ledger.find("BATCH_ANCHOR", batch_id=batch_id) if ledger else []
    docs = sorted(world.store.get_collection("documents").find({"batch_id": batch_id}, {"_id": 0, "doc_id": 1, "type": 1}),
                  key=lambda d: d["doc_id"])
    return {"batch": batch, "certificates": certs, "documents": docs,
            "anchor": {"block": anchors[-1].hash, "index": anchors[-1].index, "at": anchors[-1].timestamp} if anchors else None,
            "shipments": [s for s in shipments(world) if s["batch_id"] == batch_id]}


def shipments(world) -> list[dict]:
    out = []
    for j, ledger in world.ledgers.items():
        done = {b.payload.get("shipment_id"): b for b in ledger.find("TRANSFERRED")}
        aborted = {b.payload.get("shipment_id"): b for b in ledger.find("UNLOCK")}
        for lock in ledger.find("LOCK"):
            sid, waybill = lock.payload.get("shipment_id"), lock.payload.get("waybill") or {}
            end = done.get(sid) or aborted.get(sid)
            out.append({
                "shipment_id": sid, "batch_id": lock.payload.get("batch_id"), "origin": j,
                "dest": lock.payload.get("dest"), "weight_kg": waybill.get("weight_kg"),
                "packages": waybill.get("packages"), "locked_at": lock.timestamp,
                "status": "COMMITTED" if sid in done else "ABORTED" if sid in aborted else "LOCKED",
                "updated_at": end.timestamp if end else lock.timestamp,
            })
    return sorted(out, key=lambda s: str(s["shipment_id"]))


def documents(world) -> list[dict]:
    hidden = {"_id": 0, "twin": 0, "signature": 0, "qr_payload": 0, "path": 0}
    return sorted(world.store.get_collection("documents").find({}, hidden), key=lambda d: str(d.get("doc_id")))


def _bars(counter: Counter, order=None) -> list[dict]:
    keys = order or sorted(counter)
    top = max(counter.values(), default=0) or 1
    return [{"label": k, "value": counter.get(k, 0), "max": top} for k in keys]


def overview(conn, world, with_prices: bool) -> dict:
    """Counts for the overview page. Order values are only computed when `with_prices`
    (admin), so an employee's page never contains them."""
    rows, ships, docs = batches(conn, world), shipments(world), documents(world)
    status = Counter(r["status"] for r in rows)
    origins = sorted({r["origin"] for r in rows})
    data = {
        "batches": len(rows), "status": status,
        "status_bars": _bars(status, ["verified", "rejected", "pending"]),
        "by_origin": [{"origin": o, **Counter(r["status"] for r in rows if r["origin"] == o)} for o in origins],
        "shipments": len(ships), "shipment_bars": _bars(Counter(s["status"] for s in ships), list(SHIPMENT_STATES)),
        "routes": _bars(Counter(f"{s['origin']} -> {s['dest']}" for s in ships if s["status"] == "COMMITTED")),
        "documents": len(docs),
        "document_bars": _bars(Counter(d.get("type") for d in docs)),
        "ledgers": {j: len(l.blocks) for j, l in world.ledgers.items()},
    }
    if with_prices:
        committed = {s["shipment_id"] for s in ships if s["status"] == "COMMITTED"}
        value = defaultdict(lambda: {"ordered": 0.0, "shipped": 0.0})
        for o in conn.execute("SELECT order_id, quantity, unit_price, currency FROM orders"):
            amount = o["quantity"] * o["unit_price"]
            value[o["currency"]]["ordered"] += amount
            if o["order_id"] in committed:
                value[o["currency"]]["shipped"] += amount
        data["order_value"] = {c: {k: round(v, 2) for k, v in d.items()} for c, d in sorted(value.items())}
    return data
