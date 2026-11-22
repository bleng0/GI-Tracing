import pytest

from gi_trace.oracle import run_oracle
from gi_trace.seed import build_world


@pytest.fixture(autouse=True)
def no_real_mongodb(monkeypatch):
    """Tests always use the local document store, never Atlas, and never read .env
    (the command-line main() functions load it)."""
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    # Every CLI main() refuses to run outside the image; tests exercise them in
    # process (and in child processes that inherit this environment).
    monkeypatch.setenv("GI_DOCKER", "1")
    monkeypatch.setenv("GI_MODE", "offline")
    for var in ("ATLAS_WRITE_URI", "ATLAS_READ_URI"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def world():
    return build_world()


@pytest.fixture
def verified_world(world, tmp_path):
    """World where HEMP-NP-0001 and CHAP-PK-0001 have passed the oracle."""
    for batch_id, j in [("HEMP-NP-0001", "NP"), ("CHAP-PK-0001", "PK")]:
        r = run_oracle(world.conn, world.coll, world.ledgers[j], world.directory,
                       world.nodes[f"GI-REG-{j}"], batch_id, world.lab_files.get(batch_id), tmp_path)
        assert r.passed, r.reasons
    world.qr_dir = tmp_path
    return world
