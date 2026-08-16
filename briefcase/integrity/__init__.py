"""Tamper-evidence primitives: canonical JSON, hash chains, Ed25519 signing.

Imports on a bare install; only the signing helpers need the
``integrity`` extra (PyNaCl).
"""

from briefcase.integrity.canonical import canonical_json, canonical_json_compat, sha256_hex
from briefcase.integrity.chain import (
    GENESIS_PRIOR_HASH,
    HASH_SPEC_VERSION,
    HashChainAppender,
    HashChainEntry,
    HashChainStore,
    compute_entry_hash,
    compute_payload_hash,
    verify_chain_segment,
)
from briefcase.integrity.signing import (
    jwk_thumbprint,
    public_key_jwk,
    sign_digest,
    sign_json,
    verify_digest,
    verify_json_signature,
)
from briefcase.integrity.stores import (
    InMemoryHashChainStore,
    JsonlHashChainStore,
    TruncatedChainFileError,
)

__all__ = [
    "GENESIS_PRIOR_HASH",
    "HASH_SPEC_VERSION",
    "HashChainAppender",
    "HashChainEntry",
    "HashChainStore",
    "InMemoryHashChainStore",
    "JsonlHashChainStore",
    "TruncatedChainFileError",
    "canonical_json",
    "canonical_json_compat",
    "compute_entry_hash",
    "compute_payload_hash",
    "jwk_thumbprint",
    "public_key_jwk",
    "sha256_hex",
    "sign_digest",
    "sign_json",
    "verify_chain_segment",
    "verify_digest",
    "verify_json_signature",
]
