"""Node identities (Ed25519) and canonical hashing shared by every tier."""
from __future__ import annotations

import hashlib
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# Roles a node can hold in the network.
ARTISAN_COOP = "ARTISAN_COOP"
BOTANICAL_LAB = "BOTANICAL_LAB"
TANNING_REGISTRY = "TANNING_REGISTRY"
CUSTOMS = "CUSTOMS"
GI_REGISTRY = "GI_REGISTRY"


def canonical(obj) -> bytes:
    """Deterministic JSON bytes, so the same record always hashes the same."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Node:
    """A participant (cooperative, lab, customs office, registry) with a signing key."""

    def __init__(self, name: str, role: str, jurisdiction: str, private_key: bytes | None = None):
        self.name = name
        self.role = role
        self.jurisdiction = jurisdiction
        self._key = (Ed25519PrivateKey.from_private_bytes(private_key) if private_key is not None
                     else Ed25519PrivateKey.generate())
        self.public_key = self._key.public_key().public_bytes_raw()

    def private_bytes(self) -> bytes:
        """Raw private key, for the local keystore only."""
        return self._key.private_bytes_raw()

    def sign(self, message: bytes) -> str:
        return self._key.sign(message).hex()


def verify(public_key: bytes, message: bytes, signature_hex: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            bytes.fromhex(signature_hex), message
        )
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def directory_of(nodes) -> dict[str, tuple[str, bytes]]:
    """Public key directory: node name -> (role, public key)."""
    return {n.name: (n.role, n.public_key) for n in nodes}
