#  Python Basic Example

This example demonstrates the core features of Briefcase AI using Python, including decision tracking, drift detection, cost analysis, and data sanitization.

##  Installation

```bash
# Install dependencies
pip install briefcase-ai pandas matplotlib

# Or using the development version
cd ../../crates/python
maturin develop
pip install pytest pandas matplotlib
```

##  Quick Start

```bash
python basic_usage.py
```

##  Examples

### 1. Basic Decision Tracking
```python
import briefcase as bf

# Create a decision snapshot
decision = bf.DecisionSnapshot("sentiment_analysis")

# Add input data
input_text = bf.Input("review", "This product is amazing!", "string")
decision.add_input(input_text)

# Add model parameters
params = bf.ModelParameters("bert-base-uncased")
params.with_parameter("max_length", 512)
params.with_parameter("temperature", 0.7)
decision.with_model_parameters(params)

# Add output with confidence
output = bf.Output("sentiment", "positive", "string")
output.with_confidence(0.94)
decision.add_output(output)

# Track execution metrics
decision.with_execution_time(45.2)
decision.add_tag("environment", "production")
decision.add_tag("model_version", "v2.1")

print(f"Decision: {decision.function_name}")
print(f"Confidence: {output.confidence}")
```

### 2. Drift Detection
```python
import briefcase as bf

# Create drift calculator
calculator = bf.DriftCalculator()

# Simulate model outputs over time
week1_outputs = ["positive", "positive", "neutral", "positive"]
week2_outputs = ["negative", "neutral", "negative", "positive"]
week3_outputs = ["negative", "negative", "negative", "negative"]

# Analyze drift
for week, outputs in enumerate([week1_outputs, week2_outputs, week3_outputs], 1):
    metrics = calculator.calculate_drift(outputs)
    status = calculator.get_status(metrics)

    print(f"Week {week}:")
    print(f"  Consistency: {metrics.consistency_score:.2f}")
    print(f"  Status: {status}")
    print(f"  Consensus: {metrics.consensus_output}")
```

### 3. Cost Analysis
```python
import briefcase as bf

# Initialize cost calculator
calculator = bf.CostCalculator()

# Compare different models
models = ["gpt-4", "gpt-3.5-turbo", "claude-3-sonnet"]
input_tokens = 1000
output_tokens = 500

print("Model Cost Comparison:")
for model in models:
    try:
        estimate = calculator.estimate_cost(model, input_tokens, output_tokens)
        print(f"{model:15} ${estimate.total_cost:.4f}")
    except Exception as e:
        print(f"{model:15} Error: {e}")

# Budget monitoring
budget = 100.0
current_spend = 75.0
status = calculator.check_budget(current_spend, budget)

print(f"\nBudget Status: {status.status}")
print(f"Spent: ${status.spent_usd:.2f} / ${status.budget_usd:.2f}")
print(f"Remaining: ${status.remaining_usd:.2f}")
```

### 4. Data Sanitization
```python
import briefcase as bf

# Create sanitizer
sanitizer = bf.Sanitizer()

# Test data with PII
test_data = {
    "customer_feedback": "Please contact me at john.doe@company.com or call 555-123-4567",
    "payment_info": "My credit card is 4532-1234-5678-9012",
    "system_logs": "API request from 192.168.1.100 using key sk-abc123def456"
}

print("Data Sanitization:")
for key, value in test_data.items():
    result = sanitizer.sanitize(value)
    print(f"\n{key}:")
    print(f"  Original: {value}")
    print(f"  Sanitized: {result.sanitized}")
    print(f"  Redactions: {len(result.redactions)}")

# JSON sanitization
json_data = {
    "user": {
        "email": "admin@company.com",
        "ssn": "123-45-6789"
    },
    "api_config": {
        "key": "sk-1234567890abcdef",
        "timeout": 30
    }
}

json_result = sanitizer.sanitize_json(json_data)
print(f"\nJSON Sanitization:")
print(f"Redacted fields: {len(json_result.redactions)}")
```

##  Advanced Examples

### Session Management
```python
import briefcase as bf

# Create a session to group multiple decisions
session = bf.Snapshot("session")

# Simulate multiple AI decisions
for i in range(5):
    decision = bf.DecisionSnapshot(f"classify_text_{i}")

    # Add varied inputs and outputs
    input_data = bf.Input("text", f"Sample text {i}", "string")
    decision.add_input(input_data)

    # Simulate different confidence levels
    confidence = 0.8 + (i * 0.04)  # 0.8 to 0.96
    output = bf.Output("category", f"category_{i % 3}", "string")
    output.with_confidence(confidence)
    decision.add_output(output)

    session.add_decision(decision)

print(f"Session contains {session.decision_count} decisions")

# Analyze the session
decisions_data = session.to_object()
confidences = []
for decision in decisions_data["decisions"]:
    for output in decision["outputs"]:
        if output.get("confidence"):
            confidences.append(output["confidence"])

avg_confidence = sum(confidences) / len(confidences)
print(f"Average confidence: {avg_confidence:.2f}")
```

### Performance Monitoring
```python
import time
import briefcase as bf

def monitor_ai_function():
    """Example of monitoring a real AI function"""

    # Start timing
    start_time = time.time()

    # Create decision snapshot
    decision = bf.DecisionSnapshot("text_generation")

    # Add input
    prompt = bf.Input("prompt", "Write a short story about AI", "string")
    decision.add_input(prompt)

    # Simulate AI model call
    time.sleep(0.1)  # Simulate processing time
    generated_text = "Once upon a time, there was an AI..."

    # Calculate execution time
    execution_time = (time.time() - start_time) * 1000  # Convert to ms

    # Add output
    output = bf.Output("generated_text", generated_text, "string")
    output.with_confidence(0.87)
    decision.add_output(output)

    # Add execution metrics
    decision.with_execution_time(execution_time)
    decision.add_tag("model_size", "7B")
    decision.add_tag("gpu_used", "A100")

    return decision

# Run monitored function
result = monitor_ai_function()
print(f"Generated text in {result.execution_time_ms:.1f}ms")
print(f"Tags: {result.tags}")
```

##  Running Tests

```bash
# Run the example tests
pytest test_examples.py -v

# Run with coverage
pytest test_examples.py --cov=. --cov-report=html
```

##  Next Steps

1. **Explore Storage**: Learn about SQLite backends
2. **Validation**: Check out the [validation example](../validation/)
3. **Versioning**: Try the [lakeFS versioning example](../lakefs_versioning/)
4. **Custom Patterns**: Add domain-specific PII detection patterns

##  Related Examples

- [Validation](../validation/) - Prompt validation engine
- [lakeFS Versioning](../lakefs_versioning/) - Versioned storage
- [Multi-Agent Correlation](../multi_agent_correlation/) - Workflow tracing

---

** Questions? Check out the [documentation](../../docs/python/) or open an issue!**