"""Chameleon hash with a t-of-n shared trapdoor (mechanism 3).

Scheme: Ateniese & de Medeiros (2004), the key-exposure-free chameleon hash.
The textbook Krawczyk-Rabin hash CH(m, r) = g^m * h^r was not used: anyone who
sees a block before and after one redaction can solve x = (m - m') / (r' - r)
and recover the trapdoor, which would make the threshold split worthless after
the first correction. Here every forgery uses fresh randomness k, so published
collisions do not reveal x.

    public key   y = g^x mod p
    hash         CH(m, r, s) = r - (y^e * g^s mod p) mod q,  e = H(m || r)
    forge        pick k;  r' = C + (g^k mod p) mod q;  s' = k - H(m' || r') * x mod q

The trapdoor x is split with Shamir secret sharing over Z_q.
"""
from __future__ import annotations

import hashlib
import secrets

# RFC 3526 group 14: a 2048-bit safe prime p = 2q + 1.
P = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74"
    "020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F1437"
    "4FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF05"
    "98DA48361C55D39A69163FA8FD24CF5F83655D23DCA3AD961C62F356208552BB"
    "9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF695581718"
    "3995497CEA956AE515D2261898FA051015728E5A8AACAA68FFFFFFFFFFFFFFFF",
    16,
)
Q = (P - 1) // 2
G = 4  # a quadratic residue mod P, so it generates the subgroup of prime order Q


def _e(message: bytes, r: int) -> int:
    return int.from_bytes(hashlib.sha256(message + r.to_bytes(256, "big")).digest(), "big") % Q


def keygen() -> tuple[int, int]:
    """CH.KeyGen: returns (public key y, trapdoor x)."""
    x = secrets.randbelow(Q - 1) + 1
    return pow(G, x, P), x


def ch_hash(y: int, message: bytes, r: int | None = None, s: int | None = None):
    """CH.Hash: returns (digest, r, s). Fresh randomness is drawn when r, s are omitted."""
    if r is None or s is None:
        r, s = secrets.randbelow(Q), secrets.randbelow(Q)
    digest = (r - (pow(y, _e(message, r), P) * pow(G, s, P) % P)) % Q
    return digest, r, s


def ch_verify(y: int, message: bytes, digest: int, r: int, s: int) -> bool:
    return ch_hash(y, message, r, s)[0] == digest


def ch_forge(x: int, digest: int, new_message: bytes) -> tuple[int, int]:
    """CH.Forge: randomness (r', s') so new_message hashes to the existing digest."""
    k = secrets.randbelow(Q - 1) + 1
    r = (digest + pow(G, k, P)) % Q
    s = (k - _e(new_message, r) * x) % Q
    return r, s


def split_secret(secret: int, n: int, t: int) -> list[tuple[int, int]]:
    """Shamir split of `secret` into n shares, any t of which reconstruct it."""
    coeffs = [secret] + [secrets.randbelow(Q) for _ in range(t - 1)]
    return [
        (i, sum(c * pow(i, j, Q) for j, c in enumerate(coeffs)) % Q)
        for i in range(1, n + 1)
    ]


def combine_shares(shares: list[tuple[int, int]], at: int = 0) -> int:
    """Lagrange interpolation at `at` (0 gives the secret)."""
    secret = 0
    for i, y_i in shares:
        num, den = 1, 1
        for j, _ in shares:
            if j != i:
                num = num * (at - j) % Q
                den = den * (i - j) % Q
        secret = (secret + y_i * num * pow(den, -1, Q)) % Q
    return secret


def shares_consistent(shares: list[tuple[int, int]], t: int, public_key: int) -> bool:
    """True if the first t shares rebuild the trapdoor of `public_key` and every other
    share lies on the same polynomial (so no share was altered)."""
    if len(shares) < t or len({i for i, _ in shares}) != len(shares):
        return False
    base = shares[:t]
    if pow(G, combine_shares(base), P) != public_key:
        return False
    return all(combine_shares(base, at=i) == y % Q for i, y in shares[t:])
