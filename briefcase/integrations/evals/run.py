"""Framework-agnostic logger that turns evaluation cases into Briefcase
decision records.

Any harness (inspect-ai, lm-eval-harness, a hand-rolled loop) calls
``log_case()`` per case and ``finish()`` once; each case becomes an
``"eval.case"`` record and the run becomes one ``"eval.run"`` record, exported
through the same path as ``@briefcase.capture``. No evaluation framework is
imported here.

Usage:
    from briefcase.integrations.evals import EvalRun

    with EvalRun("gsm8k", model="claude-opus-5") as run:
        for case in cases:
            answer = model(case.question)
            run.log_case(case.id, inputs=case.question, outputs=answer,
                         target=case.answer, passed=answer == case.answer)
    # run.summary() -> pass_rate, per-score stats, token totals, cost
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from briefcase._export_mixin import ExportMixin
from briefcase._logging import get_logger

logger = get_logger(__name__)

_MAX_CHARS = 1000


def _numeric(value: Any) -> Optional[float]:
    """Return value as a float when it is a real number, else None.

    Guards every read off a calculator result: under the test suite
    ``briefcase._native`` is a MagicMock, so attribute access yields mocks
    rather than numbers.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class EvalRun(ExportMixin):
    """Collects evaluation cases and exports them as decision records.

    Args:
        name: Run name; becomes ``function_name`` on every record.
        exporter: Per-instance exporter. Falls back to the global
            ``briefcase.observe()`` exporter when None.
        async_capture: False by default so batch jobs that exit immediately do
            not drop records queued on daemon threads.
        run_id: Correlation id shared by every record. Defaults to a uuid4.
        model: Model name, used for the cost estimate.
        metadata: Arbitrary run-level metadata carried on the run record.
        cost_calculator: Object with ``estimate_cost(model, in_tokens,
            out_tokens)``. Defaults lazily to ``briefcase.cost.CostCalculator``.
        drift_calculator: Object with ``calculate_drift(outputs)``. Defaults
            lazily to ``briefcase.drift.DriftCalculator``.
    """

    def __init__(
        self,
        name: str,
        *,
        exporter: Any = None,
        async_capture: bool = False,
        run_id: Optional[str] = None,
        model: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        cost_calculator: Any = None,
        drift_calculator: Any = None,
    ) -> None:
        self.name = name
        self._exporter = exporter
        self.async_capture = async_capture
        self.run_id = run_id or str(uuid.uuid4())
        self.model = model
        self.metadata = dict(metadata or {})
        self._cost_calculator = cost_calculator
        self._drift_calculator = drift_calculator
        self._cases: List[Dict[str, Any]] = []
        self._started_at = datetime.now(timezone.utc)
        self._run_record: Optional[Dict[str, Any]] = None

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> "EvalRun":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Returns None, never True: the run record is emitted on the way out but
        # an exception in the body still propagates.
        self.finish()

    # -- logging -----------------------------------------------------------

    def log_case(
        self,
        case_id: str,
        *,
        inputs: Any = None,
        outputs: Any = None,
        target: Any = None,
        scores: Optional[Dict[str, Any]] = None,
        passed: Optional[bool] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        tags: Optional[Sequence[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record one evaluation case and export it as an ``"eval.case"``."""
        if self._run_record is not None:
            raise RuntimeError(
                f"EvalRun {self.name!r} is already finished; log cases before finish()"
            )

        record: Dict[str, Any] = {
            "decision_id": str(uuid.uuid4()),
            "decision_type": "eval.case",
            "function_name": self.name,
            "run_id": self.run_id,
            "case_id": case_id,
            "model": self.model,
            "inputs": {} if inputs is None else {"inputs": repr(inputs)[:_MAX_CHARS]},
            "outputs": {} if outputs is None else {"result": repr(outputs)[:_MAX_CHARS]},
            "scores": dict(scores or {}),
            "passed": passed,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "tags": list(tags or []),
            "metadata": dict(metadata or {}),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        if target is not None:
            record["target"] = repr(target)[:_MAX_CHARS]

        self._cases.append({
            "passed": passed,
            "scores": dict(scores or {}),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "output_text": outputs if isinstance(outputs, str) else None,
        })
        self._trigger_export(record)
        return record

    def ingest(self, cases: Iterable[Dict[str, Any]]) -> int:
        """Log a sequence of normalized case dicts (as produced by the parsers).

        Returns the number of cases logged.
        """
        count = 0
        for index, case in enumerate(cases):
            case_id = case.get("case_id")
            self.log_case(
                f"case-{index}" if case_id is None else case_id,
                inputs=case.get("inputs"),
                outputs=case.get("outputs"),
                target=case.get("target"),
                scores=case.get("scores"),
                passed=case.get("passed"),
                input_tokens=case.get("input_tokens"),
                output_tokens=case.get("output_tokens"),
                tags=case.get("tags"),
                metadata=case.get("metadata"),
            )
            count += 1
        return count

    # -- aggregation -------------------------------------------------------

    def summary(self, *, include_drift: bool = False) -> Dict[str, Any]:
        """Aggregate the logged cases into pass rate, score stats, cost, drift."""
        flags = [c["passed"] for c in self._cases if c["passed"] is not None]
        passed = sum(1 for f in flags if f)
        input_tokens = sum(c["input_tokens"] or 0 for c in self._cases)
        output_tokens = sum(c["output_tokens"] or 0 for c in self._cases)

        return {
            "run_id": self.run_id,
            "name": self.name,
            "model": self.model,
            "started_at": self._started_at.isoformat(),
            "total_cases": len(self._cases),
            "passed": passed,
            "failed": len(flags) - passed,
            "pass_rate": (passed / len(flags)) if flags else None,
            "scores": self._score_stats(),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost": self._estimate_cost(input_tokens, output_tokens),
            "drift": self._estimate_drift() if include_drift else None,
            "metadata": dict(self.metadata),
        }

    def finish(self, *, include_drift: bool = False) -> Dict[str, Any]:
        """Emit the single ``"eval.run"`` record. Idempotent."""
        if self._run_record is not None:
            return self._run_record

        summary = self.summary(include_drift=include_drift)
        ended_at = datetime.now(timezone.utc)
        record = {
            "decision_id": str(uuid.uuid4()),
            "decision_type": "eval.run",
            "function_name": self.name,
            "run_id": self.run_id,
            "model": self.model,
            "inputs": {},
            "outputs": summary,
            "started_at": self._started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "execution_time_ms": (ended_at - self._started_at).total_seconds() * 1000,
        }
        self._run_record = record
        self._trigger_export(record)
        return record

    # -- internals ---------------------------------------------------------

    def _score_stats(self) -> Dict[str, Dict[str, float]]:
        buckets: Dict[str, List[float]] = {}
        for case in self._cases:
            for key, value in case["scores"].items():
                number = _numeric(value)
                if number is not None:
                    buckets.setdefault(key, []).append(number)
        return {
            key: {
                "mean": sum(values) / len(values),
                "min": min(values),
                "max": max(values),
                "count": len(values),
            }
            for key, values in buckets.items()
        }

    def _estimate_cost(self, input_tokens: int, output_tokens: int) -> Optional[float]:
        if not self.model or (input_tokens == 0 and output_tokens == 0):
            return None
        calculator = self._resolve(
            "_cost_calculator", "briefcase.cost", "CostCalculator"
        )
        if calculator is None:
            return None
        try:
            estimate = calculator.estimate_cost(self.model, input_tokens, output_tokens)
            return _numeric(getattr(estimate, "total_cost", None))
        except Exception:
            logger.debug("Cost estimate unavailable for run %s", self.run_id, exc_info=True)
            return None

    def _estimate_drift(self) -> Optional[Dict[str, float]]:
        outputs = [c["output_text"] for c in self._cases if c["output_text"] is not None]
        if len(outputs) < 2:
            return None
        calculator = self._resolve(
            "_drift_calculator", "briefcase.drift", "DriftCalculator"
        )
        if calculator is None:
            return None
        values: Dict[str, float] = {}
        try:
            metrics = calculator.calculate_drift(outputs)
            for key in ("drift_score", "consistency_score", "agreement_rate"):
                number = _numeric(getattr(metrics, key, None))
                if number is None:
                    return None
                values[key] = number
        except Exception:
            logger.debug("Drift metrics unavailable for run %s", self.run_id, exc_info=True)
            return None
        return values

    def _resolve(self, attr: str, module: str, class_name: str) -> Any:
        """Return the injected calculator, else lazily construct the native one."""
        existing = getattr(self, attr)
        if existing is not None:
            return existing
        try:
            import importlib

            instance = getattr(importlib.import_module(module), class_name)()
        except Exception:
            logger.debug("Could not construct %s.%s", module, class_name, exc_info=True)
            return None
        setattr(self, attr, instance)
        return instance


__all__ = ["EvalRun"]
