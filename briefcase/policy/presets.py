"""The built-in rule sets: ``observe``, ``guarded`` and ``strict``.

A policy file names one with ``preset = "guarded"``; its own ``[[rule]]``
entries are checked first, then the preset's. ``disable = ["<id>"]`` in the
user policy switches a preset rule off. ``briefcase-ai policy check`` lists every
rule with its id.

- ``observe``: no rules. Every decision is logged and nothing is blocked.
- ``guarded``: deny reading secret files and running destructive commands;
  ask before package installs and commands that can send data out.
- ``strict``: ``guarded``, with the asks for sending data out turned into
  denies, plus: ask before ``sudo``, before writing outside the project,
  before any MCP tool and before any web fetch.

The rules that need to understand a command (is this ``rm`` recursive and
aimed at ``/``?) are Python predicates here rather than patterns, because a
pattern language that could express them would not be readable.
"""

from __future__ import annotations

import os

from briefcase.policy.model import Rule

# Files that hold credentials. Globs are matched against the absolute path
# after `..` and symlinks are resolved; a pattern with no leading `/` or `~`
# matches at any depth.
SECRET_PATHS = (
    "~/.ssh", "~/.gnupg", "~/.aws/credentials", "~/.aws/sso/cache", "~/.aws/cli/cache",
    "~/.config/gcloud", "~/.azure", "~/.kube/config", "~/.docker/config.json", "~/.netrc", "~/.npmrc",
    "~/.pypirc", "~/.git-credentials", "~/.config/gh/hosts.yml", "~/.terraform.d/credentials.tfrc.json",
    "~/.claude/.credentials.json", "~/.codex/auth.json", "~/.config/op", "~/.password-store",
    ".env", ".env.*", "*.pem", "*.p12", "*.pfx", "*.keystore", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "secrets.json", "secrets.yaml", "secrets.yml", "credentials.json", "service-account*.json",
)
SECRET_EXCEPT = (
    "~/.ssh/*.pub", "~/.ssh/known_hosts", "~/.ssh/config", ".env.example", ".env.sample", ".env.template",
    ".env.dist", ".env.defaults", "*.pub.pem", "cacert.pem", "ca-bundle.pem", "ca-certificates.pem",
)

# What a deny must survive: the agent rewriting the policy, the hook
# configuration that calls it, or briefcase's own state. Writes only; reading
# these files is fine.
PROTECTED_PATHS = (
    "~/.briefcase", ".briefcase/policy.toml",
    "~/.claude/settings.json", ".claude/settings.json", ".claude/settings.local.json",
    "~/.cursor/hooks.json", ".cursor/hooks.json",
    "~/.codex/hooks.json", "~/.codex/config.toml", ".codex/hooks.json", ".codex/config.toml",
)
SHELL_NAMES = ("sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "fish")
_SYSTEM_DIRS = ("/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64", "/opt", "/proc", "/root",
                "/sbin", "/sys", "/usr", "/var", "/Users", "/System", "/Library", "/Applications", "/private",
                "/Volumes")


def _rule(rule_id, tool, effect, reason, source="preset", **matchers):
    kinds = {"file": ("read", "write")}.get(tool, (tool,))
    return Rule(rule_id, kinds, effect, reason, source=source,
                command=matchers.get("command", ()), paths=matchers.get("paths", ()),
                excepts=matchers.get("excepts", ()), outside=matchers.get("outside", ()),
                server=matchers.get("server", ()), name=matchers.get("name", ()),
                hosts=matchers.get("hosts", ()), check=matchers.get("check"))


def installed_package_dir():
    """The installed ``briefcase`` package, when it lives in ``site-packages``.
    A source checkout is left out so the SDK itself can be worked on."""
    package = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parent = os.path.basename(os.path.dirname(package))
    return package if parent in ("site-packages", "dist-packages") else None


def protection(settings):
    """Rules that guard the policy itself. They are checked before everything
    else and no rule can pre-empt them; ``protect_self = false`` in the user
    policy removes them."""
    paths = list(PROTECTED_PATHS)
    package = installed_package_dir()
    if package:
        paths.append(package)       # the hook's own code
    return [
        _rule("self.files", "write", "deny",
              "This file is part of the briefcase-ai policy or the hook configuration that enforces it, and the "
              "agent may not change it. Edit it yourself, or turn enforcement off with `briefcase-ai policy off`.",
              source="built-in", paths=paths),
        _rule("self.commands", "shell", "deny",
              "This command would turn off or rewrite the briefcase-ai policy. Run it yourself in a terminal.",
              source="built-in", check="self_command"),
    ]


def rules(preset, settings):
    if preset == "observe":
        return []
    strict = preset == "strict"
    exfil = "deny" if strict else "ask"
    out = [
        _rule("secrets.files", "file", "deny",
              "This path matches the built-in list of credential files (SSH keys, cloud credentials, .env). "
              "To allow one path, add an allow rule for it above the preset in your policy.",
              paths=SECRET_PATHS, excepts=SECRET_EXCEPT),
        _rule("shell.rm-root", "shell", "deny",
              "Recursive delete of a root, home or system directory.", check="rm_root"),
        _rule("shell.disk", "shell", "deny",
              "Writes directly to a disk device or reformats one.", check="disk_destroy"),
        _rule("shell.pipe-to-shell", "shell", "deny",
              "Downloads a script and runs it without anyone reading it. Download it, read it, then run it.",
              check="pipe_to_shell"),
        _rule("git.force-push-protected", "shell", "deny",
              "Force-push (or delete) of a protected branch.", check="git_force_protected"),
        _rule("git.force-push", "shell", "ask",
              "Force-push rewrites history on the remote.", check="git_force"),
        _rule("pkg.install", "shell", "ask",
              "Installs packages, which runs code from the internet on this machine.",
              command=(
                  "pip|pip3|pipx install", "python*|py -m pip install", "uv pip install", "uv add|sync",
                  "poetry add|install", "pdm add|install", "conda|mamba install",
                  "npm install|i|add|ci|exec", "pnpm install|i|add|dlx", "yarn add|install|dlx",
                  "bun add|install|x", "npx", "bunx", "pnpx", "uvx",
                  "cargo install|add", "go install|get", "gem install", "bundle install|add",
                  "brew install|upgrade|tap", "apt|apt-get install|upgrade", "dnf|yum install",
                  "apk add", "pacman -S", "snap install", "composer require|install", "dotnet add",
              )),
        _rule("net.exfil", "shell", exfil,
              "Can send local data to another machine.",
              command=(
                  "curl -d|--data|--data-binary|--data-raw|--data-urlencode|-F|--form|--form-string"
                  "|-T|--upload-file|--json",
                  "curl -X|--request POST|PUT|PATCH", "curl -XPOST|-XPUT|-XPATCH",
                  "wget --post-data|--post-file|--body-data|--body-file",
                  "nc|ncat|netcat|socat", "scp", "sftp", "ftp", "rsync *:*", "rclone",
                  "aws s3 cp|sync|mv", "gsutil cp|rsync|mv", "az storage",
                  "gh gist create",
              )),
    ]
    if strict:
        out += [
            _rule("shell.sudo", "shell", "ask", "Runs with elevated privileges.", command=("sudo|doas|su",)),
            _rule("write.outside-project", "write", "ask",
                  "Writes outside the project directory.", outside=("${project}", "${tmp}")),
            _rule("mcp.any", "mcp", "ask",
                  "Every MCP tool call needs confirmation under the strict preset. Add an allow rule for the "
                  "servers you trust."),
            _rule("web.any", "web", "ask",
                  "Every web request needs confirmation under the strict preset. Add an allow rule for the "
                  "hosts you trust."),
        ]
    return out


# --- predicates for the shell rules -------------------------------------------
# Each takes (cmd, ctx) and returns a short detail string when it matches.

def _flags(cmd):
    """``(short letters, long options)`` given before ``--``."""
    short, long_ = set(), set()
    for arg, dynamic in zip(cmd.argv[1:], cmd.dyn[1:]):
        if dynamic:
            continue
        if arg == "--":
            break
        if arg.startswith("--"):
            long_.add(arg.split("=", 1)[0])
        elif arg.startswith("-") and len(arg) > 1:
            short.update(arg[1:])
    return short, long_


def _targets(cmd):
    from briefcase.policy.shell import _positionals
    return _positionals(cmd)


def _is_root_like(text, cmd, home):
    from briefcase.policy.shell import normalize_path
    if text in ("/*", "~/*", "~", "~/", "/", "$HOME", "${HOME}", "$HOME/", "$HOME/*", ".*", "*"):
        return True
    stripped = text[:-2] if text.endswith("/*") else text
    path = normalize_path(stripped, cmd.cwd, home)
    if path is None:
        return False
    if path in _SYSTEM_DIRS or path == os.path.normpath(home):
        return True
    return os.path.dirname(path) in ("/home", "/Users")     # someone's home directory


def rm_root(cmd, ctx):
    name = cmd.name
    home = cmd.home or ctx.home
    if name == "find" and cmd.find_delete:
        for text, dynamic in _targets(cmd)[:1]:
            if not dynamic and _is_root_like(text, cmd, home):
                return f"find {text} -delete"
        return None
    if name != "rm":
        return None
    short, long_ = _flags(cmd)
    if not ({"r", "R"} & short or "--recursive" in long_):
        return None
    if "--no-preserve-root" in long_:
        return "rm --no-preserve-root"
    for text, dynamic in _targets(cmd):
        if not dynamic and _is_root_like(text, cmd, home):
            return f"rm -r {text}"
    return None


def disk_destroy(cmd, ctx):
    name = cmd.name
    if name == "dd":
        for arg, dynamic in zip(cmd.argv[1:], cmd.dyn[1:]):
            if not dynamic and arg.startswith("of=/dev/") and arg not in ("of=/dev/null", "of=/dev/stdout",
                                                                          "of=/dev/stderr"):
                return arg
        return None
    if name.startswith("mkfs") or name in ("wipefs", "fdisk", "sfdisk", "parted", "mkswap"):
        return name
    if name == "diskutil" and any(a.lower().startswith(("erase", "partitiondisk", "zerodisk", "securerase"))
                                  for a in cmd.argv[1:]):
        return "diskutil erase"
    if name == "shred" and any(a.startswith("/dev/") for a in cmd.argv[1:]):
        return "shred a device"
    for op, target, dynamic in cmd.redirs:
        disks = ("/dev/sd", "/dev/nvme", "/dev/disk", "/dev/hd", "/dev/vd", "/dev/mmcblk")
        if not dynamic and op in (">", ">>", ">|", "&>") and target.startswith(disks):
            return f"redirect to {target}"
    return None


_FETCHERS = ("curl", "wget", "fetch", "http", "https", "aria2c")


def _reads_code_from_stdin(cmd):
    """``sh``, ``bash -s`` or a bare ``python``: the program is whatever arrives on stdin.
    ``python -c '...'`` and ``sh script.sh`` are not: there stdin is data."""
    from briefcase.policy import shell
    name = cmd.name
    args = [a for a, d in zip(cmd.argv[1:], cmd.dyn[1:]) if not d]
    if name in SHELL_NAMES:
        short = "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))
        if "c" in short:
            return False
        return "s" in short or not any(not a.startswith("-") for a in args)
    if shell.interpreter_of(name):
        return cmd.opaque is None and not any(not a.startswith("-") for a in args)
    return False


def _fetches(cmd):
    from briefcase.policy import shell
    # `sudo curl ...`, `env X=1 wget ...`: look past leading wrapper words.
    return any(not dynamic and shell.command_name(arg) in _FETCHERS
               for arg, dynamic in zip(cmd.argv[:4], cmd.dyn[:4]))


def pipe_to_shell(cmd, ctx):
    name = cmd.name
    if _reads_code_from_stdin(cmd):
        feeder = cmd.prev
        while feeder is not None:
            if _fetches(feeder):
                return f"{feeder.name or feeder.argv[0]} ... | {name}"
            feeder = feeder.prev
    # bash -c "$(curl ...)", eval "$(curl ...)", source <(curl ...)
    if name in SHELL_NAMES or name in ("eval", "source", "."):
        for sub in cmd.subs:
            if _fetches(sub):
                return f"{name} runs the output of {sub.name or sub.argv[0]}"
    return None


def _git_push(cmd):
    """``(flags, positionals after "push")`` when ``cmd`` is a git push, else ``None``."""
    if cmd.name != "git":
        return None
    argv, dyn = cmd.argv, cmd.dyn
    i = 1
    while i < len(argv) and not dyn[i] and argv[i].startswith("-"):
        i += 2 if argv[i] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path") else 1
    if i >= len(argv) or dyn[i] or argv[i] != "push":
        return None
    flags, rest = [], []
    literal = False
    for arg, dynamic in zip(argv[i + 1:], dyn[i + 1:]):
        if dynamic:
            rest.append(None)
        elif not literal and arg == "--":
            literal = True
        elif not literal and arg.startswith("-"):
            flags.append(arg)
        else:
            rest.append(arg)
    return flags, rest


def _push_plan(cmd, ctx):
    """``(forced, branches)``; a branch of ``None`` means "could not tell"."""
    parsed = _git_push(cmd)
    if parsed is None:
        return None
    flags, rest = parsed
    forced = False
    everything = False
    deleting = False
    for flag in flags:
        name = flag.split("=", 1)[0]
        if name in ("--force", "--force-with-lease", "--force-if-includes") or (
                not flag.startswith("--") and "f" in flag[1:]):
            forced = True
        if name in ("--mirror", "--all", "--prune"):
            everything = True
        if name == "--delete" or (not flag.startswith("--") and "d" in flag[1:]):
            deleting = True
    refspecs = rest[1:]            # rest[0] is the remote
    branches = []
    for spec in refspecs:
        if spec is None:
            branches.append(None)
            continue
        if spec.startswith("+"):
            forced = True
            spec = spec[1:]
        if ":" in spec:
            src, dst = spec.split(":", 1)
            if not src:
                deleting = True
            spec = dst
        if spec.startswith("refs/heads/"):
            spec = spec[len("refs/heads/"):]
        branches.append(_current_branch(cmd.cwd) if spec in ("HEAD", "@") else spec)
    if not refspecs:
        branches.append(_current_branch(cmd.cwd))
    if everything and (forced or deleting or "--mirror" in flags):
        return True, ["*"]
    return forced or deleting, branches


def _current_branch(cwd):
    """The checked-out branch of the repository containing ``cwd``, read from ``.git/HEAD``."""
    current = cwd
    for _ in range(40):
        if not current:
            return None
        head = os.path.join(current, ".git", "HEAD")
        try:
            with open(head, "r", encoding="utf-8") as handle:
                line = handle.readline().strip()
            return line[len("ref: refs/heads/"):] if line.startswith("ref: refs/heads/") else None
        except OSError:
            pass
        try:
            # A worktree or submodule: `.git` is a file pointing at the real directory.
            with open(os.path.join(current, ".git"), "r", encoding="utf-8") as handle:
                pointer = handle.readline().strip()
            if pointer.startswith("gitdir:"):
                with open(os.path.join(current, pointer[7:].strip(), "HEAD"), "r", encoding="utf-8") as handle:
                    line = handle.readline().strip()
                return line[len("ref: refs/heads/"):] if line.startswith("ref: refs/heads/") else None
        except OSError:
            pass
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    return None


def git_force_protected(cmd, ctx):
    import fnmatch
    plan = _push_plan(cmd, ctx)
    if plan is None or not plan[0]:
        return None
    for branch in plan[1]:
        if branch == "*":
            return "force-push of every branch"
        if branch is None:
            continue
        for pattern in ctx.settings.get("protected_branches", ()):
            if fnmatch.fnmatchcase(branch, pattern):
                return f"force-push to {branch}"
    return None


def git_force(cmd, ctx):
    plan = _push_plan(cmd, ctx)
    if plan is None or not plan[0]:
        return None
    named = [b for b in plan[1] if b]
    return "force-push to " + (", ".join(named) if named else "a branch that could not be determined")


def self_command(cmd, ctx):
    """``briefcase-ai policy off|init`` and ``briefcase-ai connect --undo|--managed``, however briefcase is started."""
    argv = [a for a, d in zip(cmd.argv, cmd.dyn) if not d]
    name = cmd.name
    rest = None
    if name in ("briefcase-ai", "briefcase-ai.exe"):
        rest = argv[1:]
    elif name.startswith(("python", "py")) or name in ("uv", "uvx", "pipx"):
        for index, arg in enumerate(argv):
            if arg in ("briefcase.cli", "briefcase.cli.__main__", "briefcase-ai") and index > 0:
                rest = argv[index + 1:]
                break
    if name.startswith("pip") or "pip" in argv[1:3] or name == "uv":
        if "uninstall" in argv and any(a in ("briefcase-ai", "briefcase_ai") for a in argv):
            return "uninstall briefcase-ai"
    if name == "claude":
        # A nested Claude Code that skips user hooks (`claude --help`, 2.1.330): --safe-mode
        # disables every non-managed hook, --setting-sources can leave out the user settings,
        # and --settings can load `disableAllHooks`.
        for arg in argv[1:]:
            flag = arg.split("=", 1)[0]
            if flag in ("--safe-mode", "--setting-sources", "--settings"):
                return f"claude {flag} (runs Claude Code without the user's hooks)"
    if rest is None:
        return None
    words = [a for a in rest if not a.startswith("-")]
    if words[:1] == ["policy"] and len(words) > 1 and words[1] in ("off", "init"):
        return "briefcase-ai policy " + words[1]
    if words[:1] == ["connect"] and ("--undo" in rest or "--enforce" in rest):
        return "briefcase-ai connect " + ("--undo" if "--undo" in rest else "--enforce")
    return None


CHECKS = {
    "rm_root": rm_root, "disk_destroy": disk_destroy, "pipe_to_shell": pipe_to_shell,
    "git_force_protected": git_force_protected, "git_force": git_force, "self_command": self_command,
}
