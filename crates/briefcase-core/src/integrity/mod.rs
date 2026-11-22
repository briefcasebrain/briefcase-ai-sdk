//! Evidence primitives: RFC 8785 canonical JSON, Merkle proofs and (with the
//! `chain` feature) Ed25519 signing compatible with `briefcase.integrity`.

pub mod canonical;
pub mod merkle;
#[cfg(feature = "chain")]
pub mod signing;

pub use canonical::{
    canonical_json_v2, es_number_to_string, jcs_sha256_hex, numbers_as_doubles,
    parse_canonical_text, sha256_hex, CanonicalError, HashScheme, HASH_SCHEME_JCS1,
    HASH_SCHEME_JSON_SORTED1,
};
