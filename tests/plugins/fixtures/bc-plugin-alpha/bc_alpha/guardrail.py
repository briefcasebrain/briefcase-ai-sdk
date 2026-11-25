"""briefcase.guardrails: make("alpha-check-v1", blocked=["delete"])."""
from briefcase.guardrails.framework import BaseGuardrailEnv, Effect, EvalResult

BRIEFCASE_PLUGIN_API = 1


class AlphaCheck(BaseGuardrailEnv):
    _name = "alpha-check"

    def __init__(self, blocked=("delete",)) -> None:
        self.blocked = set(blocked)

    def evaluate(self, request):
        if request.action in self.blocked:
            return EvalResult(Effect.DENY, self._name, reason=f"{request.action} is blocked")
        return EvalResult(Effect.ALLOW, self._name)
