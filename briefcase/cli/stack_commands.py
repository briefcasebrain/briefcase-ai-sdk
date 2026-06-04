"""``briefcase stack`` — manage the bundled, pinned engine stack via ``docker compose``.

The compose invocation goes through an injected ``runner`` (default ``subprocess.run``), mirroring
``OciJJEngine``'s runner seam, so the commands unit-test with the same ``RecordingRunner`` fake. The
``doctor`` command (an install/compat preflight) lives in ``doctor.py``.
"""
from __future__ import annotations

import os
import subprocess
import sys

from briefcase.cli.stack import compose_path, load_compat


class Stack:
    """Thin wrapper over ``docker compose -f <bundled compose>`` with an injected runner."""

    def __init__(self, runner=None, compose_resolver=None) -> None:
        self.runner = runner or subprocess.run
        self._compose_resolver = compose_resolver or compose_path

    def _compose(self, *args, env=None):
        merged = {**os.environ, **(env or {})}
        # Honor $BRIEFCASE_OCIJJ_COMPOSE as an override (existing checkout-based users); otherwise the
        # bundled, pinned compose shipped in the wheel.
        override = os.environ.get("BRIEFCASE_OCIJJ_COMPOSE")
        if override:
            return self.runner(["docker", "compose", "-f", override, *args], env=merged)
        with self._compose_resolver() as compose_file:
            return self.runner(["docker", "compose", "-f", str(compose_file), *args], env=merged)

    def _tag_env(self) -> dict:
        """Pin the image tag from compat.json so the bundled compose pulls exactly that release."""
        try:
            tag = load_compat().get("oci_jj_image_tag")
        except Exception:
            tag = None
        return {"OCI_JJ_IMAGE_TAG": tag} if tag else {}

    def up(self, *, scorer=True, detach=True):
        args = ["up"]
        if detach:
            args.append("-d")
        if scorer:
            # the private verdict-worker is gated behind the 'scorer' compose profile
            return self._compose("--profile", "scorer", *args, env=self._tag_env())
        return self._compose(*args, env=self._tag_env())

    def down(self, *, volumes=False):
        args = ["down"]
        if volumes:
            args.append("-v")
        return self._compose(*args)

    def status(self):
        return self._compose("ps")

    def logs(self, service=None, follow=False):
        args = ["logs"]
        if follow:
            args.append("-f")
        if service:
            args.append(service)
        return self._compose(*args)


def _rc(result) -> int:
    return getattr(result, "returncode", 0)


def cmd_stack_up(args, store, engine, stack=None) -> int:
    stack = stack or Stack()
    want_scorer = not getattr(args, "no_scorer", False)
    rc = _rc(stack.up(scorer=want_scorer))
    if rc != 0 and want_scorer:
        # Most likely the private verdict-worker image couldn't be pulled. Bring up everything else so
        # runs still enqueue (just unscored), and tell the user exactly how to enable scoring.
        print("could not start the verdict-worker scorer (private image).", file=sys.stderr)
        print("  request beta access, then: docker login ghcr.io", file=sys.stderr)
        print("  starting the rest of the stack without scoring (runs enqueue but are not scored)...",
              file=sys.stderr)
        rc = _rc(stack.up(scorer=False))
    return rc


def cmd_stack_down(args, store, engine, stack=None) -> int:
    stack = stack or Stack()
    return _rc(stack.down(volumes=getattr(args, "volumes", False)))


def cmd_stack_status(args, store, engine, stack=None) -> int:
    stack = stack or Stack()
    return _rc(stack.status())


def cmd_stack_logs(args, store, engine, stack=None) -> int:
    stack = stack or Stack()
    return _rc(stack.logs(service=getattr(args, "service", None), follow=getattr(args, "follow", False)))
