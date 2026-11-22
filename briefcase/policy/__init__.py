"""Policy for coding tools: allow, ask or deny what an agent may run.

The user-facing entry point is the CLI (``briefcase policy ...``). The modules
here are split so the hook, which runs before every tool call, imports as
little as possible:

- :mod:`briefcase.policy.model`   the policy file: parse, validate, merge
- :mod:`briefcase.policy.presets` the built-in ``observe``/``guarded``/``strict`` rules
- :mod:`briefcase.policy.shell`   shell command analysis (argv, not regex)
- :mod:`briefcase.policy.engine`  tool call -> decision
- :mod:`briefcase.policy.tools`   each tool's hook payload and response format
- :mod:`briefcase.policy.hook`    the hook process (``briefcase hook <tool> --enforce``)
- :mod:`briefcase.policy.guardrail`  the same engine as a ``GuardrailEnv``

Nothing is imported here on purpose: importing this package must stay free.
"""
