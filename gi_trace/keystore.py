"""Saves and loads node keys, per-ledger chameleon public keys and Shamir shares.

    data/keys/nodes.json          node name -> role, jurisdiction, Ed25519 private key (hex)
    data/keys/ledgers.json        jurisdiction -> chameleon public key y (hex), redaction threshold
    data/keys/shares/<name>.json  one file per regulator: its Shamir share of each ledger's trapdoor
    data/keys/checkpoint.json     private key that signs each ledger's latest-block checkpoint

The chameleon trapdoor itself is never written, only its shares. In real use each
share would be held by a different regulator on its own machine; locally they all
sit in one folder. That is a demo convenience, not key custody.
"""
from __future__ import annotations

import json
from pathlib import Path

from .crypto import Node
from .storage import atomic_write_text, read_text


class KeystoreError(Exception):
    pass


def write_json(path: Path, data) -> None:
    atomic_write_text(path, json.dumps(data, indent=1, sort_keys=True))


def _read_json(path: Path):
    try:
        return json.loads(read_text(path))
    except FileNotFoundError:
        raise KeystoreError(f"missing key file {path}") from None
    except ValueError as e:
        raise KeystoreError(f"corrupt key file {path}: {e}") from e


def save_keys(keys_dir, nodes: dict[str, Node], ledgers: dict, shares: dict[str, dict[str, tuple[int, int]]]) -> None:
    keys_dir = Path(keys_dir)
    write_json(keys_dir / "nodes.json", {
        n.name: {"role": n.role, "jurisdiction": n.jurisdiction, "private_key": n.private_bytes().hex()}
        for n in nodes.values()
    })
    write_json(keys_dir / "ledgers.json", {
        j: {"ch_public_key": hex(l.ch_public_key), "redaction_threshold": l.redaction_threshold}
        for j, l in ledgers.items()
    })
    holders: dict[str, dict] = {}
    for j, by_holder in shares.items():
        for holder, (i, y) in by_holder.items():
            holders.setdefault(holder, {})[j] = [i, hex(y)]
    for holder, held in holders.items():
        write_json(keys_dir / "shares" / f"{holder}.json", held)


def load_nodes(keys_dir) -> dict[str, Node]:
    data = _read_json(Path(keys_dir) / "nodes.json")
    try:
        return {
            name: Node(name, d["role"], d["jurisdiction"], bytes.fromhex(d["private_key"]))
            for name, d in data.items()
        }
    except (KeyError, ValueError, TypeError, AttributeError) as e:
        raise KeystoreError(f"bad node key entry: {e}") from e


def load_ledger_keys(keys_dir) -> dict[str, dict]:
    data = _read_json(Path(keys_dir) / "ledgers.json")
    try:
        return {
            j: {"ch_public_key": int(d["ch_public_key"], 16), "redaction_threshold": int(d["redaction_threshold"])}
            for j, d in data.items()
        }
    except (KeyError, ValueError, TypeError, AttributeError) as e:
        raise KeystoreError(f"bad ledger key entry: {e}") from e


def load_shares(keys_dir, holders) -> dict[str, dict[str, tuple[int, int]]]:
    """Shares held by each name in `holders`, as {jurisdiction: {holder: (i, y)}}."""
    shares: dict[str, dict[str, tuple[int, int]]] = {}
    for holder in holders:
        held = _read_json(Path(keys_dir) / "shares" / f"{holder}.json")
        try:
            for j, (i, y) in held.items():
                shares.setdefault(j, {})[holder] = (int(i), int(y, 16))
        except (ValueError, TypeError, AttributeError) as e:
            raise KeystoreError(f"bad share file for {holder}: {e}") from e
    return shares


CHECKPOINT_SIGNER = "LEDGER-CHECKPOINT"


def new_checkpoint_key() -> Node:
    return Node(CHECKPOINT_SIGNER, "CHECKPOINT", "LOCAL")


def save_checkpoint_key(keys_dir, key: Node) -> None:
    write_json(Path(keys_dir) / "checkpoint.json", {"private_key": key.private_bytes().hex()})


def load_checkpoint_key(keys_dir) -> Node:
    data = _read_json(Path(keys_dir) / "checkpoint.json")
    try:
        return Node(CHECKPOINT_SIGNER, "CHECKPOINT", "LOCAL", bytes.fromhex(data["private_key"]))
    except (KeyError, ValueError, TypeError, AttributeError) as e:
        raise KeystoreError(f"bad checkpoint key: {e}") from e
