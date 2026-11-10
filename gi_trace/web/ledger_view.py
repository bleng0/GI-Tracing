"""Ledger viewer (admin only): the blocks of one ledger as they are stored now, and a
fresh chain check.

The blocks are read again on every request (the files offline, ledger_blocks on Atlas),
not taken from the world the dashboard opened, so an edit made while the dashboard runs
shows up. They are checked with Ledger.verify_chain() (links, chameleon digests, member
signatures, redaction records) and against the signed checkpoint, using the keys the
dashboard loaded from data/keys/.
"""
from __future__ import annotations

import json
import re

from ..ledger import Ledger, LedgerError
from ..ledger_io import block_from_json

PAGE_SIZE = 50


def inspect(world, j: str) -> dict:
    """{"blocks": [row, ...], "problems": [text, ...], "bad": {block index, ...}}"""
    loaded = world.ledgers[j]
    ledger = Ledger(j, loaded.ch_public_key, [], loaded.redaction_threshold)
    ledger.members = dict(loaded.members)
    problems, rows = [], []
    try:
        raw = world.ledger_store.raw_blocks(j)
    except LedgerError as e:
        return {"blocks": [], "problems": [_no_paths(str(e), world)], "bad": set()}
    unread = []  # blocks from the first unreadable one on, shown as stored but not checked
    for i, d in enumerate(raw):
        try:
            ledger.blocks.append(block_from_json(d))
        except Exception as e:  # hand-edited into the wrong shape
            problems.append(f"block {i}: cannot be read ({type(e).__name__}); this and later blocks not checked")
            unread = raw[i:]
            break
    try:
        problems += ledger.verify_chain()
    except Exception as e:  # wrong types or out-of-range numbers
        problems.append(f"the chain cannot be checked: {type(e).__name__}")
    try:
        world.ledger_store.check_checkpoint(ledger)
    except LedgerError as e:
        problems.append(_no_paths(str(e), world))
    except Exception as e:  # a checkpoint naming a block that cannot be hashed
        problems.append(f"the signed checkpoint cannot be checked: {type(e).__name__}")
    bad = {int(m) for p in problems for m in re.findall(r"\bblock (\d+)\b", p)}
    for b in ledger.blocks:
        rows.append({"index": b.index, "timestamp": b.timestamp, "tx_type": b.tx_type, "signer": b.signer,
                     "hash": b.hash, "prev_hash": b.prev_hash,
                     "payload": json.dumps(b.payload, sort_keys=True, ensure_ascii=False, default=str)})
    for i, d in enumerate(unread, start=len(ledger.blocks)):
        bad.add(i)
        rows.append({"index": i, "timestamp": "", "tx_type": "(unreadable)", "signer": "", "hash": "", "prev_hash": "",
                     "payload": json.dumps(d, sort_keys=True, ensure_ascii=False, default=str)})
    return {"blocks": rows, "problems": problems, "bad": bad}


def _no_paths(text: str, world) -> str:
    return text.replace(str(world.data_dir), "data") if world.data_dir else text
