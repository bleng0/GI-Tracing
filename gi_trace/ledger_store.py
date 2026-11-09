"""Where the ledgers are kept: LedgerStore, with a file backend and an Atlas backend.

  FileLedgerStore   data/ledgers/<J>.json (ledger_io), <J>.head.json, pending_signatures.json
  AtlasLedgerStore  the ledger_blocks, ledger_heads and pending_signatures collections

Both refuse a second block with the same dedupe_key ("ATTESTATION:<batch>:<cert_type>",
"BATCH_ANCHOR:<batch>", otherwise "BLOCK:<index>"); on Atlas the unique indexes made by
atlas_setup enforce it too. After every append both write a checkpoint (latest block
number and hash) signed with a key that stays on this PC (data/keys/checkpoint.json),
so blocks cut off the end of a ledger are detected on load. Blocks after the
checkpoint are accepted (they still need valid member signatures): that is what a
crash between writing a block and writing its checkpoint leaves behind. Rolling the
ledger and its checkpoint back together to an earlier signed state is not detected.

Both also keep the signatures collected for a border handshake, so a shipment
interrupted between signing and commit can be finished after a restart.
"""
from __future__ import annotations

import json
from pathlib import Path

from .crypto import Node, canonical, verify
from .keystore import write_json
from .ledger import Block, Ledger, LedgerError
from .ledger_io import attach as attach_file, block_from_json, block_to_json, load_ledger, verify_loaded
from .storage import read_text

GENESIS = "0" * 64


def dedupe_key(b: Block) -> str:
    if b.tx_type == "ATTESTATION":
        return f"ATTESTATION:{b.payload.get('batch_id')}:{b.payload.get('cert_type')}"
    if b.tx_type == "BATCH_ANCHOR":
        return f"BATCH_ANCHOR:{b.payload.get('batch_id')}"
    return f"BLOCK:{b.index}"


def _checkpoint_message(jurisdiction: str, index: int, block_hash: str) -> bytes:
    return canonical({"jurisdiction": jurisdiction, "index": index, "hash": block_hash})


def _tip(ledger: Ledger) -> tuple[int, str]:
    return len(ledger.blocks) - 1, (ledger.blocks[-1].hash if ledger.blocks else GENESIS)


def make_checkpoint(ledger: Ledger, key: Node) -> dict:
    index, block_hash = _tip(ledger)
    return {"jurisdiction": ledger.jurisdiction, "index": index, "hash": block_hash,
            "signature": key.sign(_checkpoint_message(ledger.jurisdiction, index, block_hash))}


def check_checkpoint(ledger: Ledger, head: dict | None, public_key: bytes) -> None:
    j = ledger.jurisdiction
    if head is None:
        raise LedgerError(f"the {j} ledger has no checkpoint")
    try:
        index, block_hash, signature = head["index"], head["hash"], head["signature"]
    except (KeyError, TypeError) as e:
        raise LedgerError(f"the {j} checkpoint is malformed: {e!r}") from e
    if not verify(public_key, _checkpoint_message(j, index, block_hash), signature):
        raise LedgerError(f"the {j} checkpoint signature is invalid")
    last = len(ledger.blocks) - 1
    if not isinstance(index, int) or index > last:
        raise LedgerError(f"the {j} ledger ends at block {last} but its signed checkpoint says block {index}: "
                          "blocks were removed")
    if (ledger.blocks[index].hash if index >= 0 else GENESIS) != block_hash:
        raise LedgerError(f"the {j} ledger's block {index} is not the one its signed checkpoint names")


def _refuse_duplicates(ledger: Ledger, saved: int) -> None:
    """Drops the unsaved blocks and raises if one repeats a dedupe_key."""
    seen = {dedupe_key(b) for b in ledger.blocks[:saved]}
    for b in ledger.blocks[saved:]:
        key = dedupe_key(b)
        if key in seen:
            del ledger.blocks[saved:]
            raise LedgerError(f"the {ledger.jurisdiction} ledger already has {key}; duplicate block refused")
        seen.add(key)


class FileLedgerStore:
    def __init__(self, ledgers_dir, checkpoint_key: Node):
        self.dir, self.key = Path(ledgers_dir), checkpoint_key

    def _ledger_path(self, j: str) -> Path:
        return self.dir / f"{j}.json"

    def _head_path(self, j: str) -> Path:
        return self.dir / f"{j}.head.json"

    def load(self, ledger: Ledger) -> Ledger:
        return load_ledger(self._ledger_path(ledger.jurisdiction), ledger)

    def check_checkpoint(self, ledger: Ledger) -> None:
        path = self._head_path(ledger.jurisdiction)
        try:
            head = json.loads(read_text(path))
        except FileNotFoundError:
            head = None
        except ValueError as e:
            raise LedgerError(f"corrupt checkpoint file {path}: {e}") from e
        check_checkpoint(ledger, head, self.key.public_key)

    def attach(self, ledger: Ledger, save_now: bool = True) -> None:
        attach_file(ledger, self._ledger_path(ledger.jurisdiction), save_now=False)
        save_file, state = ledger.on_change, {"saved": len(ledger.blocks)}

        def save(l: Ledger) -> None:
            _refuse_duplicates(l, state["saved"])
            save_file(l)
            write_json(self._head_path(l.jurisdiction), make_checkpoint(l, self.key))
            state["saved"] = len(l.blocks)

        ledger.on_change = save
        if save_now:
            save(ledger)

    def _pending_path(self) -> Path:
        return self.dir / "pending_signatures.json"

    def _pending(self) -> dict:
        path = self._pending_path()
        try:
            return json.loads(read_text(path))
        except FileNotFoundError:
            return {}
        except ValueError as e:
            raise LedgerError(f"corrupt {path}: {e}") from e

    def save_signature(self, shipment_id: str, role: str, signer: str, signature: str) -> None:
        data = self._pending()
        data.setdefault(shipment_id, {})[role] = {"signer": signer, "signature": signature}
        write_json(self._pending_path(), data)

    def load_signatures(self, shipment_id: str) -> dict[str, dict]:
        return self._pending().get(shipment_id, {})


class AtlasLedgerStore:
    def __init__(self, db, checkpoint_key: Node):
        self.blocks, self.heads = db["ledger_blocks"], db["ledger_heads"]
        self.pending, self.key = db["pending_signatures"], checkpoint_key

    def has_ledgers(self) -> bool:
        return self.blocks.find_one({}) is not None or self.heads.find_one({}) is not None

    def load(self, ledger: Ledger) -> Ledger:
        if ledger.blocks:
            raise LedgerError("load needs an empty ledger")
        j = ledger.jurisdiction
        try:
            blocks = [block_from_json(d) for d in self.blocks.find({"jurisdiction": j}, {"_id": 0}).sort("index", 1)]
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise LedgerError(f"cannot read the {j} ledger from Atlas: {e}") from e
        return verify_loaded(ledger, blocks, f"the {j} ledger in Atlas")

    def check_checkpoint(self, ledger: Ledger) -> None:
        check_checkpoint(ledger, self.heads.find_one({"jurisdiction": ledger.jurisdiction}, {"_id": 0}),
                         self.key.public_key)

    def attach(self, ledger: Ledger, save_now: bool = True) -> None:
        from pymongo.errors import DuplicateKeyError
        j, state = ledger.jurisdiction, {"saved": len(ledger.blocks)}

        def save(l: Ledger) -> None:
            _refuse_duplicates(l, state["saved"])
            for b in l.blocks[state["saved"]:]:
                try:
                    self.blocks.insert_one(dict(block_to_json(b), jurisdiction=j, dedupe_key=dedupe_key(b)))
                except DuplicateKeyError as e:
                    del l.blocks[b.index:]
                    raise LedgerError(f"Atlas refused {j} block {b.index} ({dedupe_key(b)}) as a duplicate; "
                                      "another process may be writing this ledger") from e
                state["saved"] = b.index + 1
                if b.tx_type == "REDACTION":  # the record is stored first, then the corrected block
                    t = l.blocks[b.payload["target_index"]]
                    self.blocks.update_one({"jurisdiction": j, "index": t.index},
                                           {"$set": {"payload": t.payload, "ch_r": hex(t.ch_r), "ch_s": hex(t.ch_s)}})
            self.heads.update_one({"jurisdiction": j}, {"$set": make_checkpoint(l, self.key)}, upsert=True)

        ledger.on_change = save
        if save_now:
            save(ledger)

    def save_signature(self, shipment_id: str, role: str, signer: str, signature: str) -> None:
        self.pending.update_one({"shipment_id": shipment_id, "role": role},
                                {"$set": {"signer": signer, "signature": signature}}, upsert=True)

    def load_signatures(self, shipment_id: str) -> dict[str, dict]:
        return {d["role"]: d for d in self.pending.find({"shipment_id": shipment_id}, {"_id": 0})}
