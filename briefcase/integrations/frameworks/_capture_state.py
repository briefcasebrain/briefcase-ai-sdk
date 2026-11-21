"""Shared record-keeping for the framework handlers.

Every handler under ``briefcase.integrations.frameworks`` keeps completed
records for ``get_records()`` / ``get_decisions()`` and, where the framework
reports start and end separately, partial records for runs still in flight.
This module gives them one implementation of three things:

- :class:`CaptureOptions`: the content controls ``@briefcase.capture``
  exposes (``capture_content`` and ``redact``), applied to the ``inputs``,
  ``outputs`` and ``error`` of a record before it is stored or exported.
- :func:`bounded_records`: a ring buffer, so a long-lived handler keeps the
  most recent ``max_records`` records instead of growing without limit.
- :class:`InflightStore`: a mapping with a size cap and a TTL, so a start
  callback whose end callback never arrives cannot leak its entry forever.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict, deque
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from briefcase._logging import get_logger

logger = get_logger(__name__)

# Mirrors briefcase.decorators.CAPTURE_CONTENT_MODES.
CAPTURE_CONTENT_MODES = ("full", "hash", "none")

DEFAULT_MAX_RECORDS = 10_000
DEFAULT_MAX_INFLIGHT = 10_000
DEFAULT_INFLIGHT_TTL_SECONDS = 3600.0

REDACTION_FAILED = "<redaction-failed>"
OMITTED = "<omitted>"


_CONTENT_KEYS = ("inputs", "outputs", "input", "output")
_TEXT_KEYS = ("error", "pageindex.tree.path")
_NESTED_KEYS = ("child_spans", "spans")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _stable_text(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except Exception:
        return str(value)


class CaptureOptions:
    """Content controls for handler records, matching ``@capture``.

    Args:
        capture_content: ``"full"`` (default) keeps content, optionally
            rewritten by ``redact``. ``"hash"`` replaces ``inputs`` and
            ``outputs`` with a SHA-256 digest and a character count.
            ``"none"`` drops them.
        redact: Optional ``str -> str`` hook applied to every string inside
            ``inputs`` / ``outputs`` and to the ``error`` text in ``"full"``
            mode. Fails closed: if it raises, the string is replaced with
            ``"<redaction-failed>"``.
    """

    def __init__(
        self,
        capture_content: str = "full",
        redact: Optional[Callable[[str], str]] = None,
    ) -> None:
        if capture_content not in CAPTURE_CONTENT_MODES:
            raise ValueError(
                f"capture_content must be one of {CAPTURE_CONTENT_MODES}, "
                f"got {capture_content!r}"
            )
        if redact is not None and not callable(redact):
            raise TypeError("redact must be a callable str -> str")
        self.capture_content = capture_content
        self.redact = redact

    @property
    def is_passthrough(self) -> bool:
        return self.capture_content == "full" and self.redact is None

    def content(self, value: Any) -> Any:
        """Apply the options to an ``inputs`` / ``outputs`` value."""
        if self.is_passthrough:
            return value
        if self.capture_content == "none":
            return {}
        if self.capture_content == "hash":
            if value in (None, {}, [], ""):
                return {}
            text = _stable_text(value)
            return {"sha256": _sha256(text), "chars": len(text)}
        return self._redact_value(value)

    def error(self, text: Optional[str]) -> Optional[str]:
        """Apply the options to an error message."""
        if text is None or self.is_passthrough:
            return text
        if self.capture_content == "none":
            return OMITTED
        if self.capture_content == "hash":
            return f"sha256:{_sha256(text)}"
        return self._redact_text(text)

    def apply(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Apply the options to a record dict in place and return it.

        Rewrites the content keys (``inputs``, ``outputs``, ``input``,
        ``output``), the text keys (``error``, ``pageindex.tree.path``) and
        recurses into nested span lists (``child_spans``, ``spans``).
        """
        if self.is_passthrough:
            return record
        for key in _CONTENT_KEYS:
            if key in record:
                record[key] = self.content(record[key])
        for key in _TEXT_KEYS:
            if record.get(key) is not None:
                record[key] = self.error(str(record[key]))
        for key in _NESTED_KEYS:
            children = record.get(key)
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        self.apply(child)
        return record

    def _redact_text(self, text: str) -> str:
        if self.redact is None:
            return text
        try:
            result = self.redact(text)
        except Exception:
            return REDACTION_FAILED
        return result if isinstance(result, str) else REDACTION_FAILED

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._redact_text(value)
        if isinstance(value, dict):
            return {k: self._redact_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._redact_value(v) for v in value]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        # An arbitrary object may carry content in its repr.
        return self._redact_text(str(value))


class RecordBuffer(deque):
    """Ring buffer of completed records that scrubs dict records on append.

    ``append`` applies ``options`` to a dict record *in place* before
    keeping it, so the same (already scrubbed) dict can then be exported:
    raw content is neither retained nor sent.
    """

    def __init__(
        self,
        max_records: Optional[int] = DEFAULT_MAX_RECORDS,
        options: Optional[CaptureOptions] = None,
    ) -> None:
        if max_records is not None and max_records < 0:
            raise ValueError("max_records must be >= 0 or None")
        super().__init__(maxlen=max_records)
        self._options = options

    def append(self, record: Any) -> None:
        if self._options is not None and isinstance(record, dict):
            try:
                self._options.apply(record)
            except Exception as exc:
                # Fail closed: never keep or export content we could not scrub.
                logger.debug("Failed to apply capture options: %s", exc)
                for key in _CONTENT_KEYS:
                    if key in record:
                        record[key] = {}
                for key in _TEXT_KEYS:
                    if record.get(key) is not None:
                        record[key] = OMITTED
                for key in _NESTED_KEYS:
                    record.pop(key, None)
        super().append(record)


def bounded_records(
    max_records: Optional[int] = DEFAULT_MAX_RECORDS,
    options: Optional[CaptureOptions] = None,
) -> "RecordBuffer":
    """Return the ring buffer a handler keeps completed records in.

    ``max_records=None`` keeps everything (the caller owns the growth);
    ``0`` keeps nothing, for export-only handlers. With ``options``, dict
    records are scrubbed in place as they are appended.
    """
    return RecordBuffer(max_records, options)


class InflightStore:
    """Mapping of in-flight runs with a size cap and a TTL.

    Entries older than ``ttl_seconds`` are dropped on the next insert, and
    when the store is full the oldest entry makes room. Each dropped entry
    is passed to ``on_evict(key, value, reason)`` (``reason`` is ``"ttl"``
    or ``"size"``) so the owner can record it as abandoned rather than lose
    it silently. Thread-safe.
    """

    def __init__(
        self,
        max_size: Optional[int] = DEFAULT_MAX_INFLIGHT,
        ttl_seconds: Optional[float] = DEFAULT_INFLIGHT_TTL_SECONDS,
        on_evict: Optional[Callable[[Any, Any, str], None]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_size is not None and max_size < 1:
            raise ValueError("max_inflight must be >= 1 or None")
        if ttl_seconds is not None and ttl_seconds <= 0:
            raise ValueError("inflight_ttl_seconds must be > 0 or None")
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._on_evict = on_evict
        self._clock = clock
        self._lock = threading.RLock()
        self._items: "OrderedDict[Any, Tuple[float, Any]]" = OrderedDict()
        self.evicted = 0

    def __setitem__(self, key: Any, value: Any) -> None:
        evicted: List[Tuple[Any, Any, str]] = []
        with self._lock:
            now = self._clock()
            self._items.pop(key, None)
            if self.ttl_seconds is not None:
                cutoff = now - self.ttl_seconds
                while self._items:
                    oldest_key, (stamp, oldest) = next(iter(self._items.items()))
                    if stamp > cutoff:
                        break
                    del self._items[oldest_key]
                    evicted.append((oldest_key, oldest, "ttl"))
            if self.max_size is not None:
                while len(self._items) >= self.max_size:
                    oldest_key, (_, oldest) = self._items.popitem(last=False)
                    evicted.append((oldest_key, oldest, "size"))
            self._items[key] = (now, value)
            self.evicted += len(evicted)
        for item in evicted:
            self._notify(*item)

    def _notify(self, key: Any, value: Any, reason: str) -> None:
        logger.debug("Dropping in-flight entry %r (%s)", key, reason)
        if self._on_evict is None:
            return
        try:
            self._on_evict(key, value, reason)
        except Exception as exc:
            logger.debug("in-flight eviction callback failed: %s", exc)

    def __getitem__(self, key: Any) -> Any:
        with self._lock:
            return self._items[key][1]

    def __delitem__(self, key: Any) -> None:
        with self._lock:
            del self._items[key]

    def __contains__(self, key: Any) -> bool:
        with self._lock:
            return key in self._items

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __iter__(self) -> Iterator[Any]:
        with self._lock:
            return iter(list(self._items))

    def __bool__(self) -> bool:
        return len(self) > 0

    def __eq__(self, other: object) -> bool:
        # Compares like the plain dict it replaces.
        if isinstance(other, InflightStore):
            other = other.as_dict()
        if isinstance(other, dict):
            return self.as_dict() == other
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]

    def as_dict(self) -> Dict[Any, Any]:
        with self._lock:
            return {key: value for key, (_, value) in self._items.items()}

    def keys(self) -> List[Any]:
        return list(self)

    def get(self, key: Any, default: Any = None) -> Any:
        with self._lock:
            item = self._items.get(key)
            return default if item is None else item[1]

    def pop(self, key: Any, default: Any = None) -> Any:
        with self._lock:
            item = self._items.pop(key, None)
            return default if item is None else item[1]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
