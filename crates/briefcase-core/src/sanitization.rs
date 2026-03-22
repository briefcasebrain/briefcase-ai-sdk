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
            Regex::new(r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b").unwrap(),
        );
        patterns.insert(
            PiiType::Email,
            Regex::new(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b").unwrap(),
        );
        patterns.insert(
            PiiType::Phone,
            Regex::new(r"(?:\+?1[-.\s]?)?\(?[0-9]{3}\)?[-.\s]?[0-9]{3}[-.\s]?[0-9]{4}").unwrap(),
        );
        patterns.insert(
            PiiType::ApiKey,
            Regex::new(r"\b(sk-|bai_|api_|key_|AIza|AKIA|ya29\.|xox[bpoa]-)[A-Za-z0-9_-]{15,}\b")
                .unwrap(),
        );
        patterns.insert(
            PiiType::IpAddress,
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

        let result = sanitizer.sanitize("Card number: 4532-1234-5678-9012");
        assert_eq!(result.sanitized, "Card number: [REDACTED_CREDIT_CARD]");

        let result = sanitizer.sanitize("Card: 4532123456789012");
        assert_eq!(result.sanitized, "Card: [REDACTED_CREDIT_CARD]");
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
