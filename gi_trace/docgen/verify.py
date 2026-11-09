"""Checks a generated PDF against the document store and the ledger.

    python -m gi_trace.docgen.verify out/certificates/<file>.pdf [--data data]
    python -m gi_trace.docgen.verify out/certificates          (every PDF in a folder)

Reports one of:
  VALID     the file is byte-for-byte the registered one, its JSON twin is unchanged,
            the issuer's signature verifies and the ledger's DOCUMENT block matches
  TAMPERED  the file is recognised (by its embedded doc id, hash or file name) but the
            PDF, its JSON twin, the signature or the ledger record does not match
  UNKNOWN   the file is not a document this system issued
Exit code: 1 if any file is TAMPERED, else 2 if any is UNKNOWN (or no PDF was given), else 0.
"""
from __future__ import annotations

import argparse
import glob
import io
import sys
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader

from ..crypto import canonical, sha256_hex, verify
from .certificates import META_DOC_ID, META_TWIN_SHA256
from .qr import qr_payload

VALID, TAMPERED, UNKNOWN = "VALID", "TAMPERED", "UNKNOWN"
# documents-record field -> JSON twin field it must equal
REGISTERED_FIELDS = {"doc_id": "doc_id", "type": "doc_type", "batch_id": "batch_id",
                     "issuer": "issuer", "issued_at": "issued_at"}


@dataclass
class VerifyResult:
    status: str
    doc_id: str | None = None
    reasons: list[str] = field(default_factory=list)


def _embedded(pdf: bytes) -> tuple[str | None, str | None]:
    try:
        meta = PdfReader(io.BytesIO(pdf)).metadata or {}
        return meta.get(META_DOC_ID), meta.get(META_TWIN_SHA256)
    except Exception:  # a damaged PDF may not parse at all
        return None, None


def verify_pdf(pdf_path, world, valid_chains: set | None = None) -> VerifyResult:
    """`valid_chains`: when checking many files against the same unchanged world, pass one
    set to all calls so each ledger's chain is verified once instead of once per file."""
    pdf_path = Path(pdf_path)
    try:
        pdf = pdf_path.read_bytes()
    except FileNotFoundError:
        return VerifyResult(UNKNOWN, reasons=[f"{pdf_path} does not exist"])
    pdf_sha = sha256_hex(pdf)
    docs = world.store.get_collection("documents")  # read only: verifying never writes
    doc_id, embedded_twin_sha = _embedded(pdf)

    rec = docs.find_one({"sha256": pdf_sha})
    if rec is None and doc_id:
        rec = docs.find_one({"doc_id": doc_id})
    if rec is None and embedded_twin_sha:
        rec = docs.find_one({"twin_sha256": embedded_twin_sha})
    if rec is None:
        rec = docs.find_one({"file_name": pdf_path.name})
    if rec is None:
        issued = [b for l in world.ledgers.values() for b in l.find("DOCUMENT")
                  if (doc_id and b.payload.get("doc_id") == doc_id)
                  or (embedded_twin_sha and b.payload.get("twin_sha256") == embedded_twin_sha)]
        if issued:  # the ledger says it was issued, but its registration is gone
            return VerifyResult(TAMPERED, doc_id or issued[-1].payload.get("doc_id"),
                                ["issued according to the ledger, but missing from the document store"])
        return VerifyResult(UNKNOWN, doc_id, ["no matching document in the document store"])

    reasons = []
    if rec.get("sha256") != pdf_sha:
        reasons.append("PDF bytes differ from the registered file")
    if pdf_path.name != rec.get("file_name") and docs.find_one({"file_name": pdf_path.name}) is not None:
        reasons.append(f"file is named as another document but its content is {rec.get('doc_id')}")
    registered = Path(str(rec.get("path", "")))
    if registered.name != rec.get("file_name"):
        reasons.append("registered path and file name disagree")
    elif registered.is_file() and sha256_hex(registered.read_bytes()) != rec.get("sha256"):
        reasons.append(f"the file at the registered path {registered} is not the registered PDF")
    if rec.get("qr_payload") != qr_payload(doc_id=rec.get("doc_id"), twin_sha256=rec.get("twin_sha256"),
                                           ledger=rec.get("ledger"), block=rec.get("ledger_block")):
        reasons.append("registered QR payload does not match the document")
    if embedded_twin_sha is not None and embedded_twin_sha != rec.get("twin_sha256"):
        reasons.append("hash embedded in the PDF differs from the registered JSON twin hash")
    twin = rec.get("twin")
    twin_sha = sha256_hex(canonical(twin))
    if twin_sha != rec.get("twin_sha256"):
        reasons.append("JSON twin in the document store was changed")
    if isinstance(twin, dict) and any(rec.get(k) != twin.get(t) for k, t in REGISTERED_FIELDS.items()):
        reasons.append("registration fields (type, batch, issuer, issue time) disagree with the JSON twin")
    issuer = world.directory.get((twin or {}).get("issuer") if isinstance(twin, dict) else None)
    if issuer is None or not verify(issuer[1], canonical(twin), rec.get("signature") or ""):
        reasons.append("issuer signature over the JSON twin does not verify")
    ledger = world.ledgers.get(rec.get("ledger"))
    block = next((b for b in ledger.blocks if b.hash == rec.get("ledger_block")), None) if ledger else None
    if block is None or block.tx_type != "DOCUMENT":
        reasons.append("no DOCUMENT block for it on the ledger")
    elif block.payload.get("twin_sha256") != twin_sha or block.payload.get("doc_id") != rec.get("doc_id"):
        reasons.append("ledger DOCUMENT block records a different hash")
    elif valid_chains is None or ledger.jurisdiction not in valid_chains:
        if ledger.verify_chain():
            reasons.append(f"the {ledger.jurisdiction} ledger fails verification")
        elif valid_chains is not None:
            valid_chains.add(ledger.jurisdiction)
    return VerifyResult(TAMPERED if reasons else VALID, rec.get("doc_id"), reasons)


def main(argv=None) -> int:
    from ..seed import open_world

    ap = argparse.ArgumentParser(description="Verify a generated PDF: VALID, TAMPERED or UNKNOWN.")
    ap.add_argument("pdf", nargs="+", help="PDF files, folders or wildcards (expanded here, for Windows shells)")
    ap.add_argument("--data", default="data", help="data directory of the saved world (default: data)")
    args = ap.parse_args(argv)
    files = []
    for arg in args.pdf:
        if Path(arg).is_dir():
            files += sorted(str(p) for p in Path(arg).glob("*.pdf"))
        elif glob.has_magic(arg):
            files += sorted(glob.glob(arg)) or [arg]
        else:
            files.append(arg)
    if not files:
        print("UNKNOWN   no PDF files given or found")
        return 2
    try:
        world = open_world(args.data)
    except FileNotFoundError as e:
        for p in files:
            print(f"UNKNOWN   {p}\n          - {e}")
        return 2
    except Exception as e:  # the ledgers, keys or documents themselves fail verification
        for p in files:
            print(f"TAMPERED  {p}\n          - cannot be trusted: the saved world failed verification: {e}")
        return 1
    if getattr(world, "pipeline", "complete") != "complete":
        print("warning: the last run_all did not finish; out/ may be incomplete")
    statuses, valid_chains = set(), set()
    for p in files:
        r = verify_pdf(p, world, valid_chains)
        statuses.add(r.status)
        print(f"{r.status:<9} {p}" + (f"  ({r.doc_id})" if r.doc_id else ""))
        for reason in r.reasons:
            print(f"          - {reason}")
    return 1 if TAMPERED in statuses else 2 if UNKNOWN in statuses else 0


if __name__ == "__main__":
    sys.exit(main())
