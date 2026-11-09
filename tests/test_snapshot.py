"""Step 6: offline copy of the Atlas data, taken once at the end of an online run."""
import json

import pytest

from gi_trace import doc_store, snapshot
from gi_trace.run_all import run_pipeline
from gi_trace.seed import open_world
from gi_trace.snapshot import SnapshotError, take_snapshot
from test_atlas_store import FakeAtlas


def online(monkeypatch, fake):
    monkeypatch.setattr(doc_store, "connect_atlas", lambda uri: fake)
    monkeypatch.setenv("GI_MODE", "atlas")
    monkeypatch.setenv("ATLAS_WRITE_URI", "mongodb://writer")
    monkeypatch.setenv("ATLAS_READ_URI", "mongodb://reader")


@pytest.fixture
def online_run(tmp_path, monkeypatch):
    fake = FakeAtlas()
    online(monkeypatch, fake)
    summary = run_pipeline(tmp_path / "data", tmp_path / "out", sample="core", log=lambda *_: None)
    return summary, fake, tmp_path / "data"


def counts(w):
    return ({j: [b.hash for b in l.blocks] for j, l in w.ledgers.items()},
            len(list(w.store.get_collection("documents").find())), len(list(w.coll.find())))


def test_online_run_leaves_an_offline_copy_that_opens_offline(online_run, monkeypatch):
    summary, fake, data = online_run
    assert summary["offline"] is False and summary["snapshot_at"]
    on = open_world(data, role="read")
    monkeypatch.setenv("GI_MODE", "offline")
    off = open_world(data)
    assert off.offline and off.snapshot_at == summary["snapshot_at"]
    assert counts(off) == counts(on)
    assert not (data / ".snapshot_tmp").exists() and not (data / ".ledgers_old").exists()


def test_opening_the_copy_writes_nothing(online_run, monkeypatch):
    _, _, data = online_run
    monkeypatch.setenv("GI_MODE", "offline")
    before = {f: f.stat().st_mtime_ns for f in data.rglob("*") if f.is_file()}
    open_world(data).conn.close()
    assert {f: f.stat().st_mtime_ns for f in data.rglob("*") if f.is_file()} == before


def test_snapshot_command_refreshes_the_copy(online_run, monkeypatch):
    summary, _, data = online_run
    (data / "ledgers" / "NP.json").write_text("stale")  # an old or broken local copy
    assert snapshot.main(["--data", str(data)]) == 0
    monkeypatch.setenv("GI_MODE", "offline")
    assert open_world(data).ledgers["NP"].verify_chain() == []


def test_a_truncated_atlas_ledger_is_not_copied(online_run, monkeypatch, capsys):
    _, fake, data = online_run
    last = fake.db.ledger_blocks.find_one({"jurisdiction": "NP"}, sort=[("index", -1)])
    fake.db.ledger_blocks.delete_one({"_id": last["_id"]})
    before = (data / "ledgers" / "NP.json").read_bytes()
    assert snapshot.main(["--data", str(data)]) == 1
    assert capsys.readouterr().out.startswith("ERROR")  # refused by the cross-checks or the checkpoint
    assert (data / "ledgers" / "NP.json").read_bytes() == before


def test_an_offline_world_is_not_snapshotted(tmp_path):
    run_pipeline(tmp_path / "data", tmp_path / "out", sample="core", log=lambda *_: None)
    w = open_world(tmp_path / "data")
    with pytest.raises(SnapshotError):
        take_snapshot(w)


def test_an_offline_run_after_an_online_one_drops_the_snapshot_time(online_run, monkeypatch, tmp_path):
    _, _, data = online_run
    monkeypatch.setenv("GI_MODE", "offline")
    summary = run_pipeline(data, tmp_path / "out", sample="core", log=lambda *_: None)
    assert "snapshot_at" not in summary
    assert "snapshot_at" not in json.loads((data / "world.json").read_text())
    assert open_world(data).snapshot_at is None
