"""Kafka transport for Briefcase events.

Publishes :class:`briefcase.events.types.BriefcaseEvent` records as JSON
messages to a Kafka topic, keyed by the event's idempotency key.

Install
-------
Requires ``confluent-kafka``:
``pip install briefcase-ai[kafka]`` or ``pip install confluent-kafka``.
The import is lazy, so this module loads without the package installed
and fails with a clear error on first publish.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional

from briefcase._logging import get_logger
from briefcase.events.types import BriefcaseEvent

logger = get_logger(__name__)


_KAFKA_INSTALL_HINT = (
    "confluent-kafka is required for KafkaPublisher. Install it with: "
    "pip install 'briefcase-ai[kafka]' or pip install confluent-kafka"
)


class KafkaPublisher:
    """Publish BriefcaseEvents as JSON messages to a Kafka topic.

    Args:
        brokers: List of Kafka broker addresses (e.g. ["kafka:9092"]).
        topic: Target Kafka topic name.
        config: Extra librdkafka producer settings merged over
            ``bootstrap.servers``, e.g. SASL/TLS::

                KafkaPublisher(brokers, topic, config={
                    "security.protocol": "SASL_SSL",
                    "sasl.mechanisms": "PLAIN",
                    "sasl.username": "...",
                    "sasl.password": "...",
                })

    Delivery is asynchronous: ``publish()`` returning True means the message
    was queued. Broker-side delivery failures are logged at WARNING by a
    delivery callback and counted in ``delivery_failures``. Call
    :meth:`flush` (or :meth:`close`) before exit so queued messages are sent.
    """

    def __init__(
        self,
        brokers: List[str],
        topic: str,
        config: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self._brokers = brokers
        self._topic = topic
        self._config: Dict[str, Any] = dict(config or {})
        self._producer: Any = None  # lazily initialised on first publish
        self.delivery_failures = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_producer(self) -> Any:
        """Return (or lazily create) the confluent-kafka Producer."""
        if self._producer is None:
            try:
                from confluent_kafka import Producer
            except ImportError as exc:
                raise ImportError(_KAFKA_INSTALL_HINT) from exc
            self._producer = Producer(
                {"bootstrap.servers": ",".join(self._brokers), **self._config}
            )
        return self._producer

    def _on_delivery(self, err: Any, msg: Any) -> None:
        """confluent-kafka delivery report; ``err`` is None on success."""
        if err is None:
            return
        self.delivery_failures += 1
        logger.warning(
            "Kafka delivery to topic %s failed: %s", self._topic, err
        )

    @staticmethod
    def _serialize(event: BriefcaseEvent) -> bytes:
        return json.dumps(
            {
                "event_type": event.event_type,
                "decision_id": event.decision_id,
                "timestamp": event.timestamp.isoformat(),
                "idempotency_key": event.idempotency_key,
                "payload": event.payload,
            },
            default=str,
        ).encode()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def publish(self, event: BriefcaseEvent) -> bool:
        """Produce *event* to the configured Kafka topic.

        Returns True when the message was queued. Produce errors are
        logged at WARNING and reported as False; a missing client library
        still raises ImportError. Delivery itself is confirmed (or its
        failure logged) by the delivery callback during a later ``publish``,
        ``flush`` or ``close``.
        """
        try:
            producer = self._get_producer()
            value = self._serialize(event)
            producer.produce(
                topic=self._topic,
                key=event.idempotency_key.encode(),
                value=value,
                on_delivery=self._on_delivery,
            )
            producer.poll(0)  # serve delivery callbacks without blocking
            return True
        except ImportError:
            raise
        except Exception as exc:
            logger.warning(
                "Kafka publish to topic %s failed: %s: %s",
                self._topic, type(exc).__name__, exc,
            )
            return False

    def flush(self, timeout: float = 10.0) -> int:
        """Block until queued messages are delivered or ``timeout`` seconds
        pass. Returns the number of messages still undelivered (0 when the
        queue drained or no producer was ever created)."""
        if self._producer is None:
            return 0
        remaining = self._producer.flush(timeout)
        remaining = int(remaining) if isinstance(remaining, int) else 0
        if remaining:
            logger.warning(
                "Kafka flush timed out with %d message(s) undelivered to topic %s",
                remaining, self._topic,
            )
        return remaining

    def close(self, timeout: float = 10.0) -> int:
        """Flush, then drop the producer. The publisher stays usable: the
        next ``publish`` creates a new producer. Returns the number of
        messages left undelivered."""
        try:
            return self.flush(timeout)
        finally:
            self._producer = None
