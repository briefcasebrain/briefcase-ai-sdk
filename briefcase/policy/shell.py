"""Shell command analysis for policy rules: what would this line actually run?

A rule that greps the command text is bypassed by a quote (``r'm' -rf /``), a
chain (``true && rm -rf /``), a substitution (``echo $(rm -rf /)``) or a
wrapper (``sudo env X=1 rm -rf /``). So the command line is lexed the way a
POSIX shell would lex it and reduced to *simple commands*, each an argv list:

- quoting and escapes are removed (``'..'``, ``".."``, ``$'..'``, ``\\x``)
- ``;``, ``&&``, ``||``, ``|``, ``&``, newlines, ``( )`` and ``{ }`` split commands
- ``$(...)``, backticks, ``<(...)`` and here-documents are analysed recursively
- ``VAR=x`` prefixes are stripped, literal ``VAR=x; $VAR`` is resolved
- ``sudo``, ``env``, ``nohup``, ``timeout``, ``xargs``, ``find -exec``,
  ``sh -c '...'`` and ``eval '...'`` are unwrapped to the command they run
- ``cd dir && ...`` is followed, so relative paths resolve where they will run

What cannot be known statically is reported instead of guessed: a command
name computed at run time, ``eval`` of a variable, text piped into a shell.
Those lines are *unparseable* and the policy's ``on_unparseable`` setting
decides. Inline interpreter code (``python -c``) is kept as opaque text.

What this cannot see, by construction: shell aliases and functions defined in
the user's profile, the contents of a script file that is executed, and
anything an interpreter does once it runs.

Standard library only; imported by the hook on every shell tool call.
"""

from __future__ import annotations

import fnmatch
import os

MAX_LENGTH = 64 * 1024
MAX_DEPTH = 8
MAX_COMMANDS = 400

SHELLS = frozenset(("sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "fish", "csh", "tcsh"))
# interpreter -> flags whose next argument is code
INLINE_CODE = {
    "python": ("-c",), "node": ("-e", "--eval", "-p", "--print"), "nodejs": ("-e", "--eval", "-p", "--print"),
    "deno": ("eval",), "bun": ("-e", "--eval"), "perl": ("-e", "-E"), "ruby": ("-e",), "php": ("-r",),
    "lua": ("-e",), "osascript": ("-e",), "pwsh": ("-c", "-Command", "-EncodedCommand", "-e", "-ec"),
    "powershell": ("-c", "-Command", "-EncodedCommand", "-e", "-ec"), "Rscript": ("-e",),
}
READ_ONLY = frozenset((
    "cat", "less", "more", "head", "tail", "grep", "egrep", "fgrep", "rg", "ag", "ack", "ls", "ll", "stat",
    "file", "wc", "diff", "cmp", "bat", "jq", "cut", "sort", "uniq", "nl", "od", "xxd", "hexdump", "strings",
    "md5sum", "sha1sum", "sha256sum", "sha512sum", "shasum", "cksum", "readlink", "realpath", "basename",
    "dirname", "test", "[", "[[", "du", "tree", "echo", "printf", "cd", "pushd", "which", "type", "base64",
    "zcat", "bzcat", "xzcat", "column", "tac", "rev", "fold", "source", ".",
))
_KEYWORD_PREFIX = frozenset(("if", "then", "elif", "else", "do", "while", "until", "!", "{", "time", "coproc"))
_KEYWORD_ALONE = frozenset(("fi", "done", "esac", "}", "in", "then", "else", "do", "{"))
_DECLARATIONS = frozenset(("for", "select", "case", "function"))
_ASSIGNERS = frozenset(("export", "declare", "local", "readonly", "typeset"))
_GLOB_CHARS = frozenset("*?[")
_NAME_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_NAME_CHARS = _NAME_START | frozenset("0123456789")
_ANSI = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
         "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}

# option -> takes a value, for the wrappers that are unwrapped
_SUDO_VALUE = frozenset(("-u", "-g", "-h", "-p", "-C", "-r", "-t", "-T", "-U", "-D", "-R", "--user", "--group",
                         "--host", "--prompt", "--chdir", "--role", "--type", "--other-user", "--close-from"))
_XARGS_VALUE = frozenset(("-I", "-L", "-n", "-P", "-s", "-E", "-d", "-a", "--max-args", "--max-procs",
                          "--max-lines", "--max-chars", "--delimiter", "--arg-file", "--replace", "--eof"))
_TIMEOUT_VALUE = frozenset(("-s", "--signal", "-k", "--kill-after"))


class Word(object):
    __slots__ = ("text", "dynamic", "glob", "quoted", "subs", "name_literal")

    def __init__(self, text="", dynamic=False, glob=False, quoted=False, subs=None, name_literal=False):
        self.text = text
        self.dynamic = dynamic
        self.glob = glob
        self.quoted = quoted
        self.subs = subs or []
        # True when the part after the last "/" is literal even though the word
        # is dynamic, as in "$VIRTUAL_ENV/bin/python": the command name is known.
        self.name_literal = name_literal


class Cmd(object):
    """One simple command. ``argv`` is what would be exec'd; ``dyn[i]`` is true
    when ``argv[i]`` depends on something only known at run time."""

    __slots__ = ("argv", "dyn", "glob", "redirs", "cwd", "subs", "stdin", "prev", "via", "opaque",
                 "find_delete", "home")

    def __init__(self, argv, dyn, glob, redirs=None, cwd=None, subs=None, stdin=None, via=None, home=None):
        self.argv = argv
        self.dyn = dyn
        self.glob = glob
        self.redirs = redirs or []
        self.cwd = cwd
        self.subs = subs or []
        self.stdin = stdin
        self.prev = None
        self.via = via or []
        self.opaque = None
        self.find_delete = False
        self.home = home

    @property
    def name(self):
        if not self.argv or self.dyn[0]:
            return ""
        return command_name(self.argv[0])

    def text(self):
        """The argv as a person would type it: a literal argument with spaces or shell
        operators is quoted, so ``bash -c 'cd / && x'`` does not read as two commands."""
        out = []
        for arg, dynamic in zip(self.argv, self.dyn):
            if not dynamic and (not arg or any(ch in arg for ch in " \t\n;&|<>()'\"`")):
                arg = "'" + arg.replace("'", "'\"'\"'") + "'"
            out.append(arg)
        return " ".join(out)

    def derive(self, argv, dyn, glob, via):
        cmd = Cmd(argv, dyn, glob, cwd=self.cwd, via=self.via + [via], home=self.home)
        cmd.prev = self.prev
        cmd.subs = self.subs
        cmd.stdin = self.stdin
        return cmd


def command_name(arg0):
    name = arg0.replace("\\", "/").rsplit("/", 1)[-1]
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name


def interpreter_of(name):
    """``python3.12`` -> ``python``; ``None`` when ``name`` is not an interpreter we know."""
    for base in INLINE_CODE:
        if name == base or (name.startswith(base) and name[len(base):].replace(".", "").isdigit()):
            return base
    return None


class Analysis(object):
    """Every simple command a line would run, flattened, plus what could not be analysed."""

    __slots__ = ("commands", "problems", "source")

    def __init__(self, source):
        self.commands = []
        self.problems = []
        self.source = source

    def problem(self, text):
        if text not in self.problems:
            self.problems.append(text)


class _State(object):
    __slots__ = ("cwd", "vars", "home", "out", "seen")

    def __init__(self, cwd, home, out):
        self.cwd = cwd
        self.home = home
        self.vars = {}
        self.out = out
        self.seen = set()

    def lookup(self, name):
        """Value of ``$name`` when it is certain, else ``None``."""
        if name in self.vars:
            return self.vars[name]
        if name == "HOME":
            return self.home
        if name == "PWD":
            return self.cwd
        return None


def analyze(command, cwd=None, home=None):
    """Analyse one command line. Never raises."""
    out = Analysis(command)
    home = home or os.path.expanduser("~")
    if not isinstance(command, str):
        out.problem("the command is not text")
        return out
    if len(command) > MAX_LENGTH:
        out.problem(f"the command is longer than {MAX_LENGTH} characters")
        return out
    if "\x00" in command:
        out.problem("the command contains a NUL byte")
        return out
    state = _State(cwd, home, out)
    try:
        _Lexer(command, state, 0).run()
    except RecursionError:  # pragma: no cover - guarded by MAX_DEPTH
        out.problem("the command nests too deeply")
    except Exception as exc:  # the lexer must never take the hook down
        out.problem(f"the command could not be analysed ({type(exc).__name__})")
    return out


def _analyze_nested(text, state, depth, via=None, prev=None, cwd=None):
    """Analyse a nested command string; returns the top-level commands it runs."""
    if depth > MAX_DEPTH:
        state.out.problem("the command nests too deeply")
        return []
    saved_cwd = state.cwd
    if cwd is not None:
        state.cwd = cwd
    lexer = _Lexer(text, state, depth, via=via, prev=prev)
    lexer.run()
    # A substitution or `sh -c` runs in a subshell: its `cd` does not leak out.
    state.cwd = saved_cwd
    return lexer.top


class _Lexer(object):
    def __init__(self, text, state, depth, via=None, prev=None):
        self.s = text
        self.n = len(text)
        self.i = 0
        self.state = state
        self.depth = depth
        self.via = via or []
        self.inherit_prev = prev
        self.top = []          # commands at this nesting level
        self.words = []        # words of the simple command being read
        self.redirs = []       # its redirections: (op, Word)
        self.pipeline = []     # commands of the pipeline being read
        self.parts = None      # the word being read: list of (text, quoted)
        self.dynamic = False
        self.dyn_at = 0        # length of the word when it last became dynamic
        self.subs = []
        self.redir = None      # a redirection operator waiting for its target
        self.body_at = None    # where the next here-document body starts
        self.skip = None       # (index of the newline before the bodies, index after them)
        self.paren = 0
        self.cwd_stack = []    # working directory to restore when a ( subshell ) closes

    # -- words -----------------------------------------------------------------
    def _add(self, text, quoted):
        if self.parts is None:
            self.parts = []
        self.parts.append((text, quoted))

    def _mark_dynamic(self):
        self.dynamic = True
        self.dyn_at = sum(len(t) for t, _ in self.parts) if self.parts else 0

    def _end_word(self):
        if self.parts is None:
            return
        parts, dynamic, subs, dyn_at = self.parts, self.dynamic, self.subs, self.dyn_at
        self.parts, self.dynamic, self.subs, self.dyn_at = None, False, [], 0
        quoted = any(q for _, q in parts)
        glob = any(not q and _GLOB_CHARS.intersection(t) for t, q in parts)
        text = "".join(t for t, _ in parts)
        name_literal = dynamic and text.rfind("/") >= dyn_at and not text.endswith("/")
        if parts and not parts[0][1] and parts[0][0].startswith("~"):
            text, unknown_home = self._tilde(text, parts[0][0])
            if unknown_home:
                dynamic, name_literal = True, "/" in text and not text.endswith("/")
        word = Word(text, dynamic, glob, quoted, subs, name_literal)
        if self.redir is not None:
            op, self.redir = self.redir, None
            if op in ("<<", "<<-"):
                body = Word("", subs=subs)
                self._heredoc(text, op == "<<-", quoted, body)
                self.redirs.append((op, body))
            else:
                self.redirs.append((op, word))
            return
        if not quoted and not dynamic and "{" in text and "}" in text:
            expanded = brace_expand(text)
            if expanded is None:
                word.dynamic = True
            elif len(expanded) > 1:
                self.words.extend(Word(item, False, glob, False, subs if not index else [])
                                  for index, item in enumerate(expanded))
                return
        self.words.append(word)

    def _tilde(self, text, first):
        prefix = first.split("/", 1)[0]
        home = self.state.lookup("HOME")
        if prefix == "~":
            if home is None:
                return text, True
            return home + text[1:], False
        if prefix == "~+" and self.state.cwd:
            return self.state.cwd + text[2:], False
        return text, True  # ~user, ~-: not known here

    # -- quoting and expansion -------------------------------------------------
    def _dquote(self, i, closing='"'):
        """Read a double-quoted body from ``i``; returns the index after the closing quote."""
        s, n = self.s, self.n
        start = i
        while i < n:
            c = s[i]
            if closing and c == closing:
                self._add(s[start:i], True)
                return i + 1
            if c == "\\" and i + 1 < n and s[i + 1] in '$`"\\\n':
                self._add(s[start:i], True)
                if s[i + 1] != "\n":
                    self._add(s[i + 1], True)
                i += 2
                start = i
            elif c == "$":
                self._add(s[start:i], True)
                i = self._dollar(i, True)
                start = i
            elif c == "`":
                self._add(s[start:i], True)
                i = self._backtick(i)
                start = i
            else:
                i += 1
        self._add(s[start:i], True)
        if closing:
            self.state.out.problem("a double quote is never closed")
        return i

    def _ansi_c(self, i):
        """``$'...'`` starting after the opening quote."""
        s, n = self.s, self.n
        out = []
        unknown = False
        while i < n and s[i] != "'":
            c = s[i]
            if c != "\\" or i + 1 >= n:
                out.append(c)
                i += 1
                continue
            e = s[i + 1]
            i += 2
            if e in _ANSI:
                out.append(_ANSI[e])
            elif e in "xuU":
                width = {"x": 2, "u": 4, "U": 8}[e]
                digits = ""
                while i < n and len(digits) < width and s[i] in "0123456789abcdefABCDEF":
                    digits += s[i]
                    i += 1
                try:
                    out.append(chr(int(digits, 16)) if digits else "\\" + e)
                except (ValueError, OverflowError):
                    unknown = True
            elif e in "01234567":
                digits = e
                while i < n and len(digits) < 3 and s[i] in "01234567":
                    digits += s[i]
                    i += 1
                out.append(chr(int(digits, 8) & 0xFF))
            else:  # \cX and anything not known here
                unknown = True
        if i >= n:
            self.state.out.problem("a $'...' string is never closed")
        self._add("".join(out), True)
        if unknown:
            self._mark_dynamic()
        return i + 1

    def _matching(self, i, opener, closer):
        """Index of the ``closer`` matching the ``opener`` just before ``i``; -1 when missing."""
        s, n = self.s, self.n
        level = 1
        while i < n:
            c = s[i]
            if c == "\\":
                i += 2
                continue
            if c == "'":
                j = s.find("'", i + 1)
                if j < 0:
                    return -1
                i = j + 1
                continue
            if c == opener:
                level += 1
            elif c == closer:
                level -= 1
                if level == 0:
                    return i
            i += 1
        return -1

    def _substitution(self, i):
        """``$(`` or ``<(`` body starting at ``i``; returns the index after ``)``."""
        self._mark_dynamic()
        if self.depth + 1 > MAX_DEPTH:
            self.state.out.problem("the command nests too deeply")
            return self.n
        saved_cwd = self.state.cwd
        sub = _Lexer(self.s, self.state, self.depth + 1, via=self.via)
        sub.i = i
        sub.run(until=")")
        self.state.cwd = saved_cwd   # a substitution is a subshell: its `cd` does not leak
        self.subs.extend(sub.top)
        return sub.i

    def _dollar(self, i, in_dq):
        s, n = self.s, self.n
        nxt = s[i + 1] if i + 1 < n else ""
        if nxt == "(":
            self._add("", in_dq)
            if s[i + 2:i + 3] == "(":
                j = s.find("))", i + 3)
                self._mark_dynamic()
                if j < 0:
                    self.state.out.problem("an arithmetic expansion is never closed")
                    return n
                inner = s[i + 3:j]
                if "$(" in inner or "`" in inner:
                    self._scan(inner)
                return j + 2
            return self._substitution(i + 2)
        if nxt == "{":
            j = self._matching(i + 2, "{", "}")
            self._add("", in_dq)
            if j < 0:
                self._mark_dynamic()
                self.state.out.problem("a ${...} expansion is never closed")
                return n
            inner = s[i + 2:j]
            if inner and inner[0] in _NAME_START and all(ch in _NAME_CHARS for ch in inner):
                self._variable(inner, in_dq)
            else:
                self._add("${" + inner + "}", in_dq)
                self._mark_dynamic()
                if "$(" in inner or "`" in inner:
                    self._scan(inner)
            return j + 1
        if nxt == "'" and not in_dq:
            return self._ansi_c(i + 2)
        if nxt == '"' and not in_dq:
            self._add("", True)
            return self._dquote(i + 2)
        if nxt in _NAME_START:
            j = i + 1
            while j < n and s[j] in _NAME_CHARS:
                j += 1
            self._variable(s[i + 1:j], in_dq)
            return j
        if nxt and nxt in "@*#?$!-0123456789":
            self._add("$" + nxt, in_dq)
            self._mark_dynamic()
            return i + 2
        self._add("$", in_dq)
        return i + 1

    def _variable(self, name, in_dq):
        value = self.state.lookup(name)
        if value is None:
            self._add("$" + name, True)
            self._mark_dynamic()
            return
        if in_dq or not any(ch in value for ch in " \t\n"):
            self._add(value, True)
            return
        # Unquoted: the shell splits the value into words.
        pieces = value.split()
        if value[:1] in " \t\n":
            self._end_word()
        for index, piece in enumerate(pieces):
            if index:
                self._end_word()
            self._add(piece, True)
        if value[-1:] in " \t\n":
            self._end_word()

    def _scan(self, inner):
        """Find substitutions inside text that is otherwise opaque (``${x:-$(cmd)}``)."""
        if self.depth + 1 > MAX_DEPTH:
            self.state.out.problem("the command nests too deeply")
            return
        sub = _Lexer(inner, self.state, self.depth + 1, via=self.via)
        sub._dquote(0, closing=None)
        self.subs.extend(sub.subs)

    def _backtick(self, i):
        s, n = self.s, self.n
        j = i + 1
        buf = []
        while j < n and s[j] != "`":
            if s[j] == "\\" and j + 1 < n and s[j + 1] in "`\\$":
                buf.append(s[j + 1])
                j += 2
            else:
                buf.append(s[j])
                j += 1
        self._add("", False)
        self._mark_dynamic()
        if j >= n:
            self.state.out.problem("a backtick is never closed")
        self.subs.extend(_analyze_nested("".join(buf), self.state, self.depth + 1, via=self.via))
        return j + 1

    # -- here-documents --------------------------------------------------------
    def _heredoc(self, delimiter, strip_tabs, literal, body):
        """Read the body for ``<<delimiter`` now. It starts on the line after
        this one, so the main loop is told to jump over it when it gets there."""
        s, n = self.s, self.n
        if self.body_at is None:
            newline = s.find("\n", self.i)
            if newline < 0:
                self.state.out.problem("a here-document has no body")
                return
            self.skip = [newline, newline + 1]
            self.body_at = newline + 1
        at = self.body_at
        lines = []
        while at < n:
            end = s.find("\n", at)
            line = s[at:end] if end >= 0 else s[at:]
            at = end + 1 if end >= 0 else n
            if strip_tabs:
                line = line.lstrip("\t")
            if line == delimiter:
                break
            lines.append(line)
        self.body_at = at
        self.skip[1] = at
        body.text = "\n".join(lines)
        body.quoted = literal
        if not literal and ("$(" in body.text or "`" in body.text) and self.depth + 1 <= MAX_DEPTH:
            sub = _Lexer(body.text, self.state, self.depth + 1, via=self.via)
            sub._dquote(0, closing=None)
            body.subs.extend(sub.subs)

    # -- main loop -------------------------------------------------------------
    def run(self, until=None):
        s, n = self.s, self.n
        while self.i < n:
            i = self.i
            c = s[i]
            if c in " \t":
                self._end_word()
                self.i = i + 1
            elif c == "\\":
                if i + 1 >= n:
                    self._add("\\", False)
                    self.i = n
                elif s[i + 1] == "\n":
                    self.i = i + 2
                else:
                    self._add(s[i + 1], True)
                    self.i = i + 2
            elif c == "\n":
                self._end_word()
                self.i = i + 1
                if self.skip is not None and self.skip[0] == i:
                    self.i = self.skip[1]
                    self.skip = self.body_at = None
                self._operator("\n")
            elif c == "#" and self.parts is None:
                end = s.find("\n", i)
                self.i = n if end < 0 else end
            elif c == "'":
                j = s.find("'", i + 1)
                if j < 0:
                    self.state.out.problem("a single quote is never closed")
                    j = n
                self._add(s[i + 1:j], True)
                self.i = j + 1
            elif c == '"':
                self._add("", True)
                self.i = self._dquote(i + 1)
            elif c == "$":
                self.i = self._dollar(i, False)
            elif c == "`":
                self.i = self._backtick(i)
            elif c in "<>" or (c == "&" and s[i + 1:i + 2] == ">"):
                self._redirect(i)
            elif c == ";":
                self._end_word()
                self.i = i + (2 if s[i + 1:i + 2] == ";" else 1)
                self._operator(";")
            elif c == "&":
                self._end_word()
                double = s[i + 1:i + 2] == "&"
                self.i = i + (2 if double else 1)
                self._operator("&&" if double else "&")
            elif c == "|":
                self._end_word()
                nxt = s[i + 1:i + 2]
                self.i = i + (2 if nxt in ("|", "&") else 1)
                self._operator("||" if nxt == "|" else "|")
            elif c == "(":
                # Also `name()`: a function body is analysed like any other code.
                self._end_word()
                self.i = i + 1
                self.paren += 1
                self._operator("(")
                self.cwd_stack.append(self.state.cwd)
            elif c == ")":
                self._end_word()
                self.i = i + 1
                if self.paren == 0 and until == ")":
                    self._operator(";")
                    return
                self.paren = max(0, self.paren - 1)
                self._operator(")")
                if self.cwd_stack:
                    self.state.cwd = self.cwd_stack.pop()   # a subshell's `cd` ends with it
            else:
                self._add(c, False)
                self.i = i + 1
        self._end_word()
        if until == ")":
            self.state.out.problem("a $( ... ) substitution is never closed")
        self._operator(";")

    def _redirect(self, i):
        s = self.s
        if self.parts is not None and all(not q and t.isdigit() for t, q in self.parts):
            self.parts, self.dynamic, self.subs, self.dyn_at = None, False, [], 0   # an fd, as in 2>file
        else:
            self._end_word()
        for op in ("&>>", "&>", "<<<", "<<-", "<<", "<&", "<>", ">>", ">&", ">|", "<", ">"):
            if s.startswith(op, i):
                break
        if op in ("<", ">") and s[i + 1:i + 2] == "(":
            self._add("", False)
            self.i = self._substitution(i + 2)
            return
        self.redir = op
        self.i = i + len(op)

    # -- commands --------------------------------------------------------------
    def _operator(self, op):
        self.redir = None
        cmd = self._finish_simple()
        if cmd is not None:
            cmd.prev = self.pipeline[-1] if self.pipeline else self.inherit_prev
            self.pipeline.append(cmd)
            self.top.append(cmd)
            _register(cmd, self.state, self.depth)
        if op != "|":
            self.pipeline = []

    def _finish_simple(self):
        words, redirs = self.words, self.redirs
        self.words, self.redirs = [], []
        state = self.state
        subs = [sub for word in words for sub in word.subs]
        for _, target in redirs:
            subs.extend(target.subs)
        if len(words) >= 2 and not words[0].quoted and words[0].text == "function":
            words = words[2:]          # `function name { body`: keep the body
        while words and not words[0].quoted and words[0].text in _KEYWORD_PREFIX:
            words = words[1:]
        if len(words) == 1 and not words[0].quoted and words[0].text in _KEYWORD_ALONE:
            words = []
        assignments = []
        while words and _assignment(words[0]) is not None:
            assignments.append(words[0])
            words = words[1:]
        if words and not words[0].quoted and words[0].text in _DECLARATIONS:
            words = []
        if not words:
            for word in assignments:   # a bare assignment sets a shell variable
                name, value = _assignment(word)
                state.vars[name] = None if word.dynamic else value
            if not redirs:
                return None            # substitutions inside were registered as they were read
            words = [Word(":")]
        first = words[0]
        if not first.quoted and not first.dynamic and first.text in _ASSIGNERS:
            for word in words[1:]:
                pair = _assignment(word)
                if pair is not None:
                    state.vars[pair[0]] = None if word.dynamic else pair[1]
        stdin = None
        plain = []
        for op, target in redirs:
            if op in ("<<", "<<-", "<<<"):
                stdin = target
            plain.append((op, target.text, target.dynamic))
        dyn = [w.dynamic for w in words]
        if dyn[0] and first.name_literal:
            dyn[0] = False             # "$VENV/bin/python": the name is known, the directory is not
        cmd = Cmd([w.text for w in words], dyn, [w.glob for w in words],
                  redirs=plain, cwd=state.cwd, subs=subs, stdin=stdin, via=list(self.via), home=state.home)
        if cmd.name in ("cd", "pushd"):
            _change_directory(cmd, state)
        return cmd


def _assignment(word):
    """``(name, value)`` when ``word`` is ``NAME=value`` written without quoting the name."""
    text = word.text
    eq = text.find("=")
    if eq <= 0 or text[0] not in _NAME_START:
        return None
    name = text[:eq].rstrip("+")
    if not name or not all(ch in _NAME_CHARS for ch in name):
        return None
    return name, text[eq + 1:]


def _change_directory(cmd, state):
    args = [a for a, d in zip(cmd.argv[1:], cmd.dyn[1:]) if not a.startswith("-") or d]
    if not args:
        state.cwd = state.lookup("HOME")
        return
    if cmd.dyn[cmd.argv.index(args[0], 1)] or args[0] == "-" or state.cwd is None and not os.path.isabs(args[0]):
        state.cwd = None
        return
    state.cwd = _normpath(os.path.join(state.cwd or "/", args[0]))


def brace_expand(text, limit=64):
    """``a{b,c}d`` -> ``[abd, acd]``. ``None`` for forms that are not expanded here (``{1..9}``)."""
    start = text.find("{")
    if start < 0:
        return [text]
    level = 0
    commas = []
    for index in range(start, len(text)):
        ch = text[index]
        if ch == "{":
            level += 1
        elif ch == "}":
            level -= 1
            if level == 0:
                inner = text[start + 1:index]
                if not commas:
                    if ".." in inner:
                        return None
                    rest = brace_expand(text[index + 1:], limit)
                    return None if rest is None else [text[:index + 1] + item for item in rest]
                pieces, last = [], start + 1
                for comma in commas + [index]:
                    pieces.append(text[last:comma])
                    last = comma + 1
                out = []
                for piece in pieces:
                    expanded = brace_expand(text[:start] + piece + text[index + 1:], limit)
                    if expanded is None:
                        return None
                    out.extend(expanded)
                    if len(out) > limit:
                        return None
                return out
        elif ch == "," and level == 1:
            commas.append(index)
    return [text]


# --- unwrapping ---------------------------------------------------------------

def _register(cmd, state, depth):
    """Record ``cmd`` and every command it is a wrapper for."""
    out = state.out
    if id(cmd) in state.seen:      # reached again through a wrapper that re-analysed its string
        return
    state.seen.add(id(cmd))
    if len(out.commands) >= MAX_COMMANDS:
        out.problem(f"the command runs more than {MAX_COMMANDS} commands")
        return
    out.commands.append(cmd)
    if depth > MAX_DEPTH:
        out.problem("the command nests too deeply")
        return
    for derived in _unwrap(cmd, state, depth):
        _register(derived, state, depth + 1)


def _skip_options(cmd, start, with_value=(), stop_at_assignment=False):
    """Index of the first argument after the options that begin at ``start``."""
    argv = cmd.argv
    i = start
    while i < len(argv):
        arg = argv[i]
        if cmd.dyn[i]:
            return i
        if arg == "--":
            return i + 1
        if stop_at_assignment and "=" in arg and not arg.startswith("-") and arg[0] in _NAME_START:
            i += 1
            continue
        if not arg.startswith("-") or arg == "-":
            return i
        if arg in with_value:
            i += 2
        else:
            i += 1
    return i


def _rest(cmd, start, via):
    if start >= len(cmd.argv):
        return []
    return [cmd.derive(cmd.argv[start:], cmd.dyn[start:], cmd.glob[start:], via)]


def _unwrap(cmd, state, depth):
    out = state.out
    argv, dyn = cmd.argv, cmd.dyn
    if dyn[0] or cmd.glob[0]:
        resolved = _single_glob(cmd) if cmd.glob[0] and not dyn[0] else None
        if resolved is None:
            out.problem("the command name is computed at run time: " + _clip(argv[0] or "$(...)"))
            return []
        return [cmd.derive([resolved] + argv[1:], [False] + dyn[1:], [False] + cmd.glob[1:], "glob")]
    name = cmd.name
    if name in ("sudo", "doas"):
        return _rest(cmd, _skip_options(cmd, 1, _SUDO_VALUE, stop_at_assignment=True), name)
    if name == "env":
        i = 1
        while i < len(argv) and not dyn[i]:
            arg = argv[i]
            if arg in ("-S", "--split-string") and i + 1 < len(argv):
                text = " ".join(argv[i + 1:])
                if any(dyn[i + 1:]):
                    out.problem("env -S runs a string computed at run time")
                    return []
                return _analyze_nested(text, state, depth + 1, via=cmd.via + ["env -S"], prev=cmd.prev, cwd=cmd.cwd)
            if arg.startswith("--split-string="):
                return _analyze_nested(arg.split("=", 1)[1] + " " + " ".join(argv[i + 1:]), state, depth + 1,
                                       via=cmd.via + ["env -S"], prev=cmd.prev, cwd=cmd.cwd)
            if arg in ("-u", "--unset", "-C", "--chdir"):
                i += 2
            elif arg.startswith("-") or ("=" in arg and arg[0] in _NAME_START):
                i += 1
            else:
                break
        return _rest(cmd, i, "env")
    if name in ("nohup", "builtin", "setsid", "chronic", "unbuffer", "nice", "ionice", "stdbuf", "caffeinate",
                "time", "exec", "command"):
        if name == "command" and len(argv) > 1 and argv[1] in ("-v", "-V"):
            return []
        value = {"nice": ("-n", "--adjustment"), "ionice": ("-c", "-n", "-p", "--class", "--classdata"),
                 "stdbuf": ("-i", "-o", "-e", "--input", "--output", "--error"), "exec": ("-a",),
                 "time": ("-f", "-o", "--format", "--output"), "caffeinate": ("-t", "-w")}.get(name, ())
        return _rest(cmd, _skip_options(cmd, 1, value), name)
    if name == "timeout":
        i = _skip_options(cmd, 1, _TIMEOUT_VALUE)
        return _rest(cmd, i + 1, name)   # skip the duration
    if name == "xargs":
        i = _skip_options(cmd, 1, _XARGS_VALUE)
        derived = _rest(cmd, i, "xargs")
        for item in derived:              # xargs appends arguments we cannot see
            item.argv = item.argv + ["<stdin>"]
            item.dyn = item.dyn + [True]
            item.glob = item.glob + [False]
        return derived
    if name == "busybox" and len(argv) > 1:
        return _rest(cmd, 1, name)
    if name == "watch":
        i = _skip_options(cmd, 1, ("-n", "--interval", "-d", "--differences"))
        if i < len(argv):
            if any(dyn[i:]):
                out.problem("watch runs a string computed at run time")
                return []
            return _analyze_nested(" ".join(argv[i:]), state, depth + 1, via=cmd.via + ["watch"], cwd=cmd.cwd)
        return []
    if name == "su":
        for i in range(1, len(argv) - 1):
            if argv[i] in ("-c", "--command") and not dyn[i]:
                return _run_string(cmd, i + 1, "su -c", state, depth)
        return []
    if name == "eval":
        if len(argv) < 2:
            return []
        if any(dyn[1:]):
            out.problem("eval runs a string computed at run time")
            return []
        return _analyze_nested(" ".join(argv[1:]), state, depth + 1, via=cmd.via + ["eval"],
                               prev=cmd.prev, cwd=cmd.cwd)
    if name == "alias":
        derived = []
        for i in range(1, len(argv)):
            if not dyn[i] and "=" in argv[i]:   # what the alias would run when used later in the line
                derived.extend(_analyze_nested(argv[i].split("=", 1)[1], state, depth + 1,
                                               via=cmd.via + ["alias"], cwd=cmd.cwd))
        return derived
    if name == "trap" and len(argv) > 2 and not dyn[1] and argv[1] not in ("-", "-l", "-p", ""):
        return _analyze_nested(argv[1], state, depth + 1, via=cmd.via + ["trap"], cwd=cmd.cwd)
    if name == "find":
        return _find(cmd)
    if name in SHELLS:
        return _shell(cmd, state, depth)
    base = interpreter_of(name)
    if base is not None:
        _inline_code(cmd, base)
    return []


def _single_glob(cmd):
    import glob
    pattern = cmd.argv[0]
    if not os.path.isabs(pattern):
        if cmd.cwd is None:
            return None
        pattern = os.path.join(cmd.cwd, pattern)
    try:
        matches = glob.glob(pattern)
    except (OSError, ValueError):
        return None
    return matches[0] if len(matches) == 1 else None


def _run_string(cmd, index, via, state, depth):
    if cmd.dyn[index]:
        state.out.problem(f"{via} runs a string computed at run time")
        return []
    return _analyze_nested(cmd.argv[index], state, depth + 1, via=cmd.via + [via], prev=cmd.prev, cwd=cmd.cwd)


def _shell(cmd, state, depth):
    argv, dyn = cmd.argv, cmd.dyn
    name = cmd.name
    i = 1
    reads_stdin = False
    while i < len(argv):
        arg = argv[i]
        if dyn[i]:
            break
        if arg == "--":
            i += 1
            break
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 1:
            if "c" in arg[1:]:
                if i + 1 >= len(argv):
                    return []
                return _run_string(cmd, i + 1, f"{name} -c", state, depth)
            if "s" in arg[1:]:
                reads_stdin = True
            i += 2 if arg in ("-o", "-O") else 1
            continue
        if arg.startswith("--"):
            i += 2 if arg in ("--rcfile", "--init-file") else 1
            continue
        break
    has_script = i < len(argv) and not reads_stdin
    if has_script:
        return []   # runs a script file; its contents are not analysed
    if cmd.stdin is not None:
        # The here-document or here-string is the script.
        return _analyze_nested(cmd.stdin.text, state, depth + 1, via=cmd.via + [f"{name} <<"], cwd=cmd.cwd)
    if cmd.prev is not None:
        state.out.problem(f"text is piped into {name}, so what runs is decided at run time")
    elif any(op == "<" for op, _, _ in cmd.redirs):
        pass        # a script file on stdin; not analysed, like a script operand
    return []


def _inline_code(cmd, base):
    flags = INLINE_CODE[base]
    argv = cmd.argv
    for i in range(1, len(argv)):
        if cmd.dyn[i]:
            continue
        arg = argv[i]
        if arg in flags and i + 1 < len(argv):
            cmd.opaque = argv[i + 1]
            return
        if base == "python" and arg.startswith("-c") and len(arg) > 2 and not arg.startswith("--"):
            cmd.opaque = arg[2:]
            return
        if not arg.startswith("-") and arg not in flags:
            break
    if cmd.stdin is not None and cmd.stdin.text:
        cmd.opaque = cmd.stdin.text


def _find(cmd):
    argv = cmd.argv
    derived = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if cmd.dyn[i]:
            i += 1
            continue
        if arg == "-delete":
            cmd.find_delete = True
        elif arg in ("-exec", "-execdir", "-ok", "-okdir"):
            j = i + 1
            while j < len(argv) and not (argv[j] in (";", "+") and not cmd.dyn[j]):
                j += 1
            sub_argv, sub_dyn, sub_glob = argv[i + 1:j], cmd.dyn[i + 1:j], cmd.glob[i + 1:j]
            if sub_argv:
                sub_dyn = [d or a == "{}" for a, d in zip(sub_argv, sub_dyn)]
                derived.append(cmd.derive(sub_argv, sub_dyn, sub_glob, f"find {arg}"))
            i = j
        i += 1
    return derived


def _clip(text, limit=60):
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 3] + "..."


# --- paths a command touches --------------------------------------------------

def normalize_path(text, cwd, home):
    """Absolute, ``..``-free form of a path argument; ``None`` when it is relative and cwd is unknown."""
    if text.startswith("~/") or text == "~":
        text = home + text[1:]
    if not os.path.isabs(text):
        if cwd is None:
            return None
        text = os.path.join(cwd, text)
    return _normpath(text)


def _normpath(text):
    """``os.path.normpath``, except that POSIX's special case for exactly two leading slashes
    is folded away: ``//etc`` is ``/etc`` everywhere this runs."""
    path = os.path.normpath(text)
    return "/" + path.lstrip("/") if path.startswith("//") else path


def path_refs(cmd, expand_globs=True):
    """``[(absolute path, access, text as written)]`` for everything ``cmd`` names that could be a file.

    ``access`` is ``read`` for commands that only read, ``write`` for
    redirection targets, and ``any`` when the command could do either."""
    refs = []
    if not cmd.argv:
        return refs
    name = cmd.name
    access = "read" if name in READ_ONLY else "any"
    if name == "sed" and not any(a.startswith("-i") or a == "--in-place" or a.startswith("--in-place=")
                                 for a in cmd.argv[1:]):
        access = "read"
    if name in ("find", "awk", "gawk") and not cmd.find_delete:
        access = "read"

    def add(text, how, is_glob=False):
        if not text or text in ("-", "--", "{}") or "\n" in text or "://" in text or len(text) > 4096:
            return
        candidates = [text]
        if is_glob and expand_globs:
            import glob
            base = normalize_path(text, cmd.cwd, cmd.home)
            if base is not None:
                try:
                    candidates.extend(glob.glob(base)[:64])
                except (OSError, ValueError):
                    pass
        for candidate in candidates:
            path = normalize_path(candidate, cmd.cwd, cmd.home)
            if path is not None:
                refs.append((path, how, text))

    for index in range(1, len(cmd.argv)):
        if cmd.dyn[index]:
            continue
        arg = cmd.argv[index]
        is_glob = cmd.glob[index]
        if arg.startswith("-"):
            eq = arg.find("=")
            if eq > 0:
                add(arg[eq + 1:].lstrip("@"), access, is_glob)        # --file=PATH, --data=@PATH
            continue
        add(arg, access, is_glob)
        eq = arg.find("=")
        if eq > 0:
            add(arg[eq + 1:].lstrip("@"), access, is_glob)   # of=PATH, key=@PATH
        if arg.startswith("@"):
            add(arg[1:], access, is_glob)                    # curl -d @PATH
    for op, target, dynamic in cmd.redirs:
        if dynamic or op in ("<<", "<<-", "<<<"):
            continue
        if op in (">&", "<&") and (target.isdigit() or target == "-"):
            continue
        add(target, "read" if op == "<" else "write")
    return refs


# --- command patterns ---------------------------------------------------------

class Pattern(object):
    """A compiled ``command`` pattern: one stage per ``|``-separated part."""

    __slots__ = ("text", "stages")

    def __init__(self, text, stages):
        self.text = text
        self.stages = stages


def compile_pattern(text):
    """Compile ``"git push --force"`` or ``"curl|wget | sh|bash"``.

    Tokens are split like shell words. The first token of a stage is the
    command name (``a|b`` for alternatives, globs allowed). A token starting
    with ``-`` is a flag that must be present anywhere; short flags match
    inside clusters, so ``-rf`` matches ``-r -f`` and ``-fr``. Any other token
    is a positional argument; positionals must appear in this order, with
    anything in between. A lone ``|`` means "piped into".
    """
    import shlex
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise ValueError(str(exc))
    if not tokens:
        raise ValueError("the pattern is empty")
    stages, current = [], []
    for token in tokens:
        if token == "|":
            if not current:
                raise ValueError("a `|` needs a command on both sides")
            stages.append(current)
            current = []
        else:
            current.append(token)
    if not current:
        raise ValueError("a `|` needs a command on both sides")
    stages.append(current)
    compiled = []
    for stage in stages:
        names = [n for n in stage[0].split("|") if n]
        if not names or stage[0].startswith("-"):
            raise ValueError(f"{stage[0]!r} is not a command name")
        flags, positionals = [], []
        for token in stage[1:]:
            if token == "*":
                continue
            alternatives = [a for a in token.split("|") if a]
            if not alternatives:
                raise ValueError("an argument is empty")
            (flags if token.startswith("-") else positionals).append(alternatives)
        compiled.append((names, flags, positionals))
    return Pattern(text, compiled)


def _name_matches(cmd, names):
    arg0 = cmd.argv[0]
    base = command_name(arg0)
    for name in names:
        target = arg0 if "/" in name else base
        if fnmatch.fnmatchcase(target, name):
            return True
    return False


def _flag_present(args, dyn, alternatives):
    for alt in alternatives:
        if alt.startswith("--"):
            for arg, dynamic in zip(args, dyn):
                if dynamic:
                    continue
                if arg == "--":
                    break
                if arg == alt or arg.startswith(alt + "=") or fnmatch.fnmatchcase(arg, alt):
                    return True
        else:
            wanted = set(alt[1:])
            seen = set()
            for arg, dynamic in zip(args, dyn):
                if dynamic:
                    continue
                if arg == "--":
                    break
                if arg.startswith("-") and not arg.startswith("--") and len(arg) > 1:
                    if arg == alt:
                        return True
                    seen.update(arg[1:])
            if len(alt) > 1 and wanted <= seen and all(ch.isalnum() for ch in wanted):
                return True
    return False


def _positionals(cmd, start=1):
    """``[(text, dynamic)]``: arguments that are not flags, plus everything after ``--``."""
    out = []
    literal = False
    for arg, dynamic in zip(cmd.argv[start:], cmd.dyn[start:]):
        if not literal and not dynamic and arg == "--":
            literal = True
            continue
        if literal or dynamic or not arg.startswith("-") or arg == "-":
            out.append((arg, dynamic))
    return out


def _positional_matches(cmd, text, dynamic, alternatives):
    for alt in alternatives:
        if alt == "<dynamic>":
            if dynamic:
                return True
            continue
        if dynamic:
            continue
        if fnmatch.fnmatchcase(text, alt):
            return True
        if alt[:1] in "/~." and cmd.home:
            left = normalize_path(text, cmd.cwd, cmd.home)
            right = normalize_path(alt, cmd.cwd, cmd.home)
            if left is not None and right is not None and fnmatch.fnmatchcase(left, right):
                return True
    return False


def stage_matches(cmd, stage, start=0):
    """Does ``cmd.argv[start:]`` match one compiled stage?"""
    names, flags, positionals = stage
    if start >= len(cmd.argv) or cmd.dyn[start]:
        return False
    if start:
        cmd = Cmd(cmd.argv[start:], cmd.dyn[start:], cmd.glob[start:], cwd=cmd.cwd, home=cmd.home)
    if not _name_matches(cmd, names):
        return False
    args, dyn = cmd.argv[1:], cmd.dyn[1:]
    for alternatives in flags:
        if not _flag_present(args, dyn, alternatives):
            return False
    if positionals:
        have = _positionals(cmd)
        index = 0
        for alternatives in positionals:
            while index < len(have) and not _positional_matches(cmd, have[index][0], have[index][1], alternatives):
                index += 1
            if index >= len(have):
                return False
            index += 1
    return True


def upstream(cmd):
    """Commands whose output feeds ``cmd``: earlier pipeline stages, and for a
    shell or interpreter also the substitutions in its arguments
    (``bash -c "$(curl ...)"``, ``sh <(curl ...)``)."""
    out = []
    seen = cmd.prev
    while seen is not None:
        out.append(seen)
        seen = seen.prev
    if cmd.name in SHELLS or cmd.name == "eval" or interpreter_of(cmd.name) or cmd.name in ("source", "."):
        out.extend(cmd.subs)
    return out


# Commands whose arguments are text to print or search for, not something to run.
# `echo rm -rf /` must not be read as `rm -rf /` by the any-position matching below.
TEXT_ONLY = frozenset(("echo", "printf", "grep", "egrep", "fgrep", "rg", "ag", "ack", "man", "which", "type",
                       "whatis", "apropos", "help", "whereis", "cowsay", "figlet", "say", "logger"))


def suffixes(cmd):
    """``cmd`` itself, then ``cmd`` with each leading word removed: the commands an
    unrecognised wrapper (``strace -f rm -rf /``, ``mytool rm -rf /``) could be running."""
    yield cmd
    if cmd.name in TEXT_ONLY or not cmd.argv:
        return
    if cmd.name == "briefcase" and cmd.argv[1:3] == ["policy", "test"]:
        return      # `briefcase policy test -- rm -rf /` explains a command; it does not run it
    for start in range(1, len(cmd.argv)):
        if cmd.dyn[start] or cmd.argv[start].startswith("-"):
            continue
        tail = Cmd(cmd.argv[start:], cmd.dyn[start:], cmd.glob[start:], cwd=cmd.cwd, home=cmd.home)
        tail.prev, tail.subs, tail.stdin, tail.redirs = cmd.prev, cmd.subs, cmd.stdin, cmd.redirs
        yield tail


def pattern_matches(pattern, cmd, anywhere):
    """Match ``cmd`` against ``pattern``.

    With ``anywhere`` (ask and deny rules) the last stage may also start at
    any later argument, so an unknown wrapper in front does not hide the
    command. Allow rules never use it: ``evil git status`` is not ``git status``.
    """
    stages = pattern.stages
    last = stages[-1]
    spread = anywhere and cmd.name not in TEXT_ONLY and not (
        cmd.name == "briefcase" and cmd.argv[1:3] == ["policy", "test"])
    if not any(stage_matches(cmd, last, start) for start in (range(len(cmd.argv)) if spread else (0,))):
        return False
    if len(stages) == 1:
        return True
    feeders = upstream(cmd)
    for stage in reversed(stages[:-1]):
        hit = None
        for index, feeder in enumerate(feeders):
            if any(stage_matches(feeder, stage, start)
                   for start in (range(len(feeder.argv)) if anywhere else (0,))):
                hit = index
                break
        if hit is None:
            return False
        feeders = feeders[hit + 1:]
    return True
