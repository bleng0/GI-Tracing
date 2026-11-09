"""Offline copy of the Atlas data:  python -m gi_trace.snapshot   (run_all does this itself)

Writes the Atlas certificates, documents, ledgers, checkpoints and pending handshake
signatures into data/ in exactly the files an offline run uses (documents.json and
ledgers/), so with Atlas unreachable the dashboard opens this copy. Everything is
written to a temporary folder first and then swapped in, once per run rather than
once per change, and world.json records when ("snapshot_at"). The copy replaces any
earlier offline world: data/ always holds the latest run.
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from .doc_store import load_env
from .keystore import write_json
from .ledger_io import block_to_json
from .storage import FileDocStore, read_text, replace_path

INDEXES = {"certificates": [[("batch_id", 1)]], "documents": [[("doc_id", 1)], [("batch_id", 1)]]}


class SnapshotError(Exception):
    pass


def take_snapshot(world, log=print) -> str:
    """Copies an online world into world.data_dir; returns the snapshot time."""
    if world.offline:
        raise SnapshotError("the world is offline (local files already); a snapshot copies Atlas data")
    data = Path(world.data_dir)
    tmp = data / ".snapshot_tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / "ledgers").mkdir(parents=True)

    FileDocStore(tmp / "documents.json", load=False).replace_all(
        {name: list(world.store.get_collection(name).find({}, {"_id": 0})) for name in INDEXES}, INDEXES)
    for j, ledger in world.ledgers.items():
        write_json(tmp / "ledgers" / f"{j}.json",
                   {"format": 1, "jurisdiction": j, "blocks": [block_to_json(b) for b in ledger.blocks]})
        head = world.ledger_store.heads.find_one({"jurisdiction": j}, {"_id": 0})
        if head is None:
            raise SnapshotError(f"Atlas has no checkpoint for the {j} ledger")
        write_json(tmp / "ledgers" / f"{j}.head.json", head)
    pending: dict = {}
    for rec in world.ledger_store.pending.find({}, {"_id": 0}):
        if rec.get("shipment_id") and rec.get("role"):  # skip anything not written by the handshake
            pending.setdefault(rec["shipment_id"], {})[rec["role"]] = {
                "signer": rec.get("signer"), "signature": rec.get("signature")}
    write_json(tmp / "ledgers" / "pending_signatures.json", pending)

    # swap in: the documents file atomically, then the ledgers folder
    replace_path(tmp / "documents.json", data / "documents.json")
    old = data / ".ledgers_old"
    if old.exists():
        shutil.rmtree(old)
    if (data / "ledgers").exists():
        replace_path(data / "ledgers", old)
    replace_path(tmp / "ledgers", data / "ledgers")
    shutil.rmtree(old, ignore_errors=True)
    shutil.rmtree(tmp, ignore_errors=True)

    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest_path = data / "world.json"
    manifest = json.loads(read_text(manifest_path))
    manifest["snapshot_at"] = at
    write_json(manifest_path, manifest)
    world.snapshot_at = at
    log(f"offline copy of the Atlas data written to {data} ({at})")
    return at


def main(argv=None) -> int:
    import argparse
    from .seed import open_world

    ap = argparse.ArgumentParser(description="Copy the Atlas data into data/ for offline use.")
    ap.add_argument("--data", default=str(Path(__file__).resolve().parent.parent / "data"),
                    help="data folder (default: the project's data/, the one the dashboard uses)")
    args = ap.parse_args(argv)
    load_env()
    try:
        world = open_world(args.data, role="read")
        world.conn.close()
        take_snapshot(world)
    except Exception as e:  # unreachable Atlas, no world, or a world that fails verification
        print(f"ERROR: {type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
