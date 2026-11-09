import hashlib

import pytest

from gi_trace.doc_store import submit_certificate
from gi_trace.handshake import BorderGateway, HandshakeError
from gi_trace.oracle import run_oracle

WAYBILL = {"weight_kg": 12.0, "packages": 4}


def gateway(w, origin, dest):
    return BorderGateway(w.ledgers[origin], w.ledgers[dest], w.nodes[f"CUSTOMS-{origin}"],
                         w.nodes[f"CUSTOMS-{dest}"], w.directory)


def sign_all(w, gw, sid, skip=()):
    s = gw.shipments[sid]
    for role, name in s.required.items():
        if role not in skip:
            gw.sign(sid, role, w.nodes[name].sign(s.message))


def test_hemp_needs_the_four_named_parties(verified_world):
    w = verified_world
    gw = gateway(w, "NP", "IN")
    gw.initiate("S1", "HEMP-NP-0001", WAYBILL)
    assert gw.shipments["S1"].required == {
        "ARTISAN_COOP": "COOP-NP-HEMP", "BOTANICAL_LAB": "LAB-NP",
        "ORIGIN_CUSTOMS": "CUSTOMS-NP", "DEST_CUSTOMS": "CUSTOMS-IN",
    }


def test_all_signatures_mint_on_destination_and_burn_on_origin(verified_world):
    w = verified_world
    gw = gateway(w, "NP", "IN")
    gw.initiate("S1", "HEMP-NP-0001", WAYBILL)
    sign_all(w, gw, "S1")
    mint = gw.commit("S1")
    assert w.ledgers["IN"].blocks[-1] is mint
    assert w.ledgers["NP"].find("TRANSFERRED", shipment_id="S1")[0].payload["dest_mint_block"] == mint.hash
    assert gw.shipments["S1"].state == "COMMITTED"
    assert w.ledgers["NP"].verify_chain() == [] and w.ledgers["IN"].verify_chain() == []


def test_missing_signature_blocks_commit(verified_world):
    w = verified_world
    gw = gateway(w, "NP", "IN")
    gw.initiate("S1", "HEMP-NP-0001", WAYBILL)
    sign_all(w, gw, "S1", skip={"BOTANICAL_LAB"})
    with pytest.raises(HandshakeError, match="BOTANICAL_LAB"):
        gw.commit("S1")
    assert w.ledgers["IN"].blocks == []


def test_signature_from_wrong_key_is_rejected(verified_world):
    w = verified_world
    gw = gateway(w, "NP", "IN")
    msg = gw.initiate("S1", "HEMP-NP-0001", WAYBILL)
    with pytest.raises(HandshakeError, match="invalid BOTANICAL_LAB"):
        gw.sign("S1", "BOTANICAL_LAB", w.nodes["COOP-NP-HEMP"].sign(msg))


def test_unverified_batch_cannot_be_locked(world):
    with pytest.raises(HandshakeError, match="no verified attestations"):
        gateway(world, "NP", "IN").initiate("S1", "HEMP-NP-0002", WAYBILL)


def test_batch_cannot_be_locked_twice_until_aborted(verified_world):
    w = verified_world
    gw = gateway(w, "NP", "IN")
    gw.initiate("S1", "HEMP-NP-0001", WAYBILL)
    with pytest.raises(HandshakeError, match="already locked"):
        gw.initiate("S2", "HEMP-NP-0001", WAYBILL)
    gw.abort("S1", "buyer cancelled")
    gw.initiate("S2", "HEMP-NP-0001", WAYBILL)  # unlocked again
    with pytest.raises(HandshakeError, match="ABORTED"):
        gw.commit("S1")


def test_exported_batch_cannot_be_exported_again(verified_world):
    w = verified_world
    gw = gateway(w, "NP", "IN")
    gw.initiate("S1", "HEMP-NP-0001", WAYBILL)
    sign_all(w, gw, "S1")
    gw.commit("S1")
    with pytest.raises(HandshakeError, match="already locked or exported"):
        gateway(w, "NP", "PK").initiate("S2", "HEMP-NP-0001", WAYBILL)


def test_mixed_hemp_leather_batch_needs_five_signers(world, tmp_path):
    w = world
    w.conn.execute("INSERT INTO batches VALUES ('MIX-NP-1', 'COOP-NP-HEMP', 'ART-NP-001', "
                   "'Hemp-stitched chappal', '6403.99', 2.0, '2026-10-10')")
    w.conn.executemany("INSERT INTO batch_materials VALUES (?, ?)", [("MIX-NP-1", "hemp"), ("MIX-NP-1", "leather")])
    lab_file = b"sample_id,thc_pct\nMIX-NP-1,0.05\n"
    submit_certificate(w.coll, {
        "cert_type": "hemp_lab_assay", "batch_id": "MIX-NP-1", "artisan_coop_id": "COOP-NP-HEMP",
        "lab_file_sha256": hashlib.sha256(lab_file).hexdigest(), "assay": {"thc_pct": 0.05},
    }, w.nodes["LAB-NP"])
    submit_certificate(w.coll, {
        "cert_type": "tanning_attestation", "batch_id": "MIX-NP-1", "artisan_coop_id": "COOP-NP-HEMP",
        "artisan_id": "ART-NP-001", "tanning": {"method": "vegetable"},
    }, w.nodes["TANNERY-REG-PK"])
    r = run_oracle(w.conn, w.coll, w.ledgers["NP"], w.directory, w.nodes["GI-REG-NP"], "MIX-NP-1", lab_file, tmp_path)
    assert r.passed, r.reasons

    gw = gateway(w, "NP", "IN")
    gw.initiate("S1", "MIX-NP-1", WAYBILL)
    assert set(gw.shipments["S1"].required) == {
        "ARTISAN_COOP", "BOTANICAL_LAB", "TANNING_REGISTRY", "ORIGIN_CUSTOMS", "DEST_CUSTOMS",
    }


def test_redacting_waybill_keeps_handshake_signatures_valid(verified_world):
    w = verified_world
    gw = gateway(w, "NP", "IN")
    gw.initiate("S1", "HEMP-NP-0001", {"weight_kg": 120.0, "packages": 4})
    sign_all(w, gw, "S1")
    gw.commit("S1")
    np_ = w.ledgers["NP"]
    lock = np_.find("LOCK", shipment_id="S1")[0]
    shares = {k: w.shares["NP"][k] for k in ("CUSTOMS-NP", "GI-REG-NP", "GI-REG-IN")}
    np_.redact(lock.index, dict(lock.payload, waybill=WAYBILL), shares, "typo", w.nodes["CUSTOMS-NP"])
    # the MINT on IN still points at the same LOCK hash
    assert w.ledgers["IN"].blocks[-1].payload["origin_lock_block"] == lock.hash
    assert np_.verify_chain() == []
