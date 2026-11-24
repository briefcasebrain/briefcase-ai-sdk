"""The hook's command line, read once and the same way by every entry point and every failure handler.

Standard library only and no import of anything else in ``briefcase``: the launcher loads this file by path, before
it knows whether the rest of the package can be imported at all. ``--opt=value`` becomes ``--opt value``; an option
that takes a value and has none (or an empty one) is a problem, and its value is never taken from the next option;
and :attr:`Normalized.strict` is the single answer to "does the command line itself bind this hook": ``--fail-closed``
anywhere (even where a value-taking option would have claimed the token), or a required policy hash in either
spelling, with or without a value."""

VALUE_OPTIONS = ("--endpoint", "--require-policy-sha256")
STRICT_FLAG = "--fail-closed"
REQUIRE = "--require-policy-sha256"


class Normalized(object):
    __slots__ = ("argv", "strict", "require_given", "problems")

    def __init__(self, argv, strict, require_given, problems):
        self.argv = argv
        self.strict = strict
        self.require_given = require_given
        self.problems = problems


def normalize(argv):
    tokens = [a if isinstance(a, str) else str(a) for a in argv]
    out = tokens[:1]  # the tool
    problems = []
    require_given = False
    strict = False
    index = 1
    while index < len(tokens):
        token = tokens[index]
        name, eq, value = token.partition("=") if token.startswith("--") else (token, "", "")
        if name == STRICT_FLAG:
            strict = True
        if name in VALUE_OPTIONS:
            if name == REQUIRE:
                require_given = strict = True
            if not eq:
                if index + 1 < len(tokens) and not tokens[index + 1].startswith("--"):
                    index += 1
                    value = tokens[index]
                else:
                    value = ""
            if not value.strip():
                problems.append(f"{name} has no value")  # never swallow the next option as its value
            else:
                out += [name, value]
        else:
            out.append(token)
        index += 1
    if any(t == STRICT_FLAG or t.startswith(STRICT_FLAG + "=") for t in tokens[1:]):
        strict = True  # even where an option's value swallowed it
    return Normalized(out, strict, require_given, problems)


def strict(argv):
    return normalize(argv).strict
