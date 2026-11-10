"""Phase 4: one command runs the whole story and fills out/."""
import io
import time
from pathlib import Path

import pytest
from pypdf import PdfReader

from gi_trace.docgen.certificates import WATERMARK_TEXT
from gi_trace.docgen.verify import TAMPERED, VALID, verify_pdf
from gi_trace.run_all import main, print_table, run_pipeline
from gi_trace.seed import open_world


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    root = tmp_path_factory.mktemp("run")
    t = time.perf_counter()
    summary = run_pipeline(root / "data", root / "out", log=lambda *_: None)
    return summary, root, time.perf_counter() - t


def test_pipeline_ends_as_the_sample_expects(run):
    summary, _, seconds = run
    assert seconds < 60
    assert summary["all_as_expected"]
    assert (summary["passed"], summary["failed"]) == (21, 9)
    assert summary["shipments"] == 22
    assert all(v["problems"] == [] for v in summary["ledgers"].values())
    out = io.StringIO()
    print_table(summary, out)
    assert "21 passed, 9 failed, all as expected" in out.getvalue()


def test_out_is_filled(run):
    summary, root, _ = run
    out = root / "out"
    assert len(list((out / "labfiles").glob("*.csv"))) == 20  # every hemp and mixed batch
    assert len(list((out / "certificates").glob("*.pdf"))) == summary["pdf_count"]
    passing = [r["batch_id"] for r in summary["rows"] if r["result"] == "PASS"]
    assert {p.stem for p in (out / "qr").glob("*.png")} >= set(passing)
    assert {p.stem for p in (out / "qr").glob("*.svg")} >= set(passing)
    report = (out / "reports" / "summary.html").read_text(encoding="utf-8")
    assert "SAMPLE / DEMONSTRATION" in report and "HEMP-NP-0004" in report


def test_passing_batches_have_four_to_six_pdfs(run):
    summary, _, _ = run
    counts = {r["batch_id"]: r["pdfs"] for r in summary["rows"] if r["result"] == "PASS"}
    assert counts and all(4 <= n <= 6 for n in counts.values()), counts


def test_every_pdf_is_watermarked_and_valid(run):
    _, root, _ = run
    w = open_world(root / "data")  # the saved world is consistent after the run
    pdfs = sorted((root / "out" / "certificates").glob("*.pdf"))
    assert len(pdfs) == 80
    valid_chains = set()
    for p in pdfs:
        assert all(WATERMARK_TEXT in page.extract_text() for page in PdfReader(p).pages), p
        assert verify_pdf(p, w, valid_chains).status == VALID, p
    w.conn.close()


def test_redacted_waybill_and_notice(run):
    _, root, _ = run
    w = open_world(root / "data")
    from gi_trace.docgen.certificates import documents_collection
    docs = documents_collection(w)
    notice = docs.find_one({"type": "redaction_notice"})
    waybill = docs.find_one({"doc_id": "customs_waybill:ORD-0001"})
    lock = w.ledgers["NP"].find("LOCK", shipment_id="ORD-0001")[0]
    assert waybill["twin"]["content"]["waybill"] == lock.payload["waybill"]
    assert notice["twin"]["content"]["target_block"] == lock.hash
    w.conn.close()


def test_main_asks_before_replacing(tmp_path, monkeypatch, capsys):
    (tmp_path / "data").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert main([]) == 2
    assert "Nothing changed" in capsys.readouterr().out
    assert list((tmp_path / "data").iterdir()) == []


def test_main_runs_with_yes_and_keeps_other_out_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "mine.txt").write_text("keep me")
    assert main(["--yes", "--sample", "core"]) == 0
    assert main(["--yes", "--sample", "core"]) == 0  # second run replaces cleanly
    assert (tmp_path / "out" / "mine.txt").read_text() == "keep me"
    assert len(list((tmp_path / "out" / "certificates").glob("*.pdf"))) == 10


def test_interrupted_run_is_flagged(tmp_path, monkeypatch, capsys):
    import gi_trace.run_all as ra
    def boom(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(ra, "materialize_compliance_view", boom)
    with pytest.raises(KeyboardInterrupt):
        run_pipeline(tmp_path / "data", tmp_path / "out", sample="core", log=lambda *_: None)
    # the half-built world stays in data.new, flagged as running; data/ never received it
    assert not (tmp_path / "data" / "world.json").exists()
    assert open_world(tmp_path / "data.new").pipeline == "running"
    from gi_trace.docgen.verify import main as verify_main
    verify_main([str(tmp_path / "out" / "certificates"), "--data", str(tmp_path / "data.new")])
    assert "did not finish" in capsys.readouterr().out


def test_a_failed_run_leaves_the_previous_world_in_place(tmp_path, monkeypatch):
    import gi_trace.run_all as ra
    run_pipeline(tmp_path / "data", tmp_path / "out", sample="core", log=lambda *_: None)
    w = open_world(tmp_path / "data")
    before = [b.hash for b in w.ledgers["NP"].blocks]
    w.conn.close()  # Windows will not move a file that has an open handle
    (tmp_path / "data" / "users.db").write_bytes(b"accounts")  # never touched by a run

    def boom(*a, **k):
        raise RuntimeError("network dropped")
    monkeypatch.setattr(ra, "materialize_compliance_view", boom)
    with pytest.raises(RuntimeError):
        run_pipeline(tmp_path / "data", tmp_path / "out", sample="core", log=lambda *_: None)
    w = open_world(tmp_path / "data")
    assert w.pipeline == "complete" and [b.hash for b in w.ledgers["NP"].blocks] == before
    w.conn.close()
    monkeypatch.undo()
    run_pipeline(tmp_path / "data", tmp_path / "out", sample="core", log=lambda *_: None)
    assert not (tmp_path / "data.new").exists() and not (tmp_path / "data" / ".previous").exists()
    assert (tmp_path / "data" / "users.db").read_bytes() == b"accounts"


def test_switching_samples_leaves_no_stale_files(tmp_path):
    run_pipeline(tmp_path / "data", tmp_path / "out", sample="full", log=lambda *_: None)
    run_pipeline(tmp_path / "data", tmp_path / "out", sample="core", log=lambda *_: None)
    assert len(list((tmp_path / "data" / "labfiles").glob("*.csv"))) == 2
    assert len(list((tmp_path / "out" / "labfiles").glob("*.csv"))) == 2
    assert len(list((tmp_path / "out" / "certificates").glob("*.pdf"))) == 10
    assert open_world(tmp_path / "data").pipeline == "complete"


def test_duplicate_export_row_says_it_was_exported_once(run):
    summary, _, _ = run
    row = next(r for r in summary["rows"] if r["batch_id"] == "HEMP-NP-0004")
    assert row["reasons"].startswith("exported once (") and "second export refused" in row["reasons"]
