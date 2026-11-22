"""PDF certificates and documents, each with a machine-readable JSON twin.

Every document is issued the same way (issue()):
  twin        JSON record of what the document says, stored in the `documents` collection
  twin hash   SHA-256 of the canonical twin; printed on the PDF and embedded in its metadata
  signature   the issuer's Ed25519 signature over the canonical twin
  ledger      a DOCUMENT block on the relevant ledger records the twin hash and signature
  QR code     {doc_id, twin hash, ledger, block hash}
  watermark   "SAMPLE / DEMONSTRATION. NOT AN OFFICIAL DOCUMENT" on every page

All institutions, codes and people are fictional sample data.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import Color, HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from ..crypto import Node, canonical, sha256_hex
from ..doc_store import signed_part
from ..sql_store import get_batch
from ..storage import ensure_index
from .qr import qr_payload

WATERMARK_TEXT = "SAMPLE / DEMONSTRATION. NOT AN OFFICIAL DOCUMENT"
META_DOC_ID = "/GITraceDocId"
META_TWIN_SHA256 = "/GITraceTwinSHA256"

TITLES = {
    "hemp_lab_assay": "Hemp Lab Assay Certificate",
    "tanning_attestation": "Tanning and Heritage Attestation",
    "membership_letter": "Cooperative GI Membership Letter",
    "export_clearance": "Export Clearance Certificate",
    "customs_waybill": "Customs Waybill",
    "redaction_notice": "Ledger Redaction Notice",
}


def documents_collection(world):
    coll = world.store.get_collection("documents")
    ensure_index(coll, "doc_id")
    ensure_index(coll, "batch_id")
    return coll


# --------------------------------------------------------------------------- issue

def issue(world, out_dir, *, doc_type: str, key: str, issuer: Node, jurisdiction: str,
          batch_id: str | None, content: dict, fields: list[tuple[str, object]]) -> dict:
    """Signs, anchors, renders and registers one document; returns its `documents` record."""
    doc_id = f"{doc_type}:{key}"
    issued_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    twin = {
        "doc_id": doc_id, "doc_type": doc_type, "title": TITLES[doc_type], "batch_id": batch_id,
        "issuer": issuer.name, "issuer_role": issuer.role, "issuer_jurisdiction": issuer.jurisdiction,
        "issued_at": issued_at, "content": content,
    }
    twin_sha = sha256_hex(canonical(twin))
    signature = issuer.sign(canonical(twin))

    ledger = world.ledgers[jurisdiction]
    recorder = issuer if ledger.members.get(issuer.name) == issuer.public_key else world.nodes[f"GI-REG-{jurisdiction}"]
    block = ledger.append("DOCUMENT", {
        "doc_id": doc_id, "doc_type": doc_type, "batch_id": batch_id,
        "twin_sha256": twin_sha, "issuer": issuer.name, "signature": signature,
    }, recorder)

    qr = qr_payload(doc_id=doc_id, twin_sha256=twin_sha, ledger=jurisdiction, block=block.hash)
    pdf = render_pdf(TITLES[doc_type], issuer, doc_id, issued_at, fields, twin_sha, signature,
                     jurisdiction, block.hash, qr)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{doc_id.replace(':', '__')}.pdf"
    path.write_bytes(pdf)

    record = {
        "doc_id": doc_id, "type": doc_type, "batch_id": batch_id, "path": path.resolve().as_posix(),
        "file_name": path.name, "sha256": sha256_hex(pdf), "issued_at": issued_at,
        "issuer": issuer.name, "twin": twin, "twin_sha256": twin_sha, "signature": signature,
        "ledger": jurisdiction, "ledger_block": block.hash, "qr_payload": qr,
    }
    docs = documents_collection(world)
    docs.delete_many({"doc_id": doc_id})  # re-issuing replaces the old registration
    docs.insert_one(dict(record))
    return record


# --------------------------------------------------------------------------- render

def _short(hex_str: str, n: int = 16) -> str:
    return hex_str if len(hex_str) <= 2 * n + 3 else f"{hex_str[:n]}...{hex_str[-n:]}"


def _wrap(text: str, width: int) -> list[str]:
    lines = []
    for raw in str(text).split("\n"):
        while len(raw) > width:
            cut = raw.rfind(" ", 0, width)
            cut = cut if cut > width // 2 else width
            lines.append(raw[:cut])
            raw = raw[cut:].lstrip()
        lines.append(raw)
    return lines


def _page_frame(c: canvas.Canvas, page_w: float, page_h: float) -> None:
    c.saveState()
    c.setFillColor(Color(0.85, 0.1, 0.1, alpha=0.12))
    c.setFont("Helvetica-Bold", 96)
    c.translate(page_w / 2, page_h / 2)
    c.rotate(45)
    c.drawCentredString(0, -30, "SAMPLE")
    c.restoreState()
    c.saveState()
    c.setFillColor(HexColor("#B00020"))
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(page_w / 2, page_h - 12 * mm, WATERMARK_TEXT)
    c.drawCentredString(page_w / 2, 10 * mm, WATERMARK_TEXT)
    c.restoreState()


def render_pdf(title: str, issuer: Node, doc_id: str, issued_at: str, fields, twin_sha: str,
               signature: str, ledger: str, block_hash: str, qr: str) -> bytes:
    buf = io.BytesIO()
    page_w, page_h = A4
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"{title} ({WATERMARK_TEXT})")
    c.setAuthor(issuer.name)
    c.setSubject(doc_id)
    left, bottom_limit = 20 * mm, 30 * mm

    def new_page(first: bool) -> float:
        if not first:
            c.showPage()
        _page_frame(c, page_w, page_h)
        return page_h - 25 * mm

    y = new_page(True)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(left, y, title)
    y -= 7 * mm
    c.setFont("Helvetica", 9)
    for line in (f"Issuer: {issuer.name} ({issuer.role}, {issuer.jurisdiction})",
                 f"Document id: {doc_id}", f"Issued at: {issued_at}"):
        c.drawString(left, y, line)
        y -= 5 * mm
    y -= 3 * mm

    label_w = 48 * mm
    for label, value in fields:
        lines = _wrap(value if value is not None else "-", 70)
        if y - 4.5 * mm * len(lines) < bottom_limit + 45 * mm:
            y = new_page(False)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(left, y, str(label))
        c.setFont("Helvetica", 9)
        for line in lines:
            c.drawString(left + label_w, y, line)
            y -= 4.5 * mm
        y -= 1 * mm

    # integrity block with QR code, always together at the bottom of the last page
    if y < bottom_limit + 45 * mm:
        y = new_page(False)
    box_top = bottom_limit + 42 * mm
    c.setStrokeColor(HexColor("#888888"))
    c.rect(left - 2 * mm, bottom_limit - 2 * mm, page_w - 2 * left + 4 * mm, 44 * mm)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(left, box_top - 4 * mm, "Integrity")
    c.setFont("Courier", 7.5)
    ty = box_top - 9 * mm
    for line in (f"JSON twin SHA-256: {twin_sha[:32]}", f"                   {twin_sha[32:]}",
                 f"Issuer Ed25519 signature: {_short(signature)}",
                 f"Ledger: {ledger}  block {_short(block_hash, 12)}",
                 "Check inside Docker: docker compose run --rm web",
                  "  python -m gi_trace.docgen.verify <this file>"):
        c.drawString(left, ty, line)
        ty -= 4.2 * mm
    qr_size = 38 * mm
    widget = QrCodeWidget(qr)
    x0, y0, x1, y1 = widget.getBounds()
    d = Drawing(qr_size, qr_size, transform=[qr_size / (x1 - x0), 0, 0, qr_size / (y1 - y0), 0, 0])
    d.add(widget)
    renderPDF.draw(d, c, page_w - left - qr_size, bottom_limit - 1 * mm)
    c.showPage()
    c.save()

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(buf.getvalue())))
    writer.add_metadata({META_DOC_ID: doc_id, META_TWIN_SHA256: twin_sha})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# --------------------------------------------------------------------------- documents

def _batch_fields(batch: dict) -> list[tuple[str, object]]:
    return [
        ("Batch", batch["batch_id"]),
        ("Product", f"{batch['product']} (HS {batch['hs_code']}), {batch['weight_kg']} kg"),
        ("Artisan", f"{batch['artisan_name']} ({batch['artisan_id']})"),
        ("Cooperative", f"{batch['coop_name']} ({batch['coop_id']})"),
        ("GI registry code", batch["gi_registry_code"]),
        ("Origin", batch["origin"]),
        ("Batch date", batch["created_on"]),
    ]


def certificate_pdf(world, out_dir, cert: dict) -> dict:
    """Hemp lab assay or tanning attestation, from a certificate in the document store."""
    batch = get_batch(world.conn, cert["batch_id"])
    cert_sha = sha256_hex(canonical(signed_part(cert)))
    fields = _batch_fields(batch)
    if cert["cert_type"] == "hemp_lab_assay":
        a = cert.get("assay", {})
        fields += [
            ("THC", f"{a.get('thc_pct')} %"), ("CBD", f"{a.get('cbd_pct')} %"),
            ("Moisture", f"{a.get('moisture_pct', '-')} %"), ("Method", a.get("method")),
            ("Spectral peaks", ", ".join(f"{p['nm']} nm: {p['abs']}" for p in cert.get("spectral_peaks", []))),
            ("Raw lab file SHA-256", cert.get("lab_file_sha256")),
        ]
    else:
        t = cert.get("tanning", {})
        fields += [
            ("Tanning method", t.get("method")), ("Tanning agent", t.get("agent")),
            ("Heritage motifs", ", ".join(cert.get("heritage", {}).get("motifs", []))),
        ]
    fields += [("Oracle status when issued", cert.get("compliance_status")), ("Certificate SHA-256", cert_sha)]
    content = {"certificate": {k: v for k, v in cert.items() if k != "_id"}, "cert_sha256": cert_sha}
    return issue(world, out_dir, doc_type=cert["cert_type"], key=cert["batch_id"],
                 issuer=world.nodes[cert["signer"]], jurisdiction=batch["origin"],
                 batch_id=cert["batch_id"], content=content, fields=fields)


def membership_letter_pdf(world, out_dir, coop_id: str) -> dict:
    coop = dict(world.conn.execute("SELECT * FROM cooperatives WHERE coop_id = ?", (coop_id,)).fetchone())
    members = [dict(r) for r in world.conn.execute(
        "SELECT artisan_id, name, joined_on FROM artisans WHERE coop_id = ? ORDER BY artisan_id", (coop_id,))]
    fields = [
        ("Cooperative", f"{coop['name']} ({coop_id})"), ("Jurisdiction", coop["jurisdiction"]),
        ("GI registry code", coop["gi_registry_code"]),
        ("Statement", "The artisans below are registered members of this cooperative and may "
                      "issue batches under its geographical-indication code."),
        *[(f"Member {i}", f"{m['name']} ({m['artisan_id']}), joined {m['joined_on']}") for i, m in enumerate(members, 1)],
    ]
    j = coop["jurisdiction"]
    return issue(world, out_dir, doc_type="membership_letter", key=coop_id, issuer=world.nodes[f"GI-REG-{j}"],
                 jurisdiction=j, batch_id=None, content={"cooperative": coop, "members": members}, fields=fields)


def export_clearance_pdf(world, out_dir, origin: str, shipment_id: str) -> dict:
    """After a committed handshake: the signers and the ledger blocks on both sides."""
    lock = world.ledgers[origin].find("LOCK", shipment_id=shipment_id)[-1]
    dest = lock.payload["dest"]
    transferred = world.ledgers[origin].find("TRANSFERRED", shipment_id=shipment_id)
    if not transferred:
        raise ValueError(f"shipment {shipment_id} has not been committed")
    mint = world.ledgers[dest].find("MINT", shipment_id=shipment_id)[-1]
    batch = get_batch(world.conn, lock.payload["batch_id"])
    signers = mint.payload["signatures"]
    fields = _batch_fields(batch) + [
        ("Shipment", shipment_id), ("Route", f"{origin} -> {dest}"),
        *[(f"Signer: {role}", f"{s['signer']}  sig {_short(s['sig'], 12)}") for role, s in sorted(signers.items())],
        (f"{origin} LOCK block", lock.hash), (f"{origin} TRANSFERRED block", transferred[-1].hash),
        (f"{dest} MINT block", mint.hash),
    ]
    content = {"shipment_id": shipment_id, "batch_id": batch["batch_id"], "origin": origin, "dest": dest,
               "signers": signers, "lock_block": lock.hash, "transferred_block": transferred[-1].hash,
               "mint_block": mint.hash}
    return issue(world, out_dir, doc_type="export_clearance", key=shipment_id,
                 issuer=world.nodes[f"CUSTOMS-{origin}"], jurisdiction=origin,
                 batch_id=batch["batch_id"], content=content, fields=fields)


def waybill_pdf(world, out_dir, origin: str, shipment_id: str) -> dict:
    """From the LOCK payload (as corrected by any redaction)."""
    lock = world.ledgers[origin].find("LOCK", shipment_id=shipment_id)[-1]
    p = lock.payload
    batch = get_batch(world.conn, p["batch_id"])
    tariff = world.conn.execute("SELECT rate_pct FROM tariff_schedule WHERE hs_code = ? AND origin = ? AND dest = ?",
                                (batch["hs_code"], origin, p["dest"])).fetchone()
    order = world.conn.execute("SELECT * FROM orders WHERE order_id = ?", (shipment_id,)).fetchone()
    fields = _batch_fields(batch) + [
        ("Shipment", shipment_id), ("Destination", p["dest"]),
        ("Gross weight", f"{p['waybill'].get('weight_kg')} kg"), ("Packages", p["waybill"].get("packages")),
        ("Tariff rate", f"{tariff['rate_pct']} %" if tariff else "not in schedule"),
        ("Buyer", order["buyer"] if order else "-"),
        ("Order", f"{order['quantity']} x {order['unit_price']} {order['currency']}" if order else "-"),
        (f"{origin} LOCK block", lock.hash),
    ]
    content = {"shipment_id": shipment_id, "batch_id": batch["batch_id"], "origin": origin, "dest": p["dest"],
               "waybill": p["waybill"], "hs_code": batch["hs_code"],
               "tariff_rate_pct": tariff["rate_pct"] if tariff else None,
               "order": dict(order) if order else None, "lock_block": lock.hash}
    return issue(world, out_dir, doc_type="customs_waybill", key=shipment_id,
                 issuer=world.nodes[f"CUSTOMS-{origin}"], jurisdiction=origin,
                 batch_id=batch["batch_id"], content=content, fields=fields)


def redaction_notice_pdf(world, out_dir, jurisdiction: str, redaction_block) -> dict:
    p = redaction_block.payload
    target = world.ledgers[jurisdiction].blocks[p["target_index"]]
    batch_id = target.payload.get("batch_id")
    fields = [
        ("Ledger", jurisdiction), ("Redacted block", f"#{p['target_index']} ({target.tx_type}) {p['target_block']}"),
        ("Batch", batch_id or "-"), ("Reason", p["reason"]),
        ("Approvers", ", ".join(p["approvers"])),
        ("Old payload SHA-256", p["old_payload_sha256"]), ("New payload SHA-256", p["new_payload_sha256"]),
        ("Audit block", f"#{redaction_block.index} {redaction_block.hash}"),
        ("Effect", "The block hash, every later link and every signature are unchanged; "
                   "only the payload and its chameleon randomness were replaced."),
    ]
    content = {"ledger": jurisdiction, "redaction_block": redaction_block.hash, **p}
    return issue(world, out_dir, doc_type="redaction_notice", key=f"{jurisdiction}-{redaction_block.index}",
                 issuer=world.nodes[redaction_block.signer], jurisdiction=jurisdiction,
                 batch_id=batch_id, content=content, fields=fields)
