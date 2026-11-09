"""Step 3: ledgers in a LedgerStore (files offline, Atlas online), signed checkpoints,
dedupe keys, recoverable handshake signatures, and an oracle that runs only once."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pymongo.errors import DuplicateKeyError

from gi_trace import doc_store
from gi_trace.handshake import BorderGateway, HandshakeError
from gi_trace.ledger import LedgerError
from gi_trace.oracle import run_oracle
from gi_trace.seed import WorldError, build_world, open_world
from test_atlas_store import FakeAtlas

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(params=["file", "atlas"])
def backend(request, monkeypatch):
    """None for the file backend, the FakeAtlas for the Atlas backend."""
    if request.param == "file":
        return None
    fake = FakeAtlas()
    monkeypatch.setattr(doc_store, "connect_atlas", lambda uri: fake)
    monkeypatch.setenv("GI_MODE", "atlas")
    monkeypatch.setenv("ATLAS_WRITE_URI", "mongodb://writer")
    return fake


def oracle(w, batch_id, j, qr_dir):
    return run_oracle(w.conn, w.coll, w.ledgers[j], w.directory, w.nodes[f"GI-REG-{j}"],
                      batch_id, w.lab_files.get(batch_id), qr_dir)


def gateway(w):
    return BorderGateway(w.ledgers["NP"], w.ledgers["IN"], w.nodes["CUSTOMS-NP"], w.nodes["CUSTOMS-IN"],
                         w.directory, pending=w.ledger_store)


def remove_last_np_block(data, fake):
    if fake is None:
        path = data / "ledgers" / "NP.json"
        doc = json.loads(path.read_text())
        doc["blocks"].pop()
        path.write_text(json.dumps(doc))
    else:
        last = fake.db.ledger_blocks.find_one({"jurisdiction": "NP"}, sort=[("index", -1)])
        fake.db.ledger_blocks.delete_one({"_id": last["_id"]})


def locked_world(data, tmp_path):
    """HEMP-NP-0001 verified, shipment S1 locked but not committed: the LOCK is the last NP block."""
    w = build_world(data)
    assert oracle(w, "HEMP-NP-0001", "NP", tmp_path).passed
    gateway(w).initiate("S1", "HEMP-NP-0001", {"weight_kg": 120.0, "packages": 4})
    w.conn.close()


# ------------------------------------------------------------------ signed checkpoint

def test_removing_the_last_block_is_refused(tmp_path, backend):
    data = tmp_path / "data"
    locked_world(data, tmp_path)
    open_world(data).conn.close()  # a clean world opens
    remove_last_np_block(data, backend)
    with pytest.raises(LedgerError, match="NP ledger ends at block 1 but its signed checkpoint says block 2"):
        open_world(data)


def test_removing_a_trailing_redaction_record_is_refused(tmp_path, backend):
    """The case the cross-checks miss: the redacted block itself still verifies."""
    data = tmp_path / "data"
    w = build_world(data)
    w.ledgers["NP"].append("NOTE", {"weight_kg": 120.0}, w.nodes["CUSTOMS-NP"])
    three = dict(list(w.shares["NP"].items())[:3])
    w.ledgers["NP"].redact(0, {"weight_kg": 12.0}, three, "typo", w.nodes["CUSTOMS-NP"])
    w.conn.close()
    assert open_world(data).ledgers["NP"].blocks[0].payload == {"weight_kg": 12.0}
    remove_last_np_block(data, backend)
    with pytest.raises(LedgerError, match="checkpoint"):
        open_world(data)


def test_a_rewritten_checkpoint_needs_the_local_key(tmp_path, backend):
    data = tmp_path / "data"
    locked_world(data, tmp_path)
    remove_last_np_block(data, backend)
    w = None
    if backend is None:
        path = data / "ledgers" / "NP.head.json"
        head = json.loads(path.read_text())
        doc = json.loads((data / "ledgers" / "NP.json").read_text())
        head.update(index=1, hash=_hash_of_last(doc))
        path.write_text(json.dumps(head))
    else:
        doc = {"blocks": list(backend.db.ledger_blocks.find({"jurisdiction": "NP"}, {"_id": 0}).sort("index", 1))}
        backend.db.ledger_heads.update_one({"jurisdiction": "NP"}, {"$set": {"index": 1, "hash": _hash_of_last(doc)}})
    with pytest.raises(LedgerError, match="checkpoint signature is invalid"):
        open_world(data)


def _hash_of_last(doc):
    from gi_trace.ledger_io import block_from_json
    return block_from_json(doc["blocks"][-1]).hash


def test_checkpoint_key_stays_local(tmp_path, backend):
    data = tmp_path / "data"
    build_world(data).conn.close()
    key = json.loads((data / "keys" / "checkpoint.json").read_text())["private_key"]
    if backend is not None:
        for name in backend.db.list_collection_names():
            assert key not in json.dumps(list(backend.db[name].find({}, {"_id": 0})), default=str)


# ------------------------------------------------------------------ dedupe keys

def test_a_second_attestation_for_the_same_certificate_is_refused(tmp_path, backend):
    data = tmp_path / "data"
    w = build_world(data)
    assert oracle(w, "HEMP-NP-0001", "NP", tmp_path).passed
    ledger = w.ledgers["NP"]
    before = len(ledger.blocks)
    att = ledger.find("ATTESTATION", batch_id="HEMP-NP-0001")[0]
    with pytest.raises(LedgerError, match="already has ATTESTATION:HEMP-NP-0001:hemp_lab_assay"):
        ledger.append("ATTESTATION", dict(att.payload), w.nodes["GI-REG-NP"])
    assert len(ledger.blocks) == before
    w.conn.close()
    assert len(open_world(data).ledgers["NP"].blocks) == before


def test_atlas_blocks_carry_dedupe_keys_and_the_unique_index_enforces_them(tmp_path, monkeypatch):
    fake = FakeAtlas()
    monkeypatch.setattr(doc_store, "connect_atlas", lambda uri: fake)
    monkeypatch.setenv("GI_MODE", "atlas")
    monkeypatch.setenv("ATLAS_WRITE_URI", "mongodb://writer")
    w = build_world(tmp_path / "data")
    assert oracle(w, "HEMP-NP-0001", "NP", tmp_path).passed
    keys = [d["dedupe_key"] for d in fake.db.ledger_blocks.find({"jurisdiction": "NP"}).sort("index", 1)]
    assert keys == ["ATTESTATION:HEMP-NP-0001:hemp_lab_assay", "BATCH_ANCHOR:HEMP-NP-0001"]
    with pytest.raises(DuplicateKeyError):  # a writer bypassing this program is stopped by Atlas itself
        fake.db.ledger_blocks.insert_one({"jurisdiction": "NP", "index": 99, "dedupe_key": "BATCH_ANCHOR:HEMP-NP-0001"})


def test_reseeding_over_atlas_ledgers_is_refused_before_anything_changes(tmp_path, monkeypatch):
    fake = FakeAtlas()
    monkeypatch.setattr(doc_store, "connect_atlas", lambda uri: fake)
    monkeypatch.setenv("GI_MODE", "atlas")
    monkeypatch.setenv("ATLAS_WRITE_URI", "mongodb://writer")
    build_world(tmp_path / "data").conn.close()
    certs = fake.db.certificates.count_documents({})
    with pytest.raises(WorldError, match="Atlas already holds ledgers"):
        build_world(tmp_path / "data2")
    assert fake.db.certificates.count_documents({}) == certs
    assert not (tmp_path / "data2").exists()


# ------------------------------------------------------------------ oracle runs once

def test_running_the_oracle_twice_adds_no_blocks(tmp_path, backend):
    data = tmp_path / "data"
    w = build_world(data)
    first = {b: oracle(w, b, j, tmp_path) for b, j in [("HEMP-NP-0001", "NP"), ("CHAP-PK-0001", "PK")]}
    counts = {j: len(l.blocks) for j, l in w.ledgers.items()}
    again = {b: oracle(w, b, j, tmp_path) for b, j in [("HEMP-NP-0001", "NP"), ("CHAP-PK-0001", "PK")]}
    assert {j: len(l.blocks) for j, l in w.ledgers.items()} == counts
    assert all(again[b].passed and again[b].anchor_block == first[b].anchor_block for b in first)
    w.conn.close()
    w2 = open_world(data)  # and again after a restart
    assert oracle(w2, "HEMP-NP-0001", "NP", tmp_path).anchor_block == first["HEMP-NP-0001"].anchor_block
    assert {j: len(l.blocks) for j, l in w2.ledgers.items()} == counts


def test_oracle_interrupted_before_its_anchor_finishes_without_duplicates(tmp_path, monkeypatch):
    w = build_world(tmp_path / "data")
    ledger = w.ledgers["NP"]
    real_append = ledger.append

    def crash_on_anchor(tx_type, payload, signer):
        if tx_type == "BATCH_ANCHOR":
            raise RuntimeError("killed")
        return real_append(tx_type, payload, signer)

    monkeypatch.setattr(ledger, "append", crash_on_anchor)
    with pytest.raises(RuntimeError):
        oracle(w, "HEMP-NP-0001", "NP", tmp_path)
    monkeypatch.undo()
    assert oracle(w, "HEMP-NP-0001", "NP", tmp_path).passed
    assert [b.tx_type for b in ledger.blocks] == ["ATTESTATION", "BATCH_ANCHOR"]


# ------------------------------------------------------------------ interrupted handshake

KILL_SCRIPT = """
import os, sys
from gi_trace.handshake import BorderGateway
from gi_trace.oracle import run_oracle
from gi_trace.seed import build_world
data, qr = sys.argv[1], sys.argv[2]
w = build_world(data)
assert run_oracle(w.conn, w.coll, w.ledgers["NP"], w.directory, w.nodes["GI-REG-NP"],
                  "HEMP-NP-0001", w.lab_files["HEMP-NP-0001"], qr).passed
gw = BorderGateway(w.ledgers["NP"], w.ledgers["IN"], w.nodes["CUSTOMS-NP"], w.nodes["CUSTOMS-IN"],
                   w.directory, pending=w.ledger_store)
msg = gw.initiate("S1", "HEMP-NP-0001", {"weight_kg": 120.0, "packages": 4})
for role, name in gw.shipments["S1"].required.items():
    gw.sign("S1", role, w.nodes[name].sign(msg))
os._exit(9)  # killed after every party signed, before commit
"""


def test_killed_mid_handshake_restart_completes_without_resigning(tmp_path):
    data = tmp_path / "data"
    env = dict(os.environ, GI_MODE="offline")
    proc = subprocess.run([sys.executable, "-c", KILL_SCRIPT, str(data), str(tmp_path / "qr")],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 9, proc.stderr
    w = open_world(data)
    mint = gateway(w).commit("S1")  # no sign() calls in this process
    assert mint.payload["shipment_id"] == "S1"
    w.conn.close()
    w2 = open_world(data)
    assert w2.ledgers["NP"].find("TRANSFERRED", shipment_id="S1")
    assert all(l.verify_chain() == [] for l in w2.ledgers.values())


def _signed_then_restarted(data, tmp_path):
    w = build_world(data)
    assert oracle(w, "HEMP-NP-0001", "NP", tmp_path).passed
    gw = gateway(w)
    msg = gw.initiate("S1", "HEMP-NP-0001", {"weight_kg": 120.0, "packages": 4})
    for role, name in gw.shipments["S1"].required.items():
        gw.sign("S1", role, w.nodes[name].sign(msg))
    w.conn.close()
    return open_world(data)  # a new process would see only what was stored


def test_restart_completes_the_shipment_on_atlas(tmp_path, monkeypatch):
    fake = FakeAtlas()
    monkeypatch.setattr(doc_store, "connect_atlas", lambda uri: fake)
    monkeypatch.setenv("GI_MODE", "atlas")
    monkeypatch.setenv("ATLAS_WRITE_URI", "mongodb://writer")
    w = _signed_then_restarted(tmp_path / "data", tmp_path)
    assert fake.db.pending_signatures.count_documents({"shipment_id": "S1"}) == 4
    gateway(w).commit("S1")
    assert w.ledgers["IN"].find("MINT", shipment_id="S1")


def test_an_altered_pending_signature_is_not_used(tmp_path):
    data = tmp_path / "data"
    _signed_then_restarted(data, tmp_path).conn.close()
    path = data / "ledgers" / "pending_signatures.json"
    pending = json.loads(path.read_text())
    pending["S1"]["DEST_CUSTOMS"]["signature"] = pending["S1"]["ORIGIN_CUSTOMS"]["signature"]
    path.write_text(json.dumps(pending))
    with pytest.raises(HandshakeError, match="missing signatures: DEST_CUSTOMS"):
        gateway(open_world(data)).commit("S1")


def test_a_crash_between_block_and_checkpoint_still_opens(tmp_path, backend):
    """The block was stored but its checkpoint was not: the world opens and the next append catches up."""
    data = tmp_path / "data"
    w = build_world(data)
    assert oracle(w, "HEMP-NP-0001", "NP", tmp_path).passed
    if backend is None:
        head_path = data / "ledgers" / "NP.head.json"
        old_head = head_path.read_text()
    else:
        old_head = backend.db.ledger_heads.find_one({"jurisdiction": "NP"}, {"_id": 0})
    gateway(w).initiate("S1", "HEMP-NP-0001", {"weight_kg": 120.0, "packages": 4})
    w.conn.close()
    if backend is None:
        head_path.write_text(old_head)
    else:
        backend.db.ledger_heads.update_one({"jurisdiction": "NP"}, {"$set": old_head})
    w2 = open_world(data)
    assert w2.ledgers["NP"].blocks[-1].tx_type == "LOCK"
    w2.ledgers["NP"].append("NOTE", {}, w2.nodes["CUSTOMS-NP"])
    w2.conn.close()
    remove_last_np_block(data, backend)  # the checkpoint caught up, so truncation is detected again
    with pytest.raises(LedgerError, match="checkpoint"):
        open_world(data)
