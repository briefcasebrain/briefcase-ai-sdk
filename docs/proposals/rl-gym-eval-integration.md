# Integrate briefcase-ai-sdk with RL gym environments and evaluation frameworks

Status: implemented. 91 new tests in `tests/test_evals_run.py`, `tests/test_evals_parsers.py`,
`tests/test_gym_adapter.py`, `tests/test_gym_capture.py`.

## Context

The guardrails framework (`briefcase/guardrails/framework.py`) deliberately mirrors the Gymnasium API (GuardrailEnv ~ gym.Env, GuardrailWrapper ~ gym.Wrapper, PolicySpace ~ spaces, registry ~ gym.register) but nothing actually connects to `gymnasium`, no concrete env ships, and no evaluation harness can feed results into the capture/export pipeline. This plan adds both bridges:

1. Both directions of gym integration: a `gymnasium.Env` adapter over any `GuardrailEnv`, and a `gymnasium.Wrapper` that captures RL episodes as Briefcase decision records.
2. A dep-free eval bridge: a generic `EvalRun` logger any harness can call, plus stdlib-only parsers for inspect-ai and lm-eval-harness log files (frameworks are never imported).
3. Gym adapter reward is task-based via the existing `GuardrailTask` (reward 1.0 when the effect matches `expected_effect`), with an adversarial mode.

All work follows repo rules: TDD (watch red first), present-tense comments, offline-by-default examples.

## Key design decisions

- Two packages with different dep footprints: `briefcase/integrations/gym/` (extra `gym = ["gymnasium>=0.29"]`) and `briefcase/integrations/evals/` (extra `evals = []`, stdlib only). Extra name matches module name so the lakefs-style ImportError message satisfies `scripts/check_imports.py::_is_missing_extra`.
- Action space `Discrete(1 + len(injections))`: action 0 submits the sampled task's clean request; action i applies `injections[i-1].inject(...)`. Arbitrary request-crafting action spaces would make `expected_effect` (and therefore reward) meaningless.
- Single-step bandit episodes: `reset()` samples a `GuardrailTask` via `self.np_random`, `step()` evaluates once, `terminated=True`. `GuardrailEnv.evaluate()` is single-shot and side-effect-free; multi-step episodes would fabricate state.
- Observation is a fixed-shape `spaces.Dict`: Discrete vocab indices for agent/action/resource (vocab = PolicySpace values unioned with task values), Box context vector from `SpaceBound` bounds, plus `last_effect` Discrete(3) and `last_eval_time_ms`.
- No concrete GuardrailEnv added to the library (protocol-first design; `CountingEnv` test precedent). Example-local `AllowlistEnv` lives in `examples/rl_gym/`; test fakes live in the test files.
- No import-time `gymnasium.register()`; opt-in `register_with_gymnasium()` helper. The adapter is not registered in the briefcase GuardrailRegistry (it consumes a GuardrailEnv, it is not one).
- `EvalRun` defaults `async_capture=False` (batch jobs exit immediately; daemon-thread export would drop tail records). The RL capture wrapper keeps `async_capture=True` (hot path).
- Drift/cost in `EvalRun.summary()` use DI constructor args with lazy native fallback, guarded by `try/except` plus numeric `isinstance` validation on every read value, because `tests/` mocks `briefcase._native` with MagicMock (`tests/mock_core.py`).
- New `decision_type` values: `"rl.step"`, `"rl.episode"`, `"eval.case"`, `"eval.run"`. All records flow through `ExportMixin._trigger_export` (`briefcase/_export_mixin.py:52`), so they honor per-instance exporters, the `briefcase.observe()` global fallback, and never raise.
- Tests live flat in `tests/` matching `tests/test_lakefs_client.py` precedent. Gym test files do a bare `import gymnasium` at module level; `tests/conftest.py::pytest_ignore_collect` auto-skips them when the extra is absent.

## Reused existing code

- `ExportMixin` (`briefcase/_export_mixin.py:27`) for all record export; contract: set `self._exporter` and `self.async_capture`, call `_trigger_export(record)`.
- Record shape and 1000-char repr truncation conventions from `capture()` (`briefcase/decorators.py:39,152-196`).
- `GuardrailTask.utility()`, `GuardrailInjection.inject()/security()`, `PolicySpace`/`SpaceBound`, `Effect`, `Explanation.to_narrative()` (`briefcase/guardrails/framework.py:666-705,93-142`; `_types.py`).
- Extras-gated package template: `briefcase/integrations/lakefs/__init__.py:37-53` (exact ImportError message shape).
- Exporters: `MemoryExporter`, `JSONLFileExporter`, `observe()` (`briefcase/exporters/`, `briefcase/_observe.py:29`).

## Phase A: briefcase/integrations/evals/ (dep-free, first; runs under mocked-native suite)

### A1. EvalRun logger (TDD)
New `tests/test_evals_run.py`, then `briefcase/integrations/evals/run.py`:

```python
class EvalRun(ExportMixin):
    def __init__(self, name, *, exporter=None, async_capture=False, run_id=None,
                 model=None, metadata=None, cost_calculator=None, drift_calculator=None)
    def __enter__ / __exit__            # exit calls finish(); never suppresses
    def log_case(self, case_id, *, inputs=None, outputs=None, target=None, scores=None,
                 passed=None, input_tokens=None, output_tokens=None, tags=None, metadata=None) -> dict
    def ingest(self, cases) -> int      # normalized case dicts (from parsers)
    def summary(self, *, include_drift=False) -> dict   # pass_rate, per-score mean/min/max/count, cost, drift
    def finish(self, *, include_drift=False) -> dict    # emits one "eval.run" record, idempotent
```

Tests (watch fail first): case record shape and export; context-manager run record with totals; pass_rate/score stats; empty-scores behavior; cost via injected fake calculator; cost degrades to None on raising/MagicMock calculators; drift same (only attempted with >= 2 string outputs); ingest; global-exporter fallback via `briefcase.observe("memory")`.

### A2. Parsers (TDD)
New `tests/test_evals_parsers.py` (fixtures under tmp_path), then `briefcase/integrations/evals/parsers.py`:

```python
@dataclass
class ParsedEvalLog: source; name; model; cases; metrics

def from_inspect_log(path) -> ParsedEvalLog          # .json or .eval zip (zipfile.is_zipfile)
def from_lm_eval_results(results_path, samples_path=None, *, task=None) -> ParsedEvalLog
def replay(parsed, *, exporter=None, name=None, async_capture=False) -> EvalRun
```

Parsing is best-effort `.get()` chains, stdlib only. Inspect letter grades map C/I/P/N to 1.0/0.0/0.5/0.0; output pulled from `output.choices[0].message.content`; chat-message inputs flattened. lm-eval aggregate metrics normalized (strip `",filter"` suffixes, drop stderr/alias); samples jsonl produces cases keyed `task/doc_id`. Unrecognized shapes raise ValueError naming the expected format. Tests cover both formats, the zip form, grade mapping, rejection errors, and a replay round trip (N `eval.case` + 1 `eval.run` in a MemoryExporter).

### A3. Package init
`briefcase/integrations/evals/__init__.py`: plain imports (dep-free, vcs-style), docstring with numbered usage patterns, re-export `EvalRun`, `ParsedEvalLog`, `from_inspect_log`, `from_lm_eval_results`, `replay`. Update `briefcase/integrations/__init__.py` docstring list.

## Phase B: briefcase/integrations/gym/ (needs the gym extra)

### B0. Extras plumbing
`pyproject.toml`: add `gym = ["gymnasium>=0.29"]` and `evals = []`; add both to the `all` union (line 44). Local install: `pip install "gymnasium>=0.29"` into the venv (3.11, so gymnasium 1.x resolves; the `>=0.29` floor keeps `requires-python >=3.9` resolvable).

### B1. GuardrailGymEnv adapter (TDD)
New `tests/test_gym_adapter.py` (module-level `import gymnasium`; in-file fakes `AllowAllEnv`, `ContextFlagEnv(BaseGuardrailEnv)`, `SetFlagInjection`), then `briefcase/integrations/gym/adapter.py`:

```python
class GuardrailGymEnv(gymnasium.Env):
    metadata = {"render_modes": ["human", "ansi"]}
    def __init__(self, guardrail, tasks, injections=(), reward_mode="utility", render_mode=None)
    def reset(self, *, seed=None, options=None)   # options={"task_index": i} override
    def step(self, action)                        # -> obs, reward, True, False, info
    def render(self)                              # "ansi": explain(result).to_narrative()
    def close(self)                               # delegates to guardrail.close()

def register_with_gymnasium(env_id="briefcase/GuardrailEval-v0", **env_kwargs) -> None
```

`info` carries task_id, injection_id, effect, expected_effect, utility, security, reason, eval_time_ms, and the raw EvalResult. `reward_mode="adversarial"` returns `1.0 - utility`. RuntimeError on step-before-reset and double-step. Empty tasks or bad reward_mode raise ValueError.

Tests: isinstance/space shapes, seeded reset determinism, task_index override, clean-action utility reward, injection action flips effect and sets security, adversarial reward, step-before-reset errors, ansi render, close delegation, `register_with_gymnasium` + `gymnasium.make`, and `gymnasium.utils.env_checker.check_env(env, skip_render_check=True)`.

### B2. EpisodeCaptureWrapper (TDD)
New `tests/test_gym_capture.py` (in-file `TinyEnv(gymnasium.Env)`), then `briefcase/integrations/gym/capture.py`:

```python
class EpisodeCaptureWrapper(gymnasium.Wrapper, ExportMixin):
    def __init__(self, env, *, exporter=None, async_capture=True, capture_steps=True,
                 max_obs_chars=1000, max_action_chars=1000)

def capture_episodes(env, *, exporter=None, **kwargs) -> EpisodeCaptureWrapper
```

uuid4 `episode_id` per reset (finalizing any open episode as `completed=False`); per step a `"rl.step"` record (episode_id, step_index, action/obs reprs truncated, reward, terminated, truncated, info_keys, execution_time_ms); on terminated/truncated a `"rl.episode"` record (env_id from `env.spec.id` or class name, total_steps, episode_return, completed); `close()` finalizes then delegates. Tests: step record shape, episode record on termination, mid-episode reset partial record, capture_steps=False, repr truncation, close, helper, global-exporter fallback, and end-to-end over `GuardrailGymEnv`.

### B3. Package init
`briefcase/integrations/gym/__init__.py`: lakefs-style try/except ImportError guard with the exact message `"briefcase.integrations.gym requires the 'gym' extra.\nInstall it with: pip install briefcase-ai[gym]"`; docstring with numbered patterns.

## Phase C: examples, docs, packaging, changelog

- `examples/rl_gym/main.py` + `README.md`: example-local `AllowlistEnv(BaseGuardrailEnv)`; baseline `GuardrailBenchmark.run` summary; seeded random-policy loop over `GuardrailGymEnv` with render narratives; `EpisodeCaptureWrapper` + `briefcase.observe("memory")` printing captured records. Friendly SystemExit hint if gymnasium missing.
- `examples/eval_runs/main.py` + `README.md`: stdlib only; EvalRun context manager; write minimal inspect-style JSON and lm-eval results to tempfiles and replay both.
- `examples/README.md`: add both sections to the index.
- `scripts/check_imports.py`: `briefcase.integrations.evals` to REQUIRED, `briefcase.integrations.gym` to OPTIONAL.
- `README.md`: extras-table rows for `gym` and `evals`; update the sentence listing which extras pull third-party deps to include `gym`.
- `CHANGELOG.md`: extend the current unreleased entry with an `### Added` section covering all of the above.

## Verification

```bash
# TDD: run each new test file before its implementation exists and watch it fail.
python -m pytest tests/test_evals_run.py tests/test_evals_parsers.py -q     # dep-free suite
python -m pytest tests/test_gym_adapter.py tests/test_gym_capture.py -q    # before extra: auto-skipped
pip install "gymnasium>=0.29"
python -m pytest tests/test_gym_adapter.py tests/test_gym_capture.py -q    # green after B1/B2
python -m pytest tests/ -q                                                  # full mocked-native regression
python -m pytest bindings/python/tests/ -q                                  # real-native suite unaffected
python examples/rl_gym/main.py && python examples/eval_runs/main.py         # offline examples run
python scripts/check_imports.py                                             # evals OK; gym OK or SKIP
grep -rn $'\xe2\x80\x94\\|\xe2\x80\x93' briefcase/integrations/gym briefcase/integrations/evals examples/rl_gym examples/eval_runs  # em/en dash scan, expect empty
```

Sequencing: A1 -> A2 -> A3 -> B0 -> B1 -> B2 -> B3 -> C -> full verification. Risks: gymnasium 1.x needs Python >= 3.10 (floor `>=0.29` keeps 3.9 installs resolvable); `check_env` dtype strictness (use float64 consistently); MagicMock-native leakage into summary (guarded and explicitly tested).
