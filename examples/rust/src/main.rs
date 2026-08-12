use briefcase_core::storage::{SnapshotQuery, SqliteBackend, StorageBackend};
use briefcase_core::*;
use serde_json::json;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    println!(" Briefcase AI SDK Example");
    println!("============================");

    // Create an in-memory SQLite storage backend
    let storage = SqliteBackend::in_memory()?;
    println!(" Created storage backend");

    // Example 1: Create and save a decision snapshot
    println!("\n Creating AI decision snapshot...");
    let input = Input::new("user_query", json!("What is the capital of France?"), "string");
    let output = Output::new("ai_response", json!("Paris"), "string")
        .with_confidence(0.98);

    let model_params = ModelParameters::new("gpt-4")
        .with_provider("openai")
        .with_parameter("temperature", json!(0.7));

    let snapshot = DecisionSnapshot::new("answer_geography_question")
        .with_module("geography_qa")
        .add_input(input)
        .add_output(output)
        .with_model_parameters(model_params)
        .with_execution_time(350.5)
        .add_tag("category", "geography")
        .add_tag("difficulty", "easy");

    let snapshot_id = storage.save_decision(&snapshot).await?;
    println!(" Saved decision snapshot: {}", snapshot_id);

    // Example 2: Drift detection
    println!("\n Analyzing output drift...");
    let drift_calculator = DriftCalculator::new();
    let model_outputs = vec![
        "Paris".to_string(),
        "Paris, France".to_string(),
        "The capital of France is Paris".to_string(),
        "Lyon".to_string(), // This is an outlier
    ];

    let drift_metrics = drift_calculator.calculate_drift(&model_outputs);
    println!("   Consistency score: {:.2}", drift_metrics.consistency_score);
    println!("   Agreement rate: {:.1}%", drift_metrics.agreement_rate * 100.0);
    println!("   Drift score: {:.2}", drift_metrics.drift_score);
    println!("   Consensus output: {:?}", drift_metrics.consensus_output);
    println!("   Outliers found: {} at indices {:?}", drift_metrics.outliers.len(), drift_metrics.outliers);

    // Example 3: Cost estimation
    println!("\n Estimating AI costs...");
    let cost_calculator = CostCalculator::new();

    let estimate_gpt4 = cost_calculator.estimate_cost("gpt-4", 1000, 500)?;
    let estimate_gpt35 = cost_calculator.estimate_cost("gpt-3.5-turbo", 1000, 500)?;

    println!("   GPT-4 cost: ${:.4} (input: ${:.4}, output: ${:.4})",
        estimate_gpt4.total_cost, estimate_gpt4.input_cost, estimate_gpt4.output_cost);
    println!("   GPT-3.5 cost: ${:.4} (input: ${:.4}, output: ${:.4})",
        estimate_gpt35.total_cost, estimate_gpt35.input_cost, estimate_gpt35.output_cost);

    let savings = estimate_gpt4.total_cost - estimate_gpt35.total_cost;
    println!("    Savings with GPT-3.5: ${:.4} ({:.1}%)",
        savings, (savings / estimate_gpt4.total_cost) * 100.0);

    // Example 4: Budget monitoring
    let budget_status = cost_calculator.check_budget(75.0, 100.0);
    println!("    Budget status: {:?} ({:.1}% used)",
        budget_status.status, budget_status.percent_used);

    // Example 5: PII sanitization
    println!("\n Sanitizing sensitive data...");
    let sanitizer = Sanitizer::new();

    let sensitive_text = "Contact John Doe at john.doe@company.com or call 555-123-4567. His SSN is 123-45-6789 and API key is sk-1234567890abcdef1234567890abcdef.";
    let sanitized = sanitizer.sanitize(sensitive_text);

    println!("   Original: {}", sensitive_text);
    println!("   Sanitized: {}", sanitized.sanitized);
    println!("   Redactions made: {}", sanitized.redactions.len());

    for redaction in &sanitized.redactions {
        println!("     - {:?} at position {}-{}",
            redaction.pii_type, redaction.start_position, redaction.end_position);
    }

    // Example 6: Query snapshots
    println!("\n Querying saved snapshots...");
    let query = SnapshotQuery::new()
        .with_function_name("answer_geography_question")
        .with_tag("category", "geography");

    let snapshots = storage.query(query).await?;
    println!("   Found {} snapshots matching query", snapshots.len());

    // Example 7: Replay simulation
    println!("\n Setting up replay engine...");
    let replay_engine = ReplayEngine::new(storage);

    // Create a policy for validation
    let policy = ReplayPolicy::new("geography_qa_policy")
        .with_exact_match("function_name")
        .with_similarity_threshold("output", 0.9);

    println!("   Created replay policy: {}", policy.name);
    println!("   Policy rules: {}", policy.rules.len());
    println!("   Replay mode: {:?}", replay_engine.default_mode());

    println!("\n All examples completed successfully!");
    Ok(())
}