"""The whole story, start to end, with files kept on disk:

    python -m gi_trace.run_all            (asks before replacing data/ and out/)
    python -m gi_trace.run_all --yes      (no question)

Steps: reset local data, seed the 30-batch sample, raw lab files, certificate PDFs
and cooperative letters, dual-compliance oracle, QR codes, border handshake for
every order of every passing batch, one deliberate waybill redaction, export PDFs,
JSON-Tiles view, summary report in out/reports/. Prints a pass/fail table; exits 1
if any batch did not end the way the sample says it should.
"""
from __future__ import annotations

import argparse
import html
import shutil
import sys
import time
from pathlib import Path

from .docgen.certificates import (
    certificate_pdf, documents_collection, export_clearance_pdf, membership_letter_pdf,
    redaction_notice_pdf, waybill_pdf,
)
from .doc_store import load_env
from .docgen.lab_files import read_lab_file, write_lab_files
from .docgen.qr import qr_payload, write_qr
from .handshake import BorderGateway, HandshakeError
from .ledger import LedgerError
from .oracle import run_oracle
from .seed import WorldError, build_world, set_pipeline_status
from .sql_store import get_batch
from .tiles import materialize_compliance_view

OUT_SUBDIRS = ("labfiles", "certificates", "qr", "reports")
REDACTION_REASON = "clerical weight error: 10x typo on the waybill"


class PipelineError(Exception):
    pass


def reset_out(out_dir: Path) -> None:
    """Removes only the folders this pipeline fills; anything else in out/ is left alone."""
    for name in OUT_SUBDIRS:
        if (out_dir / name).exists():
            try:
                shutil.rmtree(out_dir / name)
            except PermissionError as e:  # Windows: a PDF is open in a viewer, or held by antivirus
                raise PipelineError(f"cannot replace {out_dir / name}: {e.filename} is in use. "
                                    "Close it and run again.") from e


def run_pipeline(data_dir="data", out_dir="out", sample: str = "full", log=print) -> dict:
    t0 = time.perf_counter()
    data_dir, out_dir = Path(data_dir), Path(out_dir)
    step = iter(range(1, 100))

    log(f"{next(step):>2}. reset {out_dir}/ and seed {data_dir}/ ({sample} sample)")
    reset_out(out_dir)
    w = build_world(data_dir, sample=sample)
    log("    document store: " + ("OFFLINE MODE (local file store)" if w.offline else "MongoDB Atlas"))
    set_pipeline_status(w, "running")  # set to "complete" at the very end
    batches = {b: get_batch(w.conn, b) for b in w.expected}

    log(f"{next(step):>2}. raw lab files -> {out_dir / 'labfiles'}")
    write_lab_files(w.lab_files, out_dir / "labfiles")

    log(f"{next(step):>2}. certificate PDFs and cooperative membership letters")
    certs_dir = out_dir / "certificates"
    for cert in sorted(w.coll.find(), key=lambda c: (c["batch_id"], c["cert_type"])):
        certificate_pdf(w, certs_dir, cert)
    for (coop_id,) in w.conn.execute("SELECT coop_id FROM cooperatives ORDER BY coop_id"):
        membership_letter_pdf(w, certs_dir, coop_id)

    log(f"{next(step):>2}. dual-compliance oracle")
    oracle = {}
    for batch_id, b in batches.items():
        j = b["origin"]
        oracle[batch_id] = run_oracle(w.conn, w.coll, w.ledgers[j], w.directory, w.nodes[f"GI-REG-{j}"],
                                      batch_id, read_lab_file(out_dir / "labfiles", batch_id), out_dir / "qr")

    log(f"{next(step):>2}. QR codes (SVG and PNG) for every verified batch")
    for batch_id, r in oracle.items():
        if r.passed:
            b = batches[batch_id]
            write_qr(qr_payload(batch_id=batch_id, gi=b["gi_registry_code"], ledger=b["origin"],
                                anchor_block=r.anchor_block), out_dir / "qr", batch_id)

    log(f"{next(step):>2}. border handshake for every order of every verified batch")
    shipments, refused = [], {}
    orders = w.conn.execute("SELECT * FROM orders ORDER BY order_id").fetchall()
    first_export = next((o["order_id"] for o in orders if oracle[o["batch_id"]].passed), None)
    for o in orders:
        if not oracle[o["batch_id"]].passed:
            continue
        b = batches[o["batch_id"]]
        origin, dest = b["origin"], o["dest_jurisdiction"]
        gw = BorderGateway(w.ledgers[origin], w.ledgers[dest], w.nodes[f"CUSTOMS-{origin}"],
                           w.nodes[f"CUSTOMS-{dest}"], w.directory, pending=w.ledger_store)
        weight = b["weight_kg"] * (10 if o["order_id"] == first_export else 1)  # the deliberate typo
        try:
            gw.initiate(o["order_id"], o["batch_id"], {"weight_kg": weight, "packages": max(1, round(b["weight_kg"] / 5))})
        except HandshakeError as e:
            refused[o["order_id"]] = (o["batch_id"], str(e))
            continue
        s = gw.shipments[o["order_id"]]
        for role, name in s.required.items():
            gw.sign(o["order_id"], role, w.nodes[name].sign(s.message))
        gw.commit(o["order_id"])
        shipments.append((o["order_id"], o["batch_id"], origin, dest))

    redaction = None
    if first_export is not None:
        log(f"{next(step):>2}. threshold redaction of the waybill typo on {first_export}")
        _, batch_id, origin, dest = next(s for s in shipments if s[0] == first_export)
        ledger = w.ledgers[origin]
        lock = ledger.find("LOCK", shipment_id=first_export)[-1]
        fixed = dict(lock.payload, waybill=dict(lock.payload["waybill"], weight_kg=batches[batch_id]["weight_kg"]))
        approvers = (f"CUSTOMS-{origin}", f"GI-REG-{origin}", f"GI-REG-{dest}")
        redaction = (origin, ledger.redact(lock.index, fixed, {k: w.shares[origin][k] for k in approvers},
                                           REDACTION_REASON, w.nodes[f"CUSTOMS-{origin}"]))

    log(f"{next(step):>2}. export PDFs: clearance and waybill per shipment, redaction notice")
    for shipment_id, _, origin, _ in shipments:
        export_clearance_pdf(w, certs_dir, origin, shipment_id)
        waybill_pdf(w, certs_dir, origin, shipment_id)
    if redaction:
        redaction_notice_pdf(w, certs_dir, *redaction)

    log(f"{next(step):>2}. JSON-Tiles materialised compliance view")
    tiles = materialize_compliance_view(w.conn, w.coll)

    # outcome per batch
    docs = list(documents_collection(w).find({}, {"_id": 0, "twin": 0}))
    rows = []
    for batch_id, b in batches.items():
        r = oracle[batch_id]
        dup = [msg for oid, (bid, msg) in refused.items() if bid == batch_id]
        if not r.passed:
            result, reasons, outcome = "FAIL", "; ".join(r.reasons), _classify(r.reasons)
        elif dup:
            done = ", ".join(s[0] for s in shipments if s[1] == batch_id)
            result, reasons, outcome = ("FAIL", f"exported once ({done}); second export refused: " + dup[0],
                                        "duplicate_export")
        else:
            result, reasons, outcome = "PASS", "", None
        pdfs = [d for d in docs if d["batch_id"] == batch_id or d["doc_id"] == f"membership_letter:{b['coop_id']}"]
        rows.append({
            "batch_id": batch_id, "origin": b["origin"], "product": b["product"], "created_on": b["created_on"],
            "result": result, "reasons": reasons, "expected": w.expected[batch_id],
            "as_expected": outcome == w.expected[batch_id],
            "exports": [s[0] for s in shipments if s[1] == batch_id], "pdfs": len(pdfs),
        })

    chains = {j: l.verify_chain() for j, l in w.ledgers.items()}
    summary = {
        "sample": sample, "offline": w.offline, "rows": rows, "seconds": round(time.perf_counter() - t0, 1),
        "passed": sum(r["result"] == "PASS" for r in rows), "failed": sum(r["result"] == "FAIL" for r in rows),
        "all_as_expected": all(r["as_expected"] for r in rows),
        "shipments": len(shipments), "pdf_count": len(docs),
        "redaction": f"{redaction[0]} block {redaction[1].payload['target_index']}" if redaction else None,
        "ledgers": {j: {"blocks": len(l.blocks), "problems": chains[j]} for j, l in w.ledgers.items()},
        "tiles": tiles, "data_dir": str(data_dir), "out_dir": str(out_dir),
    }
    log(f"{next(step):>2}. summary report -> {out_dir / 'reports' / 'summary.html'}")
    write_report(summary, out_dir / "reports" / "summary.html")
    set_pipeline_status(w, "complete")
    summary["seconds"] = round(time.perf_counter() - t0, 1)
    w.conn.close()
    return summary


def _classify(reasons: list[str]) -> str:
    text = " ".join(reasons)
    for needle, tag in (("THC", "thc_over_limit"), ("vegetable-tanned", "chrome_tanned"),
                        ("raw lab file", "tampered_lab_file"), ("signer is not a registered", "wrong_signer_role")):
        if needle in text:
            return tag
    return "other"


def print_table(summary: dict, out=sys.stdout) -> None:
    head = f"{'batch':<13} {'from':<4} {'product':<22} {'result':<6} {'as expected':<11} {'PDFs':>4}  reason"
    print(head, file=out)
    print("-" * len(head), file=out)
    for r in summary["rows"]:
        print(f"{r['batch_id']:<13} {r['origin']:<4} {r['product']:<22} {r['result']:<6} "
              f"{'yes' if r['as_expected'] else 'NO':<11} {r['pdfs']:>4}  {r['reasons']}", file=out)
    print("-" * len(head), file=out)
    ok = all(not v["problems"] for v in summary["ledgers"].values())
    print("document store: " + ("OFFLINE MODE (local file store)" if summary["offline"] else "MongoDB Atlas"), file=out)
    print(f"{summary['passed']} passed, {summary['failed']} failed, "
          f"{'all' if summary['all_as_expected'] else 'NOT all'} as expected | {summary['shipments']} shipments, "
          f"{summary['pdf_count']} PDFs | ledgers " + ", ".join(f"{j}: {v['blocks']} blocks" for j, v in summary["ledgers"].items())
          + f" ({'all chains valid' if ok else 'CHAIN PROBLEMS'}) | {summary['seconds']} s", file=out)


def write_report(summary: dict, path: Path) -> None:
    e = html.escape
    rows = "\n".join(
        f"<tr class='{r['result'].lower()}'><td>{e(r['batch_id'])}</td><td>{e(r['origin'])}</td><td>{e(r['product'])}</td>"
        f"<td>{e(r['created_on'])}</td><td>{r['result']}</td><td>{'yes' if r['as_expected'] else 'NO'}</td>"
        f"<td>{e(', '.join(r['exports']))}</td><td>{r['pdfs']}</td><td>{e(r['reasons'])}</td></tr>"
        for r in summary["rows"])
    ledgers = "".join(f"<li>{j}: {v['blocks']} blocks, {'chain valid' if not v['problems'] else e('; '.join(v['problems']))}</li>"
                      for j, v in summary["ledgers"].items())
    tiles = "".join(f"<li><code>{e(p)}</code> {f:.0%}</li>" for p, f in summary["tiles"].items())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>GI-Craft-Tracing pipeline summary (SAMPLE)</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #222; }}
.banner {{ color: #b00020; font-weight: bold; }}
table {{ border-collapse: collapse; font-size: 0.9rem; }}
th, td {{ border: 1px solid #ccc; padding: 0.3rem 0.5rem; text-align: left; }}
tr.fail td {{ background: #fdecea; }}
</style></head><body>
<p class="banner">SAMPLE / DEMONSTRATION. NOT AN OFFICIAL DOCUMENT. All names, codes and figures are fictional.</p>
<h1>Pipeline summary</h1>
<p>{summary['passed']} batches passed, {summary['failed']} failed
({'all as the sample expects' if summary['all_as_expected'] else 'NOT all as the sample expects'}).
{summary['shipments']} shipments committed, {summary['pdf_count']} PDFs issued,
redaction: {e(str(summary['redaction']))}. Run took {summary['seconds']} s.</p>
<h2>Batches</h2>
<table><tr><th>Batch</th><th>Origin</th><th>Product</th><th>Date</th><th>Result</th><th>As expected</th>
<th>Shipments</th><th>PDFs</th><th>Reason</th></tr>
{rows}
</table>
<h2>Ledgers</h2><ul>{ledgers}</ul>
<h2>JSON-Tiles columns extracted to SQL</h2><ul>{tiles}</ul>
</body></html>
""", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run the whole GI-Craft-Tracing pipeline locally.")
    ap.add_argument("--yes", action="store_true", help="replace existing data/ and out/ without asking")
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="out")
    ap.add_argument("--sample", default="full", choices=("core", "full"))
    args = ap.parse_args(argv)
    load_env()
    data_dir, out_dir = Path(args.data), Path(args.out)
    existing = [p for p in [data_dir, *(out_dir / n for n in OUT_SUBDIRS)] if p.exists()]
    if existing and not args.yes:
        print("This replaces the local world and generated files in: " + ", ".join(str(p) for p in existing))
        try:
            answer = input("Continue? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing changed.")
            return 2
    try:
        summary = run_pipeline(data_dir, out_dir, args.sample)
    except (PipelineError, WorldError, LedgerError) as e:
        print(f"error: {e}")
        return 3
    print()
    print_table(summary)
    return 0 if summary["all_as_expected"] else 1


if __name__ == "__main__":
    sys.exit(main())
