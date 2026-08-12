import sys
from unittest.mock import MagicMock

# Create a robust mock for the compiled _core module
mock_core = MagicMock()
mock_core.__version__ = "3.0.0"
mock_core.__author__ = "Briefcase AI"

# Mock the classes expected by the Python facade


class MockDecision:
    def __init__(self, name):
        self.function_name = name
        self.tags = {}

    def add_tag(self, k, v): self.tags[k] = v

    def add_input(self, i): pass

    def add_output(self, o): pass

    def with_model_parameters(self, p): pass

    def with_scorecard(self, s): pass

    def with_hardware(self, h): pass

    def fingerprint(self): return "mock-fingerprint"


class MockHardware:
    def __init__(self, t, n):
        self.device_type = t
        self.device_name = n

    def with_vram(self, v): return self

    def with_provider(self, p): return self

    def __str__(self): return f"Hardware({self.device_type})"


class MockOutput:
    def __init__(self, n, v, t): pass

    def with_confidence(self, c): return self


mock_core.DecisionSnapshot = MockDecision
mock_core.HardwareMetadata = MockHardware
mock_core.Input = MagicMock
mock_core.Output = MockOutput
mock_core.Scorecard = MagicMock
mock_core.SqliteBackend = MagicMock
mock_core.init_with_config = MagicMock()
mock_core.is_initialized.return_value = True
mock_core._shutdown_runtime = MagicMock()

# Inject into sys.modules
sys.modules["briefcase._native"] = mock_core
