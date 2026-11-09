"""Regression tests for the Phase 0-2 review findings (tampering, restarts, concurrency)."""
import json

import pytest

from gi_trace.handshake import BorderGateway
from gi_trace.keystore import KeystoreError
from gi_trace.ledger import LedgerError
from gi_trace.oracle import run_oracle
from gi_trace.seed import WorldError, build_world, open_world
from gi_trace.storage import FileDocStore, StorageError


def _verified(w, tmp_path, batch_id="HEMP-NP-0001", j="NP"):
    r = run_oracle(w.conn, w.coll, w.ledgers[j], w.directory, w.nodes[f"GI-REG-{j}"],
                   batch_id, w.lab_files.get(batch_id), tmp_path / "qr")
    assert r.passed, r.reasons


def _gw(w, origin="NP", dest="IN"):
    return BorderGateway(w.ledgers[origin], w.ledgers[dest], w.nodes[f"CUSTOMS-{origin}"],
                         w.nodes[f"CUSTOMS-{dest}"], w.directory)


def _sign_all(w, gw, sid):
    s = gw.shipments[sid]
    for role, name in s.required.items():
        gw.sign(sid, role, w.nodes[name].sign(s.message))


def _exported_world(tmp_path):
    data = tmp_path / "data"
    w = build_world(data)
    _verified(w, tmp_path)
    gw = _gw(w)
    gw.initiate("S1", "HEMP-NP-0001", {"weight_kg": 120.0, "packages": 4})
    _sign_all(w, gw, "S1")
    gw.commit("S1")
    w.conn.close()
    return data


def _edit(path, fn):
    doc = json.loads(path.read_text())
    fn(doc)  # edits in place
    path.write_text(json.dumps(doc))


# F1: truncation / half-written state is caught by cross-checks

def test_truncated_destination_ledger_is_refused(tmp_path):
    data = _exported_world(tmp_path)
    _edit(data / "ledgers" / "IN.json", lambda d: d.update(blocks=[]))
    with pytest.raises(WorldError, match="its MINT is missing"):
        open_world(data)


def test_emptied_origin_ledger_with_verified_documents_is_refused(tmp_path):
    data = _exported_world(tmp_path)
    _edit(data / "ledgers" / "NP.json", lambda d: d.update(blocks=[]))
    with pytest.raises(WorldError, match="no BATCH_ANCHOR"):
        open_world(data)


def test_ledger_is_saved_without_an_explicit_save(tmp_path):
    data = tmp_path / "data"
    w = build_world(data)
    _verified(w, tmp_path)  # no save_world()
    w.conn.close()
    assert [b.tx_type for b in open_world(data).ledgers["NP"].blocks] == ["ATTESTATION", "BATCH_ANCHOR"]


# F2: rolling a redacted block back is caught

def test_rolled_back_redaction_is_refused(tmp_path):
    data = tmp_path / "data"
    w = build_world(data)
    w.ledgers["NP"].append("NOTE", {"weight_kg": 120.0}, w.nodes["CUSTOMS-NP"])
    original = json.loads((data / "ledgers" / "NP.json").read_text())["blocks"][0]
    three = dict(list(w.shares["NP"].items())[:3])
    w.ledgers["NP"].redact(0, {"weight_kg": 12.0}, three, "typo", w.nodes["CUSTOMS-NP"])
    _edit(data / "ledgers" / "NP.json", lambda d: d["blocks"].__setitem__(0, original))
    with pytest.raises(LedgerError, match="does not match its latest redaction record"):
        open_world(data)


def test_redaction_by_non_member_changes_nothing(world):
    ledger = world.ledgers["NP"]
    ledger.append("NOTE", {"weight_kg": 120.0}, world.nodes["CUSTOMS-NP"])
    three = dict(list(world.shares["NP"].items())[:3])
    with pytest.raises(LedgerError, match="not a member"):
        ledger.redact(0, {"weight_kg": 12.0}, three, "typo", world.nodes["CUSTOMS-IN"])
    assert ledger.blocks[0].payload == {"weight_kg": 120.0} and ledger.verify_chain() == []


# F3: edited keys and manifest are refused

@pytest.mark.parametrize("tamper", [
    lambda d: d["COOP-PK-CHAPPAL"].update(role="TANNING_REGISTRY"),
    lambda d: d["LAB-NP"].update(jurisdiction="IN"),
    lambda d: d.update({"ROGUE-LAB": dict(d["LAB-NP"])}),
    lambda d: d.pop("TANNERY-REG-PK"),
])
def test_edited_node_directory_is_refused(tmp_path, tamper):
    data = tmp_path / "data"
    build_world(data).conn.close()
    _edit(data / "keys" / "nodes.json", tamper)
    with pytest.raises(KeystoreError, match="do not match"):
        open_world(data)


@pytest.mark.parametrize("threshold", [1, 99])
def test_edited_redaction_threshold_is_refused(tmp_path, threshold):
    data = tmp_path / "data"
    build_world(data).conn.close()
    _edit(data / "keys" / "ledgers.json", lambda d: d["NP"].update(redaction_threshold=threshold))
    with pytest.raises(KeystoreError, match="threshold"):
        open_world(data)


@pytest.mark.parametrize("tamper", [
    lambda d: d["NP"].__setitem__(1, hex(int(d["NP"][1], 16) + 1)),
    lambda d: d["NP"].__setitem__(0, 9),
    lambda d: d.pop("NP"),
])
def test_altered_or_missing_share_is_refused(tmp_path, tamper):
    data = tmp_path / "data"
    build_world(data).conn.close()
    _edit(data / "keys" / "shares" / "GI-REG-IN.json", tamper)
    with pytest.raises(KeystoreError, match="shares are missing or altered"):
        open_world(data)


def test_manifest_naming_the_wrong_sample_is_refused(tmp_path):
    data = tmp_path / "data"
    build_world(data, sample="full").conn.close()
    _edit(data / "world.json", lambda d: d.update(sample="core"))
    with pytest.raises(KeystoreError):
        open_world(data)


# F4: re-seeding leaves collections it does not own

def test_reseed_keeps_other_collections(tmp_path):
    data = tmp_path / "data"
    w = build_world(data)
    w.store.get_collection("users").insert_one({"name": "admin"})
    w.conn.close()
    build_world(data).conn.close()
    assert open_world(data).store.get_collection("users").find_one({})["name"] == "admin"


# F5: a handshake interrupted by a restart can be finished or aborted

def test_handshake_can_be_committed_after_restart(tmp_path):
    data = tmp_path / "data"
    w = build_world(data)
    _verified(w, tmp_path)
    _gw(w).initiate("S1", "HEMP-NP-0001", {"weight_kg": 12.0, "packages": 4})
    w.conn.close()

    w2 = open_world(data)
    gw = _gw(w2)
    s = gw._locked("S1")
    _sign_all(w2, gw, "S1")
    gw.commit("S1")
    assert s.state == "COMMITTED"
    w2.conn.close()
    w3 = open_world(data)
    assert w3.ledgers["IN"].find("MINT", shipment_id="S1")


def test_handshake_can_be_aborted_after_restart(tmp_path):
    data = tmp_path / "data"
    w = build_world(data)
    _verified(w, tmp_path)
    _gw(w).initiate("S1", "HEMP-NP-0001", {"weight_kg": 12.0, "packages": 4})
    w2 = open_world(data)
    gw = _gw(w2)
    gw.abort("S1", "restarted")
    gw.initiate("S2", "HEMP-NP-0001", {"weight_kg": 12.0, "packages": 4})  # unlocked again
    with pytest.raises(Exception, match="already exists"):
        _gw(w2).initiate("S1", "HEMP-NP-0001", {})


# F6: one writer at a time; opening does not write

def test_second_writer_is_refused(tmp_path):
    path = tmp_path / "documents.json"
    s1, s2 = FileDocStore(path), FileDocStore(path)
    s1.get_collection("c").insert_one({"a": 1})
    with pytest.raises(StorageError, match="changed on disk"):
        s2.get_collection("c").insert_one({"b": 2})
    assert FileDocStore(path).get_collection("c").find_one({})["a"] == 1


def test_stale_ledger_writer_is_refused(tmp_path):
    data = tmp_path / "data"
    w1 = build_world(data)
    w2 = open_world(data)
    w2.ledgers["NP"].append("NOTE", {}, w2.nodes["CUSTOMS-NP"])
    with pytest.raises(LedgerError, match="changed on disk"):
        w1.ledgers["NP"].append("NOTE", {}, w1.nodes["CUSTOMS-NP"])


def test_opening_a_world_writes_nothing(tmp_path):
    data = _exported_world(tmp_path)
    before = {f: f.stat().st_mtime_ns for f in data.rglob("*.json")}
    open_world(data).conn.close()
    assert {f: f.stat().st_mtime_ns for f in data.rglob("*.json")} == before


# F7 / F8 / F9 / F11

@pytest.mark.parametrize("field,value", [("signature", 5), ("signature", None),
                                         ("ch_r", "-0x1"), ("ch_r", hex(2 ** 2100)), ("payload", "x")])
def test_wrong_types_in_ledger_file_raise_ledger_error(tmp_path, field, value):
    data = tmp_path / "data"
    w = build_world(data)
    w.ledgers["NP"].append("NOTE", {"a": 1}, w.nodes["CUSTOMS-NP"])
    _edit(data / "ledgers" / "NP.json", lambda d: d["blocks"][0].__setitem__(field, value))
    with pytest.raises(LedgerError):
        open_world(data)


@pytest.mark.parametrize("name,content,error", [
    ("keys/ledgers.json", '{"NP": {"ch_public_key": "0x1", "redaction_threshold": 3}}', KeystoreError),
    ("keys/nodes.json", "[]", KeystoreError),
    ("world.json", "{", WorldError),
])
def test_malformed_files_raise_clear_errors(tmp_path, name, content, error):
    data = tmp_path / "data"
    build_world(data).conn.close()
    (data / name).write_text(content)
    with pytest.raises(error):
        open_world(data)


def test_files_saved_with_a_bom_still_load(tmp_path):
    data = _exported_world(tmp_path)
    for f in [data / "documents.json", data / "ledgers" / "NP.json", data / "keys" / "nodes.json", data / "world.json"]:
        f.write_bytes(b"\xef\xbb\xbf" + f.read_bytes())
    assert open_world(data).ledgers["NP"].verify_chain() == []


def test_interrupted_seed_is_not_opened(tmp_path):
    data = tmp_path / "data"
    build_world(data).conn.close()
    _edit(data / "world.json", lambda d: d.update(complete=False))
    with pytest.raises(WorldError, match="not fully seeded"):
        open_world(data)


def test_index_options_survive_reload(tmp_path):
    path = tmp_path / "documents.json"
    FileDocStore(path).get_collection("c").create_index("k", sparse=True, unique=True)
    info = FileDocStore(path).get_collection("c")._coll.index_information()["k_1"]
    assert info.get("sparse") is True and info.get("unique") is True
