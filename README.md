# GI-Craft-Tracing

Working prototype of *Multi-Jurisdictional Traceability for South Asian GI Crafts*:
Himalayan hemp and Peshawari chappal batches moving between Nepal, India and Pakistan.

This repository runs **only through Docker Compose**. The runtime and every dependency live inside the
image; you do not need Python, pip, or a virtual environment on your machine. Every command-line entry
point checks for the container flag, so a local `python -m ...` exits with a Docker reminder instead of
running.

## Run it from a fresh clone (Windows PowerShell)

You need **Git**, **Docker Desktop** with its Linux engine running, and the two MongoDB Atlas URIs from
the project owner. `data/`, `out/` and the shared accounts database are already in the repository, so
there is **no seeding and no account setup**. The dashboard reads its counts and documents live from the
Atlas connection in `.env`; the committed files supply the PDFs, QR images and SQL workbench database.

**1. Clone the repository and enter the folder.**

```powershell
git clone <REPO_URL>
cd GI-Tracing
```

Use the real GitHub URL in place of `<REPO_URL>`.

**2. Create `.env` with the Atlas URIs.** Put these two lines in the project folder, using the real URIs
you were given:

```text
ATLAS_WRITE_URI=<pipeline user URI>
ATLAS_READ_URI=<dashboard read-only user URI>
```

`.env` is ignored by Git and is never copied into the image. You and your friend each keep a copy; the
URIs point at the same Atlas database. The read-only user must not be able to write.

**3. Confirm Docker is ready.**

```powershell
docker compose version
docker info
```

`docker info` must print without an error. If it fails, start Docker Desktop, wait for it to finish, and
make sure it is using Linux containers (Docker Desktop tray → *Switch to Linux containers*).

**4. Build and start the dashboard.**

```powershell
docker compose up --build -d
docker compose logs -f web
```

Compose pins `GI_MODE=atlas`, so no shell variable is needed. Wait for
`Running on http://127.0.0.1:5000`. `Ctrl+C` closes the log view but leaves the container running.

**5. Sign in.** Open **http://127.0.0.1:5000** and use one of the shared demo accounts:

| Username | Password | Role |
| --- | --- | --- |
| `admin` | `demo-admin-2026` | admin |
| `employee` | `demo-employee-2026` | employee |
| `delivery` | `demo-delivery-2026` | delivery |

Only `admin` can open `/workbench`. Five wrong passwords lock an account for five minutes.

**6. Stop and start again.**

```powershell
docker compose down
docker compose up -d
```

The container listens on `0.0.0.0:5000` internally; Compose publishes it as `127.0.0.1:5000:5000`, so it
is reachable only from this PC. If port 5000 is busy, change the left-hand side of that mapping in
`docker-compose.yml`.

## Shared accounts and data

Accounts live in `accounts/users.db`, which **is tracked in Git**. When either of you adds an account:

```powershell
docker compose exec web python -m gi_trace.web.create_user
```

then commit `accounts/users.db` and push. The other clone gets the account on its next pull. Passwords
are stored only as scrypt hashes, so sharing the file does not share the typed passwords; tell the other
person the password separately.

`data/` and `out/` are also tracked, so a fresh clone already has the SQLite database, demo keys,
ledger copies, PDFs, QR codes and reports. The dashboard does not fall back to these files if Atlas is
unreachable: it shows a clear error instead. A wrong password or malformed URI in `.env` is also an
error, never offline mode.

To check the connection:

```powershell
docker compose run --rm web python -m gi_trace.check_atlas
```

It ends with `OK: Atlas reachable, write and read work`. On a different network, add that network's IP
under Network Access in Atlas before running the check.

## Commands

Every command runs inside the `web` container. `run` starts a one-off container for a task that exits;
`exec` targets the already-running dashboard.

| Command | What it does |
| --- | --- |
| `docker compose build` | build the image with all runtime dependencies preinstalled |
| `docker compose up -d` / `docker compose down` | start / stop the dashboard on http://127.0.0.1:5000 (this PC only) |
| `docker compose logs -f web` | follow dashboard logs; Ctrl+C leaves the container running |
| `docker compose run --rm web python -m gi_trace.run_all --yes` | optional: reseed Atlas and regenerate the tracked `data/` and `out/` files |
| `docker compose run --rm web python -m gi_trace.run_all --yes --sample core` | optional: smaller 3-batch sample |
| `docker compose run --rm web python -m gi_trace.web.create_user` | add a dashboard account (hidden password); commit the database afterwards |
| `docker compose exec web python -m gi_trace.web.create_user` | add an account while the dashboard is running; commit afterwards |
| `docker compose run --rm web python -m gi_trace.check_atlas` | check Atlas connectivity and permissions |
| `docker compose run --rm web python -m gi_trace.atlas_setup` | one-time Atlas users, collections and indexes (writes the URIs to `.env`) |
| `docker compose run --rm web python -m gi_trace.snapshot` | refresh the committed `data/` copy from Atlas |
| `docker compose run --rm web python -m gi_trace.docgen.verify out/certificates` | VALID / TAMPERED / UNKNOWN per PDF |
| `docker compose run --rm web python -m gi_trace.demo` | short in-memory walkthrough of the four mechanisms |

The source tree contains 610 tests used by CI; the runtime image deliberately excludes `tests/`.

`GI_MODE` is fixed to `atlas` in `docker-compose.yml`. The library still supports `auto` and `offline`
for tests and one-off tools, but the dashboard always uses the Atlas URIs from `.env` and never switches
to the committed local copy.

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
letter case, also as text values such as `"$out"`). Atlas stops a query after 10 s. The library's offline
path also runs each query in its own process, stopped after 10 s. CSV cells that a spreadsheet would
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

- `data/` is a committed snapshot of the latest world: `gi_trace.db` (SQLite), `keys/`, `labfiles/`,
  `documents.json`, `ledgers/` and `world.json` (including `snapshot_at`). The dashboard reads live data
  from Atlas; these files supply the SQL workbench, PDF downloads, QR images and ledger verification.
  Accounts are in the tracked `accounts/users.db`, not here.
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

**Keys on disk are committed demo keys, not key custody.** `data/keys/` holds every node's
private signing key, the key that signs each ledger's checkpoint (never sent to Atlas), and all
six regulators' Shamir shares of each ledger's redaction trapdoor (the trapdoor itself is never written).
They are in Git so a fresh clone can verify the Atlas ledgers without setup. In real use each share would
be held by a different regulator; here any three files in `data/keys/shares/` are enough to redact a
ledger. Do not reuse these keys for anything real.

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
| Shared accounts, lockout, audit log | `accounts/users.db`, `gi_trace/web/users.py`, `gi_trace/web/create_user.py` | — |
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
