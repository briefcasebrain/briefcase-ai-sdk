"""Controls semantic conventions match the shared fixture in both directions.

The TypeScript suite asserts the same fixture, so the two languages cannot
drift apart and the fixture is the authoritative table.
"""

import json
from pathlib import Path

import briefcase.semantic_conventions.controls as controls

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "semconv_controls.json").read_text()
)


def test_module_constants_equal_the_fixture_exactly():
    module_constants = {
        name: value
        for name, value in vars(controls).items()
        if name.isupper() and isinstance(value, str)
    }
    assert module_constants == FIXTURE["constants"]
