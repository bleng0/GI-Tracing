# GI-Craft-Tracing

Working prototype of *Multi-Jurisdictional Traceability for South Asian GI Crafts*:
Himalayan hemp and Peshawari chappal batches moving between Nepal, India and Pakistan.

Have Docker Desktop but not Python? Skip the setup below and go straight to
[Dashboard in Docker](#dashboard-in-docker-fresh-clone-windows-powershell).

## Set up (once, about 3 minutes, plus 12 for the optional tests)

Windows 10/11, Python 3.10 or newer (tested with 3.14). In **Command Prompt**, in the project folder:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

In PowerShell, `activate` is blocked by the default execution policy; run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or use Command Prompt.

Optional: `python -m pytest` runs 607 tests in about 12 minutes on Windows. They always run offline and never touch
Atlas, `.env`, `data/` or `out/` (they work in temporary folders).

**Atlas (online use only).** `.env` must hold `ATLAS_WRITE_URI` (the pipeline's user) and
`ATLAS_READ_URI` (the dashboard's read-only user). Without them everything runs offline. To create them
once (about 10 minutes):

1. In the Atlas website, create a free M0 cluster, then under the project's Access Manager create an API
   key with the Project Owner role.
2. Put three lines in `.env` (a text file in the project folder): `ATLAS_PROJECT_ID=...` (Project
   Settings), `ATLAS_PUBLIC_KEY=...` and `ATLAS_PRIVATE_KEY=...`.
3. `python -m gi_trace.atlas_setup`. It adds this PC's public IP to the network access list, creates the
   writer and read-only users, the collections and indexes, checks the permissions and ends with `DONE`.
   It appends the two URIs to `.env` and never prints them.
4. Delete the API key in Atlas and remove the three lines from `.env`.

`python -m gi_trace.check_atlas` checks the connection: it connects, lists the collections, writes and
reads one test document, and ends with `OK: Atlas reachable, write and read work`. **On a different
network** (school, venue) Atlas refuses connections until you add that network's IP under Network Access
in the Atlas website; run `check_atlas` there before the demo.

**Dashboard accounts.** Create the three logins once; each command asks for the username, the role
and a hidden password (8 or more characters). They are kept in `data/users.db`, which runs never replace.

```bat
python -m gi_trace.web.create_user
```

Answer `admin`, `employee` and `delivery` as the role in three runs.

## Demo, online (MongoDB Atlas)

1. `python -m gi_trace.check_atlas` must end with `OK: Atlas reachable ...`.
2. Atlas must hold no ledgers from an earlier run (see "Starting Atlas over" below), otherwise step 3
   stops with "Atlas already holds ledgers".
3. `python -m gi_trace.run_all --yes` (about 15 s offline, longer online). It prints
   `document store: MongoDB Atlas`, a table of 30 batches and
   `21 passed, 9 failed, all as expected | 22 shipments, 80 PDFs | ledgers NP: 79 blocks, IN: 45 blocks, PK: 69 blocks (all chains valid)`.
   At the end it copies the Atlas data into `data/` (the offline snapshot, below).
4. `python -m gi_trace.web`, then open http://127.0.0.1:5000 and sign in. No yellow
   OFFLINE banner means the pages read Atlas through the read-only user. The Overview shows
   30 batches, 22 verified, 8 rejected, 22 shipments, 80 documents (HEMP-NP-0004 is counted as
   verified: its certificates passed; only its second export was refused, which makes it one of the
   9 failures in the `run_all` table).

## Demo, offline (no Wi-Fi)

Two ways, both with the same dashboard:

- **Snapshot of the last online run** (best for showing "the same data, offline"): after an online
  `run_all`, turn Wi-Fi off and run only `python -m gi_trace.web`. It waits up to 30 s for Atlas, prints
  `Data: OFFLINE (local copy)`, and every page shows
  `OFFLINE MODE: data as of <time of the last online run> (copy taken after the last online run)`.
  The counts are the same as online. `python -m gi_trace.snapshot` refreshes this copy by hand while online.
- **A fresh offline run:** `set GI_MODE=offline` (PowerShell: `$env:GI_MODE="offline"`), then
  `python -m gi_trace.run_all --yes` and `python -m gi_trace.web`. The run prints
  `document store: OFFLINE MODE (local file store)` and the same 21 / 9 table; the banner reads
  `OFFLINE MODE: local data from the last offline run.` This replaces the snapshot in `data/`.
  `GI_MODE` stays set in that window: undo it with `set GI_MODE=` (PowerShell: `Remove-Item Env:GI_MODE`)
  or open a new window before running online again.
  With `GI_MODE=auto` (the default) and Wi-Fi off, `run_all` waits 30 s, prints
  `OFFLINE MODE: Atlas not reachable within 30 s`, and does the same.

If Atlas stops answering while the dashboard runs, it switches to the snapshot by itself and tries
Atlas again every five minutes.

## Dashboard in Docker (fresh clone, Windows PowerShell)

Use this path if you have Docker Desktop and do **not** want to install Python locally. All commands below
run from the project folder. They use the offline sample world, so no Atlas account or `.env` is needed.

**1. Install and check Docker.** Install Docker Desktop, start it, then:

```powershell
docker compose version
```

**2. Make the two folders the container will write to.** A Git clone does not contain `data/` or `out/`;
they hold the database, keys, PDFs and accounts and must stay out of Git.

```powershell
New-Item -ItemType Directory -Force data
New-Item -ItemType Directory -Force out
```

**3. Choose offline mode for this shell and build the image.**

```powershell
$env:GI_MODE = "offline"
docker compose build
```

`GI_MODE` is passed into the container by `docker-compose.yml`. It stays set until you close the
PowerShell window. In Command Prompt use `set GI_MODE=offline` instead.

**4. Create the sample world.** This is a one-off container that exits when the pipeline finishes. It
writes into the `data/` and `out/` folders you made in step 2.

```powershell
docker compose run --rm web python -m gi_trace.run_all --yes
```

**5. Create the three logins.** Run this next command three times. Answer `admin`, `employee` and
`delivery` as the role, with a different password of at least 8 characters each time. The password is
hidden while you type. The accounts are saved in `data/users.db`, which is why they are not in Git.

```powershell
docker compose run --rm web python -m gi_trace.web.create_user
```

If you already started the dashboard and need another account later, this also works:

```powershell
docker compose exec web python -m gi_trace.web.create_user
```

**6. Start the dashboard.**

```powershell
docker compose up -d
docker compose logs -f web
```

Wait for `Running on http://127.0.0.1:5000`, then open **http://127.0.0.1:5000** and sign in. Ctrl+C
closes the log view but leaves the container running. Only the `admin` account can open `/workbench`.

**7. Stop and start again.**

```powershell
docker compose down
docker compose up -d
```

Your data and accounts survive because `data/` and `out/` are mounted from this folder. After a code
change, rebuild before starting: `docker compose up --build -d`.

If you later want Atlas instead of the offline sample, create the project's `.env` with the Atlas URIs
before starting the container, then use `docker compose down` followed by
`docker compose up -d` in a fresh shell (leave `GI_MODE` unset so the default `auto` applies). `.env`
is ignored by Git and is never copied into the image.

The container listens on `0.0.0.0:5000` internally; Compose publishes it as `127.0.0.1:5000:5000`, so it
is reachable only from this PC. If port 5000 is busy, change the left-hand side of that mapping in
`docker-compose.yml`.

## Commands

| Command | What it does |
| --- | --- |
| `python -m gi_trace.run_all [--yes] [--sample core]` | the whole pipeline; asks before replacing `data/` and `out/` |
| `python -m gi_trace.web [--port 5000]` | the dashboard on http://127.0.0.1:5000 (this PC only); Ctrl+C stops it |
| `python -m gi_trace.web.create_user` | add a dashboard account (hidden password) |
| `python -m gi_trace.snapshot` | copy the Atlas data into `data/` for offline use (run_all does this itself) |
| `python -m gi_trace.check_atlas` | Atlas reachable? write and read one test document |
| `python -m gi_trace.docgen.verify out\certificates` | VALID / TAMPERED / UNKNOWN per PDF |
| `python -m gi_trace.demo` | short in-memory walkthrough of the four mechanisms |
| `python -m pytest` | 607 tests, offline |
| `docker compose build` | build the dashboard image (Python 3.12 slim) |
| `docker compose run --rm web python -m gi_trace.run_all --yes` | create the sample world in the mounted `data/` and `out/` |
| `docker compose run --rm web python -m gi_trace.web.create_user` | add a dashboard account inside the container |
| `docker compose up -d` / `docker compose down` | start / stop the dashboard on http://127.0.0.1:5000 (this PC only) |

`GI_MODE` picks where documents and ledgers live: `auto` (default) uses Atlas if it answers within
30 seconds and the local files otherwise; `atlas` never falls back; `offline` never tries Atlas. A wrong
password or malformed URI in `.env` is an error, not offline mode.

## Dashboard

Pages per role (PLAN.md section 4). Every route checks the role: other roles get "Not allowed" (403),
signed-out visitors go to the sign-in page. Five wrong passwords lock an account for 5 minutes.
Restarting the dashboard signs everyone out.

| Page | admin | employee | delivery |
| --- | --- | --- | --- |
| Overview (counts, charts; order values for admin only) | yes | yes | |
| Batches and certificates | yes | yes | |
| Shipments | yes | yes | yes |
| Documents, download (waybills, which print prices: admin only) | yes | yes | |
| Verify a PDF or pasted QR text | yes | yes | yes |
| Query console | yes | | |
| Query Workbench (SQL + NoSQL) | yes | | |
| Ledger viewer and chain check | yes | | |
| Audit log, users | yes | | |

**Query console.** Pick `certificates` or `documents`, type a JSON filter, optional projection and sort
(`{"field": 1}` or `-1`) and a limit of 1 to 200, then Run (table) or Download CSV. Four examples fill
the form. Read only. Refused: `$where`, `$function`, `$accumulator`, `$out`, `$merge` (anywhere, any
letter case, also as text values such as `"$out"`). Atlas stops a query after 10 s; offline each query
runs in its own Python process that is stopped after 10 s (about 1 s extra to start on Windows). CSV cells
that a spreadsheet would treat as a formula start with an apostrophe.

**Query Workbench.** Admin only. The split editor has line numbers, Tab indentation, Ctrl/⌘+Enter to run,
syntax colours and sample queries. Its left pane is the editor; the right pane lists the readable tables
(SQL) or collections (NoSQL). SQL mode accepts one `SELECT`/`WITH` query against `gi_trace.db` only — no
semicolons, no `users.db`, no attaches or writes; SQLite opens the file read-only, with `query_only`, a
statement authorizer and a 10 s stop. NoSQL mode offers `find` and read-only `aggregate` on `certificates`,
`documents`, `ledger_blocks` and `ledger_heads`; stages that reach another collection or run code are
refused. Both modes cap results at 200 rows, show the count and elapsed milliseconds, and offer a table or
JSON view and CSV/JSON downloads. The old Query console is unchanged.

**Ledger viewer.** Choose NP, IN or PK. It reads the blocks from storage on every visit (about 1 s for 80
blocks) and shows "Chain valid" or each problem, with the affected blocks marked ✗.

## What is in data/ and out/

- `data/` is the latest world: `gi_trace.db` (SQLite), `keys/`, `labfiles/`, `documents.json` and
  `ledgers/` (the offline data, or after an online run the offline copy of Atlas), `world.json` (what it is and, after an online run, `snapshot_at`),
  and `users.db` (accounts and audit log). Keys on disk are a demo convenience, see below.
- `data.new/` is where `run_all` builds the next world. It replaces `data/` only when the whole run has
  succeeded, so a failed run leaves the previous world, and the dashboard showing it, unchanged; the
  dashboard then says a run "is in progress or was interrupted". The next `run_all` removes a leftover
  `data.new/`; you can also delete it by hand.
- `out/` holds the PDFs, QR codes, lab files and `reports/summary.html`. It is emptied at the start of a
  run, so after a failed run downloads give "Not found" until a run succeeds.

## Starting Atlas over

Seeding clears the Atlas `certificates` and `documents` collections, but the pipeline's user cannot
delete ledger data, so a second online `run_all` stops with "Atlas already holds ledgers". To start over
(about 2 minutes): sign in to the Atlas website, open Data Explorer, database `gi_trace`, and in each of
`ledger_blocks`, `ledger_heads` and `pending_signatures` delete all documents (filter `{}`). Delete the
documents; do not drop the collections, because that also removes the unique indexes, which the
pipeline's user cannot recreate.

## Keys and generated files

**Keys on disk are a demo convenience, not key custody.** `data/keys/` holds every node's
private signing key, the key that signs each ledger's checkpoint (never sent to Atlas), and all
six regulators' Shamir shares of each ledger's redaction trapdoor (the trapdoor itself is never written). In real use each share would be held by a different
regulator; here any three files in `data/keys/shares/` are enough to redact a ledger.

`run_all` fills `out/`: `labfiles/` (raw lab CSVs), `certificates/` (PDFs: hemp lab assays,
tanning attestations, cooperative membership letters, export clearances, customs waybills and one
redaction notice), `qr/` (SVG and PNG per verified batch) and `reports/summary.html`. Every PDF is
stamped "SAMPLE / DEMONSTRATION. NOT AN OFFICIAL DOCUMENT"; all institutions, people and codes are
fictional. Each PDF has a JSON twin in the `documents` collection, the twin's SHA-256 (printed and
embedded in the PDF metadata), the issuer's Ed25519 signature, a QR code, and a DOCUMENT block on
the ledger. Editing one byte of a PDF or of its twin makes `verify` report TAMPERED. The PDFs are
signed by this system's own keys, not by a recognised certificate authority.

## Where each piece lives

| Tier / mechanism | File | Source paper |
| --- | --- | --- |
| Relational tier (artisans, coops, batches, orders, tariffs, THC rules) | `gi_trace/sql_store.py` | Bender et al. 2022 |
| Document tier (signed lab / tanning certificates) | `gi_trace/doc_store.py` | Bender et al. 2022 |
| SQL ↔ Mongo join, restricted to indexed `batch_id` | `gi_trace/federated.py` | Li & Gu 2018 |
| **1** JSON-Tiles materialised compliance view | `gi_trace/tiles.py` | Durner et al. 2021 |
| **2** Multi-signature border state machine | `gi_trace/handshake.py` | Hargreaves et al. 2023 |
| **3** Threshold chameleon-hash redaction | `gi_trace/chameleon.py`, `gi_trace/ledger.py` | Jia et al. 2024 |
| **4** Dual-compliance oracle + QR code | `gi_trace/oracle.py` | Perinelli et al. 2025 |
| Sample world (3 ledgers; `core`: 10 nodes, 3 batches; `full`: 13 nodes, 30 batches) | `gi_trace/seed.py` | — |
| PDFs with JSON twins, QR codes, lab files, `verify` | `gi_trace/docgen/` | — |
| Full pipeline CLI and summary report | `gi_trace/run_all.py` | — |
| Persistence: document store, keys, ledgers | `gi_trace/storage.py`, `gi_trace/keystore.py`, `gi_trace/ledger_io.py` | — |
| Atlas or offline store, connection check | `gi_trace/doc_store.py`, `gi_trace/check_atlas.py` | — |
| Ledger store (files or Atlas), signed checkpoints, pending handshake signatures | `gi_trace/ledger_store.py` | — |
| Dashboard: pages, roles, CSRF, offline switch | `gi_trace/web/app.py`, `gi_trace/web/data.py` | — |
| Local dashboard container | `Dockerfile`, `.dockerignore`, `docker-compose.yml` | — |
| Accounts, lockout, audit log | `gi_trace/web/users.py`, `gi_trace/web/create_user.py` | — |
| Query console, Query Workbench (offline worker) and ledger viewer | `gi_trace/web/query.py`, `gi_trace/web/query_worker.py`, `gi_trace/web/sql_workbench.py`, `gi_trace/web/workbench.py`, `gi_trace/web/ledger_view.py` | — |
| Offline snapshot of the Atlas data | `gi_trace/snapshot.py` | — |
| Access-control matrix and break-it check | `tests/test_roles.py`, `tests/test_break_it.py` | — |

## Design decisions that differ from, or go beyond, the spec

1. **Signers depend on the batch's materials.** The spec names four fixed parties
   including a Botanical Lab. A leather-only batch has no lab test, so it uses the
   Tanning Registry in that slot. A batch with both materials needs five signers.
2. **Mechanism 1's threshold is the share of documents containing a key** (as in JSON Tiles),
   not "the top 80 % of keys". Nested objects are flattened (`origin_gps.lat`); arrays stay in MongoDB.
   The view is rebuilt in full on each call, so it is stale until you re-run it.
3. **Key-exposure-free chameleon hash** (Ateniese & de Medeiros 2004) instead of the
   textbook `g^m·h^r` one. With the textbook hash, one public redaction reveals the
   trapdoor, which would defeat the threshold split.
4. **Composite foreign key** `batches(artisan_id, coop_id) → artisans`: a batch naming an
   artisan who is not a member of the issuing cooperative is rejected by the database.
5. **Ledger membership** is per jurisdiction: only that country's nodes can write to its ledger.
   Each ledger's trapdoor is split 3-of-6 across all six regulators (3 customs + 3 GI registries).

## Known limitations

- Ledgers are kept in Atlas (online) or JSON files (offline): no consensus or networking.
  Every load re-verifies the chain and a signed checkpoint of its latest block, so blocks cut off
  the end are detected. Rolling a ledger and its checkpoint back together to an earlier signed
  state is not.
- The trapdoor is reconstructed in one place to forge. A true threshold scheme would
  forge without ever rebuilding it.
- Handshake signatures are stored as they arrive, so a shipment interrupted before commit can
  be finished after a restart without re-signing. There is still no crash recovery between the
  destination MINT and the origin TRANSFERRED write (MP-SATP uses backup gateways for this).
- ABPRE encryption of artisan pricing (Jia et al.) and the Redis transit-state cache
  (Li & Gu adaptation) are not implemented. The spec describes both but does not list
  them among the four mechanisms.
- THC limits (0.3 %), tariff rates, GI codes and names in `seed.py` are placeholders.
- Dashboard: Verify reads a QR code only as pasted text (scan it with a phone app); there is no QR image
  decoder. Restarting the dashboard signs everyone out (a new session key each start). A user's role is
  read at sign-in, so a role changed later applies from the next sign-in.
- Offline query-console queries run in a separate Python process: about 1 s to start on Windows.
- `check_mongo.py` and `setup_mongodb.ps1` are from the earlier local-MongoDB setup and are not part of
  the Atlas demo.
