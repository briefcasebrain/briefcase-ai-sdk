//! Merkle tree hashing and inclusion proofs (RFC 6962 section 2.1 layout).
//!
//! Leaves are 32-byte digests (chain entry hashes). The tree hash is
//! `MTH({}) = SHA-256()`, `MTH({d}) = SHA-256(0x00 || d)` and, for `n > 1`
//! with `k` the largest power of two below `n`,
//! `MTH(D[n]) = SHA-256(0x01 || MTH(D[0:k]) || MTH(D[k:n]))`. The domain
//! separation bytes stop a leaf from being passed off as an inner node.
//!
//! Python's `briefcase.integrity.merkle` implements the same functions, so
//! a proof made here verifies there and vice versa.

use sha2::{Digest, Sha256};

pub type Hash = [u8; 32];

/// `SHA-256(0x00 || leaf)`.
pub fn leaf_hash(leaf: &[u8]) -> Hash {
    let mut h = Sha256::new();
    h.update([0u8]);
    h.update(leaf);
    h.finalize().into()
}

/// `SHA-256(0x01 || left || right)`.
pub fn node_hash(left: &Hash, right: &Hash) -> Hash {
    let mut h = Sha256::new();
    h.update([1u8]);
    h.update(left);
    h.update(right);
    h.finalize().into()
}

fn split_point(n: usize) -> usize {
    // Largest power of two strictly less than n (n >= 2).
    let mut k = 1;
    while k << 1 < n {
        k <<= 1;
    }
    k
}

/// Merkle tree hash over `leaves` (each leaf is hashed with [`leaf_hash`]).
pub fn merkle_root(leaves: &[Hash]) -> Hash {
    match leaves.len() {
        0 => Sha256::digest([]).into(),
        1 => leaf_hash(&leaves[0]),
        n => {
            let k = split_point(n);
            node_hash(&merkle_root(&leaves[..k]), &merkle_root(&leaves[k..]))
        }
    }
}

/// Audit path for leaf `index`: sibling hashes from the leaf upwards.
pub fn inclusion_proof(leaves: &[Hash], index: usize) -> Option<Vec<Hash>> {
    if index >= leaves.len() {
        return None;
    }
    let mut path = Vec::new();
    build_path(leaves, index, &mut path);
    Some(path)
}

fn build_path(leaves: &[Hash], index: usize, path: &mut Vec<Hash>) {
    let n = leaves.len();
    if n <= 1 {
        return;
    }
    let k = split_point(n);
    if index < k {
        build_path(&leaves[..k], index, path);
        path.push(merkle_root(&leaves[k..]));
    } else {
        build_path(&leaves[k..], index - k, path);
        path.push(merkle_root(&leaves[..k]));
    }
}

/// Recompute the root from a leaf and its audit path; `None` when the path
/// has the wrong length for `index`/`size`.
pub fn root_from_proof(leaf: &Hash, index: usize, size: usize, path: &[Hash]) -> Option<Hash> {
    if index >= size {
        return None;
    }
    fn walk(leaf: &Hash, index: usize, size: usize, path: &[Hash]) -> Option<Hash> {
        if size == 1 {
            return if path.is_empty() {
                Some(leaf_hash(leaf))
            } else {
                None
            };
        }
        let (last, rest) = path.split_last()?;
        let k = split_point(size);
        if index < k {
            Some(node_hash(&walk(leaf, index, k, rest)?, last))
        } else {
            Some(node_hash(last, &walk(leaf, index - k, size - k, rest)?))
        }
    }
    walk(leaf, index, size, path)
}

/// Whether `path` proves `leaf` is leaf `index` of a tree of `size` leaves
/// with root `root`.
pub fn verify_inclusion(
    leaf: &Hash,
    index: usize,
    size: usize,
    path: &[Hash],
    root: &Hash,
) -> bool {
    root_from_proof(leaf, index, size, path).as_ref() == Some(root)
}

/// Decode a 64-character hex digest.
pub fn hash_from_hex(hex: &str) -> Option<Hash> {
    if hex.len() != 64 {
        return None;
    }
    let mut out = [0u8; 32];
    for (i, chunk) in hex.as_bytes().chunks(2).enumerate() {
        let s = std::str::from_utf8(chunk).ok()?;
        out[i] = u8::from_str_radix(s, 16).ok()?;
    }
    Some(out)
}

/// Lowercase hex of a digest.
pub fn hash_to_hex(hash: &Hash) -> String {
    hash.iter().map(|b| format!("{:02x}", b)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn leaves(n: usize) -> Vec<Hash> {
        (0..n)
            .map(|i| Sha256::digest(i.to_le_bytes()).into())
            .collect()
    }

    #[test]
    fn every_leaf_of_every_size_proves_inclusion() {
        for n in 1..=33 {
            let ls = leaves(n);
            let root = merkle_root(&ls);
            for i in 0..n {
                let path = inclusion_proof(&ls, i).unwrap();
                assert!(
                    verify_inclusion(&ls[i], i, n, &path, &root),
                    "n={} i={}",
                    n,
                    i
                );
                // The proof does not transfer to another leaf or position.
                let other = (i + 1) % n;
                if other != i {
                    assert!(!verify_inclusion(&ls[other], i, n, &path, &root));
                }
            }
        }
    }

    #[test]
    fn a_short_or_long_path_is_rejected() {
        let ls = leaves(5);
        let root = merkle_root(&ls);
        let mut path = inclusion_proof(&ls, 2).unwrap();
        path.pop();
        assert!(!verify_inclusion(&ls[2], 2, 5, &path, &root));
        let mut path = inclusion_proof(&ls, 2).unwrap();
        path.push(root);
        assert!(!verify_inclusion(&ls[2], 2, 5, &path, &root));
        assert!(!verify_inclusion(&ls[2], 7, 5, &[], &root));
    }

    #[test]
    fn hex_round_trip() {
        let h = leaves(1)[0];
        assert_eq!(hash_from_hex(&hash_to_hex(&h)), Some(h));
        assert_eq!(hash_from_hex("zz"), None);
    }
}
