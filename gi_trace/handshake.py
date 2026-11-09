"""Multi-signature border state machine, modelled on MP-SATP (mechanism 2).

    initiate  -> LOCK block on the origin ledger                    state LOCKED
    sign      -> each required party signs the commit message
    commit    -> MINT on destination ledger, then TRANSFERRED on origin   COMMITTED
    abort     -> UNLOCK on the origin ledger                         ABORTED

Required signers are read from the origin ledger, not supplied by the caller:
the cooperative from the BATCH_ANCHOR, and the lab / tanning registry from the
ATTESTATION blocks. A hemp batch needs ARTISAN_COOP, BOTANICAL_LAB,
ORIGIN_CUSTOMS and DEST_CUSTOMS; a leather batch swaps the lab for
TANNING_REGISTRY; a batch with both materials needs all five.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .crypto import ARTISAN_COOP, BOTANICAL_LAB, TANNING_REGISTRY, Node, canonical, verify
from .ledger import Block, Ledger
from .oracle import REQUIRED_CERT

MATERIAL_SIGNER_ROLE = {"hemp": BOTANICAL_LAB, "leather": TANNING_REGISTRY}


class HandshakeError(Exception):
    pass


@dataclass
class Shipment:
    shipment_id: str
    batch_id: str
    required: dict[str, str]          # role -> node name
    message: bytes
    lock_block: str
    state: str = "LOCKED"
    signatures: dict[str, str] = field(default_factory=dict)


class BorderGateway:
    def __init__(self, origin: Ledger, dest: Ledger, origin_customs: Node, dest_customs: Node,
                 directory: dict[str, tuple[str, bytes]]):
        self.origin, self.dest = origin, dest
        self.origin_customs, self.dest_customs = origin_customs, dest_customs
        self.directory = directory
        self.shipments: dict[str, Shipment] = {}

    def initiate(self, shipment_id: str, batch_id: str, waybill: dict) -> bytes:
        """Locks the batch on the origin ledger; returns the message every party must sign."""
        if shipment_id in self.shipments or self.origin.find("LOCK", shipment_id=shipment_id):
            raise HandshakeError(f"shipment {shipment_id} already exists")
        anchors = self.origin.find("BATCH_ANCHOR", batch_id=batch_id)
        if not anchors:
            raise HandshakeError(f"{batch_id} has no verified attestations on the {self.origin.jurisdiction} ledger")
        anchor = anchors[-1].payload

        unlocked = {b.payload["shipment_id"] for b in self.origin.find("UNLOCK", batch_id=batch_id)}
        if any(b.payload["shipment_id"] not in unlocked for b in self.origin.find("LOCK", batch_id=batch_id)):
            raise HandshakeError(f"{batch_id} is already locked or exported")

        coop = self.directory.get(anchor["coop_id"])
        if coop is None or coop[0] != ARTISAN_COOP:
            raise HandshakeError(f"cooperative {anchor['coop_id']} has no registered signing key")
        required = {
            ARTISAN_COOP: anchor["coop_id"],
            "ORIGIN_CUSTOMS": self.origin_customs.name,
            "DEST_CUSTOMS": self.dest_customs.name,
        }
        for material in anchor["materials"]:
            att = self.origin.find("ATTESTATION", batch_id=batch_id, cert_type=REQUIRED_CERT[material])
            if not att:
                raise HandshakeError(f"{batch_id} is missing its {REQUIRED_CERT[material]} on the ledger")
            required[MATERIAL_SIGNER_ROLE[material]] = att[-1].payload["signer"]

        lock = self.origin.append(
            "LOCK",
            {
                "shipment_id": shipment_id,
                "batch_id": batch_id,
                "dest": self.dest.jurisdiction,
                "waybill": waybill,
                "required_signers": required,
            },
            self.origin_customs,
        )
        message = self._message(shipment_id, batch_id, lock.hash)
        self.shipments[shipment_id] = Shipment(shipment_id, batch_id, required, message, lock.hash)
        return message

    def _message(self, shipment_id: str, batch_id: str, lock_hash: str) -> bytes:
        return canonical({
            "shipment_id": shipment_id,
            "batch_id": batch_id,
            "origin": self.origin.jurisdiction,
            "dest": self.dest.jurisdiction,
            "lock_block": lock_hash,
        })

    def _recover(self, shipment_id: str) -> Shipment | None:
        """Rebuilds a shipment from its LOCK block, e.g. after a restart. Signatures
        collected before the restart are not on the ledger and must be given again."""
        locks = [b for b in self.origin.find("LOCK", shipment_id=shipment_id)
                 if b.payload.get("dest") == self.dest.jurisdiction]
        if not locks:
            return None
        lock = locks[-1]
        if self.origin.find("TRANSFERRED", shipment_id=shipment_id):
            state = "COMMITTED"
        elif self.origin.find("UNLOCK", shipment_id=shipment_id):
            state = "ABORTED"
        else:
            state = "LOCKED"
        s = Shipment(shipment_id, lock.payload["batch_id"], dict(lock.payload["required_signers"]),
                     self._message(shipment_id, lock.payload["batch_id"], lock.hash), lock.hash, state)
        self.shipments[shipment_id] = s
        return s

    def sign(self, shipment_id: str, role: str, signature: str) -> None:
        s = self._locked(shipment_id)
        if role not in s.required:
            raise HandshakeError(f"{role} is not a signer for {shipment_id}")
        _, public_key = self.directory[s.required[role]]
        if not verify(public_key, s.message, signature):
            raise HandshakeError(f"invalid {role} signature")
        s.signatures[role] = signature

    def commit(self, shipment_id: str) -> Block:
        s = self._locked(shipment_id)
        missing = sorted(set(s.required) - set(s.signatures))
        if missing:
            raise HandshakeError(f"missing signatures: {', '.join(missing)}")
        mint = self.dest.append(
            "MINT",
            {
                "shipment_id": shipment_id,
                "batch_id": s.batch_id,
                "origin": self.origin.jurisdiction,
                "origin_lock_block": s.lock_block,
                "signatures": {role: {"signer": s.required[role], "sig": sig} for role, sig in s.signatures.items()},
            },
            self.dest_customs,
        )
        self.origin.append(
            "TRANSFERRED",
            {"shipment_id": shipment_id, "batch_id": s.batch_id, "dest_mint_block": mint.hash},
            self.origin_customs,
        )
        s.state = "COMMITTED"
        return mint

    def abort(self, shipment_id: str, reason: str) -> None:
        s = self._locked(shipment_id)
        self.origin.append(
            "UNLOCK",
            {"shipment_id": shipment_id, "batch_id": s.batch_id, "reason": reason},
            self.origin_customs,
        )
        s.state = "ABORTED"

    def _locked(self, shipment_id: str) -> Shipment:
        s = self.shipments.get(shipment_id) or self._recover(shipment_id)
        if s is None:
            raise HandshakeError(f"unknown shipment {shipment_id}")
        if s.state != "LOCKED":
            raise HandshakeError(f"shipment {shipment_id} is {s.state}")
        return s
