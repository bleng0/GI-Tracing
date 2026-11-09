"""Builds a three-jurisdiction world with sample data for the demo, the pipeline and the tests.

Two samples:
  core  the original 3 batches; the unit tests are written against it
  full  30 batches across NP, IN and PK with 9 deliberate failures (EXPECTED_FAILURES)

All sample data comes from a fixed random seed, so every build produces the same
batches, certificates, lab files and orders. Node keys, chameleon keys and
chameleon randomness are fresh on every build.

With `data_dir` the world is written to disk and can be reopened in a later
Python process with open_world(); without it everything stays in memory.

    <data_dir>/gi_trace.db       SQLite (relational tier)
    <data_dir>/documents.json    document store (see storage.py)
    <data_dir>/keys/             node keys, chameleon public keys, shares (see keystore.py)
    <data_dir>/ledgers/          NP.json, IN.json, PK.json, checkpoints, pending signatures (offline;
                                 online the ledgers are in Atlas; see ledger_store.py)
    <data_dir>/labfiles/         raw lab CSVs, one per hemp batch
    <data_dir>/world.json        which sample was seeded, and whether seeding finished

A world built or opened with a data_dir saves itself on every write. open_world()
refuses a world whose keys, ledgers, documents and SQL rows do not agree.

Names, GI codes, tariff rates and THC limits below are illustrative placeholders,
not real registry, tariff or regulatory figures.
"""
from __future__ import annotations

import hashlib
import json
import random
import shutil
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from .chameleon import keygen, shares_consistent, split_secret
from .crypto import (
    ARTISAN_COOP, BOTANICAL_LAB, CUSTOMS, GI_REGISTRY, TANNING_REGISTRY, Node, canonical,
    directory_of, sha256_hex,
)
from .doc_store import is_offline, open_collection, open_store, signed_part, submit_certificate
from .keystore import (
    KeystoreError, load_checkpoint_key, load_ledger_keys, load_nodes, load_shares, new_checkpoint_key,
    save_checkpoint_key, save_keys, write_json,
)
from .ledger import Ledger
from .ledger_store import AtlasLedgerStore, FileLedgerStore
from .sql_store import connect, get_batch, reset
from .storage import StorageError, read_text, replace_path

JURISDICTIONS = ("NP", "IN", "PK")
REDACTION_THRESHOLD = 3  # of the 6 regulators (3 customs offices + 3 GI registries)
RANDOM_SEED = 2026
SAMPLES = ("core", "full")
# Collections a re-seed empties.
SEEDED_COLLECTIONS = ("certificates", "documents")

LAB_FILES = {
    "HEMP-NP-0001": b"sample_id,thc_pct,cbd_pct\nHEMP-NP-0001,0.08,1.90\n",
    "HEMP-NP-0002": b"sample_id,thc_pct,cbd_pct\nHEMP-NP-0002,0.90,0.40\n",
}

# Deliberate failures in the full sample: batch_id -> reason.
# All but duplicate_export are refused by the oracle; HEMP-NP-0004 passes the
# oracle but has two orders, and the second export must be refused at the border.
EXPECTED_FAILURES = {
    "HEMP-NP-0002": "thc_over_limit",
    "HEMP-NP-0006": "thc_over_limit",
    "HEMP-IN-0003": "thc_over_limit",
    "HEMP-IN-0007": "thc_over_limit",
    "CHAP-PK-0004": "chrome_tanned",
    "CHAP-PK-0008": "chrome_tanned",
    "HEMP-IN-0005": "tampered_lab_file",
    "CHAP-PK-0006": "wrong_signer_role",
    "HEMP-NP-0004": "duplicate_export",
}
CORE_EXPECTED_FAILURES = {"HEMP-NP-0002": "thc_over_limit"}


# --------------------------------------------------------------------------- world

def _paths(data_dir) -> SimpleNamespace:
    d = Path(data_dir)
    return SimpleNamespace(root=d, db=d / "gi_trace.db", docs=d / "documents.json", keys=d / "keys",
                           ledgers=d / "ledgers", labfiles=d / "labfiles", manifest=d / "world.json")


def _node_specs(sample: str) -> list[tuple[str, str, str]]:
    specs = [
        ("COOP-NP-HEMP", ARTISAN_COOP, "NP"),
        ("COOP-PK-CHAPPAL", ARTISAN_COOP, "PK"),
        ("LAB-NP", BOTANICAL_LAB, "NP"),
        ("TANNERY-REG-PK", TANNING_REGISTRY, "PK"),
        *[(f"CUSTOMS-{j}", CUSTOMS, j) for j in JURISDICTIONS],
        *[(f"GI-REG-{j}", GI_REGISTRY, j) for j in JURISDICTIONS],
    ]
    if sample == "full":
        specs += [
            ("COOP-IN-HEMP", ARTISAN_COOP, "IN"),
            ("LAB-IN", BOTANICAL_LAB, "IN"),
            ("LAB-PK", BOTANICAL_LAB, "PK"),
        ]
    return specs


def _regulators(nodes: dict[str, Node]) -> list[Node]:
    return [n for n in nodes.values() if n.role in (CUSTOMS, GI_REGISTRY)]


def _members(nodes: dict[str, Node], j: str) -> list[Node]:
    return [n for n in nodes.values() if n.jurisdiction == j]


class WorldError(Exception):
    """A saved world is incomplete or inconsistent and was not opened."""


def build_world(data_dir=None, sample: str = "core") -> SimpleNamespace:
    """Seeds a fresh world. With `data_dir`, any world already there is replaced."""
    if sample not in SAMPLES:
        raise ValueError(f"sample must be one of {SAMPLES}")
    nodes = {name: Node(name, role, j) for name, role, j in _node_specs(sample)}
    regulators = _regulators(nodes)

    ledgers, shares = {}, {}
    for j in JURISDICTIONS:
        public_key, trapdoor = keygen()
        split = split_secret(trapdoor, len(regulators), REDACTION_THRESHOLD)
        shares[j] = {r.name: s for r, s in zip(regulators, split)}
        del trapdoor  # only the shares survive
        ledgers[j] = Ledger(j, public_key, _members(nodes, j), REDACTION_THRESHOLD)

    data = sample_data(sample)
    paths = _paths(data_dir) if data_dir is not None else None
    try:
        store = open_store(paths.docs if paths else None)
    except StorageError:  # a corrupt store is being replaced anyway
        store = open_store(paths.docs, load=False)
    checkpoint_key, ledger_store = new_checkpoint_key(), None
    if paths:
        ledger_store = _ledger_store(store, paths, checkpoint_key)
        if isinstance(ledger_store, AtlasLedgerStore) and ledger_store.has_ledgers():
            raise WorldError("Atlas already holds ledgers, which the pipeline's user cannot delete. To start over, "
                             "delete all documents in ledger_blocks, ledger_heads and pending_signatures in the "
                             "Atlas website (see README), or run offline with GI_MODE=offline.")
        paths.root.mkdir(parents=True, exist_ok=True)
        # marked incomplete until the end, so a crash mid-seed is never opened as a world
        write_json(paths.manifest, {"format": 1, "sample": sample, "complete": False})
    conn = connect(str(paths.db) if paths else ":memory:")
    if paths:
        reset(conn)
    _seed_sql(conn, data)

    coll = open_collection(store)
    for name in SEEDED_COLLECTIONS:
        store.get_collection(name).delete_many({})  # these collections belong to the prototype
    for cert, signer in data.certificates:
        submit_certificate(coll, cert, nodes[signer])

    world = SimpleNamespace(
        conn=conn, coll=coll, store=store, nodes=nodes, directory=directory_of(nodes.values()),
        ledgers=ledgers, shares=shares, lab_files=data.lab_files, expected=data.expected,
        sample=sample, data_dir=paths.root if paths else None, offline=is_offline(store),
        ledger_store=ledger_store,
    )
    if paths:
        save_keys(paths.keys, nodes, ledgers, shares)
        save_checkpoint_key(paths.keys, checkpoint_key)
        paths.labfiles.mkdir(parents=True, exist_ok=True)
        for stale in paths.labfiles.glob("*.csv"):  # e.g. left by a run of the other sample
            if stale.stem not in data.lab_files:
                stale.unlink()
        for batch_id, content in data.lab_files.items():
            (paths.labfiles / f"{batch_id}.csv").write_bytes(content)
        for ledger in ledgers.values():
            ledger_store.attach(ledger)  # saved now and after every append
        write_json(paths.manifest, {"format": 1, "sample": sample, "complete": True,
                                    "store": "file" if world.offline else "atlas"})
    return world


def _ledger_store(store, paths, checkpoint_key):
    """Ledgers live where the documents do: Atlas online, data/ledgers/ offline."""
    if is_offline(store):
        return FileLedgerStore(paths.ledgers, checkpoint_key)
    return AtlasLedgerStore(store, checkpoint_key)


# What a world is made of in data/; publish_world moves exactly these. users.db stays.
WORLD_ITEMS = ("gi_trace.db", "documents.json", "keys", "ledgers", "labfiles")


def staging_dir(data_dir) -> Path:
    """Where run_all builds the next world before it replaces data/."""
    d = Path(data_dir)
    return d.with_name(d.name + ".new")


def publish_world(staging, data_dir) -> None:
    """Moves a finished world from `staging` into `data_dir`, replacing the world there.
    world.json goes out first and comes back last, so a half-moved folder is never
    opened as a complete world. Other files in data_dir (users.db) are left alone."""
    staging, data_dir = Path(staging), Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    old = data_dir / ".previous"
    if old.exists():
        shutil.rmtree(old)
    old.mkdir()
    if (data_dir / "world.json").exists():
        replace_path(data_dir / "world.json", old / "world.json")
    for name in WORLD_ITEMS:
        if (data_dir / name).exists():
            replace_path(data_dir / name, old / name)
        if (staging / name).exists():
            replace_path(staging / name, data_dir / name)
    replace_path(staging / "world.json", data_dir / "world.json")
    shutil.rmtree(old, ignore_errors=True)
    shutil.rmtree(staging, ignore_errors=True)


def save_world(world) -> None:
    """Saves the ledgers. Only needed for safety: a world built or opened with a
    data_dir already saves SQLite, the document store and the ledgers on every write."""
    if world.data_dir is None:
        raise ValueError("this world is in memory only (built without data_dir)")
    for ledger in world.ledgers.values():
        ledger.on_change(ledger)


def open_world(data_dir, role: str = "write", mode: str | None = None) -> SimpleNamespace:
    """Reopens a world saved under `data_dir`. Refuses (WorldError, LedgerError,
    KeystoreError or StorageError) a world that is incomplete, fails verification,
    or whose keys, ledgers, documents and SQL rows do not agree with each other.
    `role="read"` connects to Atlas with ATLAS_READ_URI (dashboard, snapshot, verify).
    A world built offline always opens offline: Atlas holds a different world.
    `mode` overrides GI_MODE (the dashboard uses "offline" when Atlas drops)."""
    paths = _paths(data_dir)
    if not paths.manifest.exists():
        raise FileNotFoundError(f"{paths.manifest} not found: no saved world in {paths.root}")
    try:
        manifest = json.loads(read_text(paths.manifest))
        sample, complete = manifest["sample"], manifest["complete"]
    except (ValueError, KeyError, TypeError) as e:
        raise WorldError(f"unreadable {paths.manifest}: {e!r}") from e
    if manifest.get("store") == "file":
        mode = "offline"
    store = open_store(paths.docs, role=role, mode=mode)
    required = [paths.db] + ([paths.docs] if is_offline(store) else [])
    for f in required:
        if not f.exists():
            raise FileNotFoundError(f"{f} not found: no saved world in {paths.root}")
    if sample not in SAMPLES:
        raise WorldError(f"unknown sample {sample!r} in {paths.manifest}")
    if complete is not True:
        raise WorldError(f"the world in {paths.root} was not fully seeded (interrupted?); seed it again")

    nodes = load_nodes(paths.keys)
    expected_nodes = {name: (role, j) for name, role, j in _node_specs(sample)}
    if {n.name: (n.role, n.jurisdiction) for n in nodes.values()} != expected_nodes:
        raise KeystoreError(f"node names, roles or jurisdictions in {paths.keys / 'nodes.json'} "
                            f"do not match the {sample} sample")
    ledger_keys = load_ledger_keys(paths.keys)
    if set(ledger_keys) != set(JURISDICTIONS):
        raise KeystoreError(f"ledger keys must cover exactly {JURISDICTIONS}")
    regulators = [r.name for r in _regulators(nodes)]
    shares = load_shares(paths.keys, regulators)
    ledger_store = _ledger_store(store, paths, load_checkpoint_key(paths.keys))
    ledgers = {}
    for j in JURISDICTIONS:
        k = ledger_keys[j]
        if k["redaction_threshold"] != REDACTION_THRESHOLD:
            raise KeystoreError(f"{j} redaction threshold is {k['redaction_threshold']}, expected {REDACTION_THRESHOLD}")
        held = shares.get(j, {})
        if set(held) != set(regulators) or not shares_consistent(
                [held[r] for r in regulators], REDACTION_THRESHOLD, k["ch_public_key"]):
            raise KeystoreError(f"the {j} trapdoor shares are missing or altered")
        empty = Ledger(j, k["ch_public_key"], _members(nodes, j), REDACTION_THRESHOLD)
        ledgers[j] = ledger_store.load(empty)

    conn = connect(str(paths.db))
    batch_ids = [r[0] for r in conn.execute("SELECT batch_id FROM batches ORDER BY batch_id")]
    if batch_ids != sorted(sample_data(sample).expected):
        raise WorldError(f"the batches in {paths.db} are not the {sample} sample")
    coll = open_collection(store)
    lab_files = {b: (paths.labfiles / f"{b}.csv").read_bytes()
                 for b in batch_ids if (paths.labfiles / f"{b}.csv").exists()}
    failures = EXPECTED_FAILURES if sample == "full" else CORE_EXPECTED_FAILURES
    world = SimpleNamespace(
        conn=conn, coll=coll, store=store, nodes=nodes, directory=directory_of(nodes.values()),
        ledgers=ledgers, shares=shares, lab_files=lab_files,
        expected={b: failures.get(b) for b in batch_ids},
        sample=sample, data_dir=paths.root, pipeline=manifest.get("pipeline", "complete"),
        offline=is_offline(store), ledger_store=ledger_store, snapshot_at=manifest.get("snapshot_at"),
        built_on=manifest.get("store"),
    )
    problems = world_problems(world)
    if problems:
        raise WorldError("saved world is inconsistent: " + "; ".join(problems))
    for ledger in ledgers.values():  # after the cross-checks, which name a missing block more precisely
        ledger_store.check_checkpoint(ledger)
    for ledger in ledgers.values():
        ledger_store.attach(ledger, save_now=False)
    return world


def set_pipeline_status(world, status: str) -> None:
    """Records in world.json whether run_all is still running or finished, so a run that
    was interrupted can be told apart from a finished one."""
    manifest_path = _paths(world.data_dir).manifest
    manifest = json.loads(read_text(manifest_path))
    manifest["pipeline"] = status
    write_json(manifest_path, manifest)
    world.pipeline = status


def world_problems(world) -> list[str]:
    """Cross-checks the ledgers against each other and against the document store.
    Catches most truncated or rolled-back ledger files and half-finished writes."""
    problems = []
    by_hash = {j: {b.hash: b for b in l.blocks} for j, l in world.ledgers.items()}
    for j, ledger in world.ledgers.items():
        for mint in ledger.find("MINT"):
            origin = mint.payload.get("origin")
            lock = by_hash.get(origin, {}).get(mint.payload.get("origin_lock_block"))
            if lock is None or lock.tx_type != "LOCK":
                problems.append(f"{j} MINT {mint.payload.get('shipment_id')}: its LOCK is missing from the {origin} ledger")
            elif not any(t.payload.get("dest_mint_block") == mint.hash
                         for t in world.ledgers[origin].find("TRANSFERRED")):
                problems.append(f"{j} MINT {mint.payload.get('shipment_id')}: no TRANSFERRED on the {origin} ledger")
        for t in ledger.find("TRANSFERRED"):
            if not any(t.payload.get("dest_mint_block") in hashes for hashes in by_hash.values()):
                problems.append(f"{j} TRANSFERRED {t.payload.get('shipment_id')}: its MINT is missing")

    anchored = set()
    for j, ledger in world.ledgers.items():
        for att in ledger.find("ATTESTATION"):
            certs = list(world.coll.find({"batch_id": att.payload.get("batch_id"),
                                          "cert_type": att.payload.get("cert_type")}))
            if not any(sha256_hex(canonical(signed_part(c))) == att.payload.get("cert_sha256") for c in certs):
                problems.append(f"{j} ATTESTATION for {att.payload.get('batch_id')}: no matching certificate")
        for anchor in ledger.find("BATCH_ANCHOR"):
            anchored.add((j, anchor.payload.get("batch_id")))
    # every issued document (latest DOCUMENT block per doc_id) is registered, and vice versa
    latest = {}
    for j, ledger in world.ledgers.items():
        for b in ledger.find("DOCUMENT"):
            latest[b.payload.get("doc_id")] = (j, b)
    records = {d.get("doc_id"): d for d in world.store.get_collection("documents").find()}
    for doc_id, (j, b) in latest.items():
        rec = records.get(doc_id)
        if rec is None:
            problems.append(f"document {doc_id} is on the {j} ledger but not in the document store")
        elif rec.get("ledger_block") != b.hash or rec.get("twin_sha256") != b.payload.get("twin_sha256"):
            problems.append(f"registration of {doc_id} does not match its DOCUMENT block on the {j} ledger")
    for doc_id in set(records) - set(latest):
        problems.append(f"document {doc_id} is registered but on no ledger")

    for cert in world.coll.find():
        batch = get_batch(world.conn, cert.get("batch_id"))
        origin = batch["origin"] if batch else None
        is_anchored = (origin, cert.get("batch_id")) in anchored
        if cert.get("compliance_status") == "verified" and not is_anchored:
            problems.append(f"{cert.get('batch_id')}: certificate verified but no BATCH_ANCHOR on the {origin} ledger")
        if is_anchored and cert.get("compliance_status") != "verified":
            problems.append(f"{cert.get('batch_id')}: anchored on the {origin} ledger but certificate is "
                            f"{cert.get('compliance_status')}")
    return problems


def _seed_sql(conn, data) -> None:
    conn.executemany("INSERT INTO cooperatives VALUES (?, ?, ?, ?)", data.cooperatives)
    conn.executemany("INSERT INTO artisans VALUES (?, ?, ?, ?)", data.artisans)
    conn.executemany("INSERT INTO batches VALUES (?, ?, ?, ?, ?, ?, ?)", data.batches)
    conn.executemany("INSERT INTO batch_materials VALUES (?, ?)", data.materials)
    conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?)", data.orders)
    conn.executemany("INSERT INTO tariff_schedule VALUES (?, ?, ?, ?)", data.tariffs)
    conn.executemany("INSERT INTO jurisdiction_rules VALUES (?, ?)", data.rules)
    conn.commit()


# --------------------------------------------------------------------------- sample data

def sample_data(sample: str) -> SimpleNamespace:
    """The rows, certificates (with signer names) and lab files of a sample. Deterministic."""
    data = _core_data()
    if sample == "full":
        _extend_full(data)
    failures = EXPECTED_FAILURES if sample == "full" else CORE_EXPECTED_FAILURES
    data.expected = {b[0]: failures.get(b[0]) for b in sorted(data.batches)}
    return data


def _core_data() -> SimpleNamespace:
    certificates = []
    for batch_id, thc, cbd in [("HEMP-NP-0001", 0.08, 1.9), ("HEMP-NP-0002", 0.9, 0.4)]:
        certificates.append(({
            "cert_type": "hemp_lab_assay",
            "batch_id": batch_id,
            "artisan_coop_id": "COOP-NP-HEMP",
            "origin_gps": {"lat": 28.64, "lon": 81.62},
            "operator_code": "OP-NP-17",
            "lab_file_sha256": hashlib.sha256(LAB_FILES[batch_id]).hexdigest(),
            "assay": {"thc_pct": thc, "cbd_pct": cbd, "method": "GC-MS"},
            "spectral_peaks": [{"nm": 228, "abs": 0.41}, {"nm": 276, "abs": 0.12}],
        }, "LAB-NP"))
    certificates.append(({
        "cert_type": "tanning_attestation",
        "batch_id": "CHAP-PK-0001",
        "artisan_coop_id": "COOP-PK-CHAPPAL",
        "artisan_id": "ART-PK-001",
        "origin_gps": {"lat": 34.01, "lon": 71.58},
        "operator_code": "OP-PK-04",
        "tanning": {"method": "vegetable", "agent": "acacia bark"},
        "heritage": {"motifs": ["hand-stitched sole", "tyre-rubber outsole"]},
    }, "TANNERY-REG-PK"))
    return SimpleNamespace(
        cooperatives=[
            ("COOP-NP-HEMP", "Karnali Hemp Weavers Cooperative", "NP", "NP-GI-HEMP-SAMPLE"),
            ("COOP-PK-CHAPPAL", "Peshawar Chappal Artisans Cooperative", "PK", "PK-GI-CHAPPAL-SAMPLE"),
        ],
        artisans=[
            ("ART-NP-001", "Sample Artisan A", "COOP-NP-HEMP", "2024-03-01"),
            ("ART-NP-002", "Sample Artisan B", "COOP-NP-HEMP", "2024-06-15"),
            ("ART-PK-001", "Sample Artisan C", "COOP-PK-CHAPPAL", "2023-11-20"),
        ],
        batches=[
            ("HEMP-NP-0001", "COOP-NP-HEMP", "ART-NP-001", "Hemp yarn", "5308.20", 12.0, "2026-10-02"),
            ("HEMP-NP-0002", "COOP-NP-HEMP", "ART-NP-002", "Hemp yarn", "5308.20", 9.0, "2026-10-05"),
            ("CHAP-PK-0001", "COOP-PK-CHAPPAL", "ART-PK-001", "Peshawari chappal", "6403.99", 8.5, "2026-10-08"),
        ],
        materials=[("HEMP-NP-0001", "hemp"), ("HEMP-NP-0002", "hemp"), ("CHAP-PK-0001", "leather")],
        orders=[
            ("ORD-0001", "HEMP-NP-0001", "Sample Buyer Delhi", "IN", 40, 6.5, "USD"),
            ("ORD-0002", "CHAP-PK-0001", "Sample Buyer Kathmandu", "NP", 25, 18.0, "USD"),
        ],
        tariffs=[("5308.20", "NP", "IN", 0.0), ("6403.99", "PK", "NP", 15.0)],
        rules=[("NP", 0.3), ("IN", 0.3), ("PK", 0.3)],
        certificates=certificates,
        lab_files=dict(LAB_FILES),
    )


# Where each jurisdiction's batches come from (placeholder coordinates; lat > 30 only in PK).
SITES = {"NP": (28.64, 81.62, "OP-NP-17"), "IN": (29.60, 79.66, "OP-IN-08"), "PK": (34.01, 71.58, "OP-PK-04")}
LAB_FOR = {"NP": "LAB-NP", "IN": "LAB-IN", "PK": "LAB-PK"}
BUYERS = {"NP": "Sample Buyer Kathmandu", "IN": "Sample Buyer Delhi", "PK": "Sample Buyer Lahore"}
PRODUCTS = {"hemp": ("Hemp yarn", "5308.20"), "leather": ("Peshawari chappal", "6403.99"),
            "mixed": ("Hemp-stitched chappal", "6403.99")}


def _extend_full(data: SimpleNamespace) -> None:
    rng = random.Random(RANDOM_SEED)
    start, span_days = date(2026, 5, 1), 184  # 1 May to 31 Oct 2026

    data.cooperatives.append(("COOP-IN-HEMP", "Kumaon Hemp Spinners Cooperative", "IN", "IN-GI-HEMP-SAMPLE"))
    data.artisans += [
        ("ART-NP-003", "Sample Artisan D", "COOP-NP-HEMP", "2023-02-11"),
        ("ART-NP-004", "Sample Artisan E", "COOP-NP-HEMP", "2025-01-09"),
        ("ART-IN-001", "Sample Artisan F", "COOP-IN-HEMP", "2022-08-30"),
        ("ART-IN-002", "Sample Artisan G", "COOP-IN-HEMP", "2023-05-17"),
        ("ART-IN-003", "Sample Artisan H", "COOP-IN-HEMP", "2024-12-02"),
        ("ART-PK-002", "Sample Artisan I", "COOP-PK-CHAPPAL", "2022-04-21"),
        ("ART-PK-003", "Sample Artisan J", "COOP-PK-CHAPPAL", "2024-01-05"),
        ("ART-PK-004", "Sample Artisan K", "COOP-PK-CHAPPAL", "2025-03-14"),
    ]
    coop_artisans: dict[str, list[str]] = {}
    for artisan_id, _, coop_id, _ in data.artisans:
        coop_artisans.setdefault(coop_id, []).append(artisan_id)

    plan = (
        [(f"HEMP-NP-{n:04d}", "NP", "COOP-NP-HEMP", "hemp") for n in range(3, 11)]
        + [(f"HEMP-IN-{n:04d}", "IN", "COOP-IN-HEMP", "hemp") for n in range(1, 9)]
        + [(f"CHAP-PK-{n:04d}", "PK", "COOP-PK-CHAPPAL", "leather") for n in range(2, 11)]
        + [("MIX-NP-0001", "NP", "COOP-NP-HEMP", "mixed"), ("MIX-PK-0001", "PK", "COOP-PK-CHAPPAL", "mixed")]
    )
    order_no = len(data.orders)
    for batch_id, origin, coop_id, kind in plan:
        failure = EXPECTED_FAILURES.get(batch_id)
        artisan_id = rng.choice(coop_artisans[coop_id])
        created_on = (start + timedelta(days=rng.randrange(span_days))).isoformat()
        product, hs_code = PRODUCTS[kind]
        data.batches.append((batch_id, coop_id, artisan_id, product, hs_code,
                             round(rng.uniform(2.0, 40.0), 1), created_on))
        materials = ["hemp", "leather"] if kind == "mixed" else [kind]
        data.materials += [(batch_id, m) for m in materials]
        lat, lon, operator = SITES[origin]
        gps = {"lat": round(lat + rng.uniform(-0.2, 0.2), 4), "lon": round(lon + rng.uniform(-0.2, 0.2), 4)}

        if "hemp" in materials:
            over = failure == "thc_over_limit"
            thc = round(rng.uniform(0.35, 1.2) if over else rng.uniform(0.02, 0.25), 2)
            cbd = round(rng.uniform(0.3, 2.5), 2)
            moisture = round(rng.uniform(6.0, 12.0), 1)
            peaks = [{"nm": 228, "abs": round(rng.uniform(0.2, 0.6), 2)},
                     {"nm": 276, "abs": round(rng.uniform(0.05, 0.2), 2)}]
            csv = _lab_csv(batch_id, thc, cbd, moisture, peaks)
            data.certificates.append(({
                "cert_type": "hemp_lab_assay",
                "batch_id": batch_id,
                "artisan_coop_id": coop_id,
                "origin_gps": gps,
                "operator_code": operator,
                "lab_file_sha256": hashlib.sha256(csv).hexdigest(),
                "assay": {"thc_pct": thc, "cbd_pct": cbd, "moisture_pct": moisture, "method": "GC-MS"},
                "spectral_peaks": peaks,
            }, LAB_FOR[origin]))
            if failure == "tampered_lab_file":
                # someone lowered the THC in the raw file after the lab certified it
                csv = _lab_csv(batch_id, round(thc / 2, 2), cbd, moisture, peaks)
            data.lab_files[batch_id] = csv

        if "leather" in materials:
            chrome = failure == "chrome_tanned"
            data.certificates.append(({
                "cert_type": "tanning_attestation",
                "batch_id": batch_id,
                "artisan_coop_id": coop_id,
                "artisan_id": artisan_id,
                "origin_gps": gps,
                "operator_code": operator,
                "tanning": ({"method": "chrome", "agent": "chromium(III) sulfate"} if chrome else
                            {"method": "vegetable", "agent": rng.choice(["acacia bark", "mimosa bark"])}),
                "heritage": {"motifs": rng.sample(
                    ["hand-stitched sole", "tyre-rubber outsole", "zari embroidery", "double-strap upper"], 2)},
            }, coop_id if failure == "wrong_signer_role" else "TANNERY-REG-PK"))

        dests = [j for j in JURISDICTIONS if j != origin]
        for dest in (dests if failure == "duplicate_export" else [rng.choice(dests)]):
            order_no += 1
            data.orders.append((f"ORD-{order_no:04d}", batch_id, BUYERS[dest], dest,
                                rng.randint(10, 200), round(rng.uniform(3.0, 40.0), 2), "USD"))

    have = {(hs, o, d) for hs, o, d, _ in data.tariffs}
    for hs_code in ("5308.20", "6403.99"):
        for o in JURISDICTIONS:
            for d in JURISDICTIONS:
                if o != d and (hs_code, o, d) not in have:
                    data.tariffs.append((hs_code, o, d, rng.choice([0.0, 5.0, 10.0, 15.0])))


def _lab_csv(batch_id: str, thc: float, cbd: float, moisture: float, peaks: list[dict]) -> bytes:
    return (
        "sample_id,thc_pct,cbd_pct,moisture_pct,peak_228nm_abs,peak_276nm_abs\n"
        f"{batch_id},{thc:.2f},{cbd:.2f},{moisture:.1f},{peaks[0]['abs']:.2f},{peaks[1]['abs']:.2f}\n"
    ).encode()
