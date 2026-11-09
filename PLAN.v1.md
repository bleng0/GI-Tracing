# GI-Craft-Tracing: implementation plan (local first, then MongoDB)

Status of the repo today: 32 passing tests; SQL, document, ledger, oracle, handshake and redaction logic exist, but everything is in memory, there are no PDFs, no persistence and no dashboard.

## 1. End goals

1. **One command runs the whole story locally**, start to end, with files kept on disk: `python -m gi_trace.run_all`.
2. **Every certificate and document is generated**, as a PDF with a machine-readable twin (JSON in the document store), each carrying a hash, a signature and a QR code.
3. **An admin-only dashboard** (browser, localhost) where an admin types a query, runs it against the document store, and sees the rows as a table and charts. Non-admins and anonymous users get nothing.
4. **Only after you sign off on the local build**, swap the local document store for a real MongoDB with a one-line config change (`MONGO_URI`).

## 2. Assumptions (tell me if any is wrong)

1. "Dashboard or mini window" means a **local web dashboard** (Flask, opens at `http://127.0.0.1:5000`). A Tkinter desktop window is the alternative (it would add about 3 hours).
2. "Locally" means no MongoDB server yet. Phase 1 adds a **file-backed document store** with the same interface as the MongoDB one, so Phase 8 is a swap, not a rewrite.
3. All institutions, labs, registries and GI codes are **fictional**, and every PDF is stamped "SAMPLE / DEMONSTRATION. NOT AN OFFICIAL DOCUMENT", so none can pass as a real certificate.
4. "Admin access only" means login with a hashed password, role check on every route, bound to 127.0.0.1. Not internet-grade security.
5. Windows 10/11, Python 3.10+.

## 3. Requirements

| Item | Needed for | Notes |
| --- | --- | --- |
| Flask, Werkzeug | dashboard, password hashing | pip |
| reportlab | PDF generation | pip, pure Python |
| pypdf | reading PDFs back in tests and verification | pip |
| qrcode (already) | QR codes in PDFs | |
| cryptography, mongomock, pymongo (already) | signing, document store | |
| Chart.js (one vendored `.js` file) | dashboard charts | copied into `static/`, no internet needed at run time |
| MongoDB Community Server 7+ and MongoDB Compass | **Phase 8 only** | Windows installer, about 700 MB |

## 4. Target folder layout

```
GI-Craft-Tracing/
  gi_trace/            (existing modules, plus the new ones below)
    storage.py         DocStore interface: FileDocStore now, MongoDocStore in Phase 8
    keystore.py        persists node keys, ledger keys and Shamir shares
    ledger_io.py       saves/loads ledgers as JSON
    docgen/            lab_files.py, certificates.py (PDF), qr.py, verify.py
    run_all.py         full pipeline
    web/               app.py, auth.py, queries.py, templates/, static/
  data/                (generated) sqlite file, document store, ledgers, keys
  out/
    labfiles/          raw lab CSVs
    certificates/      hemp assay, tanning attestation, export clearance, waybill, redaction notice (PDF)
    qr/                SVG and PNG codes
    reports/           pipeline summary (HTML or PDF)
  tests/
  PLAN.md  README.md
```

## 5. Phases

### Phase 0. Lock decisions (0.5 h)
1. Confirm the five assumptions above.
2. Add Flask, reportlab and pypdf to `requirements.txt`.
3. Add `.gitignore` for `data/`, `out/`, `.venv/`, keys.
Done when: `pip install -r requirements.txt` works on your PC.

### Phase 1. Persistence (3 h)
1. `DocStore` interface with the calls the code already uses: `insert_one`, `find`, `find_one`, `update_one`, `update_many`, `delete_many`, `create_index`.
2. `FileDocStore`: mongomock underneath, snapshot to `data/documents.json` after each write and reload on start. No separate server needed.
3. SQLite moves from `:memory:` to `data/gi_trace.db`.
4. `keystore.py`: saves node keys, per-ledger chameleon public keys and Shamir shares to `data/keys/`. The trapdoor itself is never stored. Real use would keep each share with a different regulator; locally they sit in one folder and the README says so.
5. `ledger_io.py`: ledgers saved to and loaded from `data/ledgers/<NP|IN|PK>.json`; the chain is re-verified on load.
Done when: run the pipeline, close Python, reopen it, and the data and ledgers are still there and `verify_chain()` is clean.

### Phase 2. Larger sample data (2 h)
1. Grow `seed.py` to about 30 batches across NP, IN and PK: hemp yarn and Peshawari chappal, plus 2 mixed batches.
2. Deliberately include failures: THC over limit (4 batches), chrome-tanned leather (2), tampered lab file (1), certificate signed by the wrong role (1), duplicate export attempt (1).
3. Spread dates over about 6 months so the dashboard has a time series.
4. Make the seed deterministic (fixed random seed) so demos and tests repeat.
Done when: the seed produces the same 30 batches every run, with about 20 passing and about 10 failing for known reasons.

### Phase 3. Document generation (5 h)
Each PDF gets: SAMPLE watermark, title, issuer node, batch and artisan details, key values, a QR code, the SHA-256 of the machine-readable JSON, and the issuer's Ed25519 signature (hex, shortened).
1. **Raw lab files**: `out/labfiles/<batch>.csv` (THC, CBD, moisture, peaks); the existing hash check ties the CSV to the certificate.
2. **Hemp lab assay certificate (PDF)**, issued by a Botanical Lab node.
3. **Tanning and heritage attestation (PDF)**, issued by a Tanning Registry node.
4. **Cooperative GI membership letter (PDF)**, one per cooperative.
5. **Export clearance certificate (PDF)**, created after the border handshake commits; lists the four or five signers and the ledger block hashes on both sides.
6. **Customs waybill (PDF)**, from the LOCK payload (weights, packages, tariff).
7. **Redaction notice (PDF)**, created on a chameleon-hash correction: old and new payload hashes, approvers, reason.
8. `docgen/verify.py`: given a PDF, extract the embedded JSON hash and check it against the ledger and the document store, and report one of VALID, TAMPERED or UNKNOWN.
9. Generated PDFs are also registered in the document store (`documents` collection: path, type, batch_id, sha256, issued_at) so the dashboard can list and download them.
Done when: a batch that passes the pipeline has 4 to 6 PDFs on disk; editing one byte of a PDF or its JSON makes `verify.py` report TAMPERED.

### Phase 4. End-to-end pipeline CLI (2 h)
`python -m gi_trace.run_all` does, in order: reset local data (asks first), seed, lab files, certificates, oracle, QR codes, handshake for every passing batch, one deliberate waybill redaction, export PDFs, JSON-Tiles view, summary report in `out/reports/`.
Done when: one command runs in under about 60 seconds, prints a pass/fail table, and fills `out/` as in section 4.

### Phase 5. Admin-only dashboard (6 h)
Screens:
1. **Login**: admin only. First admin is created with `python -m gi_trace.web.create_admin` (password typed at the prompt, stored as a Werkzeug scrypt hash in SQLite). No default password in code.
2. **Overview**: counts (batches, verified, rejected, shipments by country), THC distribution chart, batches over time, rejection reasons.
3. **Query console** (the main feature): choose a collection (`certificates`, `documents`), enter a filter as JSON, optional projection, sort and limit; results appear as a table with a raw-JSON toggle and CSV download. Several saved example queries are one click away.
4. **Ledger viewer**: pick NP, IN or PK, browse blocks, see "chain valid" or the list of problems, filter by tx type and batch.
5. **Documents**: list generated PDFs, download them, and run Verify.
6. **Audit log**: every login, failed login and query the admin ran (who, when, query text).

Access control and safety:
1. Every route requires a logged-in session with role `admin`; others get 403, anonymous users are redirected to login.
2. Server binds to `127.0.0.1` only. CSRF token on forms, session cookie `HttpOnly` and `SameSite=Strict`, lockout for 5 minutes after 5 failed logins.
3. The query console is **read-only and allow-listed**: only `find`, only the two collections above, operators `$where`, `$function`, `$accumulator`, `$out` and `$merge` rejected, limit capped at 200, 5-second time limit. Queries are parsed as JSON, never evaluated as code.

Example queries the demo will show:
- `{"cert_type": "hemp_lab_assay", "assay.thc_pct": {"$gt": 0.3}}`: hemp that failed the THC limit
- `{"tanning.method": "vegetable"}`: vegetable-tanned leather
- `{"compliance_status": "rejected"}`: everything the oracle refused
- `{"origin_gps.lat": {"$gt": 30}}`: Pakistan-origin certificates

Done when: logged out you cannot reach any page or API route; logged in as admin the four example queries return the right rows; a non-admin account is refused everywhere.

### Phase 6. Tests (3 h)
1. Keep the existing 32.
2. Add tests for: persistence round-trip, PDF contents (via pypdf), tamper detection in `verify.py`, deterministic seed counts, query allow-list (each banned operator refused), every dashboard route returning 401/403 for non-admins, login lockout, audit log entries.
3. Re-run the "break it on purpose" check on the new access control (remove the role check, confirm tests fail).
Done when: all tests pass on your PC and the access-control tests fail when the check is removed.

### Phase 7. Local acceptance and demo (1.5 h)
1. Fresh clone, fresh venv: README steps run from zero to dashboard in under 10 minutes.
2. Write `DEMO.md`: a 10-minute script (run pipeline, open dashboard, run the four queries, tamper with a PDF, show the failure, redact a waybill, show the chain still valid).
3. **You review and sign off. Nothing in Phase 8 starts before that.**

### Phase 8. MongoDB (3 h, after sign-off)
1. Install MongoDB Community and Compass on Windows (or create a free Atlas cluster, your choice).
2. `MongoDocStore` with the same interface; select with `MONGO_URI`; add the indexes (`batch_id`, `cert_type`, `compliance_status`).
3. One-off script `migrate_local_to_mongo.py` copies `data/documents.json` into MongoDB, then checks counts match.
4. Create a **read-only MongoDB user** for the dashboard, and a separate write user for the pipeline.
5. Re-run the full test suite and the demo against real MongoDB; fix operator differences between mongomock and MongoDB if any appear.
Done when: the same dashboard queries return identical rows from real MongoDB, and you can see the same data in Compass.

## 6. Time summary

| Phase | Estimate |
| --- | --- |
| 0 Decisions | 0.5 h |
| 1 Persistence | 3 h |
| 2 Sample data | 2 h |
| 3 Documents and PDFs | 5 h |
| 4 Pipeline CLI | 2 h |
| 5 Dashboard | 6 h |
| 6 Tests | 3 h |
| 7 Acceptance | 1.5 h |
| **Local total** | **23 h** |
| 8 MongoDB | 3 h |
| **Grand total** | **26 h** |

## 7. Expectations and known limits

1. Ledgers remain in-process JSON files: no real multi-node network, consensus or crash recovery (stated in the README).
2. Local key storage is a demo convenience, not production custody.
3. PDFs are signed by our own keys, not by recognised certificate authorities; they prove tamper-evidence inside this system only.
4. The admin console is for a single local admin; multi-user roles, password reset and HTTPS are out of scope.
5. Not included: the ABPRE encryption of artisan pricing and the Redis cache from the handoff document. They can be added after Phase 7 if your evaluators expect them (about 4 h each).
