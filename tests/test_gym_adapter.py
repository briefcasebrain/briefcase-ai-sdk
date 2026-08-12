"""Tests for GuardrailGymEnv: the gymnasium.Env view of a GuardrailEnv.

Module-level `import gymnasium` is deliberate: tests/conftest.py skips this file
when the 'gym' extra is absent.
"""

import gymnasium
import numpy as np
import pytest
from gymnasium import spaces

from briefcase.guardrails.framework import (
    BaseGuardrailEnv,
    Effect,
    EvalRequest,
    EvalResult,
    GuardrailInjection,
    GuardrailTask,
    PolicySpace,
    SpaceBound,
)
from briefcase.integrations.gym.adapter import GuardrailGymEnv, register_with_gymnasium


class AllowAllEnv(BaseGuardrailEnv):
    """Allows everything; used to check reward from expected_effect alone."""

    _name = "allow-all"
    _request_space = PolicySpace(
        agents=["nurse", "doctor"],
        actions=["read"],
        resources=["/records/*"],
        context_schema={"clearance": SpaceBound(low=0.0, high=5.0)},
    )

    def __init__(self):
        self.closed = False

    def evaluate(self, request: EvalRequest) -> EvalResult:
        return EvalResult(
            effect=Effect.ALLOW,
            guardrail_name=self._name,
            reason="allow-all",
            eval_time_ms=1.5,
        )

    def close(self) -> None:
        self.closed = True


class ContextFlagEnv(BaseGuardrailEnv):
    """Denies when context['bypass'] is set; otherwise allows."""

    _name = "context-flag"
    _request_space = PolicySpace(
        agents=["nurse"],
        actions=["read"],
        resources=["/records/*"],
        context_schema={"clearance": SpaceBound(low=0.0, high=5.0)},
    )

    def evaluate(self, request: EvalRequest) -> EvalResult:
        bypass = bool(request.context.get("bypass"))
        return EvalResult(
            effect=Effect.DENY if bypass else Effect.ALLOW,
            guardrail_name=self._name,
            reason="bypass flag set" if bypass else "clean request",
            eval_time_ms=2.0,
        )

    def explain(self, result: EvalResult):
        explanation = super().explain(result)
        explanation.policy_applied = {"name": "flag-policy", "condition": "no bypass"}
        return explanation


class SetFlagInjection(GuardrailInjection):
    """Sets context['bypass'], flipping ContextFlagEnv to DENY."""

    def inject(self, request: EvalRequest) -> EvalRequest:
        context = dict(request.context)
        context["bypass"] = True
        return EvalRequest(
            agent=request.agent,
            action=request.action,
            resource=request.resource,
            context=context,
            request_id=request.request_id,
        )


def _tasks():
    return [
        GuardrailTask(
            id="clean-nurse",
            request=EvalRequest("nurse", "read", "/records/1", {"clearance": 3.0}),
            expected_effect=Effect.ALLOW,
            category="clean",
        ),
        GuardrailTask(
            id="clean-doctor",
            request=EvalRequest("doctor", "read", "/records/2", {"clearance": 4.0}),
            expected_effect=Effect.ALLOW,
            category="clean",
        ),
    ]


def _flag_env(**kwargs):
    return GuardrailGymEnv(
        ContextFlagEnv(),
        _tasks(),
        injections=[SetFlagInjection(id="set-bypass", goal="force deny")],
        **kwargs,
    )


class TestConstruction:
    def test_is_a_gymnasium_env(self):
        assert isinstance(_flag_env(), gymnasium.Env)

    def test_action_space_covers_clean_plus_injections(self):
        env = _flag_env()
        assert env.action_space == spaces.Discrete(2)

    def test_action_space_without_injections(self):
        env = GuardrailGymEnv(AllowAllEnv(), _tasks())
        assert env.action_space == spaces.Discrete(1)

    def test_observation_space_shape(self):
        env = _flag_env()
        assert isinstance(env.observation_space, spaces.Dict)
        assert set(env.observation_space.spaces) == {
            "agent", "action", "resource", "context", "last_effect", "last_eval_time_ms",
        }
        assert env.observation_space["context"].shape == (1,)
        assert env.observation_space["last_effect"] == spaces.Discrete(3)

    def test_empty_tasks_raise(self):
        with pytest.raises(ValueError, match="task"):
            GuardrailGymEnv(AllowAllEnv(), [])

    def test_bad_reward_mode_raises(self):
        with pytest.raises(ValueError, match="reward_mode"):
            GuardrailGymEnv(AllowAllEnv(), _tasks(), reward_mode="vibes")

    def test_undeclared_render_mode_raises(self):
        with pytest.raises(ValueError, match="render_mode"):
            GuardrailGymEnv(AllowAllEnv(), _tasks(), render_mode="rgb_array")


class TestReset:
    def test_reset_returns_obs_and_info(self):
        obs, info = _flag_env().reset(seed=0)
        assert obs in _flag_env().observation_space
        assert info["task_id"] in {"clean-nurse", "clean-doctor"}

    def test_seeded_reset_is_deterministic(self):
        a = _flag_env()
        b = _flag_env()
        first = [a.reset(seed=7)[1]["task_id"] for _ in range(5)]
        second = [b.reset(seed=7)[1]["task_id"] for _ in range(5)]
        assert first == second

    def test_task_index_option_overrides_sampling(self):
        env = _flag_env()
        _, info = env.reset(seed=0, options={"task_index": 1})
        assert info["task_id"] == "clean-doctor"

    def test_out_of_range_task_index_raises(self):
        env = _flag_env()
        with pytest.raises(ValueError, match="task_index"):
            env.reset(options={"task_index": 9})

    def test_reset_clears_last_effect(self):
        env = _flag_env()
        env.reset(seed=0)
        env.step(0)
        obs, _ = env.reset(seed=0)
        assert obs["last_effect"] == 0
        assert obs["last_eval_time_ms"] == np.array([0.0], dtype=np.float64)


class TestStep:
    def test_clean_action_matching_expectation_rewards_one(self):
        env = _flag_env()
        env.reset(seed=0, options={"task_index": 0})
        obs, reward, terminated, truncated, info = env.step(0)

        assert reward == 1.0
        assert terminated is True
        assert truncated is False
        assert info["effect"] == "allow"
        assert info["expected_effect"] == "allow"
        assert info["utility"] is True
        assert info["injection_id"] is None
        assert info["reason"] == "clean request"
        assert info["eval_time_ms"] == 2.0
        assert isinstance(info["result"], EvalResult)
        assert obs["last_effect"] == 1

    def test_injection_action_flips_effect_and_reward(self):
        env = _flag_env()
        env.reset(seed=0, options={"task_index": 0})
        obs, reward, _, _, info = env.step(1)

        assert reward == 0.0
        assert info["effect"] == "deny"
        assert info["injection_id"] == "set-bypass"
        assert info["utility"] is False
        assert info["security"] is False
        assert obs["last_effect"] == 2

    def test_security_is_true_when_injection_is_resisted(self):
        env = GuardrailGymEnv(
            AllowAllEnv(),
            _tasks(),
            injections=[SetFlagInjection(id="set-bypass", goal="force deny")],
        )
        env.reset(seed=0, options={"task_index": 0})
        _, _, _, _, info = env.step(1)
        assert info["security"] is True

    def test_adversarial_reward_is_inverted(self):
        env = _flag_env(reward_mode="adversarial")
        env.reset(seed=0, options={"task_index": 0})
        _, clean_reward, _, _, _ = env.step(0)
        assert clean_reward == 0.0

        env.reset(seed=0, options={"task_index": 0})
        _, injected_reward, _, _, _ = env.step(1)
        assert injected_reward == 1.0

    def test_step_before_reset_raises(self):
        with pytest.raises(RuntimeError, match="reset"):
            _flag_env().step(0)

    def test_double_step_raises(self):
        env = _flag_env()
        env.reset(seed=0)
        env.step(0)
        with pytest.raises(RuntimeError, match="reset"):
            env.step(0)

    def test_invalid_action_raises(self):
        env = _flag_env()
        env.reset(seed=0)
        with pytest.raises(ValueError, match="action"):
            env.step(5)


class TestRenderAndClose:
    def test_ansi_render_returns_narrative(self):
        env = _flag_env(render_mode="ansi")
        env.reset(seed=0, options={"task_index": 0})
        env.step(0)
        text = env.render()
        assert "ALLOW" in text
        assert "flag-policy" in text

    def test_ansi_render_before_step_is_empty(self):
        env = _flag_env(render_mode="ansi")
        env.reset(seed=0)
        assert env.render() == ""

    def test_render_without_mode_returns_none(self):
        env = _flag_env()
        env.reset(seed=0)
        env.step(0)
        assert env.render() is None

    def test_close_delegates_to_guardrail(self):
        guardrail = AllowAllEnv()
        env = GuardrailGymEnv(guardrail, _tasks())
        env.close()
        assert guardrail.closed is True


class TestRegistration:
    def test_register_and_make(self):
        env_id = "briefcase/GuardrailEvalTest-v0"
        register_with_gymnasium(
            env_id,
            guardrail=AllowAllEnv(),
            tasks=_tasks(),
        )
        env = gymnasium.make(env_id)
        try:
            assert isinstance(env.unwrapped, GuardrailGymEnv)
            _, info = env.reset(seed=1)
            assert info["task_id"] in {"clean-nurse", "clean-doctor"}
        finally:
            env.close()

    def test_made_env_drives_the_registered_guardrail_itself(self):
        # gymnasium.make deep-copies spec.kwargs, so anything passed through
        # them reaches the env as a copy and close() never reaches the original.
        guardrail = AllowAllEnv()
        register_with_gymnasium(
            "briefcase/GuardrailEvalIdentity-v0", guardrail=guardrail, tasks=_tasks()
        )
        env = gymnasium.make("briefcase/GuardrailEvalIdentity-v0")
        assert env.unwrapped.guardrail is guardrail
        env.close()
        assert guardrail.closed is True

    def test_registers_a_guardrail_that_cannot_be_deep_copied(self):
        import threading

        guardrail = AllowAllEnv()
        guardrail.connection_lock = threading.Lock()
        register_with_gymnasium(
            "briefcase/GuardrailEvalUncopyable-v0", guardrail=guardrail, tasks=_tasks()
        )
        env = gymnasium.make("briefcase/GuardrailEvalUncopyable-v0")
        try:
            _, info = env.reset(seed=1)
            assert info["task_id"] in {"clean-nurse", "clean-doctor"}
        finally:
            env.close()

    def test_make_kwargs_override_registered_kwargs(self):
        register_with_gymnasium(
            "briefcase/GuardrailEvalOverride-v0", guardrail=AllowAllEnv(), tasks=_tasks()
        )
        env = gymnasium.make("briefcase/GuardrailEvalOverride-v0", reward_mode="adversarial")
        try:
            assert env.unwrapped.reward_mode == "adversarial"
        finally:
            env.close()


def test_passes_gymnasium_env_checker():
    from gymnasium.utils.env_checker import check_env

    check_env(_flag_env(), skip_render_check=True)
