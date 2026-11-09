"""Phase 2: the full sample is deterministic and fails for the known reasons."""
from collections import Counter

from gi_trace.doc_store import signed_part
from gi_trace.handshake import BorderGateway, HandshakeError
from gi_trace.oracle import run_oracle
from gi_trace.seed import EXPECTED_FAILURES, build_world, sample_data
from gi_trace.sql_store import get_batch

REASON_TEXT = {
    "thc_over_limit": "THC",
    "chrome_tanned": "leather is not vegetable-tanned",
    "tampered_lab_file": "raw lab file does not match the assay certificate",
    "wrong_signer_role": "signer is not a registered TANNING_REGISTRY",
}


def _dump(w):
    return list(w.conn.iterdump())


def _certs(w):
    return sorted((repr(sorted({k: v for k, v in signed_part(d).items() if k != "signer"}.items())), d["signer"])
                  for d in w.coll.find())


def test_full_sample_is_deterministic():
    assert sample_data("full") == sample_data("full")
    a, b = build_world(sample="full"), build_world(sample="full")
    assert _dump(a) == _dump(b)
    assert _certs(a) == _certs(b)
    assert a.lab_files == b.lab_files


def test_full_sample_shape():
    w = build_world(sample="full")
    rows = w.conn.execute("SELECT batch_id, product, created_on FROM batches").fetchall()
    assert len(rows) == 30
    origins = Counter(get_batch(w.conn, r["batch_id"])["origin"] for r in rows)
    assert set(origins) == {"NP", "IN", "PK"}
    assert {r["product"] for r in rows} == {"Hemp yarn", "Peshawari chappal", "Hemp-stitched chappal"}
    mixed = [r["batch_id"] for r in rows if len(get_batch(w.conn, r["batch_id"])["materials"]) == 2]
    assert len(mixed) == 2
    dates = sorted(r["created_on"] for r in rows)
    assert dates[0] < "2026-06-01" and dates[-1] >= "2026-10-01"  # about six months
    assert len({d[:7] for d in dates}) == 6
    assert Counter(EXPECTED_FAILURES.values()) == {
        "thc_over_limit": 4, "chrome_tanned": 2, "tampered_lab_file": 1,
        "wrong_signer_role": 1, "duplicate_export": 1,
    }
    assert w.expected == {r["batch_id"]: EXPECTED_FAILURES.get(r["batch_id"]) for r in rows}


def test_full_sample_passes_and_fails_for_the_known_reasons(tmp_path):
    w = build_world(sample="full")
    passed = []
    for batch_id, expected in sorted(w.expected.items()):
        j = get_batch(w.conn, batch_id)["origin"]
        r = run_oracle(w.conn, w.coll, w.ledgers[j], w.directory, w.nodes[f"GI-REG-{j}"],
                       batch_id, w.lab_files.get(batch_id), tmp_path)
        if expected in REASON_TEXT:
            assert not r.passed, batch_id
            assert any(REASON_TEXT[expected] in reason for reason in r.reasons), (batch_id, r.reasons)
            assert len(r.reasons) == 1, (batch_id, r.reasons)  # fails for that reason only
        else:
            assert r.passed, (batch_id, r.reasons)
            passed.append(batch_id)
    assert len(passed) == 22

    exported, refused = [], []
    for o in w.conn.execute("SELECT * FROM orders ORDER BY order_id"):
        if o["batch_id"] not in passed:
            continue
        origin = get_batch(w.conn, o["batch_id"])["origin"]
        gw = BorderGateway(w.ledgers[origin], w.ledgers[o["dest_jurisdiction"]], w.nodes[f"CUSTOMS-{origin}"],
                           w.nodes[f"CUSTOMS-{o['dest_jurisdiction']}"], w.directory)
        try:
            gw.initiate(o["order_id"], o["batch_id"], {"weight_kg": 1.0, "packages": 1})
        except HandshakeError as e:
            assert "already locked or exported" in str(e)
            refused.append(o["batch_id"])
            continue
        s = gw.shipments[o["order_id"]]
        for role, name in s.required.items():
            gw.sign(o["order_id"], role, w.nodes[name].sign(s.message))
        gw.commit(o["order_id"])
        exported.append(o["batch_id"])
    assert refused == ["HEMP-NP-0004"]
    assert len(set(exported)) == 22
    fully_passing = set(exported) - set(refused)
    assert len(fully_passing) == 21 and 30 - len(fully_passing) == 9
    assert all(l.verify_chain() == [] for l in w.ledgers.values())
