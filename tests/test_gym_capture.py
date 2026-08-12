"""Tests for EpisodeCaptureWrapper: RL episodes as Briefcase decision records.

Module-level `import gymnasium` is deliberate: tests/conftest.py skips this file
when the 'gym' extra is absent.
"""

import gymnasium
import numpy as np
import pytest
from gymnasium import spaces

import briefcase
from briefcase.config import BriefcaseConfig
from briefcase.exporters.memory import MemoryExporter
from briefcase.guardrails.framework import (
    BaseGuardrailEnv,
    Effect,
    EvalRequest,
    EvalResult,
    GuardrailTask,
    PolicySpace,
)
from briefcase.integrations.gym.adapter import GuardrailGymEnv
from briefcase.integrations.gym.capture import EpisodeCaptureWrapper, capture_episodes


class TinyEnv(gymnasium.Env):
    """Three-step episode; reward 1.0 per step; obs counts the steps taken."""

    metadata = {"render_modes": []}

    def __init__(self, episode_length=3):
        self.episode_length = episode_length
        self.action_space = spaces.Discrete(2)
        self.observation_space = spaces.Box(low=0.0, high=100.0, shape=(1,), dtype=np.float64)
        self.steps = 0

        self.closed = False

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        self.steps = 0
        return np.array([0.0]), {"phase": "start"}

    def step(self, action):
        self.steps += 1
        terminated = self.steps >= self.episode_length
        return (
            np.array([float(self.steps)]),
            1.0,
            terminated,
            False,
            {"phase": "running", "steps": self.steps},
        )

    def close(self):
        self.closed = True


@pytest.fixture
def mem():
    return MemoryExporter()


@pytest.fixture(autouse=True)
def _restore_global_config():
    previous = BriefcaseConfig.get().exporter
    yield
    BriefcaseConfig.get().exporter = previous


def _wrap(mem, **kwargs):
    kwargs.setdefault("async_capture", False)
    return EpisodeCaptureWrapper(TinyEnv(), exporter=mem, **kwargs)


def _steps(mem):
    return [r for r in mem.records if r["decision_type"] == "rl.step"]


def _episodes(mem):
    return [r for r in mem.records if r["decision_type"] == "rl.episode"]


class TestStepRecords:
    def test_is_a_gymnasium_wrapper(self, mem):
        env = _wrap(mem)
        assert isinstance(env, gymnasium.Wrapper)
        assert isinstance(env.unwrapped, TinyEnv)

    def test_step_record_shape(self, mem):
        env = _wrap(mem)
        env.reset(seed=0)
        env.step(1)

        record = _steps(mem)[0]
        assert record["decision_type"] == "rl.step"
        assert record["function_name"] == "TinyEnv"
        assert record["step_index"] == 0
        assert record["inputs"] == {"action": "1"}
        assert record["outputs"] == {"observation": "array([1.])"}
        assert record["reward"] == 1.0
        assert record["terminated"] is False
        assert record["truncated"] is False
        assert record["info_keys"] == ["phase", "steps"]
        assert "decision_id" in record
        assert "execution_time_ms" in record

    def test_step_index_increments_and_episode_id_is_stable(self, mem):
        env = _wrap(mem)
        env.reset(seed=0)
        env.step(0)
        env.step(0)

        records = _steps(mem)
        assert [r["step_index"] for r in records] == [0, 1]
        assert len({r["episode_id"] for r in records}) == 1

    def test_capture_steps_false_emits_only_the_episode(self, mem):
        env = _wrap(mem, capture_steps=False)
        env.reset(seed=0)
        for _ in range(3):
            env.step(0)

        assert _steps(mem) == []
        assert len(_episodes(mem)) == 1

    def test_repr_truncation(self, mem):
        env = _wrap(mem, max_obs_chars=4, max_action_chars=1)
        env.reset(seed=0)
        env.step(12345)

        record = _steps(mem)[0]
        assert record["inputs"]["action"] == "1"
        assert record["outputs"]["observation"] == "arra"

    def test_step_before_reset_raises(self, mem):
        env = _wrap(mem)
        with pytest.raises(RuntimeError, match="reset"):
            env.step(0)


class TestEpisodeRecords:
    def test_episode_record_on_termination(self, mem):
        env = _wrap(mem)
        env.reset(seed=0)
        for _ in range(3):
            env.step(0)

        episodes = _episodes(mem)
        assert len(episodes) == 1
        record = episodes[0]
        assert record["function_name"] == "TinyEnv"
        assert record["env_id"] == "TinyEnv"
        assert record["outputs"]["total_steps"] == 3
        assert record["outputs"]["episode_return"] == 3.0
        assert record["outputs"]["completed"] is True
        assert record["episode_id"] == _steps(mem)[0]["episode_id"]
        assert "execution_time_ms" in record

    def test_episode_record_is_last(self, mem):
        env = _wrap(mem)
        env.reset(seed=0)
        for _ in range(3):
            env.step(0)
        assert mem.records[-1]["decision_type"] == "rl.episode"

    def test_reset_mid_episode_finalizes_partial_episode(self, mem):
        env = _wrap(mem)
        env.reset(seed=0)
        env.step(0)
        env.reset(seed=1)

        episodes = _episodes(mem)
        assert len(episodes) == 1
        assert episodes[0]["outputs"]["completed"] is False
        assert episodes[0]["outputs"]["total_steps"] == 1

    def test_first_reset_emits_no_episode(self, mem):
        _wrap(mem).reset(seed=0)
        assert _episodes(mem) == []

    def test_truncation_also_ends_the_episode(self, mem):
        inner = gymnasium.wrappers.TimeLimit(TinyEnv(episode_length=10), max_episode_steps=2)
        env = EpisodeCaptureWrapper(inner, exporter=mem, async_capture=False)
        env.reset(seed=0)
        env.step(0)
        env.step(0)

        episodes = _episodes(mem)
        assert len(episodes) == 1
        assert episodes[0]["outputs"]["completed"] is True
        assert _steps(mem)[-1]["truncated"] is True

    def test_close_finalizes_then_delegates(self, mem):
        env = _wrap(mem)
        env.reset(seed=0)
        env.step(0)
        env.close()

        assert _episodes(mem)[0]["outputs"]["completed"] is False
        assert env.unwrapped.closed is True

    def test_close_without_open_episode_emits_nothing(self, mem):
        env = _wrap(mem)
        env.close()
        assert mem.records == []

    def test_failed_inner_reset_leaves_no_open_episode(self, mem):
        class BrokenReset(TinyEnv):
            def reset(self, **kwargs):
                raise RuntimeError("env is down")

        env = EpisodeCaptureWrapper(BrokenReset(), exporter=mem, async_capture=False)
        with pytest.raises(RuntimeError, match="env is down"):
            env.reset(seed=0)
        with pytest.raises(RuntimeError, match="reset"):
            env.step(0)
        assert mem.records == []

    def test_failed_reset_still_finalizes_the_previous_episode(self, mem):
        class BreaksOnSecondReset(TinyEnv):
            resets = 0

            def reset(self, **kwargs):
                type(self).resets += 1
                if type(self).resets > 1:
                    raise RuntimeError("env is down")
                return super().reset(**kwargs)

        env = EpisodeCaptureWrapper(BreaksOnSecondReset(), exporter=mem, async_capture=False)
        env.reset(seed=0)
        env.step(0)
        with pytest.raises(RuntimeError, match="env is down"):
            env.reset(seed=1)

        assert [r["outputs"]["completed"] for r in _episodes(mem)] == [False]
        assert _episodes(mem)[0]["outputs"]["total_steps"] == 1

    def test_env_id_uses_spec_when_registered(self, mem):
        gymnasium.register(id="briefcase/TinyCapture-v0", entry_point=lambda **kw: TinyEnv())
        inner = gymnasium.make("briefcase/TinyCapture-v0")
        env = EpisodeCaptureWrapper(inner, exporter=mem, async_capture=False)
        env.reset(seed=0)
        for _ in range(3):
            env.step(0)
        assert _episodes(mem)[0]["env_id"] == "briefcase/TinyCapture-v0"


class TestHelperAndFallback:
    def test_capture_episodes_helper(self, mem):
        env = capture_episodes(TinyEnv(), exporter=mem, async_capture=False)
        assert isinstance(env, EpisodeCaptureWrapper)
        env.reset(seed=0)
        env.step(0)
        assert len(_steps(mem)) == 1

    def test_falls_back_to_global_exporter(self):
        collected = briefcase.observe("memory")
        env = capture_episodes(TinyEnv(), async_capture=False)
        env.reset(seed=0)
        for _ in range(3):
            env.step(0)
        assert [r["decision_type"] for r in collected.records] == [
            "rl.step", "rl.step", "rl.step", "rl.episode",
        ]


class AllowAllEnv(BaseGuardrailEnv):
    _name = "allow-all"
    _request_space = PolicySpace(agents=["nurse"], actions=["read"], resources=["/r/*"])

    def evaluate(self, request: EvalRequest) -> EvalResult:
        return EvalResult(effect=Effect.ALLOW, guardrail_name=self._name, reason="ok")


def test_end_to_end_over_guardrail_gym_env(mem):
    tasks = [
        GuardrailTask(
            id="t1",
            request=EvalRequest("nurse", "read", "/r/1"),
            expected_effect=Effect.ALLOW,
        )
    ]
    env = capture_episodes(
        GuardrailGymEnv(AllowAllEnv(), tasks), exporter=mem, async_capture=False
    )
    env.reset(seed=0)
    env.step(0)

    assert [r["decision_type"] for r in mem.records] == ["rl.step", "rl.episode"]
    assert mem.records[0]["reward"] == 1.0
    assert mem.records[0]["function_name"] == "GuardrailGymEnv"
    assert mem.records[-1]["outputs"]["episode_return"] == 1.0
    assert mem.records[-1]["outputs"]["completed"] is True
