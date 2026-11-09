"""Step 2: Atlas as the document store, with the 30-second offline switch.
"Atlas" here is mongomock wrapped to behave like the gi_writer role."""
import io

import bson
import mongomock
import pymongo
import pytest
from pymongo.errors import ConfigurationError, OperationFailure, ServerSelectionTimeoutError

from gi_trace import check_atlas, doc_store
from gi_trace.atlas_setup import build_schema
from gi_trace.doc_store import AtlasUnavailable, is_offline, open_store
from gi_trace.run_all import print_table, run_pipeline
from gi_trace.seed import open_world
from gi_trace.storage import FileDocStore

DELETABLE = {"certificates", "documents"}


class WriterColl:
    """A collection as gi_writer sees it: no index creation, deletes only where allowed."""

    def __init__(self, coll):
        self._coll = coll

    def __getattr__(self, name):
        return getattr(self._coll, name)

    def create_index(self, *a, **k):  # the refusal exactly as real Atlas sends it
        raise OperationFailure(f"user is not allowed to do action [createIndex] on [gi_trace.{self._coll.name}]",
                               code=8000, details={"codeName": "AtlasError"})

    def delete_many(self, *a, **k):
        if self._coll.name not in DELETABLE:
            raise OperationFailure(f"not authorized to remove from {self._coll.name}", code=13)
        return self._coll.delete_many(*a, **k)


class FakeAtlas:
    """The gi_trace database after atlas_setup: same collections and (unique) indexes."""

    def __init__(self):
        client = mongomock.MongoClient()
        build_schema(client)
        self.db = client.gi_trace

    def get_collection(self, name):
        return WriterColl(self.db[name])

    __getitem__ = get_collection

    def list_collection_names(self):
        return self.db.list_collection_names()


@pytest.fixture
def atlas(monkeypatch):
    fake, calls = FakeAtlas(), []

    def connect(uri):
        calls.append(uri)
        return fake

    monkeypatch.setattr(doc_store, "connect_atlas", connect)
    monkeypatch.setenv("ATLAS_WRITE_URI", "mongodb://writer")
    monkeypatch.setenv("ATLAS_READ_URI", "mongodb://reader")
    fake.calls = calls
    return fake


def failing(exc):
    def connect(uri):
        raise exc
    return connect


# ------------------------------------------------------------------ choosing the store

def test_offline_mode_never_connects(atlas, monkeypatch):
    monkeypatch.setenv("GI_MODE", "offline")
    assert is_offline(open_store(None, log=lambda *_: None))
    assert atlas.calls == []


@pytest.mark.parametrize("mode", ["auto", "atlas"])
def test_online_uses_the_write_uri_by_default_and_the_read_uri_for_the_dashboard(atlas, monkeypatch, mode):
    monkeypatch.setenv("GI_MODE", mode)
    assert open_store(None) is atlas and not is_offline(atlas)
    open_store(None, role="read")
    assert atlas.calls == ["mongodb://writer", "mongodb://reader"]


@pytest.mark.parametrize("exc", [ServerSelectionTimeoutError("timed out"),
                                 ConfigurationError("The DNS query name does not exist")])
def test_auto_falls_back_to_the_file_store_when_atlas_is_unreachable(monkeypatch, tmp_path, exc):
    monkeypatch.setenv("GI_MODE", "auto")
    monkeypatch.setenv("ATLAS_WRITE_URI", "mongodb+srv://nowhere")
    monkeypatch.setattr(doc_store, "connect_atlas", failing(exc))
    messages = []
    store = open_store(tmp_path / "documents.json", log=messages.append)
    assert isinstance(store, FileDocStore) and is_offline(store)
    assert messages and messages[0].startswith("OFFLINE MODE")


def test_auto_without_a_uri_is_offline(monkeypatch):
    monkeypatch.setenv("GI_MODE", "auto")
    messages = []
    assert is_offline(open_store(None, log=messages.append))
    assert "ATLAS_WRITE_URI is not set" in messages[0]


def test_a_rejected_login_is_an_error_not_offline_mode(monkeypatch):
    monkeypatch.setenv("GI_MODE", "auto")
    monkeypatch.setenv("ATLAS_WRITE_URI", "mongodb+srv://x")
    monkeypatch.setattr(doc_store, "connect_atlas", failing(OperationFailure("bad auth", code=8000)))
    with pytest.raises(OperationFailure):
        open_store(None)


def test_atlas_mode_never_falls_back(monkeypatch):
    monkeypatch.setenv("GI_MODE", "atlas")
    with pytest.raises(AtlasUnavailable):
        open_store(None)
    monkeypatch.setenv("ATLAS_WRITE_URI", "mongodb+srv://x")
    monkeypatch.setattr(doc_store, "connect_atlas", failing(ServerSelectionTimeoutError("timed out")))
    with pytest.raises(ServerSelectionTimeoutError):
        open_store(None)


def test_unknown_mode_is_refused(monkeypatch):
    monkeypatch.setenv("GI_MODE", "cloud")
    with pytest.raises(ValueError):
        open_store(None)


def test_connect_waits_30_seconds(monkeypatch):
    seen = {}

    class Client:
        def __init__(self, uri, **kwargs):
            seen.update(kwargs)
            self.admin, self.gi_trace = self, "db"

        def command(self, name):
            seen["command"] = name

    monkeypatch.setattr(pymongo, "MongoClient", Client)
    assert doc_store.connect_atlas("mongodb://x") == "db"
    assert seen == {"serverSelectionTimeoutMS": 30000, "command": "ping"}


def test_real_unreachable_server_falls_back(monkeypatch):
    """Real pymongo against a closed port, with the 30 s wait shortened to 0.5 s."""
    monkeypatch.setenv("GI_MODE", "auto")
    monkeypatch.setenv("ATLAS_WRITE_URI", "mongodb://127.0.0.1:1/?directConnection=true")
    monkeypatch.setattr(doc_store, "ATLAS_TIMEOUT_MS", 500)
    assert is_offline(open_store(None, log=lambda *_: None))


# ------------------------------------------------------------------ pipeline on Atlas

@pytest.fixture(scope="module")
def atlas_run(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    fake = FakeAtlas()
    mp.setattr(doc_store, "connect_atlas", lambda uri: fake)
    mp.setenv("GI_MODE", "atlas")
    mp.setenv("ATLAS_WRITE_URI", "mongodb://writer")
    # data a re-seed must leave alone
    for name in ("audit_log", "pending_signatures"):
        fake.db[name].insert_one({"keep": name})
    root = tmp_path_factory.mktemp("atlas_run")
    summary = run_pipeline(root / "data", root / "out", log=lambda *_: None)
    yield summary, fake, root, mp
    mp.undo()


def test_pipeline_fills_atlas_as_before(atlas_run):
    summary, fake, root, _ = atlas_run
    assert summary["offline"] is False and summary["all_as_expected"]
    assert (summary["passed"], summary["failed"]) == (21, 9)
    assert fake.db.certificates.count_documents({}) > 0
    assert fake.db.documents.count_documents({}) == summary["pdf_count"]
    assert not (root / "data" / "documents.json").exists()
    out = io.StringIO()
    print_table(summary, out)
    assert "document store: MongoDB Atlas" in out.getvalue()


def test_seeding_clears_only_certificates_and_documents(atlas_run):
    _, fake, _, _ = atlas_run
    for name in ("audit_log", "pending_signatures"):
        assert fake.db[name].find_one({"keep": name}) is not None


def test_everything_stored_is_valid_bson(atlas_run):
    """mongomock is lenient; a real server rejects what BSON cannot encode (e.g. ints over 64 bits)."""
    _, fake, _, _ = atlas_run
    for name in ("certificates", "documents", "ledger_blocks", "ledger_heads", "pending_signatures"):
        for doc in fake.db[name].find():
            bson.encode(doc)


def test_world_reopens_from_atlas_without_a_local_documents_file(atlas_run, monkeypatch):
    _, _, root, _ = atlas_run
    monkeypatch.setenv("GI_MODE", "atlas")  # the autouse conftest fixture set offline
    monkeypatch.setenv("ATLAS_WRITE_URI", "mongodb://writer")
    w = open_world(root / "data")
    assert w.offline is False
    w.conn.close()


def test_offline_pipeline_reports_offline(tmp_path):
    summary = run_pipeline(tmp_path / "data", tmp_path / "out", sample="core", log=lambda *_: None)
    assert summary["offline"] is True and summary["all_as_expected"]
    out = io.StringIO()
    print_table(summary, out)
    assert "OFFLINE MODE" in out.getvalue()


# ------------------------------------------------------------------ check_atlas

def test_check_atlas_writes_reads_and_cleans_up():
    fake = FakeAtlas()
    log = []
    code = check_atlas.run({"ATLAS_WRITE_URI": "w", "ATLAS_READ_URI": "r"}, connect=lambda uri: fake, log=log.append)
    assert code == 0, log
    assert fake.db.certificates.count_documents({}) == 0
    assert log[-1].startswith("OK")


def test_check_atlas_reports_missing_uri_and_connection_failure():
    log = []
    assert check_atlas.run({"ATLAS_WRITE_URI": "w"}, log=log.append) == 1
    assert "ATLAS_READ_URI is not set" in log[-1]
    assert check_atlas.run({"ATLAS_WRITE_URI": "w", "ATLAS_READ_URI": "r"},
                           connect=failing(ServerSelectionTimeoutError("timed out")), log=log.append) == 1
    assert log[-1].startswith("ERROR: ServerSelectionTimeoutError")


@pytest.mark.parametrize("error,ignored", [
    (OperationFailure("not authorized", code=13), True),
    (OperationFailure("user is not allowed to do action [createIndex] on [gi_trace.x]", code=8000), True),
    (OperationFailure("user is not allowed to do action [insert] on [gi_trace.x]", code=8000), False),
    (OperationFailure("something else", code=2), False),
])
def test_ensure_index_ignores_only_an_index_refusal(error, ignored):
    from gi_trace.storage import ensure_index

    class Coll:
        def create_index(self, *a, **k):
            raise error

    if ignored:
        assert ensure_index(Coll(), "batch_id") is None
    else:
        with pytest.raises(OperationFailure):
            ensure_index(Coll(), "batch_id")
