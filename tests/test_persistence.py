"""Phase 1: the document store, SQLite, keys and ledgers survive a restart."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from bson import ObjectId

from gi_trace.chameleon import combine_shares
from gi_trace.handshake import BorderGateway
from gi_trace.keystore import KeystoreError
from gi_trace.ledger import LedgerError
from gi_trace.oracle import run_oracle
from gi_trace.seed import build_world, open_world, save_world
from gi_trace.storage import FileDocStore, StorageError

ROOT = Path(__file__).resolve().parents[1]


def test_file_doc_store_round_trip(tmp_path):
    path = tmp_path / "documents.json"
    store = FileDocStore(path)
    coll = store.get_collection("certificates")
    coll.create_index("batch_id")
    oid = coll.insert_one({"batch_id": "B1", "assay": {"thc_pct": 0.1}}).inserted_id
    coll.insert_one({"batch_id": "B2"})
    coll.update_one({"batch_id": "B1"}, {"$set": {"compliance_status": "verified"}})
    coll.delete_many({"batch_id": "B2"})

    reopened = FileDocStore(path).get_collection("certificates")
    doc = reopened.find_one({"batch_id": "B1"})
    assert isinstance(doc["_id"], ObjectId) and doc["_id"] == oid
    assert doc["compliance_status"] == "verified" and doc["assay"]["thc_pct"] == 0.1
    assert list(reopened.find({"batch_id": "B2"})) == []
    assert "batch_id_1" in reopened._coll.index_information()


def test_corrupt_document_store_is_refused(tmp_path):
    path = tmp_path / "documents.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StorageError):
        FileDocStore(path)


def _sign_all(w, gw, sid):
    s = gw.shipments[sid]
    for role, name in s.required.items():
        gw.sign(sid, role, w.nodes[name].sign(s.message))


def _run_story(w, tmp_path):
    for batch_id, j in [("HEMP-NP-0001", "NP"), ("HEMP-NP-0002", "NP"), ("CHAP-PK-0001", "PK")]:
        run_oracle(w.conn, w.coll, w.ledgers[j], w.directory, w.nodes[f"GI-REG-{j}"],
                   batch_id, w.lab_files.get(batch_id), tmp_path / "qr")
    gw = BorderGateway(w.ledgers["NP"], w.ledgers["IN"], w.nodes["CUSTOMS-NP"], w.nodes["CUSTOMS-IN"], w.directory)
    gw.initiate("S1", "HEMP-NP-0001", {"weight_kg": 120.0, "packages": 4})
    _sign_all(w, gw, "S1")
    gw.commit("S1")
    lock = w.ledgers["NP"].find("LOCK", shipment_id="S1")[0]
    shares = {k: w.shares["NP"][k] for k in ("CUSTOMS-NP", "GI-REG-NP", "GI-REG-IN")}
    w.ledgers["NP"].redact(lock.index, dict(lock.payload, waybill={"weight_kg": 12.0, "packages": 4}),
                           shares, "typo", w.nodes["CUSTOMS-NP"])
    save_world(w)


def _summary(w):
    return {
        "blocks": {j: [b.hash for b in l.blocks] for j, l in w.ledgers.items()},
        "problems": {j: l.verify_chain() for j, l in w.ledgers.items()},
        "status": {d["batch_id"]: d["compliance_status"] for d in w.coll.find()},
        "batches": w.conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0],
        "lock_weight": w.ledgers["NP"].find("LOCK", shipment_id="S1")[0].payload["waybill"]["weight_kg"],
    }


def test_world_survives_a_new_python_process(tmp_path):
    data = tmp_path / "data"
    w = build_world(data)
    _run_story(w, tmp_path)
    before = _summary(w)
    w.conn.close()

    script = ("import json, sys; from gi_trace.seed import open_world; "
              "from tests.test_persistence import _summary; "
              "print(json.dumps(_summary(open_world(sys.argv[1]))))")
    out = subprocess.run([sys.executable, "-c", script, str(data)], cwd=ROOT,
                         capture_output=True, text=True, check=True)
    after = json.loads(out.stdout)
    assert after == json.loads(json.dumps(before))
    assert after["problems"] == {"NP": [], "IN": [], "PK": []}
    assert after["lock_weight"] == 12.0
    assert after["status"] == {"HEMP-NP-0001": "verified", "HEMP-NP-0002": "rejected", "CHAP-PK-0001": "verified"}


def test_reloaded_shares_can_still_redact(tmp_path):
    w = build_world(tmp_path / "data")
    w.ledgers["PK"].append("NOTE", {"weight_kg": 120.0}, w.nodes["CUSTOMS-PK"])
    save_world(w)
    w2 = open_world(tmp_path / "data")
    three = dict(list(w2.shares["PK"].items())[:3])
    w2.ledgers["PK"].redact(0, {"weight_kg": 12.0}, three, "typo", w2.nodes["GI-REG-PK"])
    save_world(w2)
    w3 = open_world(tmp_path / "data")
    assert w3.ledgers["PK"].blocks[0].payload == {"weight_kg": 12.0}
    assert w3.ledgers["PK"].verify_chain() == []


def test_trapdoor_is_never_written(tmp_path):
    w = build_world(tmp_path / "data")
    for j in ("NP", "IN", "PK"):
        x = combine_shares(list(w.shares[j].values())[:3])
        for f in (tmp_path / "data").rglob("*"):
            if f.is_file() and f.suffix == ".json":
                text = f.read_text(encoding="utf-8").lower()
                assert hex(x)[2:] not in text and str(x) not in text, f


def _tamper_np_ledger(data):
    w = build_world(data)
    w.ledgers["NP"].append("NOTE", {"weight_kg": 120.0}, w.nodes["CUSTOMS-NP"])
    w.ledgers["NP"].append("NOTE", {"next": True}, w.nodes["CUSTOMS-NP"])
    save_world(w)
    return data / "ledgers" / "NP.json"


def test_edited_ledger_payload_is_refused_on_load(tmp_path):
    path = _tamper_np_ledger(tmp_path / "data")
    doc = json.loads(path.read_text())
    doc["blocks"][0]["payload"]["weight_kg"] = 12.0
    path.write_text(json.dumps(doc))
    with pytest.raises(LedgerError, match="block 0: payload does not match"):
        open_world(tmp_path / "data")


def test_removed_middle_block_is_refused_on_load(tmp_path):
    path = _tamper_np_ledger(tmp_path / "data")
    doc = json.loads(path.read_text())
    del doc["blocks"][0]
    path.write_text(json.dumps(doc))
    with pytest.raises(LedgerError, match="broken link"):
        open_world(tmp_path / "data")


@pytest.mark.parametrize("content", ["", "{", '{"format": 1, "jurisdiction": "IN", "blocks": []}', "[]"])
def test_unreadable_or_wrong_ledger_file_is_refused(tmp_path, content):
    path = _tamper_np_ledger(tmp_path / "data")
    path.write_text(content)
    with pytest.raises(LedgerError):
        open_world(tmp_path / "data")


def test_swapped_node_key_breaks_ledger_signatures(tmp_path):
    data = tmp_path / "data"
    _tamper_np_ledger(data)
    keys = json.loads((data / "keys" / "nodes.json").read_text())
    keys["CUSTOMS-NP"]["private_key"] = keys["GI-REG-NP"]["private_key"]
    (data / "keys" / "nodes.json").write_text(json.dumps(keys))
    with pytest.raises(LedgerError, match="bad or unauthorised signature"):
        open_world(data)


def test_missing_key_file_is_refused(tmp_path):
    data = tmp_path / "data"
    build_world(data)
    (data / "keys" / "ledgers.json").write_text("nope")
    with pytest.raises(KeystoreError):
        open_world(data)


def test_open_world_without_a_saved_world_fails_clearly(tmp_path):
    with pytest.raises(FileNotFoundError, match="no saved world"):
        open_world(tmp_path / "empty")
    assert not (tmp_path / "empty" / "gi_trace.db").exists()


def test_reseeding_the_same_directory_replaces_the_world(tmp_path):
    data = tmp_path / "data"
    w = build_world(data, sample="full")
    _run_story(w, tmp_path)
    w.conn.close()
    w2 = build_world(data, sample="full")
    w2.conn.close()
    w3 = open_world(data)
    assert all(l.blocks == [] for l in w3.ledgers.values())
    assert w3.conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 30
    assert {d["compliance_status"] for d in w3.coll.find()} == {"pending"}
    assert w3.nodes["CUSTOMS-NP"].public_key == w2.nodes["CUSTOMS-NP"].public_key


def test_reseed_recovers_from_a_corrupt_store(tmp_path):
    data = tmp_path / "data"
    build_world(data).conn.close()
    (data / "documents.json").write_text("garbage")
    build_world(data).conn.close()
    assert len(list(open_world(data).coll.find())) == 3
