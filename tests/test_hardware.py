import pytest
from unittest.mock import patch

from briefcase import HardwareMetadata
from briefcase.hardware import detect_hardware

def test_hardware_metadata_creation():
    hw = HardwareMetadata("cuda", "NVIDIA A100")
    hw.with_vram(80.0)
    hw.with_provider("lambda")
    # No direct getters exposed in MVP, but verify it doesn't crash
    assert hw is not None

def test_detect_hardware_metal():
    with patch("platform.system", return_value="Darwin"):
        with patch("platform.machine", return_value="arm64"):
            hw = detect_hardware()
            # On Apple Silicon it should detect metal
            assert "metal" in str(hw).lower() or "apple" in str(hw).lower()

def test_detect_hardware_cpu_fallback():
    # Mock subprocess to fail (simulate no nvidia-smi)
    with patch("subprocess.check_output", side_effect=Exception("no nvidia")):
        with patch("platform.system", return_value="Linux"):
            hw = detect_hardware()
            assert "cpu" in str(hw).lower()
