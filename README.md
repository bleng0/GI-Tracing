# GI-Craft-Tracing

Working prototype of *Multi-Jurisdictional Traceability for South Asian GI Crafts*:
Himalayan hemp and Peshawari chappal batches moving between Nepal, India and Pakistan.

## Run it

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
python -m pytest                   # 118 tests, under 2 minutes
python -m gi_trace.run_all         # whole pipeline on the 30-batch sample; asks before replacing data/ and out/
python -m gi_trace.docgen.verify out\certificates       # VALID / TAMPERED / UNKNOWN per PDF
python -m gi_trace.demo            # short in-memory walkthrough of the four mechanisms
```

No database server is needed. The demo and tests keep everything in memory.
`build_world(data_dir="data", sample="full")` seeds the 30-batch sample and saves it under `data/`
(SQLite `gi_trace.db`, document store `documents.json`, `keys/`, `ledgers/`, `labfiles/`);
`open_world("data")` reopens it in a later session. It re-verifies every ledger, cross-checks
the ledgers against each other and against the documents, and refuses a world that does not add up.
A world on disk saves itself on every write; only one process should write to it at a time
(a second writer is refused rather than overwriting).
To use a real MongoDB, set `MONGO_URI` (e.g. `mongodb://localhost:27017`);
**seeding (and so `run_all`) clears the `gi_trace.certificates` and `gi_trace.documents` collections**,
whatever `--data` directory is used, because they all share that one database. The tests ignore
`MONGO_URI` and never touch MongoDB.

**Keys on disk are a demo convenience, not key custody.** `data/keys/` holds every node's
private signing key and all six regulators' Shamir shares of each ledger's redaction trapdoor
(the trapdoor itself is never written). In real use each share would be held by a different
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

- Ledgers are Python objects saved to JSON files: no consensus, networking or crash recovery.
  A ledger file is re-verified on load, but cutting blocks off its end is not detected
  (a shorter chain is still a valid chain).
- The trapdoor is reconstructed in one place to forge. A true threshold scheme would
  forge without ever rebuilding it.
- No crash recovery between the destination MINT and the origin TRANSFERRED write
  (MP-SATP uses backup gateways for this).
- ABPRE encryption of artisan pricing (Jia et al.) and the Redis transit-state cache
  (Li & Gu adaptation) are not implemented. The spec describes both but does not list
  them among the four mechanisms.
- THC limits (0.3 %), tariff rates, GI codes and names in `seed.py` are placeholders.
