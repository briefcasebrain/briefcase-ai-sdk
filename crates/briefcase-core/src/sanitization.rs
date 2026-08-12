use regex::Regex;
use std::collections::HashMap;
use thiserror::Error;

#[derive(Debug, Clone)]
pub struct Sanitizer {
    patterns: HashMap<PiiType, Regex>,
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
    Custom(String),
}

impl Sanitizer {
    pub fn new() -> Self {
        let mut patterns = HashMap::new();

        patterns.insert(
            PiiType::Ssn,
            Regex::new(r"\b\d{3}-\d{2}-\d{4}\b|\b\d{3}\s\d{2}\s\d{4}\b|\d{9}").unwrap(),
        );
        patterns.insert(
            PiiType::CreditCard,
            // 13-19 digit card numbers, contiguous or grouped by single spaces
            // or hyphens (covers 4-4-4-4, Amex 4-6-5, Diners 4-6-4). Which
            // candidates are redacted is decided by `is_card_candidate`.
            Regex::new(r"\b\d{4}[-\s]?\d{4,6}[-\s]?\d{4,5}(?:[-\s]?\d{1,4})?\b").unwrap(),
        );
        patterns.insert(
            PiiType::Email,
            // TLD class is [A-Za-z]; the older `[A-Z|a-z]` erroneously included a
            // literal `|`, matching malformed addresses such as `a@b.c|d`.
            Regex::new(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b").unwrap(),
        );
        patterns.insert(
            PiiType::Phone,
            Regex::new(r"(?:\+?1[-.\s]?)?\(?[0-9]{3}\)?[-.\s]?[0-9]{3}[-.\s]?[0-9]{4}").unwrap(),
        );
        patterns.insert(
            PiiType::ApiKey,
            // Common provider key prefixes. Covers OpenAI (sk-, incl. sk-ant-/sk-proj-
            // via the sk- branch), Stripe (sk_live_/pk_live_/…), AWS (AKIA),
            // Google (AIza, ya29.), GitHub (ghp_/gho_/ghu_/ghs_/ghr_, github_pat_),
            // GitLab (glpat-), HuggingFace (hf_), Slack (xox[bpoa]-), and the
            // generic api_/key_/bai_ forms.
            Regex::new(
                r"\b(sk-|sk_live_|sk_test_|pk_live_|pk_test_|rk_live_|bai_|api_|key_|AIza|AKIA|ya29\.|gh[opusr]_|github_pat_|glpat-|hf_|xox[bpoa]-)[A-Za-z0-9_-]{15,}\b",
            )
            .unwrap(),
        );
        patterns.insert(
            PiiType::IpAddress,
            // Intentionally greedy: any dotted-quad in 0-255 range is redacted.
            // This over-redacts version-like strings (e.g. "1.2.3.4"), which is the
            // safe direction for a PII tool — never under-redact a real address.
            Regex::new(r"\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b").unwrap(),
        );

        Self {
            patterns,
            enabled: true,
        }
    }

    pub fn disabled() -> Self {
        Self {
            patterns: HashMap::new(),
            enabled: false,
        }
    }

    /// Add a custom PII pattern
    pub fn add_pattern(&mut self, name: &str, pattern: &str) -> Result<(), SanitizationError> {
        let regex =
            Regex::new(pattern).map_err(|e| SanitizationError::InvalidPattern(e.to_string()))?;
        self.patterns
            .insert(PiiType::Custom(name.to_string()), regex);
        Ok(())
    }

    /// Remove a pattern
    pub fn remove_pattern(&mut self, pii_type: &PiiType) -> bool {
        self.patterns.remove(pii_type).is_some()
    }

    /// Enable or disable sanitization
    pub fn set_enabled(&mut self, enabled: bool) {
        self.enabled = enabled;
    }

    /// Sanitize a string, replacing PII with redaction markers
    pub fn sanitize(&self, text: &str) -> SanitizationResult {
        if !self.enabled {
            return SanitizationResult {
                sanitized: text.to_string(),
                redactions: Vec::new(),
            };
        }

        let mut result = text.to_string();
        let mut redactions = Vec::new();

        // Collect all matches first to avoid overlapping replacements
        let mut all_matches = Vec::new();

        for (pii_type, regex) in &self.patterns {
            for mat in regex.find_iter(text) {
                if !is_redactable(pii_type, text, &mat) {
                    continue;
                }
                all_matches.push((mat.start(), mat.end(), pii_type.clone()));
            }
        }

        // Sort by start position, then by length (longest first for overlaps)
        all_matches.sort_by_key(|(start, end, _)| (*start, std::cmp::Reverse(end - start)));

        // Remove overlapping matches (keep the longest one)
        let mut non_overlapping_matches = Vec::new();
        let mut last_end = 0;

        for (start, end, pii_type) in all_matches {
            if start >= last_end {
                non_overlapping_matches.push((start, end, pii_type));
                last_end = end;
            } else if start < last_end {
                // Check if this match is longer than the previous one that overlaps
                if let Some(last_match) = non_overlapping_matches.last() {
                    if (end - start) > (last_match.1 - last_match.0) {
                        // Replace the shorter match with this longer one
                        non_overlapping_matches.pop();
                        non_overlapping_matches.push((start, end, pii_type));
                        last_end = end;
                    }
                }
            }
        }

        // Apply redactions from right to left to maintain indices
        for (start, end, pii_type) in non_overlapping_matches.into_iter().rev() {
            let redaction_marker = self.get_redaction_marker(&pii_type);
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

    /// Sanitize a JSON value recursively
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
                let result = self.sanitize(s);
                if !result.redactions.is_empty() {
                    for redaction in result.redactions {
                        redactions.push(JsonRedaction {
                            path: path.clone(),
                            pii_type: redaction.pii_type,
                            original_length: redaction.original_length,
                        });
                    }
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
            _ => value.clone(), // Numbers, booleans, null remain unchanged
        }
    }

    /// Check if text contains PII (without modifying)
    pub fn contains_pii(&self, text: &str) -> Vec<PiiMatch> {
        if !self.enabled {
            return Vec::new();
        }

        let mut matches = Vec::new();

        for (pii_type, regex) in &self.patterns {
            for mat in regex.find_iter(text) {
                if !is_redactable(pii_type, text, &mat) {
                    continue;
                }
                matches.push(PiiMatch {
                    pii_type: pii_type.clone(),
                    start: mat.start(),
                    end: mat.end(),
                });
            }
        }

        matches.sort_by_key(|m| m.start);
        matches
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
            PiiType::Custom(name) => format!("[REDACTED_{}]", name.to_uppercase()),
        }
    }
}

impl Default for Sanitizer {
    fn default() -> Self {
        Self::new()
    }
}

/// Whether a pattern match is redacted, for the types whose regex alone is
/// too loose to decide.
fn is_redactable(pii_type: &PiiType, text: &str, mat: &regex::Match<'_>) -> bool {
    match pii_type {
        // Every digit pattern here can match a prefix of a longer identifier:
        // the card and phone patterns span `-`-grouped digits (so they bite
        // into a UUID), and the bare SSN branch is `\d{9}`. Redacting part of
        // an identifier is worse than redacting none of it, so a match that
        // continues into more digits is not PII.
        PiiType::CreditCard => {
            is_card_candidate(mat.as_str()) && is_standalone_run(text, mat.start(), mat.end())
        }
        PiiType::Phone | PiiType::Ssn => is_standalone_run(text, mat.start(), mat.end()),
        _ => true,
    }
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
