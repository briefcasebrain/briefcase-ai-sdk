"""Tests for briefcase/events/kafka.py (KafkaPublisher).

The confluent-kafka client is stubbed via sys.modules injection or by
injecting a mock producer directly; no broker is needed.
"""

import json
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from briefcase.events.types import BriefcaseEvent
from briefcase.events.kafka import KafkaPublisher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event() -> BriefcaseEvent:
    return BriefcaseEvent(
        event_type="decision.low_confidence",
        decision_id="dec-kafka-001",
        payload={"confidence": 0.3},
        idempotency_key="idem-kafka-xyz",
    )


def _make_publisher_with_mock_producer():
    """Return (KafkaPublisher, mock_producer) with the producer injected."""
    mock_producer = MagicMock()
    mock_producer.produce = MagicMock()
    mock_producer.poll = MagicMock()

    publisher = KafkaPublisher(brokers=["kafka:9092"], topic="briefcase-events")
    publisher._producer = mock_producer
    return publisher, mock_producer


class _FakeProducer:
    """Stand-in for confluent_kafka.Producer."""

    def __init__(self, conf):
        self.conf = conf

    def produce(self, **kwargs):
        pass

    def poll(self, timeout):
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_kafka_publishes_event():
    """publish() calls producer.produce() with the configured topic."""
    publisher, mock_producer = _make_publisher_with_mock_producer()
    event = _make_event()

    result = publisher.publish(event)

    assert result is True
    mock_producer.produce.assert_called_once()
    call_kwargs = mock_producer.produce.call_args[1]
    assert call_kwargs["topic"] == "briefcase-events"


def test_kafka_message_key():
    """The message key equals the event's idempotency_key (encoded)."""
    publisher, mock_producer = _make_publisher_with_mock_producer()
    event = _make_event()

    publisher.publish(event)

    call_kwargs = mock_producer.produce.call_args[1]
    assert call_kwargs["key"] == event.idempotency_key.encode()


def test_kafka_payload_valid_json():
    """The message value is valid JSON containing all BriefcaseEvent fields."""
    publisher, mock_producer = _make_publisher_with_mock_producer()
    event = _make_event()

    publisher.publish(event)

    call_kwargs = mock_producer.produce.call_args[1]
    payload = json.loads(call_kwargs["value"])
    assert payload["event_type"] == event.event_type
    assert payload["decision_id"] == event.decision_id
    assert payload["idempotency_key"] == event.idempotency_key
    assert "timestamp" in payload
    assert "payload" in payload


def test_kafka_producer_error_silent():
    """If produce() raises, publish() returns False without propagating."""
    publisher, mock_producer = _make_publisher_with_mock_producer()
    mock_producer.produce.side_effect = RuntimeError("broker unavailable")

    event = _make_event()
    result = publisher.publish(event)

    assert result is False


def test_kafka_missing_dependency():
    """ImportError with install hint when confluent-kafka is not installed."""
    with patch.dict(sys.modules, {"confluent_kafka": None}):
        publisher = KafkaPublisher(brokers=["kafka:9092"], topic="t")
        publisher._producer = None  # force the lazy init path

        with pytest.raises(ImportError, match="pip install"):
            publisher.publish(_make_event())


def test_kafka_producer_lazy_init(monkeypatch):
    """_get_producer creates a Producer with the broker config on first call."""
    stub = ModuleType("confluent_kafka")
    stub.Producer = _FakeProducer
    monkeypatch.setitem(sys.modules, "confluent_kafka", stub)

    publisher = KafkaPublisher(brokers=["kafka:9092", "kafka2:9092"], topic="t")
    producer = publisher._get_producer()
    assert isinstance(producer, _FakeProducer)
    assert producer.conf == {"bootstrap.servers": "kafka:9092,kafka2:9092"}
    # Second call returns the cached producer.
    assert publisher._get_producer() is producer


# ---------------------------------------------------------------------------
# Extra config, delivery callback, flush/close
# ---------------------------------------------------------------------------

def test_kafka_extra_config_is_merged(monkeypatch):
    stub = ModuleType("confluent_kafka")
    stub.Producer = _FakeProducer
    monkeypatch.setitem(sys.modules, "confluent_kafka", stub)

    publisher = KafkaPublisher(
        brokers=["kafka:9093"], topic="t",
        config={"security.protocol": "SASL_SSL", "sasl.mechanisms": "PLAIN",
                "sasl.username": "u", "sasl.password": "p"},
    )
    assert publisher._get_producer().conf == {
        "bootstrap.servers": "kafka:9093",
        "security.protocol": "SASL_SSL",
        "sasl.mechanisms": "PLAIN",
        "sasl.username": "u",
        "sasl.password": "p",
    }


def test_kafka_delivery_callback_logs_failures(caplog):
    import logging

    publisher, mock_producer = _make_publisher_with_mock_producer()
    publisher.publish(_make_event())
    callback = mock_producer.produce.call_args[1]["on_delivery"]

    with caplog.at_level(logging.WARNING, logger="briefcase.events.kafka"):
        callback(None, MagicMock())  # success: silent
        assert publisher.delivery_failures == 0
        callback("Broker: Message timed out", MagicMock())
    assert publisher.delivery_failures == 1
    assert any("Message timed out" in r.getMessage() for r in caplog.records)


def test_kafka_produce_error_is_logged(caplog):
    import logging

    publisher, mock_producer = _make_publisher_with_mock_producer()
    mock_producer.produce.side_effect = RuntimeError("broker unavailable")
    with caplog.at_level(logging.WARNING, logger="briefcase.events.kafka"):
        assert publisher.publish(_make_event()) is False
    assert any("broker unavailable" in r.getMessage() for r in caplog.records)


def test_kafka_flush_and_close():
    publisher, mock_producer = _make_publisher_with_mock_producer()
    mock_producer.flush = MagicMock(return_value=0)
    assert publisher.flush(timeout=2.5) == 0
    mock_producer.flush.assert_called_once_with(2.5)

    mock_producer.flush = MagicMock(return_value=3)
    assert publisher.close(timeout=1.0) == 3
    assert publisher._producer is None

    # No producer was ever created: nothing to do, nothing raised.
    idle = KafkaPublisher(brokers=["kafka:9092"], topic="t")
    assert idle.flush() == 0 and idle.close() == 0
