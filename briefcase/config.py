from typing import Any, List, Optional


class BriefcaseConfig:
    """Central configuration for Briefcase SDK extensions."""

    _instance: Optional["BriefcaseConfig"] = None

    def __init__(self):
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
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    @property
    def guardrail_registry(self) -> Any:
        """Expose the global guardrail registry from framework.py."""
        from briefcase.guardrails.framework import _default_registry
        return _default_registry


def setup(
    exporter=None,
    router=None,
    webhook_url=None,
    webhook_secret=None,
    events=None,
    event_bus=None,
    storage=None,
    guardrail_packs: Optional[List[str]] = None,
) -> BriefcaseConfig:
    """Configure Briefcase SDK extensions.

    Does not interfere with briefcase.init() for the Rust runtime.

    Args:
        guardrail_packs: Deprecated. Guardrail packs are no longer bundled.
    """
    config = BriefcaseConfig.get()
    if exporter is not None:
        config.exporter = exporter
    if router is not None:
        config.router = router
    if webhook_url is not None:
        config.webhook_url = webhook_url
    if webhook_secret is not None:
        config.webhook_secret = webhook_secret
    if events is not None:
        config.subscribed_events = events
    if event_bus is not None:
        config.event_bus = event_bus
    if storage is not None:
        config.storage = storage

    if guardrail_packs:
        raise ValueError("guardrail_packs are no longer supported")

    return config
