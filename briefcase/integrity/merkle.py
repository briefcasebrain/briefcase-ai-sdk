"""Merkle tree hashing and inclusion proofs (RFC 6962 section 2.1 layout).

Byte-for-byte the same functions as ``briefcase_core::integrity::merkle`` in
Rust, so a proof made by either verifies in the other. Leaves are 32-byte
digests (chain entry hashes):

* ``leaf_hash(d) = SHA-256(0x00 || d)``
* ``node_hash(l, r) = SHA-256(0x01 || l || r)``
* for ``n > 1`` leaves, with ``k`` the largest power of two below ``n``,
  ``root(D[n]) = node_hash(root(D[:k]), root(D[k:]))``.

Pure standard library.
"""

from __future__ import annotations

import hashlib
from typing import List, Optional, Sequence


def leaf_hash(leaf: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + leaf).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _split(n: int) -> int:
    k = 1
    while k << 1 < n:
        k <<= 1
    return k


def merkle_root(leaves: Sequence[bytes]) -> bytes:
    """Tree hash over ``leaves`` (each is hashed with :func:`leaf_hash`)."""
    n = len(leaves)
    if n == 0:
        return hashlib.sha256(b"").digest()
    if n == 1:
        return leaf_hash(leaves[0])
    k = _split(n)
    return node_hash(merkle_root(leaves[:k]), merkle_root(leaves[k:]))


def inclusion_proof(leaves: Sequence[bytes], index: int) -> List[bytes]:
    """Audit path (sibling hashes from the leaf upwards) for leaf ``index``."""
    if not 0 <= index < len(leaves):
        raise IndexError("leaf %d is outside a tree of %d leaves" % (index, len(leaves)))
    path: List[bytes] = []

    def build(nodes: Sequence[bytes], i: int) -> None:
        if len(nodes) <= 1:
            return
        k = _split(len(nodes))
        if i < k:
            build(nodes[:k], i)
            path.append(merkle_root(nodes[k:]))
        else:
            build(nodes[k:], i - k)
            path.append(merkle_root(nodes[:k]))

    build(leaves, index)
    return path


def root_from_proof(leaf: bytes, index: int, size: int, path: Sequence[bytes]) -> Optional[bytes]:
    """Recompute the root; ``None`` when the path does not fit ``index``/``size``."""
    if not 0 <= index < size:
        return None

    def walk(i: int, n: int, p: Sequence[bytes]) -> Optional[bytes]:
        if n == 1:
            return leaf_hash(leaf) if not p else None
        if not p:
            return None
        last, rest = p[-1], p[:-1]
        k = _split(n)
        if i < k:
            sub = walk(i, k, rest)
            return None if sub is None else node_hash(sub, last)
        sub = walk(i - k, n - k, rest)
        return None if sub is None else node_hash(last, sub)

    return walk(index, size, list(path))


def verify_inclusion(leaf: bytes, index: int, size: int, path: Sequence[bytes], root: bytes) -> bool:
    return root_from_proof(leaf, index, size, path) == root


__all__ = [
    "inclusion_proof",
    "leaf_hash",
    "merkle_root",
    "node_hash",
    "root_from_proof",
    "verify_inclusion",
]
