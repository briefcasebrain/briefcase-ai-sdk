"""Fixtures for the bitemporal backend tests.

Installs a ``pykx`` stub in ``sys.modules`` before any test module
imports :mod:`briefcase.bitemporal.backends.kdb`. The real pykx is
commercial and not installed in CI; the backend imports it lazily, so
the stub satisfies that lazy import and lets tests monkeypatch
``QConnection``. pyiceberg is deliberately not stubbed here because
``test_iceberg_backend.py`` skips itself via ``importorskip`` and must
see the real library when present.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock


def _install_pykx_stub() -> None:
    if "pykx" in sys.modules and hasattr(sys.modules["pykx"], "QConnection"):
        return
    stub = ModuleType("pykx")
    stub.QConnection = MagicMock()  # type: ignore[attr-defined]
    stub.q = MagicMock()  # type: ignore[attr-defined]
    sys.modules["pykx"] = stub


_install_pykx_stub()
