use crate::models::Output;
use rayon::prelude::*;
use std::collections::HashMap;

/// `1 - distance / longer length`, the same value as
/// `strsim::normalized_levenshtein`, computed with a tighter kernel: ASCII
/// text is compared as bytes, and the shared prefix and suffix (most of two
/// near-identical model outputs) are skipped before the quadratic part.
fn normalized_levenshtein(a: &str, b: &str) -> f64 {
    if a.is_empty() && b.is_empty() {
        return 1.0;
    }
    let (distance, longer) = if a.is_ascii() && b.is_ascii() {
        (
            edit_distance(a.as_bytes(), b.as_bytes()),
            a.len().max(b.len()),
        )
    } else {
        let a: Vec<char> = a.chars().collect();
        let b: Vec<char> = b.chars().collect();
        (edit_distance(&a, &b), a.len().max(b.len()))
    };
    1.0 - distance as f64 / longer as f64
}

/// Levenshtein distance by single-row dynamic programming.
fn edit_distance<T: PartialEq>(a: &[T], b: &[T]) -> usize {
    let prefix = a.iter().zip(b).take_while(|(x, y)| x == y).count();
    let (a, b) = (&a[prefix..], &b[prefix..]);
    let suffix = a
        .iter()
        .rev()
        .zip(b.iter().rev())
        .take_while(|(x, y)| x == y)
        .count();
    let (a, b) = (&a[..a.len() - suffix], &b[..b.len() - suffix]);

    // Keep the row over the shorter side.
    let (a, b) = if a.len() < b.len() { (b, a) } else { (a, b) };
    if b.is_empty() {
        return a.len();
    }

    let mut row: Vec<usize> = (0..=b.len()).collect();
    for (i, x) in a.iter().enumerate() {
        let mut diagonal = row[0];
        row[0] = i + 1;
        for (j, y) in b.iter().enumerate() {
            let substituted = if x == y { diagonal } else { diagonal + 1 };
            diagonal = row[j + 1];
            row[j + 1] = substituted.min(row[j] + 1).min(diagonal + 1);
        }
    }
    row[b.len()]
}

/// Similarity of every unordered pair of outputs, computed once (in parallel)
/// and shared by every pass of [`DriftCalculator::calculate_drift`]. Stored
/// as the upper triangle in row order: (0,1), (0,2), ..., (1,2), ...
struct PairwiseSimilarities {
    len: usize,
    upper: Vec<f64>,
}

impl PairwiseSimilarities {
    fn get(&self, i: usize, j: usize) -> f64 {
        if i == j {
            return 1.0;
        }
        let (i, j) = if i < j { (i, j) } else { (j, i) };
        self.upper[i * self.len - i * (i + 1) / 2 + (j - i - 1)]
    }
}

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

        // Every pass below reads from this one table, so each pair's
        // (expensive) edit distance is computed exactly once.
        let similarities = self.calculate_pairwise_similarities(outputs);

        // Calculate average similarity (consistency score)
        let total_pairs = outputs.len() * (outputs.len() - 1) / 2;
        let avg_similarity = similarities.upper.iter().sum::<f64>() / total_pairs as f64;

        // Calculate agreement rate (exact or near-exact matches)
        let agreement_rate = self.calculate_agreement_rate(&similarities);

        // Drift score is inverse of consistency
        let drift_score = 1.0 - avg_similarity;

        // Find consensus output
        let consensus_index = self.find_consensus(outputs, &similarities);
        let consensus_output = Some(outputs[consensus_index].clone());

        // Determine consensus confidence
        let consensus_confidence = match agreement_rate {
            rate if rate > 0.8 => ConsensusConfidence::High,
            rate if rate >= 0.5 => ConsensusConfidence::Medium,
            rate if rate > 0.0 => ConsensusConfidence::Low,
            _ => ConsensusConfidence::None,
        };

        // Find outliers (outputs significantly different from consensus)
        let outliers = self.find_outliers(consensus_index, &similarities);

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

    /// Calculate pairwise similarities for all combinations, in parallel.
    fn calculate_pairwise_similarities(&self, outputs: &[String]) -> PairwiseSimilarities {
        let len = outputs.len();
        // `collect` keeps the rows in order, so the layout (and the order the
        // average is summed in) does not depend on thread scheduling.
        let upper = (0..len)
            .into_par_iter()
            .flat_map_iter(|i| {
                ((i + 1)..len).map(move |j| self.semantic_similarity(&outputs[i], &outputs[j]))
            })
            .collect();
        PairwiseSimilarities { len, upper }
    }

    /// Calculate agreement rate (percentage of outputs that are similar enough)
    fn calculate_agreement_rate(&self, similarities: &PairwiseSimilarities) -> f64 {
        // Greedy clustering: each output joins the first cluster whose
        // representative (its first member) it is similar enough to.
        let mut clusters: Vec<(usize, usize)> = Vec::new(); // (representative, size)

        for i in 0..similarities.len {
            let joined = clusters
                .iter_mut()
                .find(|(repr, _)| similarities.get(i, *repr) >= self.similarity_threshold);
            match joined {
                Some((_, size)) => *size += 1,
                None => clusters.push((i, 1)),
            }
        }

        // Find the largest cluster
        let max_cluster_size = clusters.iter().map(|(_, size)| *size).max().unwrap_or(0);
        max_cluster_size as f64 / similarities.len as f64
    }

    /// Find the index of the consensus output (most common or centroid)
    fn find_consensus(&self, outputs: &[String], similarities: &PairwiseSimilarities) -> usize {
        // Try frequency-based consensus first: (first index, count) per value
        let mut frequency_map: HashMap<&str, (usize, usize)> = HashMap::new();
        for (i, output) in outputs.iter().enumerate() {
            frequency_map.entry(output).or_insert((i, 0)).1 += 1;
        }

        // If there's a clear most frequent output
        if let Some(&(index, count)) = frequency_map.values().max_by_key(|(_, count)| *count) {
            if count > outputs.len() / 2 {
                return index;
            }
        }

        // Otherwise, find the output with highest average similarity to all others
        let mut best_index = 0;
        let mut best_avg_similarity = 0.0;

        for candidate in 0..outputs.len() {
            let total: f64 = (0..outputs.len())
                .map(|other| similarities.get(candidate, other))
                .sum();
            let avg_similarity = total / outputs.len() as f64;

            if avg_similarity > best_avg_similarity {
                best_avg_similarity = avg_similarity;
                best_index = candidate;
            }
        }

        best_index
    }

    /// Find outliers (outputs significantly different from consensus)
    fn find_outliers(&self, consensus: usize, similarities: &PairwiseSimilarities) -> Vec<usize> {
        (0..similarities.len)
            // More strict for outliers
            .filter(|&i| similarities.get(i, consensus) < self.similarity_threshold * 0.7)
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

    /// The straightforward sequential definition of each metric, recomputing
    /// every similarity where it is needed. `calculate_drift` must agree with
    /// it exactly while computing each pair only once.
    fn reference_metrics(
        calc: &DriftCalculator,
        outputs: &[String],
    ) -> (f64, f64, String, Vec<usize>) {
        let n = outputs.len();
        let mut total = 0.0;
        for i in 0..n {
            for j in (i + 1)..n {
                total += calc.semantic_similarity(&outputs[i], &outputs[j]);
            }
        }
        let consistency = total / (n * (n - 1) / 2) as f64;

        let mut clusters: Vec<Vec<&String>> = Vec::new();
        for output in outputs {
            match clusters
                .iter_mut()
                .find(|c| calc.semantic_similarity(output, c[0]) >= calc.similarity_threshold)
            {
                Some(cluster) => cluster.push(output),
                None => clusters.push(vec![output]),
            }
        }
        let agreement = clusters.iter().map(Vec::len).max().unwrap() as f64 / n as f64;

        let majority = outputs
            .iter()
            .find(|o| outputs.iter().filter(|p| p == o).count() > n / 2);
        let consensus = majority.cloned().unwrap_or_else(|| {
            let mut best = outputs[0].clone();
            let mut best_avg = 0.0;
            for candidate in outputs {
                let avg = outputs
                    .iter()
                    .map(|other| calc.semantic_similarity(candidate, other))
                    .sum::<f64>()
                    / n as f64;
                if avg > best_avg {
                    best_avg = avg;
                    best = candidate.clone();
                }
            }
            best
        });

        let outliers = (0..n)
            .filter(|&i| {
                calc.semantic_similarity(&outputs[i], &consensus) < calc.similarity_threshold * 0.7
            })
            .collect();

        (consistency, agreement, consensus, outliers)
    }

    #[test]
    fn test_shared_pairwise_table_matches_the_sequential_definition() {
        let strings = |items: &[&str]| items.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        // Deterministic pseudo-random text, so no majority and no duplicates.
        let mut seed = 0x2545_f491_4f6c_dd1d_u64;
        let mut varied = Vec::new();
        for i in 0..40 {
            let mut text = String::new();
            for _ in 0..(20 + i % 7) {
                seed ^= seed << 13;
                seed ^= seed >> 7;
                seed ^= seed << 17;
                text.push((b'a' + (seed % 6) as u8) as char);
            }
            varied.push(text);
        }

        let cases = vec![
            strings(&["yes", "yes", "yes", "no"]),
            strings(&[
                "the cat sat",
                "the cat sat.",
                "a dog ran",
                "the cat sits",
                "zzz",
            ]),
            strings(&["100", "101", "99", "250", "100"]),
            strings(&["a", "b"]),
            strings(&["same", "same"]),
            varied,
        ];

        for threshold in [0.5, 0.85] {
            let calc = DriftCalculator::with_threshold(threshold);
            for outputs in &cases {
                let metrics = calc.calculate_drift(outputs);
                let (consistency, agreement, consensus, outliers) =
                    reference_metrics(&calc, outputs);
                assert_eq!(metrics.consistency_score, consistency, "{outputs:?}");
                assert_eq!(metrics.agreement_rate, agreement, "{outputs:?}");
                assert_eq!(metrics.consensus_output, Some(consensus), "{outputs:?}");
                assert_eq!(metrics.outliers, outliers, "{outputs:?}");
            }
        }
    }

    #[test]
    fn test_normalized_levenshtein_matches_strsim() {
        let mut seed = 0x9e37_79b9_7f4a_7c15_u64;
        let mut next = move |bound: u64| {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            seed % bound
        };
        let alphabets: [&[char]; 2] = [&['a', 'b', 'c', ' '], &['a', 'é', '日', '😀', 'b']];
        let mut cases = vec![
            (String::new(), String::new()),
            (String::new(), "abc".to_string()),
            ("kitten".to_string(), "sitting".to_string()),
            (
                "prefix-same-suffix".to_string(),
                "prefix-diff-suffix".to_string(),
            ),
            ("aaa".to_string(), "aaaa".to_string()),
        ];
        for round in 0..400 {
            let alphabet = alphabets[round % 2];
            let mut word = || -> String {
                (0..next(24))
                    .map(|_| alphabet[next(alphabet.len() as u64) as usize])
                    .collect()
            };
            cases.push((word(), word()));
        }

        for (a, b) in &cases {
            assert_eq!(
                normalized_levenshtein(a, b),
                strsim::normalized_levenshtein(a, b),
                "{a:?} vs {b:?}"
            );
        }
    }

    #[test]
    fn test_pairwise_table_indexing() {
        let calc = DriftCalculator::new();
        let outputs: Vec<String> = ["alpha", "alpine", "beta", "betamax", "gamma"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        let table = calc.calculate_pairwise_similarities(&outputs);

        assert_eq!(table.upper.len(), 10);
        for i in 0..outputs.len() {
            for j in 0..outputs.len() {
                assert_eq!(
                    table.get(i, j),
                    calc.semantic_similarity(&outputs[i], &outputs[j]),
                    "({i}, {j})"
                );
            }
        }
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
