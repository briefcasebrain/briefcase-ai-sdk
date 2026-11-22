"""Auto-instrumentation for Amazon Bedrock Runtime through ``aiobotocore``
(async clients, also what ``aioboto3`` builds on).

``aiobotocore.client.AioBaseClient`` overrides ``_make_api_call`` with a
coroutine function, so the :mod:`.bedrock` patch on botocore's
``BaseClient`` never sees these calls; this module patches the override.
Everything else (which operations, what is read, the limits) is shared
with :mod:`.bedrock`: ``response["stream"]`` / ``response["body"]`` event
streams are wrapped in an async proxy, and an ``InvokeModel`` body is
observed through its (awaitable) ``read``.

Verification status, stated plainly: this was written against the source
of ``aiobotocore==3.10.0`` (``client.py``, ``eventstream.py``,
``response.py``) and is exercised by tests against a stand-in client with
the same method shape. It has **not** been run against a real
``aiobotocore`` install: no released aiobotocore accepted the botocore
version in the environment this was developed in. ``TESTED_VERSIONS`` is
therefore empty and ``briefcase.auto.status()`` says so.
"""

from __future__ import annotations

import importlib
from typing import Any

from briefcase.integrations.providers._base import ProviderInstrumentor
from briefcase.integrations.providers.bedrock import LIMITS, _async_factory

PROVIDER = "bedrock"
NAME = "aiobotocore"
TESTED_VERSIONS: tuple = ()
#: Version whose source the patch was written against (not run).
READ_VERSION = "3.10.0"
VERIFIED_VERSION = "none (written against the source of 3.10.0)"


def instrument(**options: Any) -> ProviderInstrumentor:
    """Patch aiobotocore for Bedrock Runtime calls. Returns the
    instrumentor; ``briefcase.auto.undo("aiobotocore")`` restores.

    Raises:
        ImportError: ``aiobotocore`` is not installed.
    """
    try:
        sdk = importlib.import_module("aiobotocore")
        importlib.import_module("aiobotocore.client")
    except ImportError as exc:
        raise ImportError(
            "briefcase.auto: aiobotocore is not installed, so nothing was "
            "instrumented. Install with: pip install aiobotocore"
        ) from exc

    inst = ProviderInstrumentor(PROVIDER, **options)
    inst.sdk_version = getattr(sdk, "__version__", None)
    inst.tested_versions = TESTED_VERSIONS
    inst.limits = list(LIMITS) + [
        "not run against a real aiobotocore install (written against the 3.10.0 source)",
    ]
    inst.patch("aiobotocore.client", "AioBaseClient", "_make_api_call", _async_factory(inst),
               ("operation_name", "api_params"))
    inst.report_skipped(VERIFIED_VERSION)
    return inst


__all__ = ["NAME", "PROVIDER", "READ_VERSION", "TESTED_VERSIONS", "VERIFIED_VERSION", "instrument"]
