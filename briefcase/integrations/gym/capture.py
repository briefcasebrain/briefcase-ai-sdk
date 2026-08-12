"""Capture RL episodes from any gymnasium.Env as Briefcase decision records.

Wraps an environment and emits one `"rl.step"` record per step and one
`"rl.episode"` record per episode through the configured exporter, so RL
rollouts land in the same decision store as `@briefcase.capture` calls. A reset
mid-episode, or `close()`, finalizes the open episode with `completed=False`.

Usage:
    from briefcase.integrations.gym import capture_episodes

    env = capture_episodes(gymnasium.make("CartPole-v1"), exporter=my_exporter)
    obs, info = env.reset(seed=0)
    obs, reward, terminated, truncated, info = env.step(env.action_space.sample())
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import gymnasium

from briefcase._export_mixin import ExportMixin


class EpisodeCaptureWrapper(gymnasium.Wrapper, ExportMixin):
    """Records every step and episode of the wrapped environment.

    Args:
        env: The environment to wrap.
        exporter: Per-instance exporter. Falls back to the global
            ``briefcase.observe()`` exporter when None.
        async_capture: True by default; step capture is on the hot path.
        capture_steps: False emits only the per-episode record.
        max_obs_chars: Truncation limit for the observation repr.
        max_action_chars: Truncation limit for the action repr.
    """

    def __init__(
        self,
        env: gymnasium.Env,
        *,
        exporter: Any = None,
        async_capture: bool = True,
        capture_steps: bool = True,
        max_obs_chars: int = 1000,
        max_action_chars: int = 1000,
    ) -> None:
        super().__init__(env)
        self._exporter = exporter
        self.async_capture = async_capture
        self.capture_steps = capture_steps
        self.max_obs_chars = max_obs_chars
        self.max_action_chars = max_action_chars

        self.episode_id: Optional[str] = None
        self._step_index = 0
        self._episode_return = 0.0
        self._episode_started_at: Optional[datetime] = None

    # -- gymnasium API -----------------------------------------------------

    def reset(self, **kwargs):
        """Finalize any open episode, then start a new one.

        The new episode opens only after the wrapped reset returns, so a reset
        that raises leaves no episode open and the next step() says so.
        """
        self._finalize_episode(completed=False)
        result = self.env.reset(**kwargs)
        self.episode_id = str(uuid.uuid4())
        self._step_index = 0
        self._episode_return = 0.0
        self._episode_started_at = datetime.now(timezone.utc)
        return result

    def step(self, action):
        if self.episode_id is None:
            raise RuntimeError("step() called before reset(); call reset() first")

        started_at = datetime.now(timezone.utc)
        observation, reward, terminated, truncated, info = self.env.step(action)
        ended_at = datetime.now(timezone.utc)

        self._episode_return += float(reward)
        step_index = self._step_index
        self._step_index += 1

        if self.capture_steps:
            self._trigger_export({
                "decision_id": str(uuid.uuid4()),
                "decision_type": "rl.step",
                "function_name": self._env_id(),
                "episode_id": self.episode_id,
                "step_index": step_index,
                "inputs": {"action": repr(action)[: self.max_action_chars]},
                "outputs": {"observation": repr(observation)[: self.max_obs_chars]},
                "reward": float(reward),
                "terminated": bool(terminated),
                "truncated": bool(truncated),
                "info_keys": sorted(info) if isinstance(info, dict) else [],
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "execution_time_ms": (ended_at - started_at).total_seconds() * 1000,
            })

        if terminated or truncated:
            self._finalize_episode(completed=True)

        return observation, reward, terminated, truncated, info

    def close(self):
        self._finalize_episode(completed=False)
        return self.env.close()

    # -- internals ---------------------------------------------------------

    def _finalize_episode(self, *, completed: bool) -> None:
        if self.episode_id is None:
            return

        started_at = self._episode_started_at or datetime.now(timezone.utc)
        ended_at = datetime.now(timezone.utc)
        record: Dict[str, Any] = {
            "decision_id": str(uuid.uuid4()),
            "decision_type": "rl.episode",
            "function_name": self._env_id(),
            "episode_id": self.episode_id,
            "env_id": self._env_id(),
            "inputs": {},
            "outputs": {
                "total_steps": self._step_index,
                "episode_return": self._episode_return,
                "completed": completed,
            },
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "execution_time_ms": (ended_at - started_at).total_seconds() * 1000,
        }
        self.episode_id = None
        self._episode_started_at = None
        self._trigger_export(record)

    def _env_id(self) -> str:
        spec = getattr(self.env, "spec", None)
        env_id = getattr(spec, "id", None)
        return env_id if env_id else type(self.env.unwrapped).__name__


def capture_episodes(env: gymnasium.Env, *, exporter: Any = None, **kwargs: Any):
    """Wrap `env` in an EpisodeCaptureWrapper.

    Export runs on a background thread by default, because step capture is on
    the hot path. A process that exits immediately after `close()` can exit
    before those threads deliver; pass ``async_capture=False`` in short runs.
    """
    return EpisodeCaptureWrapper(env, exporter=exporter, **kwargs)


__all__ = ["EpisodeCaptureWrapper", "capture_episodes"]
