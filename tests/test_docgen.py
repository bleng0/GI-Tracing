"""Phase 3: PDFs with JSON twins, watermark, hash, signature, QR; verify.py detects tampering."""
import io
import json

import pytest
from pypdf import PdfReader

from gi_trace.crypto import canonical, sha256_hex
from gi_trace.docgen.certificates import (
    META_DOC_ID, META_TWIN_SHA256, WATERMARK_TEXT, certificate_pdf, documents_collection,
    export_clearance_pdf, membership_letter_pdf, redaction_notice_pdf, waybill_pdf,
)
from gi_trace.docgen.lab_files import read_lab_file, write_lab_files
from gi_trace.docgen.verify import TAMPERED, UNKNOWN, VALID, main, verify_pdf
from gi_trace.handshake import BorderGateway
from gi_trace.oracle import run_oracle
from gi_trace.seed import build_world, open_world


def _issue_all(w, out):
    """Core sample: certificates for every batch, letters, one export NP->IN with a redacted waybill."""
    write_lab_files(w.lab_files, out / "labfiles")
    for batch_id, j in [("HEMP-NP-0001", "NP"), ("HEMP-NP-0002", "NP"), ("CHAP-PK-0001", "PK")]:
        run_oracle(w.conn, w.coll, w.ledgers[j], w.directory, w.nodes[f"GI-REG-{j}"], batch_id,
                   read_lab_file(out / "labfiles", batch_id), out / "qr")
    certs = out / "certificates"
    recs = [certificate_pdf(w, certs, c) for c in w.coll.find()]
    recs += [membership_letter_pdf(w, certs, c) for c in ("COOP-NP-HEMP", "COOP-PK-CHAPPAL")]
    gw = BorderGateway(w.ledgers["NP"], w.ledgers["IN"], w.nodes["CUSTOMS-NP"], w.nodes["CUSTOMS-IN"], w.directory)
    gw.initiate("ORD-0001", "HEMP-NP-0001", {"weight_kg": 120.0, "packages": 4})
    s = gw.shipments["ORD-0001"]
    for role, name in s.required.items():
        gw.sign("ORD-0001", role, w.nodes[name].sign(s.message))
    gw.commit("ORD-0001")
    lock = w.ledgers["NP"].find("LOCK", shipment_id="ORD-0001")[0]
    shares = {k: w.shares["NP"][k] for k in ("CUSTOMS-NP", "GI-REG-NP", "GI-REG-IN")}
    audit = w.ledgers["NP"].redact(lock.index, dict(lock.payload, waybill={"weight_kg": 12.0, "packages": 4}),
                                   shares, "clerical weight error", w.nodes["CUSTOMS-NP"])
    recs += [export_clearance_pdf(w, certs, "NP", "ORD-0001"), waybill_pdf(w, certs, "NP", "ORD-0001"),
             redaction_notice_pdf(w, certs, "NP", audit)]
    return recs


@pytest.fixture
def issued(tmp_path):
    w = build_world(tmp_path / "data")
    return w, _issue_all(w, tmp_path / "out"), tmp_path


def _text(path):
    return [p.extract_text() for p in PdfReader(path).pages]


def test_every_pdf_carries_watermark_hash_signature_and_qr(issued):
    w, recs, _ = issued
    assert {r["type"] for r in recs} == {"hemp_lab_assay", "tanning_attestation", "membership_letter",
                                        "export_clearance", "customs_waybill", "redaction_notice"}
    for r in recs:
        pages = _text(r["path"])
        assert all(WATERMARK_TEXT in t for t in pages), r["path"]
        body = "".join(pages)
        assert r["twin_sha256"][:32] in body and r["twin_sha256"][32:] in body
        assert r["signature"][:16] in body and r["issuer"] in body
        meta = PdfReader(r["path"]).metadata
        assert meta[META_DOC_ID] == r["doc_id"] and meta[META_TWIN_SHA256] == r["twin_sha256"]
        assert json.loads(r["qr_payload"])["twin_sha256"] == r["twin_sha256"]
        assert verify_pdf(r["path"], w).status == VALID, r["doc_id"]


def test_documents_are_registered(issued):
    w, recs, _ = issued
    docs = list(documents_collection(w).find())
    assert len(docs) == len(recs) == 3 + 2 + 3
    for d in docs:
        assert {"path", "type", "batch_id", "sha256", "issued_at"} <= set(d)
        assert d["twin_sha256"] == sha256_hex(canonical(d["twin"]))
    # and they survive a restart, still VALID
    w.conn.close()
    w2 = open_world(w.data_dir)
    assert all(verify_pdf(r["path"], w2).status == VALID for r in recs)


def test_passing_batch_has_four_to_six_pdfs(issued):
    w, recs, _ = issued
    def pdfs_for(batch_id, coop_id):
        return [r for r in recs if r["batch_id"] == batch_id or r["doc_id"] == f"membership_letter:{coop_id}"]
    # assay, membership letter, export clearance, waybill, redaction notice
    assert len(pdfs_for("HEMP-NP-0001", "COOP-NP-HEMP")) == 5


def test_waybill_shows_the_redacted_weight_and_clearance_lists_signers(issued):
    _, recs, _ = issued
    by_type = {r["type"]: r for r in recs}
    assert by_type["customs_waybill"]["twin"]["content"]["waybill"]["weight_kg"] == 12.0
    signers = by_type["export_clearance"]["twin"]["content"]["signers"]
    assert set(signers) == {"ARTISAN_COOP", "BOTANICAL_LAB", "ORIGIN_CUSTOMS", "DEST_CUSTOMS"}
    assert by_type["redaction_notice"]["twin"]["content"]["approvers"] == ["CUSTOMS-NP", "GI-REG-IN", "GI-REG-NP"]


@pytest.mark.parametrize("where", [0, 100, 0.5, -10, -1])
def test_one_changed_byte_in_a_pdf_is_tampered(issued, where):
    w, recs, _ = issued
    for r in recs:
        data = bytearray(open(r["path"], "rb").read())
        i = int(len(data) * where) if isinstance(where, float) else where
        data[i] ^= 0x01
        open(r["path"], "wb").write(bytes(data))
        res = verify_pdf(r["path"], w)
        assert res.status == TAMPERED and res.doc_id == r["doc_id"], (r["doc_id"], i)


def test_one_changed_byte_in_the_json_twin_is_tampered(issued):
    w, recs, tmp_path = issued
    docs_file = tmp_path / "data" / "documents.json"
    text = docs_file.read_text()
    target = recs[0]
    old = f'"twin_sha256": "{target["twin_sha256"]}"'
    assert old in text
    # change one character of the twin (the issued_at timestamp) and nothing else
    rec_start = text.index(f'"doc_id": "{target["doc_id"]}"')
    twin_start = text.index('"twin": {', rec_start)
    ts = text.index('"issued_at": "', twin_start) + len('"issued_at": "')
    text = text[:ts] + ("1" if text[ts] != "1" else "2") + text[ts + 1:]
    docs_file.write_text(text)
    w.conn.close()
    res = verify_pdf(target["path"], open_world(tmp_path / "data"))
    assert res.status == TAMPERED
    assert any("JSON twin" in reason for reason in res.reasons)


def test_edited_registration_field_is_tampered(issued):
    w, recs, _ = issued
    documents_collection(w).update_one({"doc_id": recs[0]["doc_id"]}, {"$set": {"batch_id": "OTHER"}})
    assert verify_pdf(recs[0]["path"], w).status == TAMPERED


def test_rehashed_twin_is_still_caught_by_signature_and_ledger(issued):
    w, recs, _ = issued
    docs = documents_collection(w)
    r = recs[0]
    twin = dict(r["twin"], issuer_jurisdiction="XX")
    docs.update_one({"doc_id": r["doc_id"]}, {"$set": {"twin": twin, "twin_sha256": sha256_hex(canonical(twin))}})
    res = verify_pdf(r["path"], w)
    assert res.status == TAMPERED
    assert any("signature" in x for x in res.reasons) and any("ledger" in x for x in res.reasons)


def test_unregistered_pdf_is_unknown(issued, tmp_path):
    w, recs, _ = issued
    stranger = tmp_path / "stranger.pdf"
    stranger.write_bytes(b"%PDF-1.4\n%nothing\n")
    assert verify_pdf(stranger, w).status == UNKNOWN
    assert verify_pdf(tmp_path / "missing.pdf", w).status == UNKNOWN


def test_verify_cli_exit_codes(issued, capsys):
    w, recs, tmp_path = issued
    w.conn.close()
    assert main([recs[0]["path"], "--data", str(tmp_path / "data")]) == 0
    p = recs[1]["path"]
    data = bytearray(open(p, "rb").read()); data[len(data) // 2] ^= 1; open(p, "wb").write(bytes(data))
    assert main([p, "--data", str(tmp_path / "data")]) == 1
    assert "TAMPERED" in capsys.readouterr().out
    folder = str(tmp_path / "out" / "certificates")
    assert main([folder, "--data", str(tmp_path / "data")]) == 1  # a folder: one PDF is tampered
    assert main([folder + "/*letter*.pdf", "--data", str(tmp_path / "data")]) == 0  # wildcard expanded here
    assert capsys.readouterr().out.count("VALID") == (len(recs) - 1) + 2  # folder minus the tampered PDF, plus 2 letters


def test_tampered_lab_csv_fails_the_oracle(tmp_path):
    w = build_world()
    write_lab_files(w.lab_files, tmp_path)
    p = tmp_path / "HEMP-NP-0001.csv"
    p.write_bytes(p.read_bytes().replace(b"0.08", b"0.07"))
    r = run_oracle(w.conn, w.coll, w.ledgers["NP"], w.directory, w.nodes["GI-REG-NP"], "HEMP-NP-0001",
                   read_lab_file(tmp_path, "HEMP-NP-0001"), tmp_path / "qr")
    assert "raw lab file does not match the assay certificate" in r.reasons


# --- review findings, Phases 3-4

def _rec(recs, doc_type, n=0):
    return [r for r in recs if r["type"] == doc_type][n]


def test_valid_pdf_copied_over_another_documents_name_is_tampered(issued):
    w, recs, _ = issued
    passing, failing = _rec(recs, "hemp_lab_assay", 0), _rec(recs, "hemp_lab_assay", 1)
    open(failing["path"], "wb").write(open(passing["path"], "rb").read())
    res = verify_pdf(failing["path"], w)
    assert res.status == TAMPERED and any("named as another document" in x for x in res.reasons)


def test_renamed_copy_under_a_new_name_is_still_valid(issued, tmp_path):
    w, recs, _ = issued
    copy = tmp_path / "downloaded.pdf"
    copy.write_bytes(open(recs[0]["path"], "rb").read())
    assert verify_pdf(copy, w).status == VALID


def test_deleted_registration_is_tampered_not_unknown(issued):
    w, recs, tmp_path = issued
    r = recs[0]
    w.store.get_collection("documents").delete_many({"doc_id": r["doc_id"]})
    res = verify_pdf(r["path"], w)
    assert res.status == TAMPERED and "missing from the document store" in res.reasons[0]
    w.conn.close()
    from gi_trace.seed import WorldError
    with pytest.raises(WorldError, match="not in the document store"):
        open_world(tmp_path / "data")
    assert main([r["path"], "--data", str(tmp_path / "data")]) == 1


@pytest.mark.parametrize("field,value", [("path", "/elsewhere/x.pdf"), ("file_name", "x.pdf"), ("qr_payload", "{}")])
def test_edited_path_file_name_or_qr_is_tampered(issued, field, value):
    w, recs, _ = issued
    w.store.get_collection("documents").update_one({"doc_id": recs[0]["doc_id"]}, {"$set": {field: value}})
    assert verify_pdf(recs[0]["path"], w).status == TAMPERED


def test_registered_path_pointing_at_another_file_is_tampered(issued):
    w, recs, _ = issued
    a, b = recs[0], recs[1]
    open(a["path"], "wb").write(open(b["path"], "rb").read())  # the dashboard would serve b as a
    res = verify_pdf(b["path"], w)  # b itself is untouched, but a's registered path no longer holds a
    assert verify_pdf(a["path"], w).status == TAMPERED
    assert res.status == VALID


def test_cli_exit_codes_rank_tampered_first_and_empty_is_unknown(issued, tmp_path, capsys):
    w, recs, root = issued
    w.conn.close()
    stranger = tmp_path / "mix" / "stranger.pdf"
    stranger.parent.mkdir()
    stranger.write_bytes(b"%PDF-1.4\n")
    tampered = tmp_path / "mix" / recs[0]["file_name"]
    data = bytearray(open(recs[0]["path"], "rb").read()); data[50] ^= 1
    tampered.write_bytes(bytes(data))
    assert main([str(tmp_path / "mix"), "--data", str(root / "data")]) == 1
    (tmp_path / "empty").mkdir()
    assert main([str(tmp_path / "empty"), "--data", str(root / "data")]) == 2


def test_verify_never_writes_the_store(issued):
    w, recs, root = issued
    w.conn.close()
    before = {f: f.stat().st_mtime_ns for f in (root / "data").rglob("*.json")}
    assert main([str(root / "out" / "certificates"), "--data", str(root / "data")]) == 0
    assert {f: f.stat().st_mtime_ns for f in (root / "data").rglob("*.json")} == before


def test_status_label_says_when_issued(issued):
    _, recs, _ = issued
    assert "Oracle status when issued" in _text(_rec(recs, "hemp_lab_assay")["path"])[0]
