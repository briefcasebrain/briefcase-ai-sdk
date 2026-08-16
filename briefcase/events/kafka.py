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
from typing import Any, List

from briefcase.events.types import BriefcaseEvent


_KAFKA_INSTALL_HINT = (
    "confluent-kafka is required for KafkaPublisher. Install it with: "
    "pip install 'briefcase-ai[kafka]' or pip install confluent-kafka"
)


class KafkaPublisher:
    """Publish BriefcaseEvents as JSON messages to a Kafka topic.

    Args:
        brokers: List of Kafka broker addresses (e.g. ["kafka:9092"]).
        topic: Target Kafka topic name.
    """

    def __init__(self, brokers: List[str], topic: str) -> None:
        self._brokers = brokers
        self._topic = topic
        self._producer: Any = None  # lazily initialised on first publish

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
                {"bootstrap.servers": ",".join(self._brokers)}
            )
        return self._producer

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

        Returns True on success. Produce errors are swallowed and reported
        as False; a missing client library still raises ImportError.
        """
        try:
            producer = self._get_producer()
            value = self._serialize(event)
            producer.produce(
                topic=self._topic,
                key=event.idempotency_key.encode(),
                value=value,
            )
            producer.poll(0)  # non-blocking flush of the internal queue
            return True
        except ImportError:
            raise
        except Exception:
            return False
