"""Saves ledgers to data/ledgers/<NP|IN|PK>.json and loads them back.

A ledger file holds only blocks. The chameleon public key and the member keys a
loaded ledger is checked against come from the keystore (data/keys/), not from the
ledger file. Every load re-runs verify_chain() and refuses a ledger with problems.

What this does not protect against: anyone who can write data/keys/ holds every
signing key and share, so they can rewrite any ledger. And a ledger cut short at
its end is still a valid chain; seed.open_world() cross-checks the ledgers against
each other and against the document store, which catches most but not all
truncation; ledger_store.FileLedgerStore adds a signed checkpoint that catches the rest.

attach() saves the ledger after every append, and refuses to save if the file was
changed on disk by another process since this process last read or wrote it.
"""
from __future__ import annotations

import json
from pathlib import Path

from .keystore import write_json
from .ledger import Block, Ledger, LedgerError
from .storage import mtime_ns, read_text

FORMAT_VERSION = 1


def block_to_json(b: Block) -> dict:
    return {
        "index": b.index, "prev_hash": b.prev_hash, "timestamp": b.timestamp, "tx_type": b.tx_type,
        "signer": b.signer, "payload": b.payload, "ch_digest": hex(b.ch_digest),
        "ch_r": hex(b.ch_r), "ch_s": hex(b.ch_s), "signature": b.signature,
    }


def block_from_json(d: dict) -> Block:
    return Block(
        index=d["index"], prev_hash=d["prev_hash"], timestamp=d["timestamp"], tx_type=d["tx_type"],
        signer=d["signer"], payload=d["payload"], ch_digest=int(d["ch_digest"], 16),
        ch_r=int(d["ch_r"], 16), ch_s=int(d["ch_s"], 16), signature=d["signature"],
    )


def save_ledger(ledger: Ledger, path) -> None:
    write_json(Path(path), {
        "format": FORMAT_VERSION,
        "jurisdiction": ledger.jurisdiction,
        "blocks": [block_to_json(b) for b in ledger.blocks],
    })


def load_ledger(path, ledger: Ledger) -> Ledger:
    """Fills the empty `ledger` (built from the keystore) with the blocks in `path` and verifies it."""
    path = Path(path)
    if ledger.blocks:
        raise LedgerError("load_ledger needs an empty ledger")
    try:
        data = json.loads(read_text(path))
        if data.get("format") != FORMAT_VERSION:
            raise ValueError(f"unknown format {data.get('format')!r}")
        if data["jurisdiction"] != ledger.jurisdiction:
            raise ValueError(f"file is the {data['jurisdiction']} ledger, expected {ledger.jurisdiction}")
        blocks = [block_from_json(d) for d in data["blocks"]]
    except FileNotFoundError:
        raise LedgerError(f"missing ledger file {path}") from None
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise LedgerError(f"cannot read ledger file {path}: {e}") from e
    return verify_loaded(ledger, blocks, f"ledger file {path}")


def verify_loaded(ledger: Ledger, blocks: list[Block], source: str) -> Ledger:
    """Puts `blocks` into the empty `ledger` and refuses them if the chain does not verify."""
    ledger.blocks = blocks
    try:
        problems = ledger.verify_chain()
    except Exception as e:  # wrong types or out-of-range numbers in hand-edited data
        ledger.blocks = []
        raise LedgerError(f"cannot verify {source}: {e!r}") from e
    if problems:
        ledger.blocks = []
        raise LedgerError(f"{ledger.jurisdiction} ledger failed verification on load: " + "; ".join(problems))
    return ledger


def attach(ledger: Ledger, path, save_now: bool = True) -> None:
    """Saves `ledger` to `path` after every append (and now, if `save_now`)."""
    path = Path(path)
    state = {"mtime": mtime_ns(path)}

    def save(l: Ledger) -> None:
        if mtime_ns(path) != state["mtime"]:
            raise LedgerError(f"{path} was changed on disk by another process; reopen the world before writing")
        save_ledger(l, path)
        state["mtime"] = mtime_ns(path)

    ledger.on_change = save
    if save_now:
        save(ledger)
