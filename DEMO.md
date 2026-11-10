# GI-Craft-Tracing: demo guide

A 10-minute script for presenting the prototype, then a feature-by-feature guide. Every
institution, person and code is fictional; every PDF is stamped SAMPLE / DEMONSTRATION.

Labels used below (PLAN.md section 3):

- **[BLOCKCHAIN]**: a real ledger mechanism: hash-linked blocks, Ed25519 signatures, multi-party
  commit, chameleon-hash redaction, tamper-evident anchoring, append-only storage.
- **[NOT BLOCKCHAIN]**: ordinary database or web features: SQL, MongoDB, the dashboard, PDFs, login.

Say this once, early: *this is a permissioned ledger simulation. There is no network of independent
nodes, no consensus algorithm, no mining or tokens; one program plays every party.*

## Before you start (5 minutes, the day before)

1. Online: `python -m gi_trace.check_atlas` ends with `OK`. Empty the three Atlas ledger collections if
   an earlier run filled them (README, "Starting Atlas over").
2. `python -m gi_trace.run_all --yes`: `21 passed, 9 failed, all as expected`.
3. Three logins exist (`python -m gi_trace.web.create_user`): admin, employee, delivery.
4. Copy one PDF to the desktop and make a tampered twin: open a copy of
   `out\certificates\hemp_lab_assay__HEMP-NP-0001.pdf` in Notepad, change one digit, save.
5. Offline rehearsal: Wi-Fi off, `python -m gi_trace.web`, check the banner reads
   `OFFLINE MODE: data as of ...`. Wi-Fi back on.

## The 10-minute script

| Min | Do | Say |
| --- | --- | --- |
| 0-1 | Terminal: the `run_all` table | 30 craft batches from Nepal, India and Pakistan. 21 pass the dual-compliance check, 9 fail for planted reasons: THC over 0.3 %, leather not vegetable-tanned, a lab file that does not match its certificate, a tanning certificate signed by the wrong party, a second export of the same batch. **[NOT BLOCKCHAIN]** for the SQL and MongoDB data; the checks write their results to the ledger **[BLOCKCHAIN]**. |
| 1-2 | `python -m gi_trace.web`, sign in as **admin**, Overview (30 batches, 22 verified, 8 rejected, 22 shipments, 80 documents) | Counts and charts from Atlas through a read-only user. HEMP-NP-0004 shows as verified: its certificates passed, only its second export was refused. **[NOT BLOCKCHAIN]** |
| 2-3 | Batches → HEMP-NP-0001 | Signed certificate fields, the ledger block that anchors this batch, its shipments and documents. The anchor is **[BLOCKCHAIN]** (tamper-evident anchoring). |
| 3-4 | Shipments | Each export committed only after 4 or 5 parties signed: cooperative, lab or tanning registry, both customs offices. **[BLOCKCHAIN]** (multi-party commit, modelled on MP-SATP). |
| 4-5 | Ledger → NP | Hash-linked blocks, each signed by its writer; "Chain valid". Point at the REDACTION block: three of six regulators corrected a waybill typo with a chameleon hash, and the chain still verifies. **[BLOCKCHAIN]** (hash links, signatures, controlled correction). |
| 5-6 | Verify: upload the original PDF, then the tampered one | VALID, then TAMPERED: the PDF's hash, the issuer's signature and the ledger block must all agree. **[BLOCKCHAIN]** for the ledger check; the PDF itself is **[NOT BLOCKCHAIN]**. |
| 6-7 | Query console: example "Hemp assays over the 0.3 % THC limit", Run (4 rows), then Download CSV | A read-only window into MongoDB for the admin, with a 10-second limit, at most 200 rows and dangerous operators refused. **[NOT BLOCKCHAIN]** |
| 7-8 | Sign out; sign in as **delivery** | Delivery staff see only Shipments and Verify; typing `/query` gives "Not allowed". Same for employee with admin pages. **[NOT BLOCKCHAIN]** (role-based access). |
| 8-9 | Turn Wi-Fi off, Ctrl+C the dashboard, start it again (up to 30 s), sign in as **admin** (a restart signs everyone out), Overview | Same counts from the offline copy, with the banner "OFFLINE MODE: data as of ...". **[NOT BLOCKCHAIN]** |
| 9-10 | Wrap up | Section 7 of PLAN.md: one program holds every key, PDFs are signed by this system's own keys, offline data is not merged back into Atlas. |

## Feature by feature

### Data tiers [NOT BLOCKCHAIN]
SQLite holds artisans, cooperatives, batches, orders, tariffs and THC rules (`sql_store.py`). MongoDB
(Atlas online, a local file offline) holds the signed certificates and the documents (`doc_store.py`).
The two are joined on the indexed `batch_id` (`federated.py`). Mechanism 1, the JSON-Tiles view
(`tiles.py`), copies the certificate fields that appear in most documents into an indexed SQL table.

### Dual-compliance oracle [NOT BLOCKCHAIN] and tamper-evident anchoring [BLOCKCHAIN]
`oracle.py` checks each batch against the rules of its origin and destination (THC limit, tanning,
registered signers, lab file hash). The checking is ordinary code; what makes its result tamper-evident is
that it writes an ATTESTATION and a BATCH_ANCHOR block to the origin's ledger, and the QR code carries the anchor
block's hash.

### Hash-linked, signed ledgers [BLOCKCHAIN]
One ledger per country (NP, IN, PK). Each block stores the previous block's hash and is signed with its
writer's Ed25519 key; only that country's nodes may write. `verify_chain()` reports any edit. A signed
checkpoint of the last block catches blocks cut off the end.
*Show it:* Ledger page, "Chain valid". To show a failure, edit a **copy**, never the live `data/`
(Command Prompt, project folder, after any run):

1. `xcopy /E /I data %TEMP%\gi-demo-data`
2. `set GI_MODE=offline` then `python -m gi_trace.web --data %TEMP%\gi-demo-data --port 5001`
   (offline, so it reads the copy's files rather than Atlas). Sign in as admin at http://127.0.0.1:5001,
   open Ledger, NP: "Chain valid".
3. `notepad %TEMP%\gi-demo-data\ledgers\NP.json`, change one letter of the `batch_id` in the second
   block's payload, save.
4. Reload the Ledger page: "Chain NOT valid", "block 1: payload does not match its chameleon digest", and
   block 1 marked ✗. Close that window when done.

### Append-only storage [BLOCKCHAIN]
Online the blocks are in Atlas under a user that cannot delete, with unique block positions; offline in
`data/ledgers/`. Either way the signed checkpoint catches removed blocks.

### Multi-party border commit [BLOCKCHAIN]
`handshake.py`: an export locks the batch, collects 4 or 5 signatures (5 when a batch has both hemp and
leather), and only then commits on both the origin and destination ledgers. Missing signatures abort it.
Signatures are saved as they arrive, so an interrupted shipment finishes after a restart without re-signing.
*Show it:* Shipments page (22 COMMITTED). HEMP-NP-0004's refused second export never got a lock, so
it is not listed there: show the `run_all` table row ("second export refused") or its batch page, which
lists one shipment.

### Controlled correction (chameleon hash) [BLOCKCHAIN]
`chameleon.py`: each ledger's trapdoor is split 3-of-6 among the regulators. Three shares rewrite one
block's payload without breaking the hash links, and a REDACTION block records who approved it.
*Show it:* Ledger NP, the REDACTION block; Documents, the redaction notice PDF.

### Documents, PDFs, QR codes and Verify [NOT BLOCKCHAIN], checked against the ledger [BLOCKCHAIN]
Each PDF has a JSON twin, the twin's SHA-256, the issuer's signature and a DOCUMENT block on the ledger.
Verify (all roles) accepts a PDF or the pasted text of a QR code (scan with any phone app and paste; there
is no image decoder) and answers VALID, TAMPERED or UNKNOWN. The PDFs and QR images are **[NOT
BLOCKCHAIN]**; the ledger block and signature they are checked against are **[BLOCKCHAIN]**.
The PDFs are signed with this project's own keys, not a recognised certificate authority.

### Dashboard, roles, login [NOT BLOCKCHAIN]
Flask on 127.0.0.1 only. Three roles (table in the README). Passwords are stored as scrypt hashes; five
wrong passwords lock an account for 5 minutes; every sign-in and lock is in the audit log (admin). Forms
carry a CSRF token; cookies are HttpOnly and SameSite=Strict. Restarting the dashboard signs everyone out.

### Query console [NOT BLOCKCHAIN]
Admin only. Read-only queries on `certificates` and `documents`, at most 200 rows, 10-second limit,
`$where`, `$function`, `$accumulator`, `$out` and `$merge` refused. CSV export neutralises spreadsheet
formulas. Four saved examples: rejected certificates; hemp assays over 0.3 % THC; all documents for
HEMP-NP-0001; export clearances on the Nepal ledger.

### Online and offline [NOT BLOCKCHAIN]
With Atlas unreachable for 30 seconds the app uses local files and shows OFFLINE MODE. After every online
run the Atlas data is copied into `data/`, so the dashboard can show the same data with Wi-Fi off.
Data written offline stays local.

## If something goes wrong during the demo

| You see | Do |
| --- | --- |
| "Atlas already holds ledgers" | README, "Starting Atlas over" (2 minutes), or demo offline. |
| NO DATA banner | Run `python -m gi_trace.run_all --yes`, reload. |
| "A run is in progress or was interrupted" | A `data.new` folder was left; the next `run_all` removes it. The dashboard keeps showing the previous data. |
| Signed out after restarting the dashboard | Expected: sign in again. |
| Download says "Not found" | The last run failed after emptying `out/`; run `run_all` again. |
| Dashboard start waits 30 s | It is trying Atlas; with Wi-Fi off it then opens the offline copy. |
