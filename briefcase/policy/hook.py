"""The enforcement hook: ``briefcase-ai hook <tool> --enforce``.

The tool runs this before every tool call with the call as JSON on stdin and
waits for it, so the whole path is: read stdin, load the policy, decide in
this process, print the tool's response, exit 0. There is no network hop and
nothing here needs the receiver. After the response is flushed the decision is
appended to ``~/.briefcase/policy/decisions.jsonl`` and, when a receiver is
around, handed to a detached child that posts it; the hook does not wait.

Failure semantics
    Default is fail OPEN: if the policy file is invalid, the payload is not
    understood, or this code raises, the call is allowed. That is never
    silent: the problem is written to ``~/.briefcase/policy/health.json``
    (shown by ``briefcase-ai policy status``, ``briefcase-ai status`` and
    ``briefcase-ai doctor --connect``), logged, and shown in the tool as a
    warning where the tool has a channel for one.

    ``fail_closed = true`` in a policy, or ``--fail-closed`` on the hook
    command, turns each of those cases into a deny whose message says how to
    fix it. An invalid policy file is never applied in part: the strongest
    file that exists must be valid or no rule from any file is used.

Content
    The command, path or URL is not written to the log or sent to the
    receiver. A keyed hash (HMAC-SHA256 with a per-install key) and the rule
    that matched are. ``--capture-content`` adds the text itself.
"""

from __future__ import annotations

import json
import os
import sys
import time

MAX_STDIN_BYTES = 4 * 1024 * 1024
DEFAULT_ENDPOINT = "http://127.0.0.1:4318"
REPORT_PATH = "/v1/policy/decisions"
WARN_EVERY_SECONDS = 600
LOG_MAX_BYTES = 5 * 1024 * 1024
FIX = "Fix it with `briefcase-ai policy check`, or turn enforcement off with `briefcase-ai policy off`."


class Options(object):
    __slots__ = ("tool", "fail_closed", "capture_content", "endpoint", "endpoint_set", "report", "require_policy",
                 "parse_error", "require_given")

    def __init__(self, argv):
        """Hand-rolled on purpose: argparse exits 2 on an unknown flag, and exit 2 blocks the tool call. The command
        line is read by :func:`briefcase.policy.optargs.normalize`, the one reading every entry point shares."""
        from briefcase.policy import optargs

        raw = list(argv)
        norm = optargs.normalize(raw[1:])
        argv = raw[:1] + norm.argv
        self.tool = argv[0] if argv else ""
        self.fail_closed = any(t == "--fail-closed" or t.startswith("--fail-closed=") for t in raw[1:])
        self.require_policy = ""
        self.require_given = norm.require_given
        self.capture_content = False
        self.parse_error = norm.problems[0] if norm.problems else ""
        self.endpoint = os.environ.get("BRIEFCASE_RECEIVER_ENDPOINT") or ""
        self.endpoint_set = bool(self.endpoint)
        self.report = os.environ.get("BRIEFCASE_POLICY_REPORT", "1") not in ("0", "false", "off", "no")
        index = 1
        while index < len(argv):
            flag = argv[index]
            if flag == "--capture-content":
                self.capture_content = True
            elif flag == "--no-report":
                self.report = False
            elif flag in ("--require-policy-sha256", "--endpoint"):
                index += 1  # normalize left only options that have a value
                if flag == "--endpoint":
                    self.endpoint, self.endpoint_set = argv[index], True
                else:
                    self.require_policy = argv[index].strip().lower()
            index += 1
        if not self.endpoint:
            self.endpoint = DEFAULT_ENDPOINT


# --- local state --------------------------------------------------------------

def state_dir():
    from briefcase.policy import model
    return model.state_dir()


def log_path():
    return os.path.join(state_dir(), "decisions.jsonl")


def health_path():
    return os.path.join(state_dir(), "health.json")


def _ensure_dir():
    path = state_dir()
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError:
        pass
    return path


def hash_key():
    """A per-install random key, so a hash in a record cannot be reversed by
    hashing guessed commands. It never leaves the machine."""
    path = os.path.join(_ensure_dir(), "hash.key")
    try:
        with open(path, "rb") as handle:
            key = handle.read()
        if len(key) >= 16:
            return key
    except OSError:
        pass
    key = os.urandom(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
    except FileExistsError:
        try:
            with open(path, "rb") as handle:
                return handle.read() or key
        except OSError:
            pass
    except OSError:
        pass
    return key


def keyed_hash(text):
    import hashlib
    import hmac
    return hmac.new(hash_key(), text.encode("utf-8", "replace"), hashlib.sha256).hexdigest()


def append_log(entry):
    """One JSON line, one ``write`` call on an ``O_APPEND`` descriptor, so
    concurrent hooks never interleave. The file is rotated once at 5 MiB."""
    path = log_path()
    _ensure_dir()
    try:
        if os.path.getsize(path) > LOG_MAX_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass
    line = (json.dumps(entry, separators=(",", ":"), default=str) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def read_health():
    try:
        with open(health_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_problem(text, tool, fail_closed):
    """Remember a hook problem for status/doctor. Returns ``True`` when the
    person should be told now (at most once every ten minutes)."""
    now = time.time()
    health = read_health()
    health["errors"] = int(health.get("errors", 0)) + 1
    health["last_error"] = {"at": now, "message": text, "tool": tool, "fail_closed": bool(fail_closed)}
    warn = now - float(health.get("warned_at", 0) or 0) > WARN_EVERY_SECONDS
    if warn:
        health["warned_at"] = now
    try:
        _ensure_dir()
        tmp = health_path() + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(health, handle)
        os.replace(tmp, health_path())
    except OSError:
        pass
    return warn


# --- reporting ----------------------------------------------------------------

def token_for(endpoint, environ=None):
    """The receiver token that belongs to ``endpoint``, or ``""``. The ambient ``BRIEFCASE_RECEIVER_TOKEN`` goes to a
    loopback receiver, or to the one endpoint named in ``BRIEFCASE_RECEIVER_TOKEN_ENDPOINT``; an endpoint someone
    else chose never receives it."""
    environ = os.environ if environ is None else environ
    token = environ.get("BRIEFCASE_RECEIVER_TOKEN") or ""
    if not token:
        return ""
    wanted = str(endpoint or "").rstrip("/")
    bound = (environ.get("BRIEFCASE_RECEIVER_TOKEN_ENDPOINT") or "").rstrip("/")
    if bound:  # an explicit binding decides: only that exact endpoint
        return token if bound == wanted else ""
    return token if wanted == DEFAULT_ENDPOINT.rstrip("/") else ""  # unbound: only the default local receiver


def receiver_expected(options):
    """Only start the reporting child when a receiver was configured or has been started here."""
    if not options.report:
        return False
    if options.endpoint_set:
        return True
    from briefcase.policy import model
    return os.path.exists(os.path.join(model.briefcase_home(), "receiver", "receiver.pid"))


def spawn_report(options, event):
    """Fire and forget: a detached child posts ``event``; this process does not wait for it.

    The child gets its input on a pipe and its stdout and stderr go to
    ``/dev/null``. Claude Code keeps reading a hook's output while any process
    still holds it open, and refuses the decision if a background child does."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report.py")
    body = json.dumps({"endpoint": options.endpoint, "path": REPORT_PATH, "event": event,
                       "token": token_for(options.endpoint)}).encode("utf-8")
    argv = [sys.executable, "-I", "-S", script]
    if hasattr(os, "posix_spawn") and len(body) < 60000:
        read_end, write_end = os.pipe()
        try:
            actions = [
                (os.POSIX_SPAWN_DUP2, read_end, 0),
                (os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0),
                (os.POSIX_SPAWN_OPEN, 2, os.devnull, os.O_WRONLY, 0),
                (os.POSIX_SPAWN_CLOSE, write_end),
            ]
            try:
                os.posix_spawn(argv[0], argv, {}, file_actions=actions, setsid=True)
            except (NotImplementedError, TypeError):
                os.posix_spawn(argv[0], argv, {}, file_actions=actions)
            os.close(read_end)
            read_end = None
            os.write(write_end, body)
        finally:
            if read_end is not None:
                os.close(read_end)
            os.close(write_end)
        return
    import subprocess
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=flags, close_fds=True)
    child.stdin.write(body)
    child.stdin.close()


# --- the hook -----------------------------------------------------------------

def _is_digest(text):
    return len(text) == 64 and all(ch in "0123456789abcdef" for ch in text)


def _read_required(model, wanted):
    """``(bytes, "")`` when the system policy file is the one this hook was deployed with, else ``(None, why)``.

    The bytes returned are the bytes that were hashed; the caller hands them to the loader, so what is judged is
    exactly what was authenticated and a file swapped in between cannot be."""
    import hashlib

    try:
        with open(model.system_policy_path(), "rb") as handle:
            data = handle.read()
    except OSError:
        data = None
    if data is not None and hashlib.sha256(data).hexdigest() == wanted:
        return data, ""
    return None, ("the system policy this hook was deployed with is missing or is not the one it requires "
                  f"(sha256 {wanted[:12]}...), so no weaker policy is used in its place")


def _load_errors(loaded):
    return "; ".join(f"{path or source}: {text}" for source, path, text in loaded.errors)


def _verdict(tool, event, started, entry, effect, text, rule, problem=None):
    entry.update(effect=effect, would=effect, rule=rule, source="setting")
    if problem:
        entry["error"] = problem
    entry["ms"] = round((time.perf_counter() - started) * 1000, 2)
    from briefcase.policy import tools
    return tools.respond(tool, event, effect, text), entry


def decide(options, payload):
    """``(stdout, log entry or None)`` for one hook payload. Raises on anything unexpected.

    The only way to ``allow`` under strictness is: a fully valid event of a tool the policy covers, the required
    policy (if one is named) loaded from the very bytes that were hashed, every policy file applied, and the
    policy's own explicit ``allow``. Every other path ends in a deny."""
    from briefcase.policy import engine, model, tools

    tool = options.tool
    started = time.perf_counter()
    call = tools.parse(tool, payload)
    event = tools.event_of(tool, payload)
    required, required_problem = (None, "")
    if options.require_given and not _is_digest(options.require_policy):
        required_problem = ("the policy hash this hook requires is missing or is not a sha256 digest, so it cannot "
                            "authenticate a policy")
    elif options.require_policy:
        required, required_problem = _read_required(model, options.require_policy)
    loaded = model.load(call.project_dir or call.cwd, texts={"system": required} if required is not None else None)
    fail_closed = options.fail_closed = options.fail_closed or loaded.fail_closed
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tool": tool, "event": event, "kind": call.kind,
        "tool_name": call.tool_name,
    }
    if call.session_id:
        entry["session"] = call.session_id
    if call.tool_use_id:
        entry["tool_use_id"] = call.tool_use_id
    target = call.target()
    if target:
        entry["target_hmac_sha256"] = keyed_hash(target)
        if options.capture_content:
            entry["target"] = target[:4000]
    if required_problem:  # terminal, strict or not: a hook bound to a policy never judges by another
        verdict = _verdict(tool, event, started, entry, "deny", f"Blocked by briefcase-ai policy: "
                           f"{required_problem}. {FIX}", "fail_closed", required_problem)
        try:  # bookkeeping comes after the verdict exists and can never change it
            record_problem(required_problem, tool, True)
        except BaseException:  # noqa: B036
            pass
        return verdict
    if options.parse_error and fail_closed:
        problem = f"the hook command line is malformed ({options.parse_error})"
        return _verdict(tool, event, started, entry, "deny",
                        f"Blocked by briefcase-ai policy: {problem}, and the policy is fail_closed. {FIX}",
                        "fail_closed", problem)
    defect = call.note if call.kind == "invalid" else call.defect
    if defect:
        if fail_closed:
            problem = f"the hook input is not a complete {tool} event for a tool this policy covers ({defect})"
            return _verdict(tool, event, started, entry, "deny",
                            f"Blocked by briefcase-ai policy: {problem}, and the policy is fail_closed, so nothing "
                            f"runs. {FIX}", "fail_closed", defect)
        if call.kind == "other":
            return tools.respond(tool, event, "allow"), None
        if call.kind == "invalid":  # not enforcing strictly: an event this policy cannot judge is not blocked
            entry["note"] = call.note
            entry.update(effect="allow", would="allow", rule="not_judged", source="setting")
            entry["ms"] = round((time.perf_counter() - started) * 1000, 2)
            return tools.respond(tool, event, "allow"), entry
    warning = None
    problem = None
    if loaded.policy is None:
        if loaded.errors:
            problem = "the policy cannot be applied (" + _load_errors(loaded) + ")"
        elif fail_closed:
            problem = "no policy file was found, and the hook was installed with --fail-closed"
        else:
            problem = "no policy file was found (" + model.user_policy_path() + ")"
    elif loaded.errors:
        problem = "part of the policy cannot be applied (" + _load_errors(loaded) + ")"
    if problem is not None:
        entry["error"] = problem
        if record_problem(problem, tool, fail_closed):
            warning = (f"briefcase-ai policy: {problem}. "
                       + ("Tool calls are blocked until it is fixed. " if fail_closed else
                          ("Enforcement is OFF until it is fixed. " if loaded.policy is None else
                           "The rest of the policy is still enforced. "))
                       + FIX)
        if fail_closed:
            text = f"Blocked by briefcase-ai policy: {problem}. The policy is fail_closed, so nothing runs. {FIX}"
            entry.update(effect="deny", would="deny", rule="fail_closed", source="setting")
            entry["ms"] = round((time.perf_counter() - started) * 1000, 2)
            return tools.respond(tool, event, "deny", text), entry
        if loaded.policy is None:
            entry.update(effect="allow", would="allow", rule="fail_open", source="setting")
            entry["ms"] = round((time.perf_counter() - started) * 1000, 2)
            return tools.respond(tool, event, "allow", warning=warning), entry
    decision = engine.evaluate(loaded.policy, call)
    effect = decision.effect
    downgraded = False
    if effect == "ask" and not tools.can_ask(tool, event):
        effect = loaded.policy.settings["ask_fallback"]
        downgraded = True
    if fail_closed and (effect not in ("allow", "deny", "ask")
                        or (call.kind == "shell" and not decision.units)):  # nothing was judged: nothing is allowed
        effect = "deny"
        decision.would = "deny"
    entry.update(effect=effect, would=decision.would, mode=decision.mode, rule=decision.rule_id,
                 source=decision.match.source if decision.match else None)
    if decision.match is not None:
        entry["reason"] = decision.reason
        if options.capture_content:
            entry["unit"] = decision.match.unit
    if downgraded:
        entry["ask_fallback"] = effect
    if decision.problems:
        entry["unparseable"] = len(decision.problems)
    if call.note:
        entry["note"] = call.note
    text = tools.message(decision, effect, downgraded) if effect != "allow" else ""
    entry["ms"] = round((time.perf_counter() - started) * 1000, 2)
    return tools.respond(tool, event, effect, text, warning=warning), entry


def settle(code):
    """Make ``code`` the process's exit status even when stdout is a broken pipe.

    A buffered stdout that failed to flush is flushed again by the interpreter at shutdown, and a failure there
    turns the exit status into 120: a blocking 2 would read as an ordinary hook failure. Flush now, and if that
    fails leave without the second attempt."""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except BaseException:  # noqa: B036
        os._exit(code)
    return code


def main(argv=None, stdin=None, stdout=None):
    """Entry point. Returns 0, or 2 (the hosts' blocking status) when a fail-closed verdict could not be delivered.
    The process's own exit status is settled here when this writes to the real stdout."""
    code = _main(argv, stdin, stdout)
    return settle(code) if stdout is None else code


def _main(argv=None, stdin=None, stdout=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    options = None
    tool = argv[0] if argv else ""
    out = stdout if stdout is not None else sys.stdout
    try:
        options = Options(argv)
        stream = stdin if stdin is not None else sys.stdin
        raw = stream.buffer.read(MAX_STDIN_BYTES + 1) if hasattr(stream, "buffer") else stream.read(
            MAX_STDIN_BYTES + 1)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if len(raw) > MAX_STDIN_BYTES:
            raise ValueError("the hook input is larger than 4 MiB")
        payload = json.loads(raw)
        text, entry = decide(options, payload)
        try:
            if text:
                out.write(text + "\n")
            out.flush()
        except BaseException:  # noqa: B036 - the verdict could not be delivered
            if _strict(argv) or options.fail_closed:
                _block_stderr("the verdict could not be written")
                return 2  # the host's blocking status: a verdict that never arrived must not read as allow
            return 0
    except BaseException as exc:  # noqa: B036 - nothing may escape: a traceback or exit code 2 would block the tool
        # Options may be the thing that failed: the command line itself still says whether to fail closed.
        delivered = _failed(tool, options, exc, out, _strict(argv))
        if not delivered and (_strict(argv) or (options and options.fail_closed)):
            _block_stderr("the verdict could not be written")
            return 2
        return 0
    # The verdict is out. Everything below is bookkeeping and must not change it.
    try:
        if entry is not None:
            append_log(entry)
            if receiver_expected(options):
                spawn_report(options, entry)
    except BaseException:  # noqa: B036
        pass
    return 0


def _strict(argv):
    """Does the command line itself bind this hook (``--fail-closed``, or a required system policy)? Read from argv, so
    it holds even when option parsing or anything after it failed; the same answer as every other entry point."""
    try:
        from briefcase.policy import optargs

        return optargs.strict(list(argv)[1:])
    except BaseException:  # noqa: B036 - the reading itself failed: any mention of a binding option binds
        return any("--fail-closed" in a or "--require-policy-sha256" in a for a in argv)


def _block_stderr(why):
    """The reason, on stderr and first: Codex takes a nonempty stderr with exit 2 as its block. When stdout and
    stderr both fail nothing can be shown, and exit status 2 is all that is left."""
    try:
        sys.stderr.write(f"Blocked by briefcase-ai policy: {why}, and the hook is fail-closed or bound to a required "
                         f"policy. {FIX}\n")
        sys.stderr.flush()
    except BaseException:  # noqa: B036
        pass


def _failed(tool, options, exc, out, argv_fail_closed=False):
    from briefcase.policy import tools
    fail_closed = bool(options and options.fail_closed) or bool(argv_fail_closed)
    problem = f"the hook failed ({type(exc).__name__}: {exc})"
    warn = False
    try:
        warn = record_problem(problem, tool, fail_closed)
        append_log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tool": tool, "kind": "error", "error": problem,
                    "effect": "deny" if fail_closed else "allow", "rule": "fail_closed" if fail_closed else "fail_open",
                    "source": "setting"})
    except BaseException:  # noqa: B036
        pass
    try:
        if fail_closed and tool not in tools.TOOLS:
            out.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                         "permissionDecisionReason": f"Blocked: {problem}"}}) + "\n")
        elif fail_closed:
            event = "beforeShellExecution" if tool == "cursor" else "PreToolUse"
            text = (f"Blocked by briefcase-ai policy: {problem}. The hook was installed with --fail-closed, "
                    f"so nothing runs. {FIX}")
            out.write(tools.respond(tool, event, "deny", text) + "\n")
        else:
            text = tools.fail_open_output(tool)
            if not text and warn and tool in tools.TOOLS:
                text = json.dumps({"systemMessage": f"briefcase-ai policy: {problem}. This call was allowed. {FIX}"})
            if text:
                out.write(text + "\n")
        out.flush()
    except BaseException:  # noqa: B036
        return False
    return True


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
