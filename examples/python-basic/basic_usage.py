#!/usr/bin/env python3
"""
Briefcase AI Python Basic Usage Example

This script demonstrates all core features of Briefcase AI:
- Decision tracking and snapshots
- Drift detection and analysis
- Cost calculation and budget monitoring
- Data sanitization and PII protection

Run: python basic_usage.py
"""

import sys
import time

try:
    from briefcase import (
        DecisionSnapshot,
        Input,
        ModelParameters,
        Output,
        Snapshot,
    )
    from briefcase.cost import CostCalculator
    from briefcase.drift import DriftCalculator
    from briefcase.sanitize import Sanitizer
    print(" Briefcase imported successfully")
except ImportError as e:
    print(f" Failed to import briefcase: {e}")
    print("Please install with: pip install briefcase-ai")
    print("Or build from source: maturin develop")
    sys.exit(1)


def demo_decision_tracking():
    """Demonstrate decision tracking and snapshots"""
    print("\n Decision Tracking Demo")
    print("=" * 50)

    # Create a decision snapshot for sentiment analysis
    decision = DecisionSnapshot("sentiment_analysis")

    # Add input data
    review_text = "This product exceeded my expectations! Highly recommended."
    input_data = Input("review_text", review_text, "string")
    decision.add_input(input_data)

    # Add model parameters
    params = ModelParameters("bert-sentiment-v2")
    params.with_provider("huggingface")
    params.with_parameter("max_length", 512)
    params.with_parameter("temperature", 0.7)
    params.with_parameter("do_sample", True)
    decision.with_model_parameters(params)

    # Simulate AI model execution time
    start_time = time.time()
    time.sleep(0.05)  # Simulate 50ms processing
    execution_time = (time.time() - start_time) * 1000

    # Add output with confidence score
    sentiment_output = Output("sentiment", "positive", "string")
    sentiment_output.with_confidence(0.947)
    decision.add_output(sentiment_output)

    # Add execution metadata
    decision.with_execution_time(execution_time)
    decision.with_module("sentiment_service")
    decision.add_tag("environment", "production")
    decision.add_tag("model_version", "v2.1.3")
    decision.add_tag("gpu_memory", "8GB")

    # Create session and add decision
    session = Snapshot("session")
    session.add_decision(decision)

    print(f" Decision tracked: {decision.function_name}")
    print(f"   Input: {review_text[:50]}...")
    print(f"   Output: {sentiment_output.value} (confidence: {sentiment_output.confidence})")
    print(f"   Execution time: {execution_time:.1f}ms")
    print(f"   Session decisions: {len(session.decisions)}")

    return session


def demo_drift_detection():
    """Demonstrate drift detection capabilities"""
    print("\n Drift Detection Demo")
    print("=" * 50)

    calculator = DriftCalculator()

    # Simulate model outputs over different time periods
    scenarios = [
        ("Week 1 (Stable)", ["positive", "positive", "neutral", "positive", "positive"]),
        ("Week 2 (Mixed)", ["positive", "negative", "neutral", "positive", "negative"]),
        ("Week 3 (Drift)", ["negative", "negative", "neutral", "negative", "negative"]),
        ("Week 4 (Chaotic)", ["positive", "negative", "neutral", "unknown", "error"]),
    ]

    print("Analyzing model output consistency over time:")
    print()

    for period, outputs in scenarios:
        metrics = calculator.calculate_drift(outputs)
        status = metrics.get_status(calculator)

        print(f" {period}:")
        print(f"   Outputs: {outputs}")
        print(f"   Consistency Score: {metrics.consistency_score:.3f}")
        print(f"   Agreement Rate: {metrics.agreement_rate:.3f}")
        print(f"   Status: {status.upper()}")
        print(f"   Consensus: {metrics.consensus_output}")
        print(f"   Outliers: {len(metrics.outliers)} detected")
        print()

    # Test with custom threshold
    print(" Testing with stricter threshold (0.95):")
    strict_calculator = DriftCalculator()
    strict_calculator.with_similarity_threshold(0.95)
    outputs = ["hello", "helo", "hello"]  # One typo
    strict_metrics = strict_calculator.calculate_drift(outputs)

    print(f"   Outputs: {outputs}")
    print(f"   Agreement Rate: {strict_metrics.agreement_rate:.3f}")
    print(f"   Status: {strict_metrics.get_status(strict_calculator)}")


def demo_cost_analysis():
    """Demonstrate cost calculation and budget monitoring"""
    print("\n Cost Analysis Demo")
    print("=" * 50)

    calculator = CostCalculator()

    # Compare costs across different models
    print("Model cost comparison for 1000 input + 500 output tokens:")
    print()

    models_to_compare = [
        "gpt-4",
        "gpt-5.5",
        "gpt-5.4-mini",
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
    ]

    estimates = []
    for model in models_to_compare:
        try:
            estimate = calculator.estimate_cost(model, 1000, 500)
            estimates.append((model, estimate))
            print(f" {model:15} ${estimate.total_cost:.4f} (${estimate.input_cost:.4f} + ${estimate.output_cost:.4f})")
        except Exception as e:
            print(f" {model:15} Error: {str(e)[:30]}...")

    # Find the cheapest option
    if estimates:
        cheapest = min(estimates, key=lambda x: x[1].total_cost)
        most_expensive = max(estimates, key=lambda x: x[1].total_cost)
        savings = most_expensive[1].total_cost - cheapest[1].total_cost

        print(f"\n Cheapest: {cheapest[0]} (${cheapest[1].total_cost:.4f})")
        print(f"   Most expensive: {most_expensive[0]} (${most_expensive[1].total_cost:.4f})")
        print(f"   Potential savings: ${savings:.4f} ({(savings/most_expensive[1].total_cost)*100:.1f}%)")

    # Rate cards: same model, different pricing schemes (platform x tier x modifiers)
    print("\n Rate cards for claude-opus-4-8 (500k input + 50k output):")
    for card in ["standard", "batch", "bedrock:standard,regional", "first_party:fast"]:
        try:
            est = calculator.estimate_cost("claude-opus-4-8", 500_000, 50_000, rate_card=card)
            print(f"   {card:28} ${est.total_cost:.4f}")
        except Exception as e:
            print(f"   {card:28} Error: {str(e)[:30]}...")

    # Budget monitoring example
    print("\n Budget Monitoring:")
    budget_scenarios = [
        (45.0, 100.0),   # OK
        (85.0, 100.0),   # Warning
        (96.0, 100.0),   # Critical
        (105.0, 100.0),  # Exceeded
    ]

    for spent, budget in budget_scenarios:
        status = calculator.check_budget(spent, budget)
        icon = {"ok": "", "warning": "", "critical": "", "exceeded": ""}[status.status]
        print(f"   {icon} ${spent:5.1f} / ${budget:5.1f} ({status.percent_used:5.1f}%) - {status.status.upper()}")

    # Monthly projection
    print("\n Monthly Projection (GPT-4, 5k input + 2k output daily):")
    try:
        monthly = calculator.project_monthly_cost("gpt-4", 5000, 2000, 30.0)
        print(f"   Monthly: ${monthly:.2f}")
        print(f"   Daily:   ${monthly / 30:.2f}")
        print(f"   Annual:  ${monthly * 12:.2f}")
    except Exception as e:
        print(f"    Error: {e}")


def demo_data_sanitization():
    """Demonstrate PII detection and sanitization"""
    print("\n Data Sanitization Demo")
    print("=" * 50)

    sanitizer = Sanitizer()

    # Test various types of sensitive data
    test_cases = [
        ("Customer Email", "Please contact our support at support@company.com for assistance"),
        ("Phone Number", "Call me at (555) 123-4567 or text 555.987.6543"),
        ("Credit Card", "Payment with card 4532-1234-5678-9012 was successful"),
        ("SSN", "Social security number 123-45-6789 on file"),
        ("API Key", "Using API key sk-1234567890abcdef for authentication"),
        ("IP Address", "Request originated from 192.168.1.100"),
        ("Mixed PII", "John (john.doe@company.com) called from 555-123-4567 about card ****-1234"),
    ]

    print("Text sanitization results:")
    print()

    for category, text in test_cases:
        result = sanitizer.sanitize(text)
        redaction_types = [r.pii_type for r in result.redactions]

        print(f" {category}:")
        print(f"   Original:  {text}")
        print(f"   Sanitized: {result.sanitized}")
        print(f"   Detected:  {len(result.redactions)} PII items ({', '.join(redaction_types)})")
        print()

    # JSON sanitization demo
    print(" JSON sanitization:")
    sensitive_config = {
        "database": {
            "host": "192.168.1.50",
            "user": "admin",
            "connection_limit": 100
        },
        "api_keys": {
            "openai": "sk-abcd1234567890ef",
            "anthropic": "sk-ant-api_1234567890"
        },
        "user_data": {
            "admin_email": "admin@company.com",
            "support_phone": "1-800-555-0123"
        }
    }

    json_result = sanitizer.sanitize_json(sensitive_config)
    print(f"   Original keys: {list(sensitive_config.keys())}")
    print(f"   Redactions: {json_result.redaction_count}")
    print(f"   Sanitized: {json_result.sanitized}")

    # Custom pattern example
    print("\n Custom pattern example (Employee IDs):")
    sanitizer.add_pattern("employee_id", r"\bEMP-\d{6}\b")

    employee_text = "Employee EMP-123456 reported the issue to manager EMP-789012"
    custom_result = sanitizer.sanitize(employee_text)
    print(f"   Original:  {employee_text}")
    print(f"   Sanitized: {custom_result.sanitized}")


def demo_performance_monitoring():
    """Demonstrate performance monitoring capabilities"""
    print("\n Performance Monitoring Demo")
    print("=" * 50)

    def simulate_ai_batch_processing(batch_size: int = 10):
        """Simulate processing a batch of AI requests"""
        session = Snapshot("batch")
        total_time = 0
        confidences = []

        print(f"Processing batch of {batch_size} requests...")

        for i in range(batch_size):
            # Create decision
            decision = DecisionSnapshot(f"process_request_{i}")

            # Add input
            input_data = Input("request_id", f"req_{i:03d}", "string")
            decision.add_input(input_data)

            # Simulate processing time variation
            start_time = time.time()
            processing_time = 0.02 + (i * 0.005)  # 20-70ms range
            time.sleep(processing_time)
            execution_time = (time.time() - start_time) * 1000

            # Simulate confidence variation
            confidence = max(0.7, 0.95 - (i * 0.02))  # Decreasing confidence
            output = Output("result", f"processed_{i}", "string")
            output.with_confidence(confidence)
            decision.add_output(output)

            decision.with_execution_time(execution_time)
            decision.add_tag("batch_id", "batch_001")

            session.add_decision(decision)
            total_time += execution_time
            confidences.append(confidence)

        # Calculate batch statistics
        avg_time = total_time / batch_size
        avg_confidence = sum(confidences) / len(confidences)
        min_confidence = min(confidences)

        print(" Batch completed:")
        print(f"   Requests processed: {len(session.decisions)}")
        print(f"   Total time: {total_time:.1f}ms")
        print(f"   Average time: {avg_time:.1f}ms")
        print(f"   Average confidence: {avg_confidence:.3f}")
        print(f"   Minimum confidence: {min_confidence:.3f}")

        # Check for performance issues
        if avg_time > 50:
            print("    Performance alert: Average response time high")
        if min_confidence < 0.8:
            print("    Quality alert: Low confidence detected")

        return session

    # Run performance demo
    simulate_ai_batch_processing(5)


def main():
    """Run all demo functions"""
    print(" Briefcase AI Python Example")
    print("=" * 60)
    print("This example demonstrates core Briefcase AI functionality")
    print()

    try:
        # Run all demonstrations
        demo_decision_tracking()
        demo_drift_detection()
        demo_cost_analysis()
        demo_data_sanitization()
        demo_performance_monitoring()

        print("\n All demos completed successfully!")
        print("\n Next steps:")
        print("    Try the validation example: ../validation/")
        print("    Try the lakeFS versioning example: ../lakefs_versioning/")
        print("    Read the documentation at https://briefcaseai.io")

    except Exception as e:
        print(f"\n Error during demo: {e}")
        print("Please check your installation and try again.")
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
