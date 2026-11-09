"""Relational tier: artisans, cooperatives, batches, orders, tariffs, rules (SQLite)."""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS cooperatives (
    coop_id          TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    jurisdiction     TEXT NOT NULL CHECK (jurisdiction IN ('NP', 'IN', 'PK')),
    gi_registry_code TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artisans (
    artisan_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    coop_id    TEXT NOT NULL REFERENCES cooperatives (coop_id),
    joined_on  TEXT NOT NULL,
    UNIQUE (artisan_id, coop_id)
);

-- The composite FK means a batch can only name an artisan who is a member of
-- the cooperative issuing it: counterfeit "GI" batches fail at insert time.
CREATE TABLE IF NOT EXISTS batches (
    batch_id   TEXT PRIMARY KEY,
    coop_id    TEXT NOT NULL,
    artisan_id TEXT NOT NULL,
    product    TEXT NOT NULL,
    hs_code    TEXT NOT NULL,
    weight_kg  REAL NOT NULL CHECK (weight_kg > 0),
    created_on TEXT NOT NULL,
    FOREIGN KEY (artisan_id, coop_id) REFERENCES artisans (artisan_id, coop_id)
);

CREATE TABLE IF NOT EXISTS batch_materials (
    batch_id TEXT NOT NULL REFERENCES batches (batch_id),
    material TEXT NOT NULL CHECK (material IN ('hemp', 'leather')),
    PRIMARY KEY (batch_id, material)
);

CREATE TABLE IF NOT EXISTS orders (
    order_id          TEXT PRIMARY KEY,
    batch_id          TEXT NOT NULL REFERENCES batches (batch_id),
    buyer             TEXT NOT NULL,
    dest_jurisdiction TEXT NOT NULL CHECK (dest_jurisdiction IN ('NP', 'IN', 'PK')),
    quantity          INTEGER NOT NULL CHECK (quantity > 0),
    unit_price        REAL NOT NULL,
    currency          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tariff_schedule (
    hs_code  TEXT NOT NULL,
    origin   TEXT NOT NULL,
    dest     TEXT NOT NULL,
    rate_pct REAL NOT NULL,
    PRIMARY KEY (hs_code, origin, dest)
);

CREATE TABLE IF NOT EXISTS jurisdiction_rules (
    jurisdiction TEXT PRIMARY KEY,
    max_thc_pct  REAL NOT NULL
);
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


# Tables this module and tiles.py own, children before parents. Other tables
# in the same file (e.g. dashboard users, later) are left alone by reset().
OWNED_TABLES = ("compliance_view", "orders", "batch_materials", "batches", "artisans",
                "cooperatives", "tariff_schedule", "jurisdiction_rules")


def reset(conn: sqlite3.Connection) -> None:
    """Drops and recreates the prototype's tables (used before re-seeding a file database)."""
    for table in OWNED_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript(SCHEMA)
    conn.commit()


def get_batch(conn: sqlite3.Connection, batch_id: str) -> dict | None:
    row = conn.execute(
        """
        SELECT b.*, c.jurisdiction AS origin, c.gi_registry_code, c.name AS coop_name,
               a.name AS artisan_name
        FROM batches b
        JOIN cooperatives c ON c.coop_id = b.coop_id
        JOIN artisans a ON a.artisan_id = b.artisan_id
        WHERE b.batch_id = ?
        """,
        (batch_id,),
    ).fetchone()
    if row is None:
        return None
    batch = dict(row)
    batch["materials"] = sorted(
        r["material"]
        for r in conn.execute("SELECT material FROM batch_materials WHERE batch_id = ?", (batch_id,))
    )
    return batch


def max_thc_pct(conn: sqlite3.Connection, jurisdiction: str) -> float:
    row = conn.execute(
        "SELECT max_thc_pct FROM jurisdiction_rules WHERE jurisdiction = ?", (jurisdiction,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no THC rule for {jurisdiction}")
    return row["max_thc_pct"]
