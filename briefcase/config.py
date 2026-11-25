import threading
from typing import Any, List, Optional


class _Unset:
    """Sentinel for "argument not passed", so ``None`` can mean "clear"."""

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "<unset>"


_UNSET: Any = _Unset()


class BriefcaseConfig:
    """Central configuration for Briefcase SDK extensions.

    A process-wide singleton (``BriefcaseConfig.get()``), populated by
    :func:`setup`. Who reads each field:

    - ``exporter``: ``@capture`` and every framework handler.
    - ``event_bus``: ``briefcase.events.emit``.
    - ``webhook_url`` / ``webhook_secret`` / ``subscribed_events``:
      ``briefcase.events.emit`` delivers each event to that URL through a
      ``WebhookEmitter`` (signed with the secret, filtered to the
      subscribed event types).
    - ``router`` / ``storage``: reserved. Nothing in the SDK reads them;
      they are kept so existing ``setup(router=..., storage=...)`` calls
      keep working and applications can fetch the objects back.
    """

    _instance: Optional["BriefcaseConfig"] = None
    _lock = threading.RLock()

    def __init__(self) -> None:
        self.exporter: Optional[Any] = None
        self.router: Optional[Any] = None
        self.webhook_url: Optional[str] = None
        self.webhook_secret: Optional[str] = None
        self.subscribed_events: Optional[List[str]] = None
        self.event_bus: Optional[Any] = None
        self.storage: Optional[Any] = None
        self._guardrail_packs_loaded: List[str] = []

    @classmethod
    def get(cls) -> "BriefcaseConfig":
        instance = cls._instance
        if instance is None:
            # Double-checked: the common path stays lock-free, and two
            # threads racing on first use still share one instance.
            with cls._lock:
                instance = cls._instance
                if instance is None:
                    instance = cls._instance = cls()
        return instance

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._instance = None

    @property
    def guardrail_registry(self) -> Any:
        """Expose the global guardrail registry from framework.py."""
        from briefcase.guardrails.framework import _default_registry
        return _default_registry


def setup(
    exporter: Any = _UNSET,
    router: Any = _UNSET,
    webhook_url: Any = _UNSET,
    webhook_secret: Any = _UNSET,
    events: Any = _UNSET,
    event_bus: Any = _UNSET,
    storage: Any = _UNSET,
    guardrail_packs: Optional[List[str]] = None,
) -> BriefcaseConfig:
    """Configure Briefcase SDK extensions.

    Does not interfere with briefcase.init() for the Rust runtime.

    Only the arguments you pass are changed. Passing ``None`` explicitly
    clears that setting: ``setup(exporter=None)`` turns capture export off
    again.

    Args:
        exporter: Exporter that receives decision records.
        router: Reserved; stored on the config, not read by the SDK.
        webhook_url: Deliver events emitted via ``briefcase.events.emit``
            to this URL.
        webhook_secret: HMAC secret for signing those webhook requests.
        events: Event types to forward to the webhook (all when omitted).
        event_bus: Object with ``emit``/``publish`` that receives events.
        storage: Reserved; stored on the config, not read by the SDK.
        guardrail_packs: Deprecated. Guardrail packs are no longer bundled.
    """
    if guardrail_packs:
        raise ValueError("guardrail_packs are no longer supported")

    with BriefcaseConfig._lock:
        config = BriefcaseConfig.get()
        if exporter is not _UNSET:
            config.exporter = exporter
            if exporter is not None:
                # Register the exit-time drain now, not lazily at the first
                # record (which may happen during interpreter shutdown).
                from briefcase._export_mixin import ensure_atexit_registered

                ensure_atexit_registered()
        if router is not _UNSET:
            config.router = router
        if webhook_url is not _UNSET:
            config.webhook_url = webhook_url
        if webhook_secret is not _UNSET:
            config.webhook_secret = webhook_secret
        if events is not _UNSET:
            config.subscribed_events = events
        if event_bus is not _UNSET:
            config.event_bus = event_bus
        if storage is not _UNSET:
            config.storage = storage

    return config
