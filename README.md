# GI-Craft-Tracing

Working prototype of *Multi-Jurisdictional Traceability for South Asian GI Crafts*:
Himalayan hemp and Peshawari chappal batches moving between Nepal, India and Pakistan.

This repository runs **only through Docker Compose**. The runtime and every dependency live inside the
image; you do not need Python, pip, or a virtual environment on your machine. Every command-line entry
point checks for the container flag, so a local `python -m ...` exits with a Docker reminder instead of
running.

## Requirements

- Windows 10/11 with Docker Desktop installed and running (or Docker Engine with Compose on Linux/macOS).
- Git, to clone or update the repository.

That is all. The commands below are PowerShell; on Linux/macOS use the same `docker compose` lines and
set the environment variable with `export GI_MODE=offline`.

## Atlas credentials (online mode only)

A fresh clone works offline and needs no Atlas account. To use MongoDB Atlas instead, create `.env` in
the project folder with two lines:

```text
ATLAS_WRITE_URI=<pipeline user URI>
ATLAS_READ_URI=<dashboard read-only user URI>
```

Create those users in Atlas; the read-only user must not be able to write. `.env` is ignored by Git and is
never copied into the image. Compose reads it when the container is created, so after changing it run
`docker compose down` and then `docker compose up -d`. Check the connection with:

```powershell
docker compose run --rm web python -m gi_trace.check_atlas
```

It ends with `OK: Atlas reachable, write and read work`. On a different network, add that network's IP
under Network Access in Atlas before running the check.

Without those two URIs the dashboard uses the local snapshot, and a fresh offline run works normally.

## Run the project with Docker (fresh clone)

All commands run from the project folder. The first pass uses the offline sample world, so no Atlas
account or `.env` is needed.

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
writes into the `data/` and `out/` folders you made in step 2. It ends with a line like
`21 passed, 9 failed, all as expected | 22 shipments, 80 PDFs | ... (all chains valid)`.

```powershell
docker compose run --rm web python -m gi_trace.run_all --yes
```

For a smaller world with 3 batches instead of 30, add `--sample core`. Re-running this command replaces
`data/` and `out/` but never `data/users.db`.

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

With the offline sample the banner reads `OFFLINE MODE: local data from the last offline run` and the
Overview shows 30 batches, 22 verified, 8 rejected, 22 shipments and 80 documents. With Atlas and no
OFFLINE banner, the same counts come from the read-only user.

**7. Stop and start again.**

```powershell
docker compose down
docker compose up -d
```

Your data and accounts survive because `data/` and `out/` are mounted from this folder. After a code
change, rebuild before starting: `docker compose up --build -d`.

To switch to Atlas later, follow [Atlas credentials](#atlas-credentials-online-mode-only): create `.env`
with the two URIs, then `docker compose down` and `docker compose up -d` from a shell where `GI_MODE` is
unset. Compose passes the URIs and `GI_MODE` into the container.

The container listens on `0.0.0.0:5000` internally; Compose publishes it as `127.0.0.1:5000:5000`, so it
is reachable only from this PC. If port 5000 is busy, change the left-hand side of that mapping in
`docker-compose.yml`.

## Commands

Every command runs inside the `web` container. `run` starts a one-off container for a task that exits;
`exec` targets the already-running dashboard.

| Command | What it does |
| --- | --- |
| `docker compose build` | build the image with all runtime dependencies preinstalled |
| `docker compose up -d` / `docker compose down` | start / stop the dashboard on http://127.0.0.1:5000 (this PC only) |
| `docker compose logs -f web` | follow dashboard logs; Ctrl+C leaves the container running |
| `docker compose run --rm web python -m gi_trace.run_all --yes` | create the sample world in `data/` and `out/` |
| `docker compose run --rm web python -m gi_trace.run_all --yes --sample core` | smaller 3-batch sample |
| `docker compose run --rm web python -m gi_trace.web.create_user` | add a dashboard account (hidden password) |
| `docker compose exec web python -m gi_trace.web.create_user` | add an account while the dashboard is running |
| `docker compose run --rm web python -m gi_trace.check_atlas` | check Atlas connectivity and permissions |
| `docker compose run --rm web python -m gi_trace.atlas_setup` | one-time Atlas users, collections and indexes (writes the URIs to `.env`) |
| `docker compose run --rm web python -m gi_trace.snapshot` | copy Atlas data into `data/` for offline use |
| `docker compose run --rm web python -m gi_trace.docgen.verify out/certificates` | VALID / TAMPERED / UNKNOWN per PDF |
| `docker compose run --rm web python -m gi_trace.demo` | short in-memory walkthrough of the four mechanisms |

The source tree contains 610 tests used by CI; the runtime image deliberately excludes `tests/`.

`GI_MODE` is set for the container by Compose from your shell: `offline` never tries Atlas, `atlas` never
falls back, and `auto` (the default) uses Atlas if it answers within 30 seconds. A wrong password or
malformed URI in `.env` is an error, not offline mode.

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
runs in its own process inside the container, also stopped after 10 s. CSV cells that a spreadsheet would
treat as a formula start with an apostrophe.

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
| Dashboard container (the only run path) | `Dockerfile`, `.dockerignore`, `docker-compose.yml` | — |
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
- Offline query-console queries run in a separate process inside the container: about 1 s to start.
