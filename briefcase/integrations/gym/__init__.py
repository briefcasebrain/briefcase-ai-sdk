"""Gymnasium bridge, in both directions.

The guardrail framework already mirrors the Gymnasium API (GuardrailEnv ~
gym.Env, GuardrailWrapper ~ gym.Wrapper, PolicySpace ~ spaces). This package
connects the two for real: a `gymnasium.Env` over any GuardrailEnv, and a
`gymnasium.Wrapper` that records RL episodes as Briefcase decision records
(`"rl.step"` and `"rl.episode"`).

Requires the 'gym' extra: pip install briefcase-ai[gym]

Usage:
    1. Train or evaluate a policy against a guardrail:

        from briefcase.integrations.gym import GuardrailGymEnv

        env = GuardrailGymEnv(guardrail, tasks, injections=injections)
        obs, info = env.reset(seed=0)
        obs, reward, terminated, truncated, info = env.step(0)  # 0 = clean request

    2. Train an attack policy instead, by inverting the reward:

        env = GuardrailGymEnv(guardrail, tasks, injections, reward_mode="adversarial")

    3. Make it from a string id, like any registered gymnasium env:

        from briefcase.integrations.gym import register_with_gymnasium

        register_with_gymnasium(guardrail=guardrail, tasks=tasks)
        env = gymnasium.make("briefcase/GuardrailEval-v0")

    4. Capture any RL rollout as decision records:

        import briefcase
        from briefcase.integrations.gym import capture_episodes

        briefcase.observe("runs.jsonl")
        env = capture_episodes(gymnasium.make("CartPole-v1"))
"""

try:
    from briefcase.integrations.gym.adapter import (
        DEFAULT_ENV_ID,
        GuardrailGymEnv,
        register_with_gymnasium,
    )
    from briefcase.integrations.gym.capture import (
        EpisodeCaptureWrapper,
        capture_episodes,
    )
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "briefcase.integrations.gym requires the 'gym' extra.\n"
        "Install it with: pip install briefcase-ai[gym]"
    ) from exc

__all__ = [
    "GuardrailGymEnv",
    "register_with_gymnasium",
    "DEFAULT_ENV_ID",
    "EpisodeCaptureWrapper",
    "capture_episodes",
]
