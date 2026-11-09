import os

from gi_trace.oracle import run_oracle


def _run(world, batch_id, j, tmp_path, lab_file="default"):
    if lab_file == "default":
        lab_file = world.lab_files.get(batch_id)
    return run_oracle(world.conn, world.coll, world.ledgers[j], world.directory,
                      world.nodes[f"GI-REG-{j}"], batch_id, lab_file, tmp_path)


def test_hemp_pass_notarises_and_writes_qr(world, tmp_path):
    r = _run(world, "HEMP-NP-0001", "NP", tmp_path)
    assert r.passed, r.reasons
    ledger = world.ledgers["NP"]
    assert [b.tx_type for b in ledger.blocks] == ["ATTESTATION", "BATCH_ANCHOR"]
    assert ledger.blocks[-1].hash == r.anchor_block
    assert os.path.getsize(r.qr_path) > 0
    assert world.coll.find_one({"batch_id": "HEMP-NP-0001"})["compliance_status"] == "verified"


def test_thc_over_limit_is_rejected_and_not_notarised(world, tmp_path):
    r = _run(world, "HEMP-NP-0002", "NP", tmp_path)
    assert not r.passed
    assert any("THC" in reason for reason in r.reasons)
    assert world.ledgers["NP"].blocks == []
    assert world.coll.find_one({"batch_id": "HEMP-NP-0002"})["compliance_status"] == "rejected"


def test_tampered_lab_file_is_rejected(world, tmp_path):
    r = _run(world, "HEMP-NP-0001", "NP", tmp_path, lab_file=b"sample_id,thc_pct\nHEMP-NP-0001,0.01\n")
    assert not r.passed
    assert "raw lab file does not match the assay certificate" in r.reasons


def test_edited_certificate_breaks_signature(world, tmp_path):
    world.coll.update_one({"batch_id": "HEMP-NP-0001"}, {"$set": {"assay.thc_pct": 0.01}})
    r = _run(world, "HEMP-NP-0001", "NP", tmp_path)
    assert "hemp_lab_assay signature is invalid" in r.reasons


def test_chrome_tanned_leather_is_rejected(world, tmp_path):
    world.coll.delete_many({"batch_id": "CHAP-PK-0001"})
    from gi_trace.doc_store import submit_certificate
    submit_certificate(world.coll, {
        "cert_type": "tanning_attestation", "batch_id": "CHAP-PK-0001",
        "artisan_coop_id": "COOP-PK-CHAPPAL", "artisan_id": "ART-PK-001",
        "tanning": {"method": "chrome"},
    }, world.nodes["TANNERY-REG-PK"])
    r = _run(world, "CHAP-PK-0001", "PK", tmp_path)
    assert "leather is not vegetable-tanned" in r.reasons


def test_certificate_from_wrong_role_is_rejected(world, tmp_path):
    world.coll.delete_many({"batch_id": "CHAP-PK-0001"})
    from gi_trace.doc_store import submit_certificate
    submit_certificate(world.coll, {
        "cert_type": "tanning_attestation", "batch_id": "CHAP-PK-0001",
        "artisan_coop_id": "COOP-PK-CHAPPAL", "artisan_id": "ART-PK-001",
        "tanning": {"method": "vegetable"},
    }, world.nodes["COOP-PK-CHAPPAL"])  # the coop vouching for itself
    r = _run(world, "CHAP-PK-0001", "PK", tmp_path)
    assert "tanning_attestation signer is not a registered TANNING_REGISTRY" in r.reasons


def test_wrong_origin_ledger_is_rejected(world, tmp_path):
    r = _run(world, "CHAP-PK-0001", "NP", tmp_path)
    assert not r.passed
    assert world.ledgers["NP"].blocks == []
