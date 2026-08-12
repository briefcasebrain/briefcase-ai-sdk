# RL Gym

Drives a guardrail from Gymnasium and captures the rollout as Briefcase
decision records.

## Requirements

```bash
pip install briefcase-ai[gym]
```

## Run

```bash
python examples/rl_gym/main.py
```

Runs offline. No network, no API key.

## What it shows

| Pattern | API | Output |
|---------|-----|--------|
| Non-RL baseline | `GuardrailBenchmark().run(env, tasks, injections)` | utility and security scores per category |
| Policy training surface | `GuardrailGymEnv(guardrail, tasks, injections)` | one bandit episode per `reset()`/`step()` pair |
| Rollout capture | `capture_episodes(env)` | one `rl.step` record per step, one `rl.episode` per episode |

## The action space

`Discrete(1 + len(injections))`. Action 0 submits the task's clean request;
action `i` submits `injections[i - 1].inject(request)`. Reward is 1.0 when the
guardrail returns the task's `expected_effect`, so a policy learns which
injections bypass the guardrail. `reward_mode="adversarial"` inverts it, which
trains an attacker instead.

Episodes are single-step by design: `GuardrailEnv.evaluate()` is side-effect
free, so multi-step episodes would fabricate state that does not exist.

## Export timing

`capture_episodes` exports on a background daemon thread by default, since step
capture is on the hot path. This example passes `async_capture=False` so the
records are present before it prints them. A long-running training job should
keep the default.

## Writing your own guardrail

`AllowlistEnv` in `main.py` is example-local on purpose. Subclass
`BaseGuardrailEnv`, set `_name` and `_request_space`, implement `evaluate()`,
and it plugs into all three patterns.
