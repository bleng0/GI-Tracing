"""End-to-end walkthrough: docker compose run --rm web python -m gi_trace.demo"""
from __future__ import annotations

from ._docker import require_container
from .federated import shipment_dossier
from .handshake import BorderGateway, HandshakeError
from .ledger import LedgerError
from .oracle import run_oracle
from .seed import build_world
from .tiles import materialize_compliance_view

QR_DIR = "out/qr"


def sign_all(world, gateway, shipment_id, skip=()):
    s = gateway.shipments[shipment_id]
    for role, name in s.required.items():
        if role not in skip:
            gateway.sign(shipment_id, role, world.nodes[name].sign(s.message))


def main() -> None:
    require_container()
    w = build_world()
    np_, in_, pk = w.ledgers["NP"], w.ledgers["IN"], w.ledgers["PK"]

    print("== 1. Dual-compliance oracle")
    for batch_id, ledger, notary in [
        ("HEMP-NP-0001", np_, "GI-REG-NP"),
        ("HEMP-NP-0002", np_, "GI-REG-NP"),
        ("CHAP-PK-0001", pk, "GI-REG-PK"),
    ]:
        r = run_oracle(w.conn, w.coll, ledger, w.directory, w.nodes[notary],
                       batch_id, w.lab_files.get(batch_id), QR_DIR)
        print(f"  {batch_id}: {'PASS -> ' + r.qr_path if r.passed else 'REJECTED: ' + '; '.join(r.reasons)}")

    print("\n== 2. JSON-Tiles materialised compliance view (threshold 0.8)")
    for path, f in materialize_compliance_view(w.conn, w.coll).items():
        print(f"  {path:<20} {f:.0%}")
    plan = w.conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM compliance_view WHERE batch_id = 'HEMP-NP-0001'"
    ).fetchall()
    print("  query plan:", plan[0]["detail"])

    print("\n== 3. Cross-engine dossier (SQL + MongoDB on batch_id)")
    d = shipment_dossier(w.conn, w.coll, "HEMP-NP-0001")
    o = d["orders"][0]
    print(f"  {d['batch']['product']} by {d['batch']['artisan_name']} ({d['batch']['coop_name']})")
    print(f"  order {o['order_id']} -> {o['dest_jurisdiction']}, tariff {o['tariff_rate_pct']}%")
    print(f"  THC from Mongo-only path assay.thc_pct: {d['certificates'][0]['assay']['thc_pct']}%")

    print("\n== 4. Border handshake NP -> IN (hemp: 4 signers)")
    gw = BorderGateway(np_, in_, w.nodes["CUSTOMS-NP"], w.nodes["CUSTOMS-IN"], w.directory)
    gw.initiate("SHP-001", "HEMP-NP-0001", waybill={"weight_kg": 120.0, "packages": 4})
    print("  signers:", ", ".join(gw.shipments["SHP-001"].required))
    sign_all(w, gw, "SHP-001")
    mint = gw.commit("SHP-001")
    print(f"  committed: MINT block {mint.hash[:16]}... on IN ledger")

    print("\n== 5. Threshold redaction of the waybill typo (120.0 kg -> 12.0 kg)")
    lock = np_.find("LOCK", shipment_id="SHP-001")[0]
    fixed = dict(lock.payload, waybill={"weight_kg": 12.0, "packages": 4})
    two = dict(list(w.shares["NP"].items())[:2])
    try:
        np_.redact(lock.index, fixed, two, "clerical weight error", w.nodes["CUSTOMS-NP"])
    except LedgerError as e:
        print("  with 2 shares:", e)
    three = {k: w.shares["NP"][k] for k in ("CUSTOMS-NP", "GI-REG-NP", "CUSTOMS-IN")}
    hash_before = lock.hash
    np_.redact(lock.index, fixed, three, "clerical weight error", w.nodes["CUSTOMS-NP"])
    print("  with 3 shares: redacted; block hash unchanged:", lock.hash == hash_before)
    print("  NP chain problems:", np_.verify_chain() or "none", "| IN chain problems:", in_.verify_chain() or "none")

    print("\n== 6. Border handshake PK -> NP (leather) missing the tanning registry")
    gw2 = BorderGateway(pk, np_, w.nodes["CUSTOMS-PK"], w.nodes["CUSTOMS-NP"], w.directory)
    gw2.initiate("SHP-002", "CHAP-PK-0001", waybill={"weight_kg": 8.5, "packages": 1})
    sign_all(w, gw2, "SHP-002", skip={"TANNING_REGISTRY"})
    try:
        gw2.commit("SHP-002")
    except HandshakeError as e:
        print("  commit refused:", e)
    gw2.abort("SHP-002", "tanning registry did not sign")
    print("  aborted; batch unlocked on PK ledger")


if __name__ == "__main__":
    main()
