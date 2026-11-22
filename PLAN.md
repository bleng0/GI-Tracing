# GI-Craft-Tracing: implementation plan v2 (MongoDB Atlas online, local files offline)

Replaces PLAN.v1.md (kept for reference). Status comes from an audit of the project folder.

## 1. Where the project stands

| Original phase | Status | Evidence |
| --- | --- | --- |
| 0 Decisions, 1 Persistence, 2 Sample data | DONE | file store, keystore, ledger_io, 30-batch seed (21 pass, 9 fail as intended) |
| 3 Documents and PDFs | DONE | `gi_trace/docgen/`, about 80 PDFs, `verify.py` (VALID / TAMPERED / UNKNOWN) |
| 4 One-command pipeline | DONE | `python -m gi_trace.run_all`, about 14 s |
| 5 Dashboard | NOT STARTED | no `web/` package, no login |
| 6 Tests | PARTIAL | 118 tests pass; none for access control or the query allow-list |
| 7 Demo guide | NOT STARTED | |
| 8 Database | PARTIAL | `MONGO_URI` switch exists; Atlas never exercised; only `batch_id` indexed |

Added this session: `gi_trace/atlas_setup.py` and `tests/test_atlas_setup.py` (9 tests, fake Atlas).

## 2. Decisions (confirmed)

1. **Web dashboard** on `127.0.0.1` (Flask), shown in a browser, never deployed publicly.
2. **Atlas** (free M0 cluster, your IP on the allow list) is the online database. The on-disk file store is the offline fallback. The local MongoDB service is not part of the demo.
3. **Offline switch:** if Atlas is not reachable within **30 seconds**, the app uses the on-disk data and shows an "OFFLINE MODE" banner on every page. Data written offline stays local. `python -m gi_trace.check_offline` proves the demo works with no connection.
4. **Ledgers move into Atlas** (`ledger_blocks`, `ledger_heads`, `pending_signatures`). The ledger logic (hashing, signing, chameleon hash, handshake) stays in Python. **Keys stay on this PC, never in Atlas.**
5. **Three logins**, created with a command that asks you to type each password (nothing stored in code or README): `admin`, `employee`, `delivery`. Usernames can be email addresses; no Google sign-in is involved.
6. All institutions and documents are fictional and every PDF is stamped SAMPLE / DEMONSTRATION.
7. Windows 10/11, Python 3.10+.
8. `git init` locally, one commit per step, no remote. `.env`, `data/`, `out/`, `.venv/` are ignored.

## 3. What is and is not "blockchain" here (say this to teachers)

| Part | Real mechanism | Where |
| --- | --- | --- |
| Hash-linked blocks | each block stores the hash of the previous one; `verify_chain()` detects any edit | `ledger.py` |
| Digital signatures | every block signed with the writer's Ed25519 key; a database editor cannot forge them | `crypto.py`, `ledger.py` |
| Multi-party commit | export commits only with 4 or 5 required signatures (cooperative, lab or tanning registry, both customs) | `handshake.py` (modelled on MP-SATP) |
| Controlled correction | chameleon hash lets 3 of 6 regulators fix a clerical error without breaking the chain; the fix is logged in a REDACTION block | `chameleon.py`, `ledger.py` |
| Tamper-evident anchoring | the oracle writes certificate hashes and the batch hash to the ledger; QR codes and PDFs carry block hashes | `oracle.py`, `docgen/` |
| Append-only storage | no-delete database role, unique block positions, signed latest-block checkpoint (Step 3) | Atlas, `ledger_store` |

**Not blockchain:** there is no network of independent nodes, no consensus algorithm, no mining or tokens; one program plays every party. Call it a **permissioned ledger simulation**. The SQL and MongoDB parts, the dashboard, the PDFs and the login are ordinary database and web features.

## 4. Who can see what

| Page | admin | employee | delivery |
| --- | --- | --- | --- |
| Overview counts and charts | yes | yes (no prices) | no |
| Query console | yes | no | no |
| Batches and certificates (read-only) | yes | yes | no |
| Shipments list and status | yes | yes | yes |
| Documents list, download, Verify (QR / PDF) | yes | yes | Verify only |
| Ledger viewer and chain check | yes | no | no |
| Audit log, user list | yes | no | no |

Customs waybill PDFs print order prices, so only admin may download them (employees see the row, not the file).

Every route checks the role; anything else gets 403, and logged-out visitors go to the login page.

## 5. Steps (each has a check; do not start the next until it passes)

### Step 1. Atlas setup, run by you (10 min)
1. Run `python -m gi_trace.atlas_setup` (instructions in the chat).
Check: it prints `DONE`; every permission check says `ok`; `.env` has `ATLAS_WRITE_URI` and `ATLAS_READ_URI`; `gi_setup` is gone from Atlas Database Access.
Then delete the API key in Atlas and remove the three `ATLAS_PROJECT_ID`, `ATLAS_PUBLIC_KEY`, `ATLAS_PRIVATE_KEY` lines.

### Step 2. Atlas as the document store, with the 30-second offline switch (2 h)
1. `open_store()` picks `GI_MODE` = `auto` (default), `atlas` or `offline`. In `auto`, try Atlas with `serverSelectionTimeoutMS=30000`; on failure use the file store and set an `offline` flag.
2. Pipeline uses `ATLAS_WRITE_URI`; the dashboard uses `ATLAS_READ_URI`.
3. Seeding clears only `certificates` and `documents` (the only collections the writer may delete from).
4. `check_atlas` command: connect, list collections, write and read one test document.
Check: `run_all` online fills Atlas (21 passed, 9 failed, same as before); the same command with Wi-Fi off finishes in offline mode; existing 118 tests still pass.

### Step 3. Ledger in Atlas (3 h)
1. `LedgerStore` interface with a file backend (existing `ledger_io`) and an Atlas backend. Each block gets `dedupe_key`; unique indexes from `atlas_setup` enforce it.
2. A signed checkpoint (latest block number and hash) is written to `ledger_heads` after every append, signed with a key kept only on this PC. Loading a ledger checks the chain, the checkpoint and its signature. Fixes open issue 1.
3. Handshake signatures saved to `pending_signatures`, so a restart can finish an interrupted shipment. Fixes issue 4.
4. Re-running the oracle on a passed batch adds nothing. Fixes issue 5.
Check (new tests): delete the last block, then loading fails; kill the program mid-handshake, restart, shipment completes without re-signing; run the oracle twice, block count unchanged; all earlier tests pass.
Known consequence: the writer cannot delete ledger data by design, so resetting Atlas ledgers means deleting all documents in `ledger_blocks`, `ledger_heads` and `pending_signatures` in the Atlas website, not dropping the collections (2 minutes, written in the README).

### Step 4. Housekeeping (30 min)
1. `git init`, `.gitignore` check, first commit.
2. Run the full test suite on your Windows PC (not only the Linux sandbox). Fixes open issue 3.
3. Rewrite the README's status lines (118 tests, Atlas, offline).
Check: tests pass on Windows; `git log` shows the commit.

### Step 5. Users and roles (2 h)
1. SQLite `users` table, scrypt password hashes, `python -m gi_trace.web.create_user` asking for username, role and password (hidden input).
2. Lockout after 5 failed logins for 5 minutes; audit log entries.
Check: three accounts can be created; wrong password five times locks the account; no password appears in any file.

### Step 6. Dashboard shell and pages (4 h)
1. Flask app on `127.0.0.1:5000`, login page, role-based menu, offline banner, CSRF token, `HttpOnly` and `SameSite=Strict` cookie.
2. Overview, shipments, batches and certificates, documents with Verify, audit log, per the table in section 4.
3. Offline snapshot: at the end of every online `run_all`, copy the Atlas documents and ledgers into `data/` once (written to a temporary folder, then swapped in), plus `python -m gi_trace.snapshot` to refresh it by hand. With Atlas unreachable the dashboard opens that copy and the banner reads "OFFLINE MODE: data as of <time of the last online run>". The copy replaces any earlier offline world: `data/` always holds the latest run.
Check: each page shows the right data online and offline; each role sees only its pages; after an online run with Wi-Fi then turned off, the dashboard shows the same counts with the snapshot time in the banner.

### Step 7. Query console and ledger viewer (3 h)
1. Query console (admin): collection picker (`certificates`, `documents`), JSON filter, projection, sort, limit 200, `maxTimeMS` 10 s, table and CSV output, four saved example queries. Operators `$where`, `$function`, `$accumulator`, `$out`, `$merge` refused.
2. Ledger viewer (admin): choose NP, IN or PK, browse blocks, "chain valid" or the list of problems.
Check: the four example queries return the right rows online; every banned operator is refused; ledger viewer reports valid on a clean chain and flags a deliberately edited block.

### Step 8. Access-control and end-to-end tests (2 h)
1. Role matrix test: every route, every role, plus logged-out (expect 302 or 403).
2. Break-it check: remove the role check in a scratch copy and confirm the tests fail.
Check: all tests pass; the break-it run fails as expected.

### Step 9. Guides and rehearsal (2 h)
1. README: full demo for **online** and **offline** use, setup, commands.
2. `DEMO.md`: 10-minute script plus a feature-by-feature guide, with the blockchain parts labelled as in section 3.
3. Rehearse from a fresh clone and fresh venv, then once with Wi-Fi off.
Check: README steps take you from zero to a working dashboard in under 15 minutes in both modes.

## 6. Time estimate for what remains

| Step | Hours |
| --- | --- |
| 1 Atlas setup (you) | 0.2 |
| 2 Atlas document store and offline switch | 2 |
| 3 Ledger in Atlas | 3 |
| 4 Housekeeping | 0.5 |
| 5 Users and roles | 2 |
| 6 Dashboard pages and offline snapshot | 5 |
| 7 Query console and ledger viewer | 3 |
| 8 Access-control tests | 2 |
| 9 Guides and rehearsal | 2 |
| **Total** | **about 19.7** |

## 7. Known limits

1. One program holds every key: this simulates a multi-party network; it is not one.
2. Local key storage is a demo convenience (open issue 2).
3. PDFs are signed with this project's own keys, so they prove tamper-evidence inside this system only.
4. Offline data is not merged back into Atlas.
5. Not included: ABPRE pricing encryption and the Redis cache from the handoff document (about 4 h each if evaluators expect them).

## 8. Open items after Step 9: plan (Steps 10 to 18)

Same rules as section 5: one step at a time, each with a Check; the next step starts only when the
Check passes; one commit per step. Never read or print `.env`; tests and break-it runs work in temporary
folders, never in `data/` or `out/`.

**How every step runs**
1. **Test writer** (subagent, fresh context, sees only this section and the code it tests): writes the
   step's tests first, from the Check below. They must fail before the change.
2. **Build** (Claude): the smallest change that makes them pass, in the existing style.
3. **Full suite + stress** (Claude): all tests on Linux, plus the step's stress test (threads).
4. **Reviewer** (subagent, report only): checks the step against its Check, looks for security and
   correctness bugs, and mutation-tests the new tests (breaks the code in a temporary copy and confirms
   a test fails). Every confirmed finding is fixed with a test.
5. **You, only where marked "You"**: the parts that need Windows, a phone, Atlas's website or your eyes.
   Collected into one Windows rehearsal script in Step 17, except where a step says otherwise.

### Step 10. Small fixes (30 min)
1. `gi_trace/web/__init__.py` docstring describes what the package holds now.
2. `test_a_locked_account_does_not_answer_faster` is timing-based and failed once under parallel load:
   compare medians of 7 samples instead of minima of 3, keeping the property it checks.
Check: the timing test passes 20 runs in a row under `-n 2`; full suite passes.

### Step 11. Sessions that survive a restart, role checked on the server (open items 3 and 6, 2.5 h)
1. A `sessions` table in `accounts/users.db`: SHA-256 of the session id (never the id itself), username,
   created, last seen. Idle expiry 8 h, absolute 24 h. Signing out deletes the row.
2. The cookie-signing key is created once and kept in `accounts/users.db` (which runs never replace), so a
   restart keeps everyone signed in.
3. Every request reads the role (and the lock state) from the `users` table, not from the cookie, so a
   changed role or a locked account takes effect on the next request.
Check (tests): restart the app (new `create_app`, same `accounts/users.db`): still signed in; sign out, then a
copied cookie is refused on GET and POST; change a user's role in the table: next request uses it;
an expired session goes to sign-in; no session id appears in `accounts/users.db`. Break-it gains two
mutations (role from cookie again; sign-out not deleting the row). Stress: 16 threads signing in, out
and browsing at once, no wrong status. **You:** restart the dashboard and reload: still signed in (2 min).

### Step 12. A failed run leaves `out/` as it was (open item 1, 1.5 h)
1. `run_all` writes PDFs, QR codes, lab files and the report into `out.new/`, and swaps them into
   `out/` only after the whole run (and the data swap) succeeded, like `data.new/`.
2. Documents register their final path under `out/`, not `out.new/`.
3. If Windows holds a file in `out/` (a PDF open in a viewer), the run stops with a clear message
   naming the file and both `data/` and `out/` keep the previous run.
Check (tests): a run made to fail after the PDF step leaves `out/` byte-for-byte unchanged and every
download still returns 200; a successful run replaces it; registered paths point into `out/`.
**You:** open one PDF from `out\certificates` in Edge, run `run_all`, see the message, close the PDF,
run again (5 min).

### Step 13. No half-replaced data on screen during an online run (open item 2, 2.5 h)
1. While a run is in progress (`data.new/` exists) an Atlas-connected dashboard shows the offline copy
   of the previous run instead of Atlas, with the banner "A run is in progress: showing the copy from
   <time>". When the run is published it goes back to Atlas. If there is no earlier copy (the very
   first online run) the banner says the data may be incomplete.
2. Remaining gap, documented: a request that started reading Atlas just before the run began.
Check (tests, FakeAtlas): a run paused right after it cleared the Atlas collections: pages show the
previous counts and the banner; after it finishes, the new data online. Stress: pages requested from
8 threads all through a real run never show a partial count (always the old or the new totals).

### Step 14. Verify reads a QR code from a photo or screenshot (open item 4, 2 h)
1. Verify accepts a PNG or JPEG (up to 10 MB, at most 40 megapixels), decodes the QR with `zxing-cpp`
   (a 1 MB wheel; a Windows build for Python 3.14 exists) and checks the text exactly as pasted text is.
2. No QR found, or several, or an unreadable image: a plain message, never an error page.
Check (tests): every QR PNG the pipeline writes verifies VALID; a QR with an edited block hash is
TAMPERED; junk bytes, a non-image, an oversized "decompression bomb" PNG and a PDF renamed .png are
refused with a message; same roles as Verify. **You:** photograph a QR code on screen with your phone,
upload the photo: VALID (5 min).

### Step 15. `python -m gi_trace.check_offline` (open item 5, 1 h)
1. Proves the demo works with no connection: opens `data/` offline, re-checks every chain, opens each
   dashboard page as a temporary admin (random password, temporary accounts file), verifies three PDFs,
   and prints `OK: the demo works with no connection` or what failed. Any network use during the
   check is an error (sockets are blocked in-process).
2. README and DEMO.md "Before you start" use it.
Check (tests): OK on an offline world and on a snapshot world; exit 1 with the reason on a broken
world, a missing `out/` and an edited ledger; no file under `data/` changes.

### Step 16. Ledger viewer under 0.3 s on a repeat visit (open item 7, 1 h)
1. Cache the chain check per ledger, keyed by the SHA-256 of the stored blocks and the checkpoint as
   read now. The blocks are still read on every visit, so an edit is seen at once; only an unchanged
   ledger skips the expensive signature and chameleon checks.
Check (tests): second visit at least 4x faster than the first; an edit made between visits is flagged
on the next one; 8 threads on the ledger pages give the same results as one. Stress p50 < 0.3 s.

### Step 17. Windows and Atlas rehearsal (1 h for Claude; about 20 min for you)
1. Update `out\rehearse_windows.ps1` and `out\rehearse_online.ps1` to cover Steps 10 to 16, and add a
   genuinely slow Atlas query (heavy `$expr` arithmetic) that real Atlas must stop with
   "Atlas stopped the query after 10 s" (open from the Step 7 rehearsal).
2. **You:** empty the three Atlas ledger collections (BULK, Bulk delete documents), run both scripts,
   reply "done"; Claude reads the log. Then empty the ledgers again and run `run_all` in the main folder.
Check: every line of both scripts PASS on Windows, including the Atlas time limit.

### Step 18. Guides and tidy-up (1 h)
1. README, DEMO.md and section 7 of this file updated for Steps 10 to 17; test count current.
2. Housekeeping, asking once: permission to delete the `stale-*` git lock files and `out\rehearse_*`;
   you delete `C:\Users\bruce\gi-rehearse` yourself.
Check: `tests/test_docs.py` passes; a reviewer subagent checks every claim in the guides against the code.

| Step | Claude | You |
| --- | --- | --- |
| 10 Small fixes | 0.5 h | |
| 11 Sessions | 2.5 h | 2 min |
| 12 `out/` staging | 1.5 h | 5 min |
| 13 Run in progress | 2.5 h | |
| 14 QR images | 2 h | 5 min |
| 15 `check_offline` | 1 h | |
| 16 Ledger cache | 1 h | |
| 17 Rehearsal | 1 h | 20 min |
| 18 Guides | 1 h | 2 min |
| **Total** | **about 13 h** | **about 35 min** |
