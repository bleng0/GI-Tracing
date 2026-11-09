"""Permissioned, hash-linked ledger with chameleon-hashed block bodies.

Each block header commits to the chameleon digest of its payload, not to the
payload itself. Block hashes, the prev_hash links and the writer's signature all
cover the header, so an authorised redaction (new payload + new randomness that
hash to the same digest) leaves every downstream link and signature valid.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from datetime import datetime, timezone

from .chameleon import G, P, ch_forge, ch_hash, ch_verify, combine_shares
from .crypto import Node, canonical, sha256_hex, verify


class LedgerError(Exception):
    pass


@dataclass
class Block:
    index: int
    prev_hash: str
    timestamp: str
    tx_type: str
    signer: str
    payload: dict
    ch_digest: int
    ch_r: int
    ch_s: int
    signature: str = ""

    def header(self) -> bytes:
        return canonical(
            [self.index, self.prev_hash, self.timestamp, self.tx_type, self.signer, hex(self.ch_digest)]
        )

    @property
    def hash(self) -> str:
        return sha256_hex(self.header())


class Ledger:
    """One jurisdiction's ledger. Only member nodes may append."""

    def __init__(self, jurisdiction: str, ch_public_key: int, members: list[Node], redaction_threshold: int):
        self.jurisdiction = jurisdiction
        self.ch_public_key = ch_public_key
        self.members = {n.name: n.public_key for n in members}
        self.redaction_threshold = redaction_threshold
        self.blocks: list[Block] = []
        # Called after every append (and so after every redaction); ledger_io uses it to save.
        self.on_change: Callable[[Ledger], None] | None = None

    def append(self, tx_type: str, payload: dict, signer: Node) -> Block:
        if self.members.get(signer.name) != signer.public_key:
            raise LedgerError(f"{signer.name} is not a member of the {self.jurisdiction} ledger")
        digest, r, s = ch_hash(self.ch_public_key, canonical(payload))
        block = Block(
            index=len(self.blocks),
            prev_hash=self.blocks[-1].hash if self.blocks else "0" * 64,
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            tx_type=tx_type,
            signer=signer.name,
            payload=payload,
            ch_digest=digest,
            ch_r=r,
            ch_s=s,
        )
        block.signature = signer.sign(block.header())
        self.blocks.append(block)
        if self.on_change is not None:
            self.on_change(self)
        return block

    def find(self, tx_type: str, **match) -> list[Block]:
        return [
            b for b in self.blocks
            if b.tx_type == tx_type and all(b.payload.get(k) == v for k, v in match.items())
        ]

    def verify_chain(self) -> list[str]:
        """Returns a list of problems; an empty list means the chain is valid."""
        problems = []
        prev = "0" * 64
        for i, b in enumerate(self.blocks):
            if b.index != i or b.prev_hash != prev:
                problems.append(f"block {i}: broken link")
            if not ch_verify(self.ch_public_key, canonical(b.payload), b.ch_digest, b.ch_r, b.ch_s):
                problems.append(f"block {i}: payload does not match its chameleon digest")
            key = self.members.get(b.signer)
            if key is None or not verify(key, b.header(), b.signature):
                problems.append(f"block {i}: bad or unauthorised signature")
            prev = b.hash
        problems += self._check_redactions()
        return problems

    def _check_redactions(self) -> list[str]:
        """Each redacted block must match its latest REDACTION record, so putting the
        pre-redaction payload and randomness back (which still match the digest) is caught."""
        latest = {}
        for b in self.blocks:
            if b.tx_type == "REDACTION":
                latest[b.payload.get("target_index")] = b
        problems = []
        for idx, rec in latest.items():
            if not isinstance(idx, int) or not 0 <= idx < len(self.blocks):
                problems.append(f"block {rec.index}: redaction record targets a missing block")
                continue
            t, p = self.blocks[idx], rec.payload
            if (p.get("target_block") != t.hash
                    or p.get("new_payload_sha256") != sha256_hex(canonical(t.payload))
                    or p.get("new_randomness") != [hex(t.ch_r), hex(t.ch_s)]):
                problems.append(f"block {idx}: does not match its latest redaction record (block {rec.index})")
        return problems

    def redact(self, index: int, new_payload: dict, shares: dict[str, tuple[int, int]],
               reason: str, recorder: Node) -> Block:
        """CH.Forge on block `index`, authorised by at least t trapdoor shares.

        `shares` maps regulator name -> Shamir share. The trapdoor is rebuilt here,
        used once and dropped. An audit REDACTION block is appended.
        """
        if self.members.get(recorder.name) != recorder.public_key:
            raise LedgerError(f"{recorder.name} is not a member of the {self.jurisdiction} ledger")
        if len(shares) < self.redaction_threshold:
            raise LedgerError(
                f"redaction needs {self.redaction_threshold} regulator shares, got {len(shares)}"
            )
        x = combine_shares(list(shares.values()))
        if pow(G, x, P) != self.ch_public_key:
            raise LedgerError("shares do not reconstruct this ledger's trapdoor")
        block = self.blocks[index]
        old_sha = sha256_hex(canonical(block.payload))
        block.ch_r, block.ch_s = ch_forge(x, block.ch_digest, canonical(new_payload))
        block.payload = new_payload
        return self.append(
            "REDACTION",
            {
                "target_index": index,
                "target_block": block.hash,
                "old_payload_sha256": old_sha,
                "new_payload_sha256": sha256_hex(canonical(new_payload)),
                "new_randomness": [hex(block.ch_r), hex(block.ch_s)],
                "approvers": sorted(shares),
                "reason": reason,
            },
            recorder,
        )
