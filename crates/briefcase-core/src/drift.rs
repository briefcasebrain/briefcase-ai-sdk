use crate::models::Output;
use std::collections::HashMap;
use strsim::normalized_levenshtein;

/// Non-exhaustive: an output type, read field-by-field; future additions are
/// not breaking changes.
#[derive(Debug, Clone, PartialEq)]
#[non_exhaustive]
pub struct DriftMetrics {
    pub consistency_score: f64, // 0.0 - 1.0, higher = more consistent
    pub agreement_rate: f64,    // Percentage of outputs that match
    pub drift_score: f64,       // 0.0 - 1.0, higher = more drift
    pub consensus_output: Option<String>,
    pub consensus_confidence: ConsensusConfidence,
    pub outliers: Vec<usize>, // Indices of outlier outputs
    pub total_samples: usize, // Number of outputs the metrics were computed over
}

#[derive(Debug, Clone, PartialEq)]
pub enum ConsensusConfidence {
    High,   // >80% agreement
    Medium, // 50-80% agreement
    Low,    // <50% agreement
    None,   // No consensus possible
}

#[derive(Debug, Clone, PartialEq)]
pub enum DriftStatus {
    Stable,   // consistency_score >= 0.85
    Drifting, // consistency_score 0.5-0.85
    Critical, // consistency_score < 0.5
}

#[derive(Clone)]
pub struct DriftCalculator {
    similarity_threshold: f64,
}

impl DriftCalculator {
    pub fn new() -> Self {
        Self {
            similarity_threshold: 0.85,
        }
    }

    pub fn with_threshold(threshold: f64) -> Self {
        Self {
            similarity_threshold: threshold.clamp(0.0, 1.0),
        }
    }

    /// Get the similarity threshold
    pub fn similarity_threshold(&self) -> f64 {
        self.similarity_threshold
    }

    /// Calculate drift metrics from a list of string outputs
    pub fn calculate_drift(&self, outputs: &[String]) -> DriftMetrics {
        if outputs.is_empty() {
            return DriftMetrics {
                consistency_score: 1.0,
                agreement_rate: 1.0,
                drift_score: 0.0,
                consensus_output: None,
                consensus_confidence: ConsensusConfidence::None,
                outliers: Vec::new(),
                total_samples: 0,
            };
        }

        if outputs.len() == 1 {
            return DriftMetrics {
                consistency_score: 1.0,
                agreement_rate: 1.0,
                drift_score: 0.0,
                consensus_output: Some(outputs[0].clone()),
                consensus_confidence: ConsensusConfidence::High,
                outliers: Vec::new(),
                total_samples: 1,
            };
        }

        // Calculate pairwise similarities
        let similarities = self.calculate_pairwise_similarities(outputs);

        // Calculate average similarity (consistency score)
        let total_pairs = outputs.len() * (outputs.len() - 1) / 2;
        let avg_similarity = similarities.iter().sum::<f64>() / total_pairs as f64;

        // Calculate agreement rate (exact or near-exact matches)
        let agreement_rate = self.calculate_agreement_rate(outputs);

        // Drift score is inverse of consistency
        let drift_score = 1.0 - avg_similarity;

        // Find consensus output
        let consensus_output = self.find_consensus(outputs);

        // Determine consensus confidence
        let consensus_confidence = match agreement_rate {
            rate if rate > 0.8 => ConsensusConfidence::High,
            rate if rate >= 0.5 => ConsensusConfidence::Medium,
            rate if rate > 0.0 => ConsensusConfidence::Low,
            _ => ConsensusConfidence::None,
        };

        // Find outliers (outputs significantly different from consensus)
        let outliers = self.find_outliers(outputs, &consensus_output);

        DriftMetrics {
            consistency_score: avg_similarity,
            agreement_rate,
            drift_score,
            consensus_output,
            consensus_confidence,
            outliers,
            total_samples: outputs.len(),
        }
    }

    /// Calculate drift from Output structs (uses value field)
    pub fn calculate_drift_from_outputs(&self, outputs: &[Output]) -> DriftMetrics {
        let strings: Vec<String> = outputs
            .iter()
            .map(|output| output.value.to_string())
            .collect();

        self.calculate_drift(&strings)
    }

    /// Determine drift status from metrics
    pub fn get_status(&self, metrics: &DriftMetrics) -> DriftStatus {
        match metrics.consistency_score {
            score if score >= 0.85 => DriftStatus::Stable,
            score if score >= 0.5 => DriftStatus::Drifting,
            _ => DriftStatus::Critical,
        }
    }

    /// Calculate semantic similarity between two strings
    /// Uses Levenshtein distance normalized by length
    fn semantic_similarity(&self, a: &str, b: &str) -> f64 {
        if a == b {
            return 1.0;
        }

        // Try to parse as numbers for numeric comparison
        if let (Ok(num_a), Ok(num_b)) = (a.parse::<f64>(), b.parse::<f64>()) {
            // For numbers, use relative difference
            let diff = (num_a - num_b).abs();
            let avg = (num_a.abs() + num_b.abs()) / 2.0;
            if avg == 0.0 {
                1.0 // Both are zero
            } else {
                (1.0 - (diff / avg)).max(0.0)
            }
        } else {
            // For text, use normalized Levenshtein distance
            normalized_levenshtein(a, b)
        }
    }

    /// Calculate pairwise similarities for all combinations
    fn calculate_pairwise_similarities(&self, outputs: &[String]) -> Vec<f64> {
        let mut similarities = Vec::new();

        for i in 0..outputs.len() {
            for j in (i + 1)..outputs.len() {
                let sim = self.semantic_similarity(&outputs[i], &outputs[j]);
                similarities.push(sim);
            }
        }

        similarities
    }

    /// Calculate agreement rate (percentage of outputs that are similar enough)
    fn calculate_agreement_rate(&self, outputs: &[String]) -> f64 {
        if outputs.len() <= 1 {
            return 1.0;
        }

        // Count unique clusters of similar outputs
        let mut clusters: Vec<Vec<String>> = Vec::new();

        for output in outputs {
            let mut found_cluster = false;

            for cluster in &mut clusters {
                let cluster_repr: &String = cluster.first().unwrap();
                if self.semantic_similarity(output, cluster_repr) >= self.similarity_threshold {
                    cluster.push(output.clone());
                    found_cluster = true;
                    break;
                }
            }

            if !found_cluster {
                clusters.push(vec![output.clone()]);
            }
        }

        // Find the largest cluster
        let max_cluster_size = clusters.iter().map(|c| c.len()).max().unwrap_or(0);
        max_cluster_size as f64 / outputs.len() as f64
    }

    /// Find the consensus output (most common or centroid)
    fn find_consensus(&self, outputs: &[String]) -> Option<String> {
        if outputs.is_empty() {
            return None;
        }

        // Try frequency-based consensus first
        let mut frequency_map: HashMap<String, usize> = HashMap::new();
        for output in outputs {
            *frequency_map.entry(output.clone()).or_insert(0) += 1;
        }

        // If there's a clear most frequent output
        if let Some((most_frequent, count)) = frequency_map.iter().max_by_key(|(_, &count)| count) {
            if *count > outputs.len() / 2 {
                return Some(most_frequent.clone());
            }
        }

        // Otherwise, find the output with highest average similarity to all others
        let mut best_output = outputs[0].clone();
        let mut best_avg_similarity = 0.0;

        for candidate in outputs {
            let similarities: Vec<f64> = outputs
                .iter()
                .map(|other| self.semantic_similarity(candidate, other))
                .collect();

            let avg_similarity = similarities.iter().sum::<f64>() / similarities.len() as f64;

            if avg_similarity > best_avg_similarity {
                best_avg_similarity = avg_similarity;
                best_output = candidate.clone();
            }
        }

        Some(best_output)
    }

    /// Find outliers (outputs significantly different from consensus)
    fn find_outliers(&self, outputs: &[String], consensus: &Option<String>) -> Vec<usize> {
        let Some(consensus_output) = consensus else {
            return Vec::new();
        };

        outputs
            .iter()
            .enumerate()
            .filter_map(|(i, output)| {
                let similarity = self.semantic_similarity(output, consensus_output);
                if similarity < self.similarity_threshold * 0.7 {
                    // More strict for outliers
                    Some(i)
                } else {
                    None
                }
            })
            .collect()
    }
}

impl Default for DriftCalculator {
    fn default() -> Self {
        Self::new()
    }
}

/// Consensus engine for N-of-M agreement
pub struct ConsensusEngine {
    required_runs: usize,
    agreement_threshold: f64,
    drift_calculator: DriftCalculator,
}

impl ConsensusEngine {
    pub fn new(required_runs: usize, agreement_threshold: f64) -> Self {
        Self {
            required_runs,
            agreement_threshold: agreement_threshold.clamp(0.0, 1.0),
            drift_calculator: DriftCalculator::new(),
        }
    }

    /// Run function N times and return consensus result
    pub fn run_with_consensus<F, T>(&self, f: F) -> ConsensusResult<T>
    where
        F: Fn() -> T,
        T: Clone + PartialEq + ToString,
    {
        let outputs: Vec<T> = (0..self.required_runs).map(|_| f()).collect();

        let output_strings: Vec<String> = outputs.iter().map(|output| output.to_string()).collect();

        let metrics = self.drift_calculator.calculate_drift(&output_strings);
        let meets_threshold = metrics.agreement_rate >= self.agreement_threshold;

        // Find consensus by matching against the consensus string
        let consensus = if let Some(consensus_str) = &metrics.consensus_output {
            outputs
                .iter()
                .find(|output| output.to_string() == *consensus_str)
                .cloned()
        } else {
            None
        };

        ConsensusResult {
            outputs,
            consensus,
            metrics,
            meets_threshold,
        }
    }
}

#[derive(Debug, Clone)]
pub struct ConsensusResult<T> {
    pub outputs: Vec<T>,
    pub consensus: Option<T>,
    pub metrics: DriftMetrics,
    pub meets_threshold: bool,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn test_drift_calculator_empty_outputs() {
        let calculator = DriftCalculator::new();
        let metrics = calculator.calculate_drift(&[]);

        assert_eq!(metrics.consistency_score, 1.0);
        assert_eq!(metrics.agreement_rate, 1.0);
        assert_eq!(metrics.drift_score, 0.0);
        assert_eq!(metrics.consensus_output, None);
        assert_eq!(metrics.consensus_confidence, ConsensusConfidence::None);
        assert!(metrics.outliers.is_empty());
    }

    #[test]
    fn test_drift_calculator_single_output() {
        let calculator = DriftCalculator::new();
        let outputs = vec!["hello".to_string()];
        let metrics = calculator.calculate_drift(&outputs);

        assert_eq!(metrics.consistency_score, 1.0);
        assert_eq!(metrics.agreement_rate, 1.0);
        assert_eq!(metrics.drift_score, 0.0);
        assert_eq!(metrics.consensus_output, Some("hello".to_string()));
        assert_eq!(metrics.consensus_confidence, ConsensusConfidence::High);
        assert!(metrics.outliers.is_empty());
    }

    #[test]
    fn test_drift_calculator_identical_outputs() {
        let calculator = DriftCalculator::new();
        let outputs = vec![
            "hello".to_string(),
            "hello".to_string(),
            "hello".to_string(),
        ];
        let metrics = calculator.calculate_drift(&outputs);

        assert_eq!(metrics.consistency_score, 1.0);
        assert_eq!(metrics.agreement_rate, 1.0);
        assert_eq!(metrics.drift_score, 0.0);
        assert_eq!(metrics.consensus_output, Some("hello".to_string()));
        assert_eq!(metrics.consensus_confidence, ConsensusConfidence::High);
        assert!(metrics.outliers.is_empty());
    }

    #[test]
    fn test_drift_calculator_different_outputs() {
        let calculator = DriftCalculator::new();
        let outputs = vec![
            "apple".to_string(),
            "orange".to_string(),
            "banana".to_string(),
        ];
        let metrics = calculator.calculate_drift(&outputs);

        assert!(metrics.consistency_score < 1.0);
        assert!(metrics.drift_score > 0.0);
        assert!(metrics.consensus_output.is_some());
    }

    #[test]
    fn test_semantic_similarity() {
        let calculator = DriftCalculator::new();

        // Identical strings
        assert_eq!(calculator.semantic_similarity("hello", "hello"), 1.0);

        // Similar strings
        let sim = calculator.semantic_similarity("hello", "helo");
        assert!(sim > 0.5 && sim < 1.0);

        // Completely different strings
        let sim = calculator.semantic_similarity("hello", "xyz");
        assert!(sim < 0.5);

        // Numbers
        let sim = calculator.semantic_similarity("100", "101");
        assert!(sim > 0.8);

        let sim = calculator.semantic_similarity("100", "200");
        assert!(sim < 0.8);
    }

    #[test]
    fn test_total_samples_counts_inputs() {
        let calculator = DriftCalculator::new();

        assert_eq!(calculator.calculate_drift(&[]).total_samples, 0);

        let one = vec!["a".to_string()];
        assert_eq!(calculator.calculate_drift(&one).total_samples, 1);

        let many = vec![
            "answer one".to_string(),
            "answer one".to_string(),
            "something else entirely".to_string(),
        ];
        let metrics = calculator.calculate_drift(&many);
        assert_eq!(metrics.total_samples, 3);
        assert!(
            metrics.total_samples >= metrics.outliers.len(),
            "sample count must not be the outlier count"
        );
    }

    #[test]
    fn test_drift_status() {
        let calculator = DriftCalculator::new();

        let high_consistency = DriftMetrics {
            consistency_score: 0.9,
            agreement_rate: 0.9,
            drift_score: 0.1,
            consensus_output: Some("test".to_string()),
            consensus_confidence: ConsensusConfidence::High,
            outliers: Vec::new(),
            total_samples: 3,
        };
        assert_eq!(
            calculator.get_status(&high_consistency),
            DriftStatus::Stable
        );

        let medium_consistency = DriftMetrics {
            consistency_score: 0.7,
            agreement_rate: 0.7,
            drift_score: 0.3,
            consensus_output: Some("test".to_string()),
            consensus_confidence: ConsensusConfidence::Medium,
            outliers: Vec::new(),
            total_samples: 3,
        };
        assert_eq!(
            calculator.get_status(&medium_consistency),
            DriftStatus::Drifting
        );

        let low_consistency = DriftMetrics {
            consistency_score: 0.3,
            agreement_rate: 0.3,
            drift_score: 0.7,
            consensus_output: Some("test".to_string()),
            consensus_confidence: ConsensusConfidence::Low,
            outliers: Vec::new(),
            total_samples: 3,
        };
        assert_eq!(
            calculator.get_status(&low_consistency),
            DriftStatus::Critical
        );
    }

    #[test]
    fn test_drift_from_outputs() {
        let calculator = DriftCalculator::new();
        let outputs = vec![
            Output::new("result", json!("hello"), "string"),
            Output::new("result", json!("hello"), "string"),
            Output::new("result", json!("hi"), "string"),
        ];

        let metrics = calculator.calculate_drift_from_outputs(&outputs);
        assert!(metrics.consistency_score > 0.5);
        assert!(metrics.consistency_score < 1.0);
    }

    #[test]
    fn test_consensus_engine() {
        let engine = ConsensusEngine::new(5, 0.8);

        // Function that always returns the same value
        let result = engine.run_with_consensus(|| "consistent".to_string());

        assert_eq!(result.outputs.len(), 5);
        assert!(result.meets_threshold);
        assert_eq!(result.consensus, Some("consistent".to_string()));
        assert_eq!(result.metrics.consistency_score, 1.0);
    }

    #[test]
    fn test_outlier_detection() {
        let calculator = DriftCalculator::new();
        let outputs = vec![
            "apple".to_string(),
            "apple".to_string(),
            "apple".to_string(),
            "completely_different_output".to_string(),
        ];

        let metrics = calculator.calculate_drift(&outputs);
        assert_eq!(metrics.outliers, vec![3]);
    }

    #[test]
    fn test_numerical_consensus() {
        let calculator = DriftCalculator::new();
        let outputs = vec!["100".to_string(), "101".to_string(), "99".to_string()];

        let metrics = calculator.calculate_drift(&outputs);
        assert!(metrics.consistency_score > 0.8);
        assert!(metrics.consensus_output.is_some());
    }

    #[test]
    fn test_threshold_configuration() {
        let calculator = DriftCalculator::with_threshold(0.9);
        let outputs = vec!["hello".to_string(), "helo".to_string()]; // Typo

        let metrics = calculator.calculate_drift(&outputs);
        // With higher threshold, agreement rate should be lower
        assert!(metrics.agreement_rate < 1.0);
    }
}
