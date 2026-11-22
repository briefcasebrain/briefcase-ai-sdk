//! Canonical JSON for hashing: RFC 8785 (JCS), hash scheme `jcs-1`.
//!
//! [`canonical_json_v2`] is byte-identical to Python's
//! `briefcase.integrity.canonical_json_v2` and TypeScript's `canonicalJsonV2`
//! on every value all three accept; `tests/vectors/canonical_json_v2.json`
//! holds the shared golden vectors.
//!
//! * Object members are sorted by their names' UTF-16 code units
//!   (RFC 8785 section 3.2.3), not by bytes or code points.
//! * Numbers are IEEE-754 doubles written with ECMAScript
//!   `Number::toString` (ECMA-262 7.1.12.1); `-0` is written `0`.
//! * Strings use ECMAScript `JSON.stringify` escaping: `\"`, `\\`, `\b \t \n
//!   \f \r`, other C0 controls as lowercase `\u00hh`, everything else as is.
//!
//! Integers: a JSON integer (`i64`/`u64` in `serde_json`) is accepted when a
//! double holds its value exactly and is written as that double (so
//! `2^60` is written `1152921504606847000`, as JavaScript would); any other
//! integer is rejected with [`CanonicalError::InexactInteger`] rather than
//! rounded. Encode large identifiers as strings.
//!
//! Reading canonical text back: ECMAScript writes a large double such as
//! `2^60` as `1152921504606847000`, which is not the double's exact integer
//! value, so a parser that keeps integer literals as integers would then
//! refuse it. Verifiers therefore read canonical text with
//! [`parse_canonical_text`], which (like `JSON.parse`) treats every number
//! beyond +/-(2^53 - 1) as a double.

use serde_json::Value;
use sha2::{Digest, Sha256};
use std::fmt;

/// Identifier of the RFC 8785 scheme, recorded next to every hash it makes.
pub const HASH_SCHEME_JCS1: &str = "jcs-1";
/// Identifier of the legacy sorted-keys profile (Python `canonical_json`).
/// This crate only names it so verifiers can report it; it does not write it.
pub const HASH_SCHEME_JSON_SORTED1: &str = "json-sorted-1";

/// Hash schemes known to this crate.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HashScheme {
    /// Sorted keys, Python float repr. Not reproducible across languages for
    /// floats; kept for data written before `jcs-1`.
    JsonSorted1,
    /// RFC 8785.
    Jcs1,
}

impl HashScheme {
    pub fn id(self) -> &'static str {
        match self {
            HashScheme::JsonSorted1 => HASH_SCHEME_JSON_SORTED1,
            HashScheme::Jcs1 => HASH_SCHEME_JCS1,
        }
    }

    /// Parse a recorded `hash_scheme` value; `None` for an unknown one.
    pub fn parse(id: &str) -> Option<Self> {
        match id {
            HASH_SCHEME_JSON_SORTED1 => Some(HashScheme::JsonSorted1),
            HASH_SCHEME_JCS1 => Some(HashScheme::Jcs1),
            _ => None,
        }
    }
}

/// Why a value has no `jcs-1` encoding.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CanonicalError {
    /// NaN or an infinity (not representable in JSON).
    NonFinite,
    /// An integer no IEEE-754 double holds exactly.
    InexactInteger(String),
}

impl fmt::Display for CanonicalError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CanonicalError::NonFinite => {
                write!(f, "jcs-1 numbers must be finite (NaN and Infinity are not JSON)")
            }
            CanonicalError::InexactInteger(n) => write!(
                f,
                "jcs-1 cannot represent the integer {} exactly (RFC 8785 numbers are IEEE-754 doubles); encode it as a string",
                n
            ),
        }
    }
}

impl std::error::Error for CanonicalError {}

/// ECMAScript `Number::toString(x)` for a finite double.
///
/// `{:e}` formatting yields the shortest digit string that round-trips to
/// `x` (and the closest one when several are equally short), which is the
/// `s`/`k` of the ECMAScript algorithm; this function applies its layout
/// rules.
pub fn es_number_to_string(x: f64) -> Result<String, CanonicalError> {
    if !x.is_finite() {
        return Err(CanonicalError::NonFinite);
    }
    if x == 0.0 {
        return Ok("0".to_string());
    }
    if x < 0.0 {
        return Ok(format!("-{}", es_number_to_string(-x)?));
    }
    // `{:e}` gives the shortest round-tripping digit count k, but breaks a
    // tie between two equally short candidates upwards. ECMAScript (Note 2)
    // wants the candidate closest to x, ties to even, which is the exact
    // decimal expansion of x rounded to k digits: `{:.prec$e}` does that
    // (round half to even), and it round-trips because it is at least as
    // close to x as the shortest candidate.
    let shortest = format!("{:e}", x);
    let shortest_mantissa = shortest
        .split_once('e')
        .map_or(shortest.as_str(), |(m, _)| m);
    let k_digits = shortest_mantissa
        .chars()
        .filter(|c| c.is_ascii_digit())
        .count();
    let sci = format!("{:.*e}", k_digits.saturating_sub(1), x);
    let (mantissa, exp) = sci.split_once('e').unwrap_or((sci.as_str(), "0"));
    let exponent: i32 = exp.parse().unwrap_or(0);
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let digits = digits.trim_end_matches('0');
    let digits = if digits.is_empty() { "0" } else { digits };
    let k = digits.len() as i32;
    // x == 0.<digits> * 10^n
    let n = exponent + 1;
    let out = if k <= n && n <= 21 {
        format!("{}{}", digits, "0".repeat((n - k) as usize))
    } else if 0 < n && n <= 21 {
        format!("{}.{}", &digits[..n as usize], &digits[n as usize..])
    } else if -6 < n && n <= 0 {
        format!("0.{}{}", "0".repeat((-n) as usize), digits)
    } else {
        let e = n - 1;
        let sign = if e >= 0 { '+' } else { '-' };
        if k == 1 {
            format!("{}e{}{}", digits, sign, e.abs())
        } else {
            format!("{}.{}e{}{}", &digits[..1], &digits[1..], sign, e.abs())
        }
    };
    Ok(out)
}

/// Largest power of two strictly above every `u64`, as a double (2^64).
const TWO_POW_64: f64 = 18_446_744_073_709_551_616.0;

fn encode_number(n: &serde_json::Number, out: &mut String) -> Result<(), CanonicalError> {
    if let Some(u) = n.as_u64() {
        let d = u as f64;
        if d >= TWO_POW_64 || d as u64 != u {
            return Err(CanonicalError::InexactInteger(u.to_string()));
        }
        out.push_str(&es_number_to_string(d)?);
    } else if let Some(i) = n.as_i64() {
        let d = i as f64;
        if d as i64 != i {
            return Err(CanonicalError::InexactInteger(i.to_string()));
        }
        out.push_str(&es_number_to_string(d)?);
    } else if let Some(f) = n.as_f64() {
        out.push_str(&es_number_to_string(f)?);
    } else {
        return Err(CanonicalError::NonFinite);
    }
    Ok(())
}

fn encode_string(s: &str, out: &mut String) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{08}' => out.push_str("\\b"),
            '\u{09}' => out.push_str("\\t"),
            '\u{0A}' => out.push_str("\\n"),
            '\u{0C}' => out.push_str("\\f"),
            '\u{0D}' => out.push_str("\\r"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

fn encode(value: &Value, out: &mut String) -> Result<(), CanonicalError> {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(n) => encode_number(n, out)?,
        Value::String(s) => encode_string(s, out),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                encode(item, out)?;
            }
            out.push(']');
        }
        Value::Object(map) => {
            let mut keys: Vec<(Vec<u16>, &String)> = map
                .keys()
                .map(|k| (k.encode_utf16().collect(), k))
                .collect();
            keys.sort_by(|a, b| a.0.cmp(&b.0));
            out.push('{');
            for (i, (_, key)) in keys.into_iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                encode_string(key, out);
                out.push(':');
                encode(&map[key], out)?;
            }
            out.push('}');
        }
    }
    Ok(())
}

/// RFC 8785 canonical JSON text of `value` (hash scheme `jcs-1`).
///
/// Rust strings are always well-formed Unicode, so the lone-surrogate
/// rejection of the other implementations happens at parse time here
/// (`serde_json` refuses them).
pub fn canonical_json_v2(value: &Value) -> Result<String, CanonicalError> {
    let mut out = String::new();
    encode(value, &mut out)?;
    Ok(out)
}

/// Turn integers beyond +/-(2^53 - 1) into the nearest double, as
/// `JSON.parse` would. Use on values parsed from canonical text only: on
/// in-memory data it would hide exactly the rounding `jcs-1` refuses.
pub fn numbers_as_doubles(value: Value) -> Value {
    const MAX_SAFE: u64 = (1u64 << 53) - 1;
    match value {
        Value::Number(n) => {
            let big = match (n.as_u64(), n.as_i64()) {
                (Some(u), _) => u > MAX_SAFE,
                (None, Some(i)) => i.unsigned_abs() > MAX_SAFE,
                _ => false,
            };
            if big {
                let d = n.as_f64().unwrap_or(f64::NAN);
                serde_json::Number::from_f64(d).map_or(Value::Number(n), Value::Number)
            } else {
                Value::Number(n)
            }
        }
        Value::Array(items) => Value::Array(items.into_iter().map(numbers_as_doubles).collect()),
        Value::Object(map) => Value::Object(
            map.into_iter()
                .map(|(k, v)| (k, numbers_as_doubles(v)))
                .collect(),
        ),
        other => other,
    }
}

/// Parse JSON text with numbers read as doubles (see the module docs).
pub fn parse_canonical_text(text: &str) -> Result<Value, serde_json::Error> {
    serde_json::from_str(text).map(numbers_as_doubles)
}

/// Hex SHA-256 of `data`.
pub fn sha256_hex(data: &[u8]) -> String {
    format!("{:x}", Sha256::digest(data))
}

/// Hex SHA-256 of the `jcs-1` encoding of `value`.
pub fn jcs_sha256_hex(value: &Value) -> Result<String, CanonicalError> {
    Ok(sha256_hex(canonical_json_v2(value)?.as_bytes()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn bits(h: u64) -> f64 {
        f64::from_bits(h)
    }

    #[test]
    fn rfc8785_appendix_b_numbers() {
        let table: &[(u64, &str)] = &[
            (0x0000000000000000, "0"),
            (0x8000000000000000, "0"),
            (0x0000000000000001, "5e-324"),
            (0x8000000000000001, "-5e-324"),
            (0x7fefffffffffffff, "1.7976931348623157e+308"),
            (0xffefffffffffffff, "-1.7976931348623157e+308"),
            (0x4340000000000000, "9007199254740992"),
            (0xc340000000000000, "-9007199254740992"),
            (0x4430000000000000, "295147905179352830000"),
            (0x44b52d02c7e14af5, "9.999999999999997e+22"),
            (0x44b52d02c7e14af6, "1e+23"),
            (0x44b52d02c7e14af7, "1.0000000000000001e+23"),
            (0x444b1ae4d6e2ef4e, "999999999999999700000"),
            (0x444b1ae4d6e2ef4f, "999999999999999900000"),
            (0x444b1ae4d6e2ef50, "1e+21"),
            (0x3eb0c6f7a0b5ed8c, "9.999999999999997e-7"),
            (0x3eb0c6f7a0b5ed8d, "0.000001"),
            (0x41b3de4355555553, "333333333.3333332"),
            (0x41b3de4355555554, "333333333.33333325"),
            (0x41b3de4355555555, "333333333.3333333"),
            (0x41b3de4355555556, "333333333.3333334"),
            (0x41b3de4355555557, "333333333.33333343"),
            (0xbecbf647612f3696, "-0.0000033333333333333333"),
            (0x43143ff3c1cb0959, "1424953923781206.2"),
        ];
        for (h, expected) in table {
            assert_eq!(
                es_number_to_string(bits(*h)).unwrap(),
                *expected,
                "{:016x}",
                h
            );
        }
        assert!(es_number_to_string(f64::NAN).is_err());
        assert!(es_number_to_string(f64::INFINITY).is_err());
    }

    #[test]
    fn rfc8785_sorting_example() {
        let v: Value = serde_json::from_str(
            r#"{"€":"Euro Sign","\r":"Carriage Return","דּ":"Hebrew Letter Dalet With Dagesh","1":"One","😀":"Emoji: Grinning Face","\u0080":"Control","ö":"Latin Small Letter O With Diaeresis"}"#,
        )
        .unwrap();
        let out = canonical_json_v2(&v).unwrap();
        let order: Vec<&str> = [
            "Carriage Return",
            "One",
            "Control",
            "Latin Small Letter O With Diaeresis",
            "Euro Sign",
            "Emoji: Grinning Face",
            "Hebrew Letter Dalet With Dagesh",
        ]
        .to_vec();
        let mut last = 0;
        for name in order {
            let at = out.find(name).unwrap();
            assert!(at >= last, "{} out of order in {}", name, out);
            last = at;
        }
    }

    #[test]
    fn integers_follow_the_exact_or_reject_rule() {
        assert_eq!(
            canonical_json_v2(&json!(9007199254740991u64)).unwrap(),
            "9007199254740991"
        );
        assert_eq!(
            canonical_json_v2(&json!(1u64 << 60)).unwrap(),
            "1152921504606847000"
        );
        assert_eq!(
            canonical_json_v2(&json!(i64::MIN)).unwrap(),
            "-9223372036854776000"
        );
        assert!(matches!(
            canonical_json_v2(&json!(9007199254740993u64)),
            Err(CanonicalError::InexactInteger(_))
        ));
        assert!(canonical_json_v2(&json!(u64::MAX)).is_err());
        assert!(canonical_json_v2(&json!(i64::MAX)).is_err());
        assert!(canonical_json_v2(&json!(i64::MIN + 1)).is_err());
    }

    #[test]
    fn strings_escape_like_ecmascript() {
        let v = json!("\u{0}\u{1f}\u{7f}\u{2028}\"\\\u{8}\t\n\u{c}\r/");
        assert_eq!(
            canonical_json_v2(&v).unwrap(),
            "\"\\u0000\\u001f\u{7f}\u{2028}\\\"\\\\\\b\\t\\n\\f\\r/\""
        );
    }

    #[test]
    fn scheme_ids_round_trip() {
        assert_eq!(HashScheme::parse("jcs-1"), Some(HashScheme::Jcs1));
        assert_eq!(
            HashScheme::parse("json-sorted-1"),
            Some(HashScheme::JsonSorted1)
        );
        assert_eq!(HashScheme::parse("md5"), None);
        assert_eq!(HashScheme::Jcs1.id(), HASH_SCHEME_JCS1);
    }

    /// Compare against V8 on a file of `<hex bits> <String(x)>` lines, made by
    /// running Node.js over random doubles. Skipped unless the env var is set.
    #[test]
    fn matches_v8_reference_file_when_given() {
        let Ok(path) = std::env::var("BRIEFCASE_V8_NUMBERS") else {
            return;
        };
        let text = std::fs::read_to_string(path).unwrap();
        let mut checked = 0;
        for line in text.lines() {
            let (h, expected) = line.split_once(' ').unwrap();
            let x = bits(u64::from_str_radix(h, 16).unwrap());
            assert_eq!(es_number_to_string(x).unwrap(), expected, "{}", h);
            checked += 1;
        }
        eprintln!("checked {} doubles against V8", checked);
    }
}
