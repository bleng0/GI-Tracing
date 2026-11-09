import sqlite3

import pytest

from gi_trace.federated import shipment_dossier
from gi_trace.tiles import flatten, materialize_compliance_view


def test_batch_must_name_a_member_artisan(world):
    with pytest.raises(sqlite3.IntegrityError):
        world.conn.execute(
            "INSERT INTO batches VALUES ('FAKE-1', 'COOP-NP-HEMP', 'ART-PK-001', 'x', '0000', 1, '2026-01-01')"
        )


def test_flatten_dicts_and_skip_arrays():
    doc = {"_id": 1, "a": {"b": 2, "c": {"d": 3}}, "tags": [1, 2], "e": "x"}
    assert flatten(doc) == {"a.b": 2, "a.c.d": 3, "e": "x"}


def test_view_extracts_frequent_paths_only(world):
    hot = materialize_compliance_view(world.conn, world.coll, threshold=0.8)
    assert {"batch_id", "artisan_coop_id", "origin_gps.lat", "compliance_status"} <= set(hot)
    # in 2 of 3 / 1 of 3 certificates -> stay only in MongoDB
    assert "assay.thc_pct" not in hot
    assert "tanning.method" not in hot
    assert "spectral_peaks" not in hot
    cols = [r[1] for r in world.conn.execute("PRAGMA table_info(compliance_view)")]
    assert cols == ["cert_id"] + sorted(hot)
    assert world.conn.execute("SELECT COUNT(*) FROM compliance_view").fetchone()[0] == 3


def test_lower_threshold_pulls_in_more_paths(world):
    hot = materialize_compliance_view(world.conn, world.coll, threshold=0.6)
    assert "assay.thc_pct" in hot
    assert world.conn.execute(
        "SELECT \"assay.thc_pct\" FROM compliance_view WHERE batch_id = 'CHAP-PK-0001'"
    ).fetchone()[0] is None


def test_view_has_planner_statistics_and_uses_index(world):
    materialize_compliance_view(world.conn, world.coll)
    stats = world.conn.execute("SELECT COUNT(*) FROM sqlite_stat1 WHERE tbl = 'compliance_view'").fetchone()[0]
    assert stats > 0
    plan = world.conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM compliance_view WHERE batch_id = 'HEMP-NP-0001'"
    ).fetchone()["detail"]
    assert "USING INDEX" in plan


def test_dossier_joins_sql_and_mongo_on_batch_id(world):
    d = shipment_dossier(world.conn, world.coll, "CHAP-PK-0001")
    assert d["batch"]["origin"] == "PK"
    assert d["batch"]["materials"] == ["leather"]
    assert d["orders"][0]["tariff_rate_pct"] == 15.0
    assert d["certificates"][0]["tanning"]["method"] == "vegetable"
    assert shipment_dossier(world.conn, world.coll, "NOPE") is None
