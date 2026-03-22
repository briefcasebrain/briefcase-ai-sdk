"""Simple CLI example that records prompts as Briefcase decisions."""

from __future__ import annotations

import argparse
import sys

from briefcase import DecisionSnapshot, Input, ModelParameters, Output, init
from briefcase.storage import SqliteBackend


def main() -> int:
    parser = argparse.ArgumentParser(description="Record a prompt/response pair")
    parser.add_argument("--model", default="gpt-4o", help="Logical model identifier")
    parser.add_argument("--prompt", required=True, help="Prompt to capture")
    args = parser.parse_args()

    init()  # ensure the native runtime is available

    decision = DecisionSnapshot("cli_prompt")
    decision.add_input(Input("prompt", args.prompt, "string"))

    params = ModelParameters(args.model)
    decision.with_model_parameters(params)

    # Replace this block with a real model invocation.
    simulated_response = args.prompt[::-1]  # toy transformation
    decision.add_output(Output("response", simulated_response, "string"))

    backend = SqliteBackend.in_memory()
    decision_id = backend.save_decision(decision)

    print(f"Recorded decision {decision_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
