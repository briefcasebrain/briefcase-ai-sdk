//! Ed25519 keys and signatures, interchangeable with `briefcase.integrity.signing`.
//!
//! Same conventions as the Python module, so a key made by either side works
//! in the other and each side verifies the other's signatures:
//!
//! * a private key is a 32-byte Ed25519 seed (RFC 8032);
//! * a public key travels as an RFC 8037 OKP JWK
//!   `{"kty":"OKP","crv":"Ed25519","x":<base64url>}` and is named by its
//!   RFC 7638 thumbprint (hex SHA-256 over `{"crv","kty","x"}`);
//! * a digest signature is Ed25519 over the 32 raw bytes of a hex SHA-256
//!   digest, encoded base64url without padding (`sign_digest` /
//!   `verify_digest` in Python).
//!
//! Key files ([`SigningKey::from_file`]) may hold a private JWK
//! (`{"kty":"OKP","crv":"Ed25519","d":<seed>,"x":<public>}`, what
//! `briefcase.integrity.keys.generate_key_file` writes), 64 hex characters,
//! or the 32 raw seed bytes.

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use ring::signature::{Ed25519KeyPair, KeyPair, UnparsedPublicKey, ED25519};
use std::fmt;
use std::path::Path;

use super::canonical::sha256_hex;
use super::merkle::hash_from_hex;

/// A key that could not be loaded or used.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KeyError(pub String);

impl fmt::Display for KeyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for KeyError {}

/// An Ed25519 private key (seed) and its public half.
pub struct SigningKey {
    seed: [u8; 32],
    pair: Ed25519KeyPair,
}

impl Clone for SigningKey {
    fn clone(&self) -> Self {
        Self::from_seed(&self.seed).expect("seed was valid when first loaded")
    }
}

impl fmt::Debug for SigningKey {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        // Never print the seed.
        write!(f, "SigningKey(key_id={})", self.key_id())
    }
}

impl SigningKey {
    /// Key from a 32-byte RFC 8032 seed.
    pub fn from_seed(seed: &[u8]) -> Result<Self, KeyError> {
        let seed: [u8; 32] = seed
            .try_into()
            .map_err(|_| KeyError(format!("an Ed25519 seed is 32 bytes, got {}", seed.len())))?;
        let pair = Ed25519KeyPair::from_seed_unchecked(&seed)
            .map_err(|e| KeyError(format!("invalid Ed25519 seed: {}", e)))?;
        Ok(Self { seed, pair })
    }

    /// Load a key file: private JWK, 64 hex characters, or 32 raw bytes.
    pub fn from_file(path: impl AsRef<Path>) -> Result<Self, KeyError> {
        let path = path.as_ref();
        let bytes = std::fs::read(path)
            .map_err(|e| KeyError(format!("cannot read key file {}: {}", path.display(), e)))?;
        Self::from_key_bytes(&bytes)
            .map_err(|e| KeyError(format!("key file {}: {}", path.display(), e.0)))
    }

    /// Parse the contents of a key file (see [`SigningKey::from_file`]).
    pub fn from_key_bytes(bytes: &[u8]) -> Result<Self, KeyError> {
        if bytes.len() == 32 && serde_json::from_slice::<serde_json::Value>(bytes).is_err() {
            return Self::from_seed(bytes);
        }
        let text = std::str::from_utf8(bytes)
            .map_err(|_| KeyError("not a JWK, hex seed or 32-byte seed".into()))?
            .trim();
        if text.starts_with('{') {
            let jwk: serde_json::Value = serde_json::from_str(text)
                .map_err(|e| KeyError(format!("not valid JSON: {}", e)))?;
            if jwk.get("kty").and_then(|v| v.as_str()) != Some("OKP")
                || jwk.get("crv").and_then(|v| v.as_str()) != Some("Ed25519")
            {
                return Err(KeyError("JWK is not an OKP Ed25519 key".into()));
            }
            let d = jwk.get("d").and_then(|v| v.as_str()).ok_or_else(|| {
                KeyError("JWK has no private part (\"d\"); this is a public key".into())
            })?;
            let seed = URL_SAFE_NO_PAD
                .decode(d.trim_end_matches('='))
                .map_err(|e| KeyError(format!("JWK \"d\" is not base64url: {}", e)))?;
            let key = Self::from_seed(&seed)?;
            if let Some(x) = jwk.get("x").and_then(|v| v.as_str()) {
                if x.trim_end_matches('=') != key.public_x() {
                    return Err(KeyError("JWK \"x\" does not match its \"d\"".into()));
                }
            }
            return Ok(key);
        }
        if text.len() == 64 {
            if let Some(seed) = hash_from_hex(text) {
                return Self::from_seed(&seed);
            }
        }
        Err(KeyError("not a JWK, hex seed or 32-byte seed".into()))
    }

    /// The 32-byte public key.
    pub fn public_key(&self) -> [u8; 32] {
        let mut out = [0u8; 32];
        out.copy_from_slice(self.pair.public_key().as_ref());
        out
    }

    /// base64url (no padding) of the public key: the JWK `x` member.
    pub fn public_x(&self) -> String {
        URL_SAFE_NO_PAD.encode(self.public_key())
    }

    /// The public OKP JWK.
    pub fn public_jwk(&self) -> serde_json::Value {
        serde_json::json!({"kty": "OKP", "crv": "Ed25519", "x": self.public_x()})
    }

    /// RFC 7638 thumbprint of the public JWK (hex SHA-256).
    pub fn key_id(&self) -> String {
        jwk_thumbprint(&self.public_x())
    }

    /// Sign raw bytes; base64url signature without padding.
    pub fn sign(&self, message: &[u8]) -> String {
        URL_SAFE_NO_PAD.encode(self.pair.sign(message).as_ref())
    }

    /// Sign the 32 raw bytes of a hex SHA-256 digest (Python `sign_digest`).
    pub fn sign_digest(&self, digest_hex: &str) -> Result<String, KeyError> {
        let raw = hash_from_hex(digest_hex)
            .ok_or_else(|| KeyError("digest must be 64 hex characters".into()))?;
        Ok(self.sign(&raw))
    }
}

/// RFC 7638 thumbprint (hex SHA-256) of an Ed25519 OKP JWK with this `x`.
pub fn jwk_thumbprint(x: &str) -> String {
    sha256_hex(format!(r#"{{"crv":"Ed25519","kty":"OKP","x":"{}"}}"#, x).as_bytes())
}

/// Whether `signature_b64` is a valid Ed25519 signature over `message` by
/// the key whose JWK `x` is `public_x`. Never panics on malformed input.
pub fn verify(message: &[u8], signature_b64: &str, public_x: &str) -> bool {
    let Ok(public) = URL_SAFE_NO_PAD.decode(public_x.trim_end_matches('=')) else {
        return false;
    };
    let Ok(signature) = URL_SAFE_NO_PAD.decode(signature_b64.trim_end_matches('=')) else {
        return false;
    };
    if public.len() != 32 || signature.len() != 64 {
        return false;
    }
    UnparsedPublicKey::new(&ED25519, public)
        .verify(message, &signature)
        .is_ok()
}

/// Python `verify_digest`: signature over the raw bytes of a hex digest.
pub fn verify_digest(digest_hex: &str, signature_b64: &str, public_x: &str) -> bool {
    match hash_from_hex(digest_hex) {
        Some(raw) => verify(&raw, signature_b64, public_x),
        None => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rfc8032_test_1_vector() {
        // RFC 8032 section 7.1, TEST 1 (empty message).
        let seed =
            hash_from_hex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
                .unwrap();
        let key = SigningKey::from_seed(&seed).unwrap();
        assert_eq!(
            key.public_key()
                .iter()
                .map(|b| format!("{:02x}", b))
                .collect::<String>(),
            "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
        );
        let sig = key.sign(b"");
        let raw = URL_SAFE_NO_PAD.decode(&sig).unwrap();
        assert_eq!(
            raw.iter().map(|b| format!("{:02x}", b)).collect::<String>(),
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155\
             5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
        );
        assert!(verify(b"", &sig, &key.public_x()));
        assert!(!verify(b"x", &sig, &key.public_x()));
    }

    #[test]
    fn key_files_in_every_accepted_form_load_the_same_key() {
        let seed = [7u8; 32];
        let key = SigningKey::from_seed(&seed).unwrap();
        let jwk = format!(
            r#"{{"kty":"OKP","crv":"Ed25519","d":"{}","x":"{}"}}"#,
            URL_SAFE_NO_PAD.encode(seed),
            key.public_x()
        );
        let hex: String = seed.iter().map(|b| format!("{:02x}", b)).collect();
        for bytes in [jwk.as_bytes(), hex.as_bytes(), &seed[..]] {
            assert_eq!(
                SigningKey::from_key_bytes(bytes).unwrap().key_id(),
                key.key_id()
            );
        }
        let public_only = format!(
            r#"{{"kty":"OKP","crv":"Ed25519","x":"{}"}}"#,
            key.public_x()
        );
        assert!(SigningKey::from_key_bytes(public_only.as_bytes())
            .unwrap_err()
            .0
            .contains("public key"));
        assert!(SigningKey::from_key_bytes(b"nope").is_err());
    }

    #[test]
    fn digest_signatures_round_trip_and_reject_tampering() {
        let key = SigningKey::from_seed(&[1u8; 32]).unwrap();
        let digest = sha256_hex(b"checkpoint");
        let sig = key.sign_digest(&digest).unwrap();
        assert!(verify_digest(&digest, &sig, &key.public_x()));
        let other = sha256_hex(b"other");
        assert!(!verify_digest(&other, &sig, &key.public_x()));
        let stranger = SigningKey::from_seed(&[2u8; 32]).unwrap();
        assert!(!verify_digest(&digest, &sig, &stranger.public_x()));
        assert!(!verify_digest(&digest, "!!", &key.public_x()));
        assert!(format!("{:?}", key).starts_with("SigningKey(key_id="));
    }
}
