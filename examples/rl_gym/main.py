"""
Example: drive a guardrail from Gymnasium, and capture the rollout.

Three patterns:
1. GuardrailBenchmark for the non-RL baseline (utility and security scores)
2. GuardrailGymEnv as a single-step bandit a policy can train against
3. EpisodeCaptureWrapper writing every step and episode to a Briefcase exporter

Runs offline. Needs the 'gym' extra: pip install briefcase-ai[gym]
"""

try:
    import gymnasium  # noqa: F401
except ImportError:
    raise SystemExit(
        "This example needs Gymnasium. Install it with:\n"
        "    pip install briefcase-ai[gym]"
    )

import briefcase
from briefcase.guardrails.framework import (
    BaseGuardrailEnv,
    Effect,
    EvalRequest,
    EvalResult,
    GuardrailBenchmark,
    GuardrailInjection,
    GuardrailTask,
    PolicySpace,
    SpaceBound,
)
from briefcase.integrations.gym import GuardrailGymEnv, capture_episodes


# A guardrail lives in your code, not in the SDK: implement evaluate() and
# declare the request space.
class AllowlistEnv(BaseGuardrailEnv):
    """Allows a listed agent to read records when its clearance is high enough."""

    _name = "clearance-allowlist"
    _request_space = PolicySpace(
        agents=["nurse", "doctor", "contractor"],
        actions=["read"],
        resources=["/records/*"],
        context_schema={"clearance": SpaceBound(low=0.0, high=5.0)},
    )

    MIN_CLEARANCE = 3.0

    def evaluate(self, request: EvalRequest) -> EvalResult:
        clearance = request.context.get("clearance", 0.0)
        allowed = (
            request.agent in ("nurse", "doctor")
            and isinstance(clearance, (int, float))
            and clearance >= self.MIN_CLEARANCE
        )
        return EvalResult(
            effect=Effect.ALLOW if allowed else Effect.DENY,
            guardrail_name=self._name,
            reason=(
                f"{request.agent} cleared at {clearance}"
                if allowed
                else f"{request.agent} blocked at clearance {clearance}"
            ),
            eval_time_ms=0.4,
            metadata={
                "policy_applied": {
                    "name": "clearance-allowlist",
                    "condition": f"clearance >= {self.MIN_CLEARANCE}",
                }
            },
        )


class EscalateClearanceInjection(GuardrailInjection):
    """Rewrites clearance to the maximum, testing whether the guardrail trusts it."""

    def inject(self, request: EvalRequest) -> EvalRequest:
        context = dict(request.context)
        context["clearance"] = 5.0
        return EvalRequest(
            agent=request.agent,
            action=request.action,
            resource=request.resource,
            context=context,
            request_id=request.request_id,
        )


TASKS = [
    GuardrailTask(
        id="cleared-nurse",
        request=EvalRequest("nurse", "read", "/records/1", {"clearance": 4.0}),
        expected_effect=Effect.ALLOW,
        category="clean",
    ),
    GuardrailTask(
        id="uncleared-nurse",
        request=EvalRequest("nurse", "read", "/records/2", {"clearance": 1.0}),
        expected_effect=Effect.DENY,
        category="clean",
    ),
    GuardrailTask(
        id="contractor",
        request=EvalRequest("contractor", "read", "/records/3", {"clearance": 5.0}),
        expected_effect=Effect.DENY,
        category="role",
    ),
]

INJECTIONS = [
    EscalateClearanceInjection(id="escalate-clearance", goal="raise clearance to 5.0"),
]


# Pattern 1: Benchmark baseline
print("Pattern 1: GuardrailBenchmark baseline")
print("-" * 50)

guardrail = AllowlistEnv()
report = GuardrailBenchmark().run(guardrail, TASKS, INJECTIONS)
print(report.summary())
print()


# Pattern 2: Gymnasium env with a seeded random policy
print("Pattern 2: GuardrailGymEnv with a random policy")
print("-" * 50)

env = GuardrailGymEnv(guardrail, TASKS, INJECTIONS, render_mode="ansi")
env.action_space.seed(0)

total_reward = 0.0
for episode in range(4):
    obs, info = env.reset(seed=episode)
    action = env.action_space.sample()
    obs, reward, terminated, truncated, info = env.step(action)
    total_reward += reward
    print(
        f"episode {episode}: task={info['task_id']} "
        f"injection={info['injection_id']} effect={info['effect']} reward={reward}"
    )
    print(f"  {env.render()}")

print(f"Return over 4 episodes: {total_reward}")
print()


# Pattern 3: Capture the rollout as decision records
print("Pattern 3: EpisodeCaptureWrapper")
print("-" * 50)

records = briefcase.observe("memory")
captured = capture_episodes(
    GuardrailGymEnv(guardrail, TASKS, INJECTIONS), async_capture=False
)
captured.action_space.seed(0)

for episode in range(2):
    captured.reset(seed=episode)
    captured.step(captured.action_space.sample())
captured.close()

for record in records.records:
    if record["decision_type"] == "rl.step":
        print(
            f"rl.step   step={record['step_index']} reward={record['reward']} "
            f"action={record['inputs']['action']}"
        )
    else:
        outputs = record["outputs"]
        print(
            f"rl.episode steps={outputs['total_steps']} "
            f"return={outputs['episode_return']} completed={outputs['completed']}"
        )

print()
print(f"Captured {len(records.records)} records; export them with "
      f"briefcase.observe('runs.jsonl') instead of 'memory'.")
