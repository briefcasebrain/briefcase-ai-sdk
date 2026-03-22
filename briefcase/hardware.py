"""Hardware detection helpers for telemetry and reporting."""

from __future__ import annotations

import platform
import subprocess
from typing import Optional

from briefcase import HardwareMetadata

__all__ = ["detect_hardware"]


def _detect_cuda() -> Optional[HardwareMetadata]:
    """Try to detect an NVIDIA GPU via nvidia-smi."""
    try:
        output = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return None

    if not output:
        return None

    first_line = output.splitlines()[0]
    parts = [segment.strip() for segment in first_line.split(",")]
    if not parts:
        return None

    name = parts[0]
    hw = HardwareMetadata("cuda", name)
    if len(parts) > 1:
        try:
            hw.with_vram(float(parts[1]))
        except ValueError:
            pass
    hw.with_provider("nvidia")
    return hw


def detect_hardware() -> HardwareMetadata:
    """Detect the best available hardware accelerator on this host."""
    system = platform.system()
    machine = platform.machine().lower()

    if system == "Darwin" and machine.startswith("arm"):
        hw = HardwareMetadata("metal", "Apple Silicon")
        hw.with_provider("apple")
        return hw

    cuda = _detect_cuda()
    if cuda:
        return cuda

    try:
        cpu_name = platform.processor() or "generic"
    except Exception:
        cpu_name = "generic"
    hw = HardwareMetadata("cpu", cpu_name)
    hw.with_provider(system.lower() or "cpu")
    return hw
