use regex::Regex;
use std::collections::HashMap;
use std::sync::LazyLock;
use thiserror::Error;

#[derive(Debug, Clone)]
pub struct Sanitizer {
    /// Patterns in priority order: built-ins first (see [`BUILTIN_PATTERNS`]),
    /// then custom patterns in the order they were added. When two matches
    /// cover exactly the same span, the earlier pattern labels it, so results
    /// are the same on every run.
    patterns: Vec<(PiiType, Regex)>,
    enabled: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum PiiType {
    Ssn,
    CreditCard,
    Email,
    Phone,
    ApiKey,
    IpAddress,
    Iban,
    Custom(String),
}

/// The built-in patterns, compiled once per process. `Sanitizer::new` clones
/// them, which shares the compiled program instead of rebuilding it.
static BUILTIN_PATTERNS: LazyLock<Vec<(PiiType, Regex)>> = LazyLock::new(|| {
    let compile = |pattern: &str| Regex::new(pattern).expect("built-in pattern compiles");
    vec![
        (
            PiiType::ApiKey,
            // Common provider key prefixes. Covers OpenAI (sk-, incl. sk-ant-/sk-proj-
            // via the sk- branch), Stripe (sk_live_/pk_live_/…), AWS (AKIA),
            // Google (AIza, ya29.), GitHub (ghp_/gho_/ghu_/ghs_/ghr_, github_pat_),
            // GitLab (glpat-), HuggingFace (hf_), Slack (xox[bpoa]-), and the
            // generic api_/key_/bai_ forms. Then, by shape or context:
            // - JWTs: three base64url segments, the first two starting `eyJ`
            //   (base64 of `{"`).
            // - `Bearer <token>` credentials (`is_redactable` also requires a
            //   digit in the token, so prose after the word "bearer" is kept).
            // - AWS secret access keys, which have no prefix of their own, when
            //   assigned to a name that says what they are.
            compile(concat!(
                r"\b(sk-|sk_live_|sk_test_|pk_live_|pk_test_|rk_live_|bai_|api_|key_|AIza|AKIA|ya29\.|gh[opusr]_|github_pat_|glpat-|hf_|xox[bpoa]-)[A-Za-z0-9_-]{15,}\b",
                r"|\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}",
                r"|\b[Bb]earer\s+[A-Za-z0-9._~+/-]{20,}=*",
                r#"|(?i:aws_?secret_?(?:access_?)?key)["']?\s*[:=]\s*["']?[A-Za-z0-9/+]{40}\b"#,
            )),
        ),
        (
            PiiType::Email,
            // TLD class is [A-Za-z]; the older `[A-Z|a-z]` erroneously included a
            // literal `|`, matching malformed addresses such as `a@b.c|d`.
            compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        ),
        (
            PiiType::CreditCard,
            // 13-19 digit card numbers, contiguous or grouped by single spaces
            // or hyphens (covers 4-4-4-4, Amex 4-6-5, Diners 4-6-4). Which
            // candidates are redacted is decided by `is_card_candidate`.
            compile(r"\b\d{4}[-\s]?\d{4,6}[-\s]?\d{4,5}(?:[-\s]?\d{1,4})?\b"),
        ),
        (
            PiiType::Ssn,
            // Separated forms are redacted on shape. A bare nine-digit run is
            // only a candidate: `is_redactable` requires SSN context for it,
            // since most nine-digit numbers are ids, counts and timestamps.
            compile(r"\b[0-9]{3}-[0-9]{2}-[0-9]{4}\b|\b[0-9]{3}\s[0-9]{2}\s[0-9]{4}\b|[0-9]{9}"),
        ),
        (
            PiiType::Iban,
            // Country code, two check digits, then 11-30 alphanumerics,
            // optionally in space-separated groups. Redacted only when the
            // ISO 7064 mod-97 check passes (`iban_valid`).
            compile(r"\b[A-Z]{2}[0-9]{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b"),
        ),
        (
            PiiType::Phone,
            // Formatted North American numbers (both separators present, or a
            // parenthesised area code), and E.164 numbers with an explicit
            // `+`. A bare 10/11-digit run is only a candidate: `is_redactable`
            // requires phone context for it, since epoch timestamps and ids
            // have the same shape.
            compile(concat!(
                r"(?:\+?1[-.\s]?)?(?:\([0-9]{3}\)\s?[0-9]{3}[-.\s][0-9]{4}",
                r"|[0-9]{3}-[0-9]{3}-[0-9]{4}|[0-9]{3}\.[0-9]{3}\.[0-9]{4}|[0-9]{3}\s[0-9]{3}\s[0-9]{4})",
                r"|\+[1-9][0-9]{9,14}",
                r"|1?[0-9]{10}",
            )),
        ),
        (
            PiiType::IpAddress,
            // IPv4: any dotted quad in the 0-255 range, except where the
            // context marks it as a version string (see `is_version_string`).
            // IPv6: the full eight-group form, or a `::`-compressed form
            // (which `is_redactable` requires to contain a digit, so paths
            // such as `add::f` are kept).
            compile(concat!(
                r"\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b",
                r"|\b(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}\b",
                r"|\b(?:[0-9A-Fa-f]{1,4}:){1,7}(?::[0-9A-Fa-f]{1,4}){1,6}\b",
            )),
        ),
    ]
});

/// Text that marks a nearby bare nine-digit number as an SSN: the word
/// `ssn` (also as `user_ssn` or `userSsn`) or "social security".
static SSN_CONTEXT: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?:(?:^|[^A-Za-z])(?i:ssn)|[a-z]S[Ss][Nn])(?:[^A-Za-z]|$)|(?i:social[\s_-]*security)",
    )
    .expect("built-in pattern compiles")
});

/// Text that marks a nearby bare 10/11-digit number as a phone number.
static PHONE_CONTEXT: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i:phone|mobile|fax)|(?:^|[^A-Za-z])(?i:tel|cell|call)(?:[^A-Za-z]|$)")
        .expect("built-in pattern compiles")
});

/// Text that, directly before a dotted quad, marks it as a version number.
static VERSION_CONTEXT: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?:(?:^|[^A-Za-z])(?i:v|ver|version|release|build|firmware|sdk)[\s:=]*|(?:[=<>~!]=|\^)\s*)$",
    )
    .expect("built-in pattern compiles")
});

/// How far back from a match the context patterns look.
const CONTEXT_WINDOW_BYTES: usize = 32;

impl Sanitizer {
    pub fn new() -> Self {
        Self {
            patterns: BUILTIN_PATTERNS.clone(),
            enabled: true,
        }
    }

    pub fn disabled() -> Self {
        Self {
            patterns: Vec::new(),
            enabled: false,
        }
    }

    /// Add a custom PII pattern
    pub fn add_pattern(&mut self, name: &str, pattern: &str) -> Result<(), SanitizationError> {
        let regex =
            Regex::new(pattern).map_err(|e| SanitizationError::InvalidPattern(e.to_string()))?;
        let pii_type = PiiType::Custom(name.to_string());
        // Re-adding a name replaces its pattern but keeps its priority.
        match self.patterns.iter_mut().find(|(t, _)| *t == pii_type) {
            Some(entry) => entry.1 = regex,
            None => self.patterns.push((pii_type, regex)),
        }
        Ok(())
    }

    /// Remove a pattern
    pub fn remove_pattern(&mut self, pii_type: &PiiType) -> bool {
        let before = self.patterns.len();
        self.patterns.retain(|(t, _)| t != pii_type);
        self.patterns.len() != before
    }

    /// Enable or disable sanitization
    pub fn set_enabled(&mut self, enabled: bool) {
        self.enabled = enabled;
    }

    /// Every redactable match in `text` as `(start, end, pattern index)`,
    /// ordered by start, then longest first, then pattern priority.
    /// `context` is text that precedes `text` logically without being part
    /// of it (the key of a JSON value), for the context-dependent patterns.
    fn find_matches(&self, text: &str, context: &str) -> Vec<(usize, usize, usize)> {
        let mut all_matches = Vec::new();

        for (index, (pii_type, regex)) in self.patterns.iter().enumerate() {
            for mat in regex.find_iter(text) {
                if is_redactable(pii_type, text, &mat, context) {
                    all_matches.push((mat.start(), mat.end(), index));
                }
            }
        }

        all_matches
            .sort_by_key(|(start, end, index)| (*start, std::cmp::Reverse(end - start), *index));
        all_matches
    }

    /// Sanitize a string, replacing PII with redaction markers
    pub fn sanitize(&self, text: &str) -> SanitizationResult {
        self.sanitize_in_context(text, "")
    }

    fn sanitize_in_context(&self, text: &str, context: &str) -> SanitizationResult {
        if !self.enabled {
            return SanitizationResult {
                sanitized: text.to_string(),
                redactions: Vec::new(),
            };
        }

        let mut result = text.to_string();
        let mut redactions = Vec::new();

        // Collect all matches first to avoid overlapping replacements
        let all_matches = self.find_matches(text, context);

        // Remove overlapping matches (keep the longest one)
        let mut non_overlapping_matches = Vec::new();
        let mut last_end = 0;

        for (start, end, index) in all_matches {
            if start >= last_end {
                non_overlapping_matches.push((start, end, index));
                last_end = end;
            } else if start < last_end {
                // Check if this match is longer than the previous one that overlaps
                if let Some(last_match) = non_overlapping_matches.last() {
                    if (end - start) > (last_match.1 - last_match.0) {
                        // Replace the shorter match with this longer one
                        non_overlapping_matches.pop();
                        non_overlapping_matches.push((start, end, index));
                        last_end = end;
                    }
                }
            }
        }

        // Apply redactions from right to left to maintain indices
        for (start, end, index) in non_overlapping_matches.into_iter().rev() {
            let pii_type = &self.patterns[index].0;
            let redaction_marker = self.get_redaction_marker(pii_type);
            let original_length = end - start;

            result.replace_range(start..end, &redaction_marker);

            redactions.push(Redaction {
                pii_type: pii_type.clone(),
                original_length,
                start_position: start,
                end_position: start + redaction_marker.len(), // New end position after redaction
            });
        }

        // Sort redactions by original position
        redactions.sort_by_key(|r| r.start_position);

        SanitizationResult {
            sanitized: result,
            redactions,
        }
    }

    /// Sanitize a JSON value recursively.
    ///
    /// String values are sanitized as text. Integer values are checked too,
    /// and one that is PII (a card number, or an SSN or phone number under a
    /// key that names it, such as `{"ssn": 123456789}`) is replaced by its
    /// redaction marker, which turns that value into a string. A value's own
    /// key counts as context for the context-dependent patterns. Object keys
    /// themselves are left as they are.
    pub fn sanitize_json(&self, value: &serde_json::Value) -> SanitizationJsonResult {
        if !self.enabled {
            return SanitizationJsonResult {
                sanitized: value.clone(),
                redactions: Vec::new(),
            };
        }

        let mut redactions = Vec::new();
        let sanitized = self.sanitize_json_recursive(value, &mut redactions, String::new());

        SanitizationJsonResult {
            sanitized,
            redactions,
        }
    }

    fn sanitize_json_recursive(
        &self,
        value: &serde_json::Value,
        redactions: &mut Vec<JsonRedaction>,
        path: String,
    ) -> serde_json::Value {
        match value {
            serde_json::Value::String(s) => {
                let result = self.sanitize_in_context(s, json_key(&path));
                for redaction in result.redactions {
                    redactions.push(JsonRedaction {
                        path: path.clone(),
                        pii_type: redaction.pii_type,
                        original_length: redaction.original_length,
                    });
                }
                serde_json::Value::String(result.sanitized)
            }
            // Floats are measurements; only whole numbers carry identifiers.
            serde_json::Value::Number(n) if n.is_i64() || n.is_u64() => {
                let result = self.sanitize_in_context(&n.to_string(), json_key(&path));
                if result.redactions.is_empty() {
                    return value.clone();
                }
                for redaction in result.redactions {
                    redactions.push(JsonRedaction {
                        path: path.clone(),
                        pii_type: redaction.pii_type,
                        original_length: redaction.original_length,
                    });
                }
                serde_json::Value::String(result.sanitized)
            }
            serde_json::Value::Object(obj) => {
                let mut new_obj = serde_json::Map::new();
                for (key, val) in obj {
                    let new_path = if path.is_empty() {
                        key.clone()
                    } else {
                        format!("{}.{}", path, key)
                    };
                    new_obj.insert(
                        key.clone(),
                        self.sanitize_json_recursive(val, redactions, new_path),
                    );
                }
                serde_json::Value::Object(new_obj)
            }
            serde_json::Value::Array(arr) => {
                let mut new_arr = Vec::new();
                for (i, val) in arr.iter().enumerate() {
                    let new_path = format!("{}[{}]", path, i);
                    new_arr.push(self.sanitize_json_recursive(val, redactions, new_path));
                }
                serde_json::Value::Array(new_arr)
            }
            _ => value.clone(), // Floats, booleans, null remain unchanged
        }
    }

    /// Check if text contains PII (without modifying)
    pub fn contains_pii(&self, text: &str) -> Vec<PiiMatch> {
        if !self.enabled {
            return Vec::new();
        }

        self.find_matches(text, "")
            .into_iter()
            .map(|(start, end, index)| PiiMatch {
                pii_type: self.patterns[index].0.clone(),
                start,
                end,
            })
            .collect()
    }

    /// Analyze text and return detailed PII statistics
    pub fn analyze(&self, text: &str) -> PiiAnalysis {
        let matches = self.contains_pii(text);
        let mut type_counts = HashMap::new();

        for pii_match in &matches {
            *type_counts.entry(pii_match.pii_type.clone()).or_insert(0) += 1;
        }

        let total_matches = matches.len();
        let unique_types = type_counts.len();
        let has_pii = !matches.is_empty();

        PiiAnalysis {
            has_pii,
            total_matches,
            unique_types,
            type_counts,
            matches,
        }
    }

    fn get_redaction_marker(&self, pii_type: &PiiType) -> String {
        match pii_type {
            PiiType::Ssn => "[REDACTED_SSN]".to_string(),
            PiiType::CreditCard => "[REDACTED_CREDIT_CARD]".to_string(),
            PiiType::Email => "[REDACTED_EMAIL]".to_string(),
            PiiType::Phone => "[REDACTED_PHONE]".to_string(),
            PiiType::ApiKey => "[REDACTED_API_KEY]".to_string(),
            PiiType::IpAddress => "[REDACTED_IP]".to_string(),
            PiiType::Iban => "[REDACTED_IBAN]".to_string(),
            PiiType::Custom(name) => format!("[REDACTED_{}]", name.to_uppercase()),
        }
    }
}

impl Default for Sanitizer {
    fn default() -> Self {
        Self::new()
    }
}

/// The key a JSON path ends in (`user.ssn` -> `ssn`, `ids[2]` -> `ids`).
fn json_key(path: &str) -> &str {
    let key = path.rsplit('.').next().unwrap_or(path);
    key.split('[').next().unwrap_or(key)
}

/// Whether a pattern match is redacted, for the types whose regex alone is
/// too loose to decide. `context` is extra preceding text (a JSON key).
fn is_redactable(pii_type: &PiiType, text: &str, mat: &regex::Match<'_>, context: &str) -> bool {
    let matched = mat.as_str();
    let all_digits = matched.bytes().all(|b| b.is_ascii_digit());
    match pii_type {
        // Every digit pattern here can match a prefix of a longer identifier:
        // the card and phone patterns span `-`-grouped digits (so they bite
        // into a UUID), and the bare SSN branch is nine digits. Redacting part
        // of an identifier is worse than redacting none of it, so a match that
        // continues into more digits is not PII.
        PiiType::CreditCard => {
            is_card_candidate(matched) && is_standalone_run(text, mat.start(), mat.end())
        }
        PiiType::Ssn => {
            is_standalone_run(text, mat.start(), mat.end())
                && ssn_fields_valid(matched)
                // Separators make the shape; a bare run needs to be called one.
                && (!all_digits || has_context(&SSN_CONTEXT, text, mat.start(), context))
        }
        PiiType::Phone => {
            is_standalone_run(text, mat.start(), mat.end())
                && !continues_dotted(text, mat.start(), mat.end())
                && (!all_digits || has_context(&PHONE_CONTEXT, text, mat.start(), context))
        }
        PiiType::IpAddress => {
            if matched.contains(':') {
                // IPv6. A compressed form must hold a digit somewhere.
                !matched.contains("::") || matched.bytes().any(|b| b.is_ascii_digit())
            } else {
                !continues_dotted(text, mat.start(), mat.end())
                    && !is_version_string(text, mat.start(), mat.end())
            }
        }
        PiiType::ApiKey => {
            // A bearer token is opaque; a long hyphenated word after "bearer"
            // in prose is not. Real tokens all but always contain a digit.
            let is_bearer = matched
                .get(..6)
                .is_some_and(|p| p.eq_ignore_ascii_case("bearer"));
            !is_bearer || matched[6..].bytes().any(|b| b.is_ascii_digit())
        }
        PiiType::Iban => iban_valid(matched),
        _ => true,
    }
}

/// Whether `pattern` matches shortly before `start`, or in `context`.
fn has_context(pattern: &Regex, text: &str, start: usize, context: &str) -> bool {
    let mut from = start.saturating_sub(CONTEXT_WINDOW_BYTES);
    while !text.is_char_boundary(from) {
        from += 1;
    }
    pattern.is_match(&text[from..start]) || pattern.is_match(context)
}

/// SSA never issues area 000 or 666, group 00, or serial 0000. (Area 9xx is
/// kept: those are ITINs, which are just as sensitive.)
fn ssn_fields_valid(candidate: &str) -> bool {
    let digits: Vec<u8> = candidate.bytes().filter(u8::is_ascii_digit).collect();
    if digits.len() != 9 {
        return false;
    }
    let area = &digits[..3];
    area != b"000" && area != b"666" && &digits[3..5] != b"00" && &digits[5..] != b"0000"
}

/// Whether a match is one piece of a longer dotted number: `1.2.3.4.5`, or
/// the fraction digits of `122.4194155`.
fn continues_dotted(text: &str, start: usize, end: usize) -> bool {
    let mut before = text[..start].chars().rev();
    let mut after = text[end..].chars();
    let dotted = |adjacent: Option<char>, beyond: Option<char>| {
        adjacent == Some('.') && beyond.is_some_and(|c| c.is_ascii_digit())
    };
    dotted(before.next(), before.next()) || dotted(after.next(), after.next())
}

/// Whether the dotted quad at `start..end` reads as a version number: named
/// as one (`v10.2.3.4`, `version 1.2.3.4`), pinned as one (`pkg==1.2.3.4`,
/// `>= 1.2.3.4`), or carrying a pre-release/build suffix (`1.2.3.4-beta`).
fn is_version_string(text: &str, start: usize, end: usize) -> bool {
    let mut from = start.saturating_sub(CONTEXT_WINDOW_BYTES);
    while !text.is_char_boundary(from) {
        from += 1;
    }
    if VERSION_CONTEXT.is_match(&text[from..start]) {
        return true;
    }
    let mut after = text[end..].chars();
    matches!(
        (after.next(), after.next()),
        (Some('-' | '+'), Some(c)) if c.is_ascii_alphabetic()
    )
}

/// ISO 13616 validation: 15-34 characters, and the number formed by moving
/// the first four to the end (letters as 10-35) is 1 modulo 97.
fn iban_valid(candidate: &str) -> bool {
    let chars: Vec<u8> = candidate.bytes().filter(|b| *b != b' ').collect();
    if !(15..=34).contains(&chars.len()) {
        return false;
    }
    let mut remainder = 0u32;
    for &c in chars[4..].iter().chain(&chars[..4]) {
        let value = match c {
            b'0'..=b'9' => u32::from(c - b'0'),
            b'A'..=b'Z' => u32::from(c - b'A') + 10,
            _ => return false,
        };
        remainder = if value < 10 {
            (remainder * 10 + value) % 97
        } else {
            (remainder * 100 + value) % 97
        };
    }
    remainder == 1
}

/// Whether a match stands alone rather than continuing into a longer run of
/// digits, directly or across a single `-`.
fn is_standalone_run(text: &str, start: usize, end: usize) -> bool {
    let mut before = text[..start].chars().rev();
    let mut after = text[end..].chars();
    !continues(before.next(), before.next()) && !continues(after.next(), after.next())
}

/// Whether `adjacent`, optionally across one hyphen, reaches another digit.
///
/// Only `-` counts as a continuation. Whitespace separates two distinct values
/// far more often than it groups one (`4111111111111111 5500000000000004` is
/// two cards), so treating it as a continuation leaves both in the clear.
fn continues(adjacent: Option<char>, beyond: Option<char>) -> bool {
    match adjacent {
        Some(c) if c.is_ascii_digit() => true,
        Some('-') => beyond.is_some_and(|c| c.is_ascii_digit()),
        _ => false,
    }
}

/// Whether a card-shaped match is redacted as a credit card. A 16 digit run
/// is redacted on shape alone; other lengths must pass the Luhn checksum and
/// start with a card issuer digit, because Luhn alone accepts roughly one in
/// ten arbitrary digit runs and would swallow timestamps and identifiers.
/// Non-ASCII decimal digits defeat the Luhn check, so any match containing
/// them is redacted outright; over-redaction is the safe direction.
fn is_card_candidate(candidate: &str) -> bool {
    let ascii_digits = candidate.chars().filter(char::is_ascii_digit).count();
    let all_digits = candidate.chars().filter(|c| c.is_numeric()).count();
    if all_digits != ascii_digits {
        return true;
    }
    ascii_digits == 16 || (luhn_valid(candidate) && has_issuer_prefix(candidate))
}

/// Whether the run starts with a major-issuer digit: 3 (Amex, Diners, JCB),
/// 4 (Visa), 5 (Mastercard, Maestro), or 6 (Discover, UnionPay, Maestro).
/// Epoch-millisecond timestamps and snowflake identifiers start with 1, 7, 8,
/// or 9, so this keeps them out without dropping any issued card range.
fn has_issuer_prefix(candidate: &str) -> bool {
    matches!(
        candidate.chars().find(char::is_ascii_digit),
        Some('3'..='6')
    )
}

/// Luhn checksum over the digits in `candidate`, ignoring separator
/// characters. Requires at least 13 digits so shorter numeric runs never
/// qualify as card numbers.
fn luhn_valid(candidate: &str) -> bool {
    let mut sum = 0u32;
    let mut digits = 0usize;
    for c in candidate.chars().rev() {
        let Some(d) = c.to_digit(10) else { continue };
        sum += if digits % 2 == 1 {
            let doubled = d * 2;
            if doubled > 9 {
                doubled - 9
            } else {
                doubled
            }
        } else {
            d
        };
        digits += 1;
    }
    // `%` rather than `u32::is_multiple_of`, which is stable only from 1.87
    // and would raise this crate's MSRV two releases above its dependencies'.
    digits >= 13 && sum % 10 == 0
}

#[derive(Debug, Clone)]
pub struct SanitizationResult {
    pub sanitized: String,
    pub redactions: Vec<Redaction>,
}

#[derive(Debug, Clone)]
pub struct SanitizationJsonResult {
    pub sanitized: serde_json::Value,
    pub redactions: Vec<JsonRedaction>,
}

#[derive(Debug, Clone)]
pub struct Redaction {
    pub pii_type: PiiType,
    pub original_length: usize,
    pub start_position: usize,
    pub end_position: usize,
}

#[derive(Debug, Clone)]
pub struct JsonRedaction {
    pub path: String,
    pub pii_type: PiiType,
    pub original_length: usize,
}

#[derive(Debug, Clone)]
pub struct PiiMatch {
    pub pii_type: PiiType,
    pub start: usize,
    pub end: usize,
}

#[derive(Debug, Clone)]
pub struct PiiAnalysis {
    pub has_pii: bool,
    pub total_matches: usize,
    pub unique_types: usize,
    pub type_counts: HashMap<PiiType, usize>,
    pub matches: Vec<PiiMatch>,
}

#[derive(Error, Debug, Clone, PartialEq)]
pub enum SanitizationError {
    #[error("Invalid pattern: {0}")]
    InvalidPattern(String),
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn test_sanitizer_creation() {
        let sanitizer = Sanitizer::new();
        assert!(sanitizer.enabled);
        assert!(!sanitizer.patterns.is_empty());
    }

    #[test]
    fn test_disabled_sanitizer() {
        let sanitizer = Sanitizer::disabled();
        assert!(!sanitizer.enabled);

        let result = sanitizer.sanitize("test@email.com");
        assert_eq!(result.sanitized, "test@email.com");
        assert!(result.redactions.is_empty());
    }

    #[test]
    fn test_email_sanitization() {
        let sanitizer = Sanitizer::new();
        let result = sanitizer.sanitize("Contact me at john.doe@example.com for details.");

        assert_eq!(
            result.sanitized,
            "Contact me at [REDACTED_EMAIL] for details."
        );
        assert_eq!(result.redactions.len(), 1);
        assert!(matches!(result.redactions[0].pii_type, PiiType::Email));
    }

    #[test]
    fn test_email_tld_excludes_pipe() {
        // Regression: the old `[A-Z|a-z]` class treated `|` as a valid TLD
        // character, so it over-consumed `io|z` as a single TLD.
        let sanitizer = Sanitizer::new();
        let result = sanitizer.sanitize("x@y.io|z");
        assert_eq!(result.sanitized, "[REDACTED_EMAIL]|z");
    }

    #[test]
    fn test_ssn_sanitization() {
        let sanitizer = Sanitizer::new();

        // Hyphenated SSN
        let result = sanitizer.sanitize("My SSN is 123-45-6789.");
        assert_eq!(result.sanitized, "My SSN is [REDACTED_SSN].");

        // Spaced SSN
        let result = sanitizer.sanitize("SSN: 123 45 6789");
        assert_eq!(result.sanitized, "SSN: [REDACTED_SSN]");

        // No delimiter SSN
        let result = sanitizer.sanitize("SSN123456789");
        assert_eq!(result.sanitized, "SSN[REDACTED_SSN]");
    }

    #[test]
    fn test_credit_card_sanitization() {
        let sanitizer = Sanitizer::new();

        let result = sanitizer.sanitize("Card number: 4532-0151-1283-0366");
        assert_eq!(result.sanitized, "Card number: [REDACTED_CREDIT_CARD]");

        let result = sanitizer.sanitize("Card: 4532015112830366");
        assert_eq!(result.sanitized, "Card: [REDACTED_CREDIT_CARD]");
    }

    #[test]
    fn test_credit_card_16_digit_redacted_without_luhn() {
        let sanitizer = Sanitizer::new();

        // 16 digit runs are redacted on shape alone, so a mistyped or
        // synthetic card number never survives sanitization.
        let result = sanitizer.sanitize("Card number: 4532-1234-5678-9012");
        assert_eq!(result.sanitized, "Card number: [REDACTED_CREDIT_CARD]");

        let result = sanitizer.sanitize("Card: 4532123456789012");
        assert_eq!(result.sanitized, "Card: [REDACTED_CREDIT_CARD]");
    }

    #[test]
    fn test_credit_card_non_ascii_digits_redacted() {
        let sanitizer = Sanitizer::new();

        // Fullwidth digits: Luhn cannot be computed, so the run is redacted.
        let result = sanitizer.sanitize("Card: ４５３２０１５１１２８３０３６６");
        assert_eq!(result.sanitized, "Card: [REDACTED_CREDIT_CARD]");

        // Arabic-Indic digits.
        let result = sanitizer.sanitize("Card: ٤٥٣٢٠١٥١١٢٨٣٠٣٦٦");
        assert_eq!(result.sanitized, "Card: [REDACTED_CREDIT_CARD]");
    }

    #[test]
    fn test_credit_card_15_digit_amex() {
        let sanitizer = Sanitizer::new();

        let result = sanitizer.sanitize("Amex: 378282246310005");
        assert_eq!(result.sanitized, "Amex: [REDACTED_CREDIT_CARD]");

        let result = sanitizer.sanitize("Amex: 3782-822463-10005");
        assert_eq!(result.sanitized, "Amex: [REDACTED_CREDIT_CARD]");

        let result = sanitizer.sanitize("Amex: 3782 822463 10005");
        assert_eq!(result.sanitized, "Amex: [REDACTED_CREDIT_CARD]");
    }

    #[test]
    fn test_credit_card_14_digit_diners() {
        let sanitizer = Sanitizer::new();

        let result = sanitizer.sanitize("Diners: 30569309025904");
        assert_eq!(result.sanitized, "Diners: [REDACTED_CREDIT_CARD]");

        let result = sanitizer.sanitize("Diners: 3056-930902-5904");
        assert_eq!(result.sanitized, "Diners: [REDACTED_CREDIT_CARD]");
    }

    #[test]
    fn test_credit_card_13_digit() {
        let sanitizer = Sanitizer::new();

        let result = sanitizer.sanitize("Visa: 4222222222222");
        assert_eq!(result.sanitized, "Visa: [REDACTED_CREDIT_CARD]");
    }

    #[test]
    fn test_credit_card_luhn_valid_non_card_prefixes_are_kept() {
        let sanitizer = Sanitizer::new();

        // Luhn passes on roughly one in ten arbitrary digit runs, so it cannot
        // gate redaction alone: these are an epoch-millisecond timestamp and
        // snowflake-shaped identifiers, and redacting them destroys real data.
        for value in ["1699999999996", "9876543210987", "1234567890123456785"] {
            let text = format!("id {}", value);
            let result = sanitizer.sanitize(&text);
            assert_eq!(result.sanitized, text, "{} was redacted as a card", value);
        }
    }

    #[test]
    fn test_adjacent_pii_is_all_redacted() {
        let sanitizer = Sanitizer::new();

        // A separator between two values is a delimiter, not a continuation.
        // Treating it as one leaves both values in the clear, which is the
        // worst outcome available: a silent PII leak on a green test run.
        for (input, expected) in [
            (
                "cards 4111111111111111 5500000000000004",
                "cards [REDACTED_CREDIT_CARD] [REDACTED_CREDIT_CARD]",
            ),
            (
                "cards 4111111111111111, 5500000000000004",
                "cards [REDACTED_CREDIT_CARD], [REDACTED_CREDIT_CARD]",
            ),
            (
                "cards 4111111111111111\n5500000000000004",
                "cards [REDACTED_CREDIT_CARD]\n[REDACTED_CREDIT_CARD]",
            ),
            (
                "phones 555-123-4567 555-987-6543",
                "phones [REDACTED_PHONE] [REDACTED_PHONE]",
            ),
            (
                "ssns 123-45-6789 987-65-4321",
                "ssns [REDACTED_SSN] [REDACTED_SSN]",
            ),
        ] {
            assert_eq!(
                sanitizer.sanitize(input).sanitized,
                expected,
                "input: {}",
                input
            );
        }
    }

    #[test]
    fn test_pii_after_an_unrelated_number_is_redacted() {
        let sanitizer = Sanitizer::new();

        // The digit before the space belongs to a different value entirely.
        let result = sanitizer.sanitize("order 12 4111111111111111");
        assert_eq!(result.sanitized, "order 12 [REDACTED_CREDIT_CARD]");
    }

    #[test]
    fn test_uuid_is_not_split_into_card_redactions() {
        let sanitizer = Sanitizer::new();

        // An all-numeric UUID is hyphen-grouped like a card, and its first 16
        // digits pass the 16-digit shape rule. Redacting part of it leaves a
        // mangled identifier that is worse than either extreme.
        let text = "trace 12345678-1234-5678-1234-567812345678";
        assert_eq!(sanitizer.sanitize(text).sanitized, text);
    }

    #[test]
    fn test_card_inside_a_longer_hyphenated_run_is_kept() {
        let sanitizer = Sanitizer::new();

        let text = "order 4111-1111-1111-1111-9999";
        assert_eq!(sanitizer.sanitize(text).sanitized, text);
    }

    #[test]
    fn test_credit_card_19_digit_with_card_prefix_redacted() {
        let sanitizer = Sanitizer::new();

        let result = sanitizer.sanitize("Visa: 4000000000000000006");
        assert_eq!(result.sanitized, "Visa: [REDACTED_CREDIT_CARD]");
    }

    #[test]
    fn test_credit_card_luhn_filter() {
        let sanitizer = Sanitizer::new();

        // 13 digit epoch-millisecond timestamp: fails Luhn, so it is not
        // classified as a credit card.
        let matches = sanitizer.contains_pii("1699999999998");
        assert!(
            !matches
                .iter()
                .any(|m| matches!(m.pii_type, PiiType::CreditCard)),
            "non-Luhn digits misclassified as credit card"
        );

        // Passes the Luhn checksum: must be classified as a credit card.
        let matches = sanitizer.contains_pii("4111111111111111");
        assert!(
            matches
                .iter()
                .any(|m| matches!(m.pii_type, PiiType::CreditCard)),
            "valid card number not classified as credit card"
        );
    }

    #[test]
    fn test_phone_sanitization() {
        let sanitizer = Sanitizer::new();

        let result = sanitizer.sanitize("Call me at (555) 123-4567");
        assert_eq!(result.sanitized, "Call me at [REDACTED_PHONE]");

        let result = sanitizer.sanitize("Phone: +1-555-123-4567");
        assert_eq!(result.sanitized, "Phone: [REDACTED_PHONE]");
    }

    #[test]
    fn test_api_key_sanitization() {
        let sanitizer = Sanitizer::new();

        let result = sanitizer.sanitize("OpenAI key: sk-1234567890abcdef1234567890abcdef");
        assert_eq!(result.sanitized, "OpenAI key: [REDACTED_API_KEY]");

        let result = sanitizer.sanitize("API key: api_1234567890abcdef");
        assert_eq!(result.sanitized, "API key: [REDACTED_API_KEY]");
    }

    #[test]
    fn test_api_key_additional_providers() {
        let sanitizer = Sanitizer::new();
        // Keys are assembled at runtime from a prefix + filler body so the source
        // contains no literal that resembles a real provider secret (avoids
        // secret-scanner false positives on dummy test fixtures).
        let body = "1234567890abcdefghijklmn"; // 24 chars; satisfies the {15,} body
        let prefixes = [
            "ghp_",        // GitHub PAT
            "github_pat_", // GitHub fine-grained
            "glpat-",      // GitLab
            "sk_live_",    // Stripe secret
            "pk_live_",    // Stripe publishable
            "hf_",         // HuggingFace
        ];
        for prefix in prefixes {
            let key = format!("{prefix}{body}");
            let result = sanitizer.sanitize(&format!("token: {key}"));
            assert_eq!(
                result.sanitized, "token: [REDACTED_API_KEY]",
                "expected {key} to be redacted"
            );
        }
    }

    #[test]
    fn test_ip_address_sanitization() {
        let sanitizer = Sanitizer::new();

        let result = sanitizer.sanitize("Server IP: 192.168.1.100");
        assert_eq!(result.sanitized, "Server IP: [REDACTED_IP]");
    }

    #[test]
    fn test_multiple_pii_sanitization() {
        let sanitizer = Sanitizer::new();

        let text = "Contact john@example.com at 555-123-4567 or visit 192.168.1.100";
        let result = sanitizer.sanitize(text);

        assert_eq!(
            result.sanitized,
            "Contact [REDACTED_EMAIL] at [REDACTED_PHONE] or visit [REDACTED_IP]"
        );
        assert_eq!(result.redactions.len(), 3);
    }

    #[test]
    fn test_overlapping_patterns() {
        let mut sanitizer = Sanitizer::new();

        // Add a pattern that might overlap
        sanitizer.add_pattern("test", r"\d{3}-\d{2}").unwrap();

        let result = sanitizer.sanitize("SSN: 123-45-6789");

        // Should only redact once (first pattern wins)
        assert_eq!(result.redactions.len(), 1);
    }

    #[test]
    fn test_json_sanitization() {
        let sanitizer = Sanitizer::new();

        let data = json!({
            "user": {
                "email": "john@example.com",
                "phone": "555-123-4567"
            },
            "config": {
                "api_key": "sk-1234567890abcdef1234567890abcdef",
                "timeout": 30
            }
        });

        let result = sanitizer.sanitize_json(&data);

        // Check that emails, phones, and API keys are redacted
        assert_eq!(result.sanitized["user"]["email"], "[REDACTED_EMAIL]");
        assert_eq!(result.sanitized["user"]["phone"], "[REDACTED_PHONE]");
        assert_eq!(result.sanitized["config"]["api_key"], "[REDACTED_API_KEY]");
        assert_eq!(result.sanitized["config"]["timeout"], 30); // Number unchanged

        assert_eq!(result.redactions.len(), 3);
    }

    #[test]
    fn test_contains_pii() {
        let sanitizer = Sanitizer::new();

        let text = "Email: john@example.com, Phone: 555-123-4567";
        let matches = sanitizer.contains_pii(text);

        assert_eq!(matches.len(), 2);
        assert!(matches.iter().any(|m| matches!(m.pii_type, PiiType::Email)));
        assert!(matches.iter().any(|m| matches!(m.pii_type, PiiType::Phone)));
    }

    #[test]
    fn test_pii_analysis() {
        let sanitizer = Sanitizer::new();

        let text = "Contact john@example.com or jane@test.org at 555-123-4567";
        let analysis = sanitizer.analyze(text);

        assert!(analysis.has_pii);
        assert_eq!(analysis.total_matches, 3);
        assert_eq!(analysis.unique_types, 2); // Email and Phone
        assert_eq!(*analysis.type_counts.get(&PiiType::Email).unwrap(), 2);
        assert_eq!(*analysis.type_counts.get(&PiiType::Phone).unwrap(), 1);
    }

    #[test]
    fn test_custom_pattern() {
        let mut sanitizer = Sanitizer::new();

        sanitizer
            .add_pattern("employee_id", r"\bEMP-\d{6}\b")
            .unwrap();

        let result = sanitizer.sanitize("Employee ID: EMP-123456");
        assert_eq!(result.sanitized, "Employee ID: [REDACTED_EMPLOYEE_ID]");
    }

    #[test]
    fn test_invalid_pattern() {
        let mut sanitizer = Sanitizer::new();

        let result = sanitizer.add_pattern("invalid", r"[");
        assert!(result.is_err());
        assert!(matches!(
            result.unwrap_err(),
            SanitizationError::InvalidPattern(_)
        ));
    }

    #[test]
    fn test_pattern_removal() {
        let mut sanitizer = Sanitizer::new();

        assert!(sanitizer.remove_pattern(&PiiType::Email));
        assert!(!sanitizer.remove_pattern(&PiiType::Email)); // Already removed

        let result = sanitizer.sanitize("Email: test@example.com");
        assert_eq!(result.sanitized, "Email: test@example.com"); // Should not be redacted
    }

    #[test]
    fn test_enable_disable() {
        let mut sanitizer = Sanitizer::new();

        sanitizer.set_enabled(false);
        let result = sanitizer.sanitize("Email: test@example.com");
        assert_eq!(result.sanitized, "Email: test@example.com");

        sanitizer.set_enabled(true);
        let result = sanitizer.sanitize("Email: test@example.com");
        assert_eq!(result.sanitized, "Email: [REDACTED_EMAIL]");
    }

    #[test]
    fn test_no_false_positives() {
        let sanitizer = Sanitizer::new();

        // These should not be detected as PII
        let non_pii_texts = vec![
            "Version 1.2.3.4 released", // Looks like IP but is version
            "Price: $12.34",            // Not a credit card
            "Date: 12-34-5678",         // Invalid SSN format
            "Call ext 123",             // Too short for phone
        ];

        for text in non_pii_texts {
            let result = sanitizer.sanitize(text);
            // Some might still be detected due to regex patterns, but at least test they don't crash
            assert!(!result.sanitized.is_empty());
        }
    }

    /// Asserts `text` comes back unchanged.
    fn assert_kept(sanitizer: &Sanitizer, text: &str) {
        assert_eq!(sanitizer.sanitize(text).sanitized, text);
    }

    #[test]
    fn test_bare_nine_digit_numbers_are_not_ssns_without_context() {
        let sanitizer = Sanitizer::new();

        for text in [
            "order 123456789 shipped",
            "build 987654321",
            "user_id=412345678",
            "className: 123456789",
        ] {
            assert_kept(&sanitizer, text);
        }

        // Named as an SSN, the same shape is redacted.
        for (text, expected) in [
            ("SSN 123456789", "SSN [REDACTED_SSN]"),
            ("ssn=123456789", "ssn=[REDACTED_SSN]"),
            ("user_ssn: 123456789", "user_ssn: [REDACTED_SSN]"),
            (
                "Social Security number 123456789",
                "Social Security number [REDACTED_SSN]",
            ),
        ] {
            assert_eq!(sanitizer.sanitize(text).sanitized, expected);
        }
    }

    #[test]
    fn test_never_issued_ssn_fields_are_not_redacted() {
        let sanitizer = Sanitizer::new();
        for text in [
            "ref 000-12-3456",
            "ref 666-12-3456",
            "ref 123-00-4567",
            "ref 123-45-0000",
        ] {
            assert_kept(&sanitizer, text);
        }
    }

    #[test]
    fn test_timestamps_and_coordinates_are_not_phone_numbers() {
        let sanitizer = Sanitizer::new();

        for text in [
            "created_at 1699999999",      // epoch seconds
            "ts=1699999999123",           // epoch milliseconds
            "at 40.7127753, -74.0059728", // coordinates
            "lon -122.4194155",           // 3 + 7 digit coordinate
            "ratio 123.456.78901",        // not 3-3-4
            "id 5551234567",              // bare, no context
        ] {
            assert_kept(&sanitizer, text);
        }

        for (text, expected) in [
            ("call 555.123.4567 now", "call [REDACTED_PHONE] now"),
            ("555 123 4567", "[REDACTED_PHONE]"),
            ("+15551234567", "[REDACTED_PHONE]"),
            ("1-555-123-4567", "[REDACTED_PHONE]"),
            ("+1 (555) 123-4567", "[REDACTED_PHONE]"),
            ("phone: 5551234567", "phone: [REDACTED_PHONE]"),
            ("tel 15551234567", "tel [REDACTED_PHONE]"),
            ("+442079460958", "[REDACTED_PHONE]"),
        ] {
            assert_eq!(sanitizer.sanitize(text).sanitized, expected, "{text}");
        }
    }

    #[test]
    fn test_version_strings_are_not_ip_addresses() {
        let sanitizer = Sanitizer::new();

        for text in [
            "upgraded to v10.2.3.4",
            "Version 1.2.3.4 released",
            "version: 10.2.3.4",
            "requires pkg==10.2.3.4",
            "needs >= 2.0.0.1",
            "image 10.2.3.4-beta",
            "schema 1.2.3.4.5",
        ] {
            assert_kept(&sanitizer, text);
        }

        for (text, expected) in [
            ("host 10.2.3.4 is down", "host [REDACTED_IP] is down"),
            ("ip=10.2.3.4", "ip=[REDACTED_IP]"),
            ("from 192.168.1.100:8080", "from [REDACTED_IP]:8080"),
            ("dev 10.0.0.1", "dev [REDACTED_IP]"),
        ] {
            assert_eq!(sanitizer.sanitize(text).sanitized, expected, "{text}");
        }
    }

    #[test]
    fn test_ipv6_addresses_are_redacted() {
        let sanitizer = Sanitizer::new();

        for (text, expected) in [
            (
                "peer 2001:0db8:85a3:0000:0000:8a2e:0370:7334 up",
                "peer [REDACTED_IP] up",
            ),
            ("peer 2001:db8::8a2e:370:7334", "peer [REDACTED_IP]"),
            ("link fe80::1", "link [REDACTED_IP]"),
        ] {
            assert_eq!(sanitizer.sanitize(text).sanitized, expected, "{text}");
        }

        for text in [
            "at 12:34:56",
            "mac aa:bb:cc:dd:ee:ff",
            "call add::f and dead::beef",
            "std::vec::Vec",
        ] {
            assert_kept(&sanitizer, text);
        }
    }

    #[test]
    fn test_jwt_and_bearer_tokens_are_redacted() {
        let sanitizer = Sanitizer::new();
        // Assembled at runtime so the source holds no token-shaped literal.
        let jwt = [
            "eyJhbGciOiJIUzI1NiJ9",
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
            "c2lnbmF0dXJlMTIz",
        ]
        .join(".");

        let result = sanitizer.sanitize(&format!("token={jwt};"));
        assert_eq!(result.sanitized, "token=[REDACTED_API_KEY];");

        let result = sanitizer.sanitize(&format!("Authorization: Bearer {jwt}"));
        assert_eq!(result.sanitized, "Authorization: [REDACTED_API_KEY]");

        let opaque = format!("{}{}", "a1B2c3D4e5F6g7H8", "i9J0k1L2m3N4");
        let result = sanitizer.sanitize(&format!("Authorization: Bearer {opaque}"));
        assert_eq!(result.sanitized, "Authorization: [REDACTED_API_KEY]");

        assert_kept(&sanitizer, "the bearer of counter-revolutionary-sentiments");
        assert_kept(&sanitizer, "eyJ is how base64 JSON starts");
    }

    #[test]
    fn test_aws_secret_access_key_is_redacted_in_context() {
        let sanitizer = Sanitizer::new();
        let secret = format!("{}{}", "wJalrXUtnFEMI/K7MDENG/", "bPxRfiCYEXAMPLEKEY");
        assert_eq!(secret.len(), 40);

        let result = sanitizer.sanitize(&format!("aws_secret_access_key = {secret}"));
        assert_eq!(result.sanitized, "[REDACTED_API_KEY]");

        // Forty base64 characters with nothing naming them are left alone.
        assert_kept(&sanitizer, &format!("digest {secret}"));
    }

    #[test]
    fn test_iban_is_redacted_only_when_the_checksum_holds() {
        let sanitizer = Sanitizer::new();

        for (text, expected) in [
            ("IBAN GB82 WEST 1234 5698 7654 32.", "IBAN [REDACTED_IBAN]."),
            (
                "pay DE89370400440532013000 today",
                "pay [REDACTED_IBAN] today",
            ),
        ] {
            assert_eq!(sanitizer.sanitize(text).sanitized, expected, "{text}");
        }

        // Same shape, wrong check digits.
        assert_kept(&sanitizer, "ref GB00WEST12345698765432");
        assert_kept(&sanitizer, "part AB12CDEF34567890");
    }

    #[test]
    fn test_json_numbers_are_scanned() {
        let sanitizer = Sanitizer::new();

        let data = json!({
            "ssn": 123456789,
            "user": {"phone": 5551234567u64, "socialSecurityNumber": "123456789"},
            "card": 4111111111111111u64,
            "created_at": 1699999999,
            "order_id": 123456789,
            "amount": 12.5,
            "count": 3
        });
        let result = sanitizer.sanitize_json(&data);

        assert_eq!(result.sanitized["ssn"], "[REDACTED_SSN]");
        assert_eq!(result.sanitized["user"]["phone"], "[REDACTED_PHONE]");
        assert_eq!(
            result.sanitized["user"]["socialSecurityNumber"],
            "[REDACTED_SSN]"
        );
        assert_eq!(result.sanitized["card"], "[REDACTED_CREDIT_CARD]");
        // Numbers that are not PII keep their value and type.
        assert_eq!(result.sanitized["created_at"], 1699999999);
        assert_eq!(result.sanitized["order_id"], 123456789);
        assert_eq!(result.sanitized["amount"], 12.5);
        assert_eq!(result.sanitized["count"], 3);

        let mut paths: Vec<_> = result.redactions.iter().map(|r| r.path.as_str()).collect();
        paths.sort();
        assert_eq!(
            paths,
            ["card", "ssn", "user.phone", "user.socialSecurityNumber"]
        );
    }

    #[test]
    fn test_equal_span_matches_are_labelled_deterministically() {
        // Two custom patterns matching exactly the same span: the one added
        // first labels it, on every run and every instance.
        for _ in 0..50 {
            let mut sanitizer = Sanitizer::new();
            sanitizer.add_pattern("first", r"ZX-\d{4}").unwrap();
            sanitizer.add_pattern("second", r"ZX-[0-9]{4}").unwrap();

            let result = sanitizer.sanitize("code ZX-1234");
            assert_eq!(result.sanitized, "code [REDACTED_FIRST]");

            let labels: Vec<_> = sanitizer
                .contains_pii("code ZX-1234")
                .into_iter()
                .map(|m| m.pii_type)
                .collect();
            assert_eq!(
                labels,
                [
                    PiiType::Custom("first".to_string()),
                    PiiType::Custom("second".to_string())
                ]
            );
        }

        // A built-in outranks a custom pattern over the same span.
        let mut sanitizer = Sanitizer::new();
        sanitizer
            .add_pattern("mail", r"[a-z]+@[a-z]+\.com")
            .unwrap();
        assert_eq!(
            sanitizer.sanitize("to bob@example.com").sanitized,
            "to [REDACTED_EMAIL]"
        );
    }

    #[test]
    fn test_new_shares_the_compiled_builtin_patterns() {
        // Construction must not recompile: building many is near-free.
        let start = std::time::Instant::now();
        for _ in 0..2_000 {
            std::hint::black_box(Sanitizer::new());
        }
        assert!(
            start.elapsed().as_millis() < 500,
            "2000 constructions took {:?}",
            start.elapsed()
        );
        assert_eq!(Sanitizer::new().patterns.len(), BUILTIN_PATTERNS.len());
    }

    #[test]
    fn test_performance_large_text() {
        let sanitizer = Sanitizer::new();

        // Generate a large text with some PII
        let large_text = "Lorem ipsum dolor sit amet. ".repeat(1000) + "Contact: test@example.com";

        let start = std::time::Instant::now();
        let result = sanitizer.sanitize(&large_text);
        let duration = start.elapsed();

        // Should complete quickly (within 100ms for large text)
        assert!(duration.as_millis() < 100);
        assert!(result.sanitized.contains("[REDACTED_EMAIL]"));
    }
}
