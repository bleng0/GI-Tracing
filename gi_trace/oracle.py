"""Dual-compliance oracle (mechanism 4).

A staged physical-to-digital pipeline loosely modelled on Perinelli et al.'s
notarisation flow. A batch gets an export QR code only if every material in it
has a valid certificate:

  hemp    -> hemp_lab_assay signed by a BOTANICAL_LAB node; the raw lab file must
             hash to the value in the certificate; THC must be within the origin
             jurisdiction's limit.
  leather -> tanning_attestation signed by a TANNING_REGISTRY node; method must be
             "vegetable" and the named artisan must be the batch's artisan.

Stages: 1 load batch from SQL  2 fetch certificates from MongoDB  3 verify
signatures  4 check material rules  5 notarise ATTESTATION blocks + BATCH_ANCHOR
on the origin ledger  6 mark certificates verified  7 write the QR code.
Nothing is written to the ledger if any check fails.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import qrcode
import qrcode.image.svg

from .crypto import BOTANICAL_LAB, TANNING_REGISTRY, Node, canonical, sha256_hex, verify
from .doc_store import signed_part
from .ledger import Ledger
from .sql_store import get_batch, max_thc_pct

REQUIRED_CERT = {"hemp": "hemp_lab_assay", "leather": "tanning_attestation"}
CERT_SIGNER_ROLE = {"hemp_lab_assay": BOTANICAL_LAB, "tanning_attestation": TANNING_REGISTRY}


@dataclass
class OracleResult:
    batch_id: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    anchor_block: str | None = None
    qr_path: str | None = None


def run_oracle(conn, coll, ledger: Ledger, directory: dict[str, tuple[str, bytes]], notary: Node,
               batch_id: str, lab_file: bytes | None, qr_dir: str | Path) -> OracleResult:
    result = OracleResult(batch_id, passed=False)

    # 1. relational record
    batch = get_batch(conn, batch_id)
    if batch is None:
        result.reasons.append("batch is not registered")
        return result
    if batch["origin"] != ledger.jurisdiction:
        result.reasons.append(f"batch originates in {batch['origin']}, not {ledger.jurisdiction}")
        return result

    # 2-4. certificates, signatures, material rules
    certs = []
    for material in batch["materials"]:
        cert_type = REQUIRED_CERT[material]
        found = list(coll.find({"batch_id": batch_id, "cert_type": cert_type}))
        if len(found) != 1:
            result.reasons.append(f"expected one {cert_type}, found {len(found)}")
            continue
        cert = found[0]
        certs.append(cert)

        signer = directory.get(cert.get("signer"))
        if signer is None or signer[0] != CERT_SIGNER_ROLE[cert_type]:
            result.reasons.append(f"{cert_type} signer is not a registered {CERT_SIGNER_ROLE[cert_type]}")
        elif not verify(signer[1], canonical(signed_part(cert)), cert.get("signature", "")):
            result.reasons.append(f"{cert_type} signature is invalid")
        if cert.get("artisan_coop_id") != batch["coop_id"]:
            result.reasons.append(f"{cert_type} names a different cooperative")

        if material == "hemp":
            if lab_file is None or hashlib.sha256(lab_file).hexdigest() != cert.get("lab_file_sha256"):
                result.reasons.append("raw lab file does not match the assay certificate")
            limit = max_thc_pct(conn, batch["origin"])
            thc = cert.get("assay", {}).get("thc_pct")
            if thc is None or thc > limit:
                result.reasons.append(f"THC {thc}% exceeds the {batch['origin']} limit of {limit}%")
        else:
            if cert.get("tanning", {}).get("method") != "vegetable":
                result.reasons.append("leather is not vegetable-tanned")
            if cert.get("artisan_id") != batch["artisan_id"]:
                result.reasons.append("tanning attestation names a different artisan")

    if result.reasons:
        coll.update_many({"batch_id": batch_id}, {"$set": {"compliance_status": "rejected"}})
        return result

    # 5. notarise
    attestation_blocks = []
    for cert in certs:
        block = ledger.append(
            "ATTESTATION",
            {
                "batch_id": batch_id,
                "cert_type": cert["cert_type"],
                "cert_sha256": sha256_hex(canonical(signed_part(cert))),
                "lab_file_sha256": cert.get("lab_file_sha256"),
                "origin_gps": cert.get("origin_gps"),
                "operator_code": cert.get("operator_code"),
                "signer": cert["signer"],
            },
            notary,
        )
        attestation_blocks.append(block.hash)
    sql_row = {k: batch[k] for k in ("batch_id", "coop_id", "artisan_id", "product", "hs_code", "weight_kg", "created_on")}
    anchor = ledger.append(
        "BATCH_ANCHOR",
        {
            "batch_id": batch_id,
            "coop_id": batch["coop_id"],
            "materials": batch["materials"],
            "batch_row_sha256": sha256_hex(canonical(sql_row)),
            "attestation_blocks": attestation_blocks,
        },
        notary,
    )

    # 6. status back into the document tier
    coll.update_many({"batch_id": batch_id}, {"$set": {"compliance_status": "verified"}})

    # 7. QR code a buyer or customs officer can scan to look up the anchor
    qr_payload = canonical({
        "batch_id": batch_id,
        "gi": batch["gi_registry_code"],
        "ledger": ledger.jurisdiction,
        "anchor_block": anchor.hash,
    }).decode()
    qr_dir = Path(qr_dir)
    qr_dir.mkdir(parents=True, exist_ok=True)
    qr_path = qr_dir / f"{batch_id}.svg"
    qrcode.make(qr_payload, image_factory=qrcode.image.svg.SvgPathImage).save(str(qr_path))

    result.passed = True
    result.anchor_block = anchor.hash
    result.qr_path = str(qr_path)
    return result
