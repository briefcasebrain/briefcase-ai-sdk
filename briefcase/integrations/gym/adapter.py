"""Gymnasium adapter over any GuardrailEnv.

Exposes a guardrail plus a task suite as a single-step bandit `gymnasium.Env`:
`reset()` samples a `GuardrailTask`, `step(action)` submits either the clean
request (action 0) or the request transformed by `injections[action - 1]`, and
the episode terminates. Reward is task-based, so an RL agent optimizes for the
guardrail producing the task's `expected_effect`.

Usage:
    from briefcase.integrations.gym import GuardrailGymEnv

    env = GuardrailGymEnv(my_guardrail, tasks, injections=[SwapContext(...)])
    obs, info = env.reset(seed=0)
    obs, reward, terminated, truncated, info = env.step(env.action_space.sample())
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import gymnasium
import numpy as np
from gymnasium import spaces

from briefcase.guardrails.framework import (
    Effect,
    EvalRequest,
    EvalResult,
    GuardrailInjection,
    GuardrailTask,
    PolicySpace,
)

DEFAULT_ENV_ID = "briefcase/GuardrailEval-v0"

# Vocabulary slot for a value an injection introduced that no task or
# PolicySpace declared, so observations stay inside the declared space.
_UNKNOWN = "<unknown>"

# last_effect: 0 = not evaluated yet, 1 = allow, 2 = deny.
_EFFECT_INDEX = {Effect.ALLOW: 1, Effect.DENY: 2}

# Finite ceiling for the observed evaluation time, in milliseconds.
_MAX_EVAL_TIME_MS = 3.6e6

_REWARD_MODES = ("utility", "adversarial")


class GuardrailGymEnv(gymnasium.Env):
    """A `gymnasium.Env` view of a `GuardrailEnv` and its task suite.

    Args:
        guardrail: Any object satisfying the GuardrailEnv protocol.
        tasks: Non-empty sequence of GuardrailTask; one is sampled per episode.
        injections: GuardrailInjection instances; action i applies injections[i - 1].
        reward_mode: "utility" rewards the guardrail being right, "adversarial"
            rewards it being wrong (for training attack policies).
        render_mode: None, "human", or "ansi".
    """

    metadata = {"render_modes": ["human", "ansi"]}

    def __init__(
        self,
        guardrail: Any,
        tasks: Sequence[GuardrailTask],
        injections: Sequence[GuardrailInjection] = (),
        reward_mode: str = "utility",
        render_mode: Optional[str] = None,
    ) -> None:
        if not tasks:
            raise ValueError("GuardrailGymEnv needs at least one task")
        if reward_mode not in _REWARD_MODES:
            raise ValueError(
                f"reward_mode must be one of {_REWARD_MODES}, got {reward_mode!r}"
            )
        if render_mode is not None and render_mode not in self.metadata["render_modes"]:
            raise ValueError(
                f"render_mode must be None or one of {self.metadata['render_modes']}, "
                f"got {render_mode!r}"
            )

        self.guardrail = guardrail
        self.tasks: List[GuardrailTask] = list(tasks)
        self.injections: List[GuardrailInjection] = list(injections)
        self.reward_mode = reward_mode
        self.render_mode = render_mode

        space = self._policy_space()
        self._context_keys = sorted(space.context_schema)
        self._agents = self._vocab(space.agents, [t.request.agent for t in self.tasks])
        self._actions = self._vocab(space.actions, [t.request.action for t in self.tasks])
        self._resources = self._vocab(
            space.resources, [t.request.resource for t in self.tasks]
        )

        lows = np.array(
            [space.context_schema[k].low for k in self._context_keys], dtype=np.float64
        )
        highs = np.array(
            [space.context_schema[k].high for k in self._context_keys], dtype=np.float64
        )

        # Kept as its own reference so observations clip against a typed Box
        # rather than indexing back into the Dict space.
        self._context_box = spaces.Box(low=lows, high=highs, dtype=np.float64)

        self.action_space = spaces.Discrete(1 + len(self.injections))
        self.observation_space = spaces.Dict({
            "agent": spaces.Discrete(len(self._agents)),
            "action": spaces.Discrete(len(self._actions)),
            "resource": spaces.Discrete(len(self._resources)),
            "context": self._context_box,
            "last_effect": spaces.Discrete(3),
            "last_eval_time_ms": spaces.Box(
                low=0.0, high=_MAX_EVAL_TIME_MS, shape=(1,), dtype=np.float64
            ),
        })

        self._task: Optional[GuardrailTask] = None
        self._result: Optional[EvalResult] = None
        self._done = True

    # -- gymnasium API -----------------------------------------------------

    def reset(self, *, seed: Optional[int] = None, options: Optional[Dict[str, Any]] = None):
        """Sample a task and return its clean request as the observation."""
        super().reset(seed=seed)

        index = (options or {}).get("task_index")
        if index is None:
            index = int(self.np_random.integers(0, len(self.tasks)))
        elif not 0 <= int(index) < len(self.tasks):
            raise ValueError(
                f"task_index {index} is out of range for {len(self.tasks)} tasks"
            )

        self._task = self.tasks[int(index)]
        self._result = None
        self._done = False

        return self._observation(self._task.request), {
            "task_id": self._task.id,
            "task_index": int(index),
            "category": self._task.category,
            "expected_effect": self._task.expected_effect.value,
        }

    def step(self, action):
        """Evaluate the sampled task once; the episode always terminates."""
        if self._done or self._task is None:
            raise RuntimeError("step() called before reset(); call reset() first")
        if not self.action_space.contains(action):
            raise ValueError(
                f"action {action!r} is outside {self.action_space}; "
                "0 submits the clean request, i submits injections[i - 1]"
            )

        index = int(action)
        injection = self.injections[index - 1] if index > 0 else None
        request = self._task.request
        if injection is not None:
            request = injection.inject(request)

        result = self.guardrail.evaluate(request)
        utility = self._task.utility(result)
        security = injection.security(result, self._task) if injection is not None else None

        self._result = result
        self._done = True

        reward = 1.0 if utility else 0.0
        if self.reward_mode == "adversarial":
            reward = 1.0 - reward

        info = {
            "task_id": self._task.id,
            "injection_id": injection.id if injection is not None else None,
            "effect": result.effect.value,
            "expected_effect": self._task.expected_effect.value,
            "utility": utility,
            "security": security,
            "reason": result.reason,
            "eval_time_ms": result.eval_time_ms,
            "result": result,
        }
        return self._observation(request), reward, True, False, info

    def render(self):
        """Return the explanation narrative under "ansi", print it under "human"."""
        if self.render_mode is None:
            return None
        text = ""
        if self._result is not None:
            text = self.guardrail.explain(self._result).to_narrative()
        if self.render_mode == "human":
            print(text)
            return None
        return text

    def close(self) -> None:
        close = getattr(self.guardrail, "close", None)
        if callable(close):
            close()

    # -- internals ---------------------------------------------------------

    def _policy_space(self) -> PolicySpace:
        space = getattr(self.guardrail, "request_space", None)
        return space if isinstance(space, PolicySpace) else PolicySpace()

    @staticmethod
    def _vocab(*sources: Sequence[str]) -> List[str]:
        values = {value for source in sources for value in source}
        return sorted(values) + [_UNKNOWN]

    def _observation(self, request: EvalRequest) -> Dict[str, Any]:
        context: Any = np.zeros(len(self._context_keys), dtype=np.float64)
        for position, key in enumerate(self._context_keys):
            value = request.context.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                context[position] = float(value)
        low = self._context_box.low
        high = self._context_box.high

        eval_time = self._result.eval_time_ms if self._result is not None else 0.0
        effect = _EFFECT_INDEX.get(self._result.effect, 0) if self._result is not None else 0

        return {
            "agent": self._index(self._agents, request.agent),
            "action": self._index(self._actions, request.action),
            "resource": self._index(self._resources, request.resource),
            "context": np.clip(context, low, high),
            "last_effect": np.int64(effect),
            "last_eval_time_ms": np.array(
                [min(max(float(eval_time), 0.0), _MAX_EVAL_TIME_MS)], dtype=np.float64
            ),
        }

    @staticmethod
    def _index(vocab: List[str], value: str) -> np.int64:
        try:
            return np.int64(vocab.index(value))
        except ValueError:
            return np.int64(len(vocab) - 1)


def register_with_gymnasium(env_id: str = DEFAULT_ENV_ID, **env_kwargs: Any) -> None:
    """Register a configured GuardrailGymEnv so `gymnasium.make(env_id)` builds it.

    Opt-in: importing this module registers nothing, so a briefcase import never
    mutates the global gymnasium registry.

    The guardrail and tasks are held in the entry point's closure rather than in
    the registry's ``kwargs``, which `gymnasium.make` deep-copies. That keeps the
    made env driving the very guardrail passed here, so ``close()`` reaches it,
    and lets a guardrail holding an uncopyable resource (a lock, a connection)
    be registered at all. Keyword arguments to `gymnasium.make` still win.
    """
    registered = dict(env_kwargs)

    def _entry_point(**overrides: Any) -> GuardrailGymEnv:
        return GuardrailGymEnv(**{**registered, **overrides})

    gymnasium.register(id=env_id, entry_point=_entry_point, kwargs={})


__all__ = ["GuardrailGymEnv", "register_with_gymnasium", "DEFAULT_ENV_ID"]
