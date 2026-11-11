"""Shell command parsing: which script or tool did a shell call actually run?

`parse(cmd)` -> dict(cmd_text, cmd_cwd, cmd_family, cmd_target). Pure functions, no I/O.

- strip_cd: a leading `cd <path> && ` / `cd <path>; ` is removed from the text and the path kept as cwd.
- split_segments: split on && ; || | and newlines, respecting quotes, $(...) / ( ) nesting and heredoc bodies.
- primary: the last non-trivial pipeline group (trivial = cd, echo, export, set, source, assignments, sleep, ...);
  inside a pipeline the first non-filter stage wins (`python x.py | tail -5` is python x.py, not tail).
- classify: family + target, e.g. python / scripts/build.py, npm-script / test, git / diff, inspect / rg.
"""
import os
import re

TRIVIAL = {"cd", "echo", "export", "set", "source", ".", "unset", "true", "false", ":", "printf", "pwd", "sleep",
           "exit", "return", "wait", "local", "declare", "readonly", "shopt", "umask", "trap", "alias", "ulimit",
           "for", "then", "else", "fi", "done", "do", "case", "esac", "in", "select",
           "function", "{", "}", "(", ")", "[[", "[", "test", "!"}
FILTERS = {"head", "tail", "grep", "egrep", "fgrep", "rg", "wc", "sort", "uniq", "tee", "cut", "tr", "jq", "less",
           "more", "awk", "sed", "xargs", "column", "nl", "fold", "cat", "tac", "rev", "paste", "cmp", "yq"}
INSPECT = {"grep", "egrep", "fgrep", "rg", "find", "fd", "ls", "cat", "sed", "awk", "head", "tail", "wc", "tree",
           "stat", "du", "df", "diff", "jq", "sort", "uniq", "cut", "file", "less", "bat", "tac", "ps", "pgrep",
           "readlink", "realpath", "basename", "dirname", "which", "type", "env", "printenv", "date", "id",
           "whoami", "uname", "hostname", "lsof", "nl", "column", "md5sum", "sha256sum", "xxd", "od", "strings"}
WRAPPERS = {"sudo", "time", "nohup", "command", "builtin", "nice", "ionice", "stdbuf", "exec", "setsid", "caffeinate"}
NODE = {"node", "bun", "deno", "tsx", "ts-node", "nodejs"}
PM = {"npm", "pnpm", "yarn"}
PM_BUILTIN = {"install", "i", "add", "remove", "rm", "uninstall", "update", "up", "upgrade", "ci", "init", "link",
              "publish", "pack", "audit", "outdated", "ls", "list", "why", "config", "cache", "info", "view", "prune",
              "dedupe", "rebuild", "store", "exec", "dlx", "create", "login", "whoami", "version"}
TEST_TOOLS = {"pytest", "py.test", "vitest", "jest", "mocha", "tox", "nosetests", "phpunit", "rspec"}
SCRIPT_EXT = {".py": "python", ".sh": "shell-script", ".bash": "shell-script", ".js": "node", ".mjs": "node",
              ".cjs": "node", ".ts": "node", ".tsx": "node"}
PY_ARG_OPTS = {"-W", "-X", "--check-hash-based-pycs"}
NODE_ARG_OPTS = {"-r", "--require", "--loader", "--import", "--env-file", "--experimental-loader", "-C", "--conditions"}
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
CD_PREFIX = re.compile(r"""\A\s*cd\s+(?:--\s+)?("(?:[^"\\]|\\.)*"|'[^']*'|[^\s;&|]+)\s*(?:&&|;)\s*""")
HEREDOC = re.compile(r"<<-?\s*(?:'([^']+)'|\"([^\"]+)\"|\\?([A-Za-z_][A-Za-z0-9_]*))")
JS_START = re.compile(r"^\s*(text\(|image\(|const\s|let\s|var\s|//|await\s|\(async|async\s|for\s*\(|import\s|if\s*\(|\{|tools\.|nodes\.|ALL_TOOLS)")
PY_VER = re.compile(r"^(python|pypy)[0-9.]*$")


def strip_cd(cmd):
    """(rest, cwd): drop one leading `cd <path> && ` / `cd <path>; `; cwd is the path (quotes removed) or None."""
    m = CD_PREFIX.match(cmd or "")
    if not m:
        return (cmd or "").strip(), None
    p = m.group(1)
    if p[:1] in "\"'" and p[-1:] == p[:1]:
        p = p[1:-1]
    return cmd[m.end():].strip(), p


def split_segments(cmd):
    """[(text, heredoc)] segments of a shell command, split on && ; || | & and newlines, outside quotes and
    $(...)/(...) nesting. heredoc is True when the segment starts a here-document (the body is dropped)."""
    segs, cur, i, n = [], [], 0, len(cmd)
    q = None          # active quote char
    depth = 0         # ( ) / $( ) nesting
    pend = []         # heredoc delimiters waiting for their body
    hd = False

    def flush():
        nonlocal cur, hd
        t = "".join(cur).strip()
        if t:
            segs.append((t, hd))
        cur, hd = [], False

    while i < n:
        c = cmd[i]
        if q:
            cur.append(c)
            if c == "\\" and q == '"' and i + 1 < n:
                cur.append(cmd[i + 1]); i += 2; continue
            if c == q:
                q = None
            i += 1; continue
        if c == "\\" and i + 1 < n:
            cur.append(cmd[i:i + 2]); i += 2; continue
        if c in "\"'":
            q = c; cur.append(c); i += 1; continue
        if c == "#" and (not cur or cur[-1] in " \t"):
            while i < n and cmd[i] != "\n":
                i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        if c == "<" and cmd.startswith("<<", i) and not cmd.startswith("<<<", i):
            m = HEREDOC.match(cmd, i)
            if m:
                pend.append(next(g for g in m.groups() if g))
                hd = True
                cur.append(m.group(0)); i = m.end(); continue
        if c == "\n":
            if pend:  # skip heredoc bodies up to each delimiter line
                i += 1
                for d in pend:
                    while i < n:
                        j = cmd.find("\n", i)
                        line = cmd[i:] if j < 0 else cmd[i:j]
                        i = n if j < 0 else j + 1
                        if line.strip() == d:
                            break
                pend = []
                flush(); continue
            flush(); i += 1; continue
        if depth == 0:
            two = cmd[i:i + 2]
            if two in ("&&", "||"):
                flush(); i += 2; continue
            if c == ";" or c == "|":
                flush(); i += 1; continue
            if c == "&" and not (i > 0 and cmd[i - 1] in "<>") and not (i + 1 < n and cmd[i + 1] == ">"):
                flush(); i += 1; continue
        cur.append(c); i += 1
    flush()
    return segs


def tokens(seg):
    """Shell-ish words of one segment (quotes removed, no expansion); redirections dropped."""
    out, cur, q, i, n, have = [], [], None, 0, len(seg), False
    while i < n:
        c = seg[i]
        if q:
            if c == "\\" and q == '"' and i + 1 < n:
                cur.append(seg[i + 1]); i += 2; continue
            if c == q:
                q = None
            else:
                cur.append(c)
            i += 1; continue
        if c in "\"'":
            q = c; have = True; i += 1; continue
        if c == "\\" and i + 1 < n:
            cur.append(seg[i + 1]); i += 2; continue
        if c in " \t\n":
            if have or cur:
                out.append("".join(cur)); cur, have = [], False
            i += 1; continue
        cur.append(c); i += 1
    if have or cur:
        out.append("".join(cur))
    keep, skip = [], False
    for t in out:
        if skip:
            skip = False; continue
        if re.fullmatch(r"\d*[<>]{1,2}&?\d*-?", t) and t not in ("<", ">") or re.fullmatch(r"\d*>&\d+", t):
            if re.fullmatch(r"\d*[<>]{1,2}", t):
                skip = True
            continue
        if re.match(r"^\d*[<>]{1,2}[^<>&=]", t) and not t.startswith("<<"):
            continue
        if t.startswith("<<"):
            continue
        keep.append(t)
    return keep


def _strip_leading(toks):
    """Drop shell keywords, env assignments and wrapper commands from the front of a word list."""
    t = list(toks)
    while t:
        w = t[0]
        if w in ("do", "then", "else", "{", "(", "!", "elif", "&&", "until", "while", "if"):
            t.pop(0)
        elif ASSIGN.match(w):
            t.pop(0)
        elif w == "env":
            t.pop(0)
            while t and (ASSIGN.match(t[0]) or t[0].startswith("-")):
                t.pop(0)
        elif w == "timeout":
            t.pop(0)
            while t and t[0].startswith("-"):
                t.pop(0)
            if t:
                t.pop(0)  # the duration
        elif w in WRAPPERS:
            t.pop(0)
            while t and t[0].startswith("-") and w in ("sudo", "nice", "ionice"):
                t.pop(0)
        else:
            break
    return t


def _name(w):
    return os.path.basename(w) if "/" in w else w


def trivial(toks):
    t = _strip_leading(toks)
    if not t:
        return True
    return _name(t[0]) in TRIVIAL


def primary(cmd):
    """(tokens, heredoc) of the primary segment, or (None, False) when everything is trivial."""
    groups = _groups(cmd)
    best = None
    for g in reversed(groups):
        cand = [(tokens(s), hd) for s, hd in g]
        cand = [(t, hd) for t, hd in cand if not trivial(t)]
        if not cand:
            continue
        nf = [x for x in cand if _name(_strip_leading(x[0])[0]) not in FILTERS]
        best = nf[0] if nf else cand[0]
        break
    if not best:
        return None, False
    return _strip_leading(best[0]), best[1]


def _groups(cmd):
    """Split into pipeline groups [[(seg, heredoc), ...], ...]: groups are separated by && ; || newline,
    stages inside a group by a single |."""
    # re-run the splitter with a marker: replace single | by a sentinel character before splitting
    marked, q, depth, i, n = [], None, 0, 0, len(cmd)
    while i < n:
        c = cmd[i]
        if q:
            marked.append(c)
            if c == "\\" and q == '"' and i + 1 < n:
                marked.append(cmd[i + 1]); i += 2; continue
            if c == q:
                q = None
        elif c == "\\" and i + 1 < n:
            marked.append(cmd[i:i + 2]); i += 2; continue
        elif c in "\"'":
            q = c; marked.append(c)
        elif c == "|" and depth == 0 and cmd[i:i + 2] != "||" and not (i > 0 and cmd[i - 1] == "|"):
            marked.append("\x00")
        else:
            if c == "(":
                depth += 1
            elif c == ")":
                depth = max(0, depth - 1)
            marked.append(c)
        i += 1
    segs = split_segments("".join(marked))
    groups, cur = [], []
    for s, hd in segs:
        parts = s.split("\x00")
        for k, p in enumerate(parts):
            if p.strip():
                cur.append((p.strip(), hd if k == len(parts) - 1 else False))
        groups.append(cur); cur = []
    return [g for g in groups if g]


def short_path(p):
    """parent/basename of a path (basename alone for a bare file name)."""
    p = p.rstrip("/")
    b = os.path.basename(p)
    par = os.path.basename(os.path.dirname(p))
    return f"{par}/{b}" if par and par not in (".", "..", "~") else b


def _first_pos(args, opts_with_arg=()):
    """First positional argument and the rest after it; skips -flags (and the value of opts_with_arg)."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return (args[i + 1], args[i + 2:]) if i + 1 < len(args) else (None, [])
        if a.startswith("-") and a != "-":
            i += 2 if a in opts_with_arg else 1
            continue
        return a, args[i + 1:]
    return None, []


def _script_family(w):
    ext = os.path.splitext(w)[1].lower()
    return SCRIPT_EXT.get(ext)


def classify(toks, heredoc=False, depth=0):
    """(family, target) for a cleaned word list."""
    if not toks:
        return "(empty)", "(empty)"
    w0 = toks[0]
    name = _name(w0)
    args = toks[1:]
    low = name.lower()

    if PY_VER.match(low) or low in ("py", "ipython"):
        return _python(args, heredoc, low)
    if low in ("uv", "pipenv", "poetry", "pdm", "hatch", "rye") and args and args[0] in ("run", "exec"):
        rest = [a for a in args[1:]]
        while rest and rest[0].startswith("-"):
            rest = rest[1:] if rest[0] not in ("--with", "--project", "-p", "--python", "--directory") else rest[2:]
        if rest and depth < 3:
            f, t = classify(rest, heredoc, depth + 1)
            if f == "(empty)":
                return "python", low + " run"
            if _script_family(rest[0]) == "python" or f in ("python", "test"):
                return f, t
            return f, t
        return "python", f"{low} run"
    if low == "uvx" and args:
        return "python", "uvx " + args[0]
    if low in ("bash", "sh", "zsh", "dash", "ksh"):
        return _shell(low, args, heredoc, depth)
    if low in NODE:
        return _node(low, args, heredoc)
    if low in ("npx", "bunx", "pnpx"):
        pkg, rest = _first_pos(args, {"-p", "--package", "-c"})
        if pkg is None:
            return "node", low
        if _name(pkg) in TEST_TOOLS:
            return "test", _name(pkg)
        return "node", f"{low} {pkg}"
    if low in PM:
        return _pm(low, args)
    if low == "make" or low == "gmake" or low == "ninja":
        t, _ = _first_pos(args, {"-C", "-f", "-j", "-l", "--directory", "--file"})
        return "make", t or "(default)"
    if low == "git":
        sub, _ = _first_pos(args, {"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
        return "git", sub or "git"
    if low == "gh":
        sub, rest = _first_pos(args, {"-R", "--repo"})
        return "gh", sub or "gh"
    if low in ("docker", "podman", "docker-compose"):
        sub, rest = _first_pos(args, {"-H", "--host", "-c", "--context", "-f", "--file", "-p", "--project-name"})
        if low == "docker-compose":
            return "docker", "compose " + (sub or "")
        if sub == "compose":
            sub2, _ = _first_pos(rest, {"-f", "--file", "-p", "--project-name"})
            return "docker", "compose " + (sub2 or "")
        return "docker", sub or low
    if low in TEST_TOOLS:
        return "test", low
    if low == "cargo" and args and args[0] in ("test", "nextest"):
        return "test", "cargo test"
    if low == "go" and args and args[0] == "test":
        return "test", "go test"
    if low == "ssh":
        return _ssh(args, depth)
    if low in ("curl", "wget"):
        for a in args:
            m = re.match(r"^(?:[a-z]+://)?(?:[^/@\s]*@)?([A-Za-z0-9.\-]+(?::\d+)?)(?:/|$)", a)
            if not a.startswith("-") and m and ("." in m.group(1) or ":" in m.group(1) or "://" in a or m.group(1) == "localhost"):
                return "network", f"{low} {m.group(1)}"
        return "network", low
    if low in ("pip", "pip3", "pipx"):
        sub, _ = _first_pos(args)
        return "pip", f"{low} {sub}" if sub else low
    if low in INSPECT:
        return "inspect", low
    if "/" in w0 or w0.startswith("."):
        f = _script_family(w0)
        if f:
            return f, short_path(w0)
    if low.endswith(".py") or low.endswith(".sh"):
        return _script_family(low), short_path(w0)
    return name, name


def _python(args, heredoc, low):
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-c":
            return "python", "python -c"
        if a == "-m":
            mod = args[i + 1] if i + 1 < len(args) else "?"
            if mod in ("pytest", "unittest", "nose"):
                return "test", mod
            return "python", f"python -m {mod}"
        if a.startswith("-m") and len(a) > 2:
            return "python", f"python -m {a[2:]}"
        if a == "-":
            return "python", "python heredoc"
        if a in PY_ARG_OPTS:
            i += 2; continue
        if a.startswith("-"):
            i += 1; continue
        return "python", short_path(a)
    if heredoc:
        return "python", "python heredoc"
    return "python", "python (stdin)"


def _shell(low, args, heredoc, depth):
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-c", "-lc", "-ic", "-cl", "-ec", "-xc", "-euc") or (a.startswith("-") and not a.startswith("--") and "c" in a[1:] and len(a) <= 5):
            if i + 1 < len(args) and depth < 3:
                inner, _ = strip_cd(args[i + 1])
                p, hd = primary(inner)
                if p is not None:
                    return classify(p, hd, depth + 1)
            return "shell-script", f"{low} -c"
        if a in ("-s", "-"):
            return "shell-script", f"{low} heredoc" if heredoc else f"{low} (stdin)"
        if a.startswith("-") or a == "+x":
            i += 1; continue
        return "shell-script", short_path(a)
    return "shell-script", f"{low} heredoc" if heredoc else low


def _node(low, args, heredoc):
    if low in ("bun", "deno") and args:
        sub = args[0]
        if sub in ("test",):
            return "test", f"{low} test"
        if sub in ("run", "x") or sub == "install" or sub == "add" or sub == "build":
            rest = [a for a in args[1:]]
            if sub == "run" and rest:
                tgt, _ = _first_pos(rest, {"--cwd", "--filter", "-r", "--config", "-c", "--allow-net", "--allow-read"} if low == "bun" else ())
                if tgt and (_script_family(tgt) or "/" in tgt) and low == "bun":
                    return "node", short_path(tgt)
                if tgt and low == "bun":
                    return "npm-script", tgt
                if tgt:
                    return "node", short_path(tgt)
            if sub == "x" and rest:
                return "node", f"bunx {rest[0]}"
            return "node", f"{low} {sub}"
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-e", "--eval", "-p", "--print"):
            return "node", f"{low} -e"
        if a in NODE_ARG_OPTS:
            i += 2; continue
        if a == "-" and True:
            return "node", f"{low} heredoc"
        if a.startswith("-"):
            i += 1; continue
        if low == "deno" and a in ("run", "eval", "test", "check", "fmt", "lint", "task", "compile"):
            if a == "test":
                return "test", "deno test"
            i += 1; continue
        return "node", short_path(a)
    return "node", f"{low} heredoc" if heredoc else f"{low} (stdin)"


def _pm(low, args):
    skipv = {"-C", "--dir", "--prefix", "--filter", "-F", "--workspace", "-w", "--cwd", "--registry"}
    sub, rest = _first_pos(args, skipv)
    if sub is None:
        return low, low
    if sub in ("run", "run-script", "rum", "urn") or (low == "pnpm" and sub in ("-r", "recursive") and rest):
        tgt, _ = _first_pos(rest, skipv)
        return "npm-script", tgt or "(list)"
    if sub in ("test", "t", "tst", "start", "stop", "restart"):
        return "npm-script", "test" if sub in ("test", "t", "tst") else sub
    if sub in PM_BUILTIN:
        return low, f"{low} {sub}"
    if low in ("pnpm", "yarn"):  # `pnpm build`, `yarn lint`
        return "npm-script", sub
    return low, f"{low} {sub}"


def _ssh(args, depth):
    host, rest = _first_pos(args, {"-i", "-p", "-o", "-l", "-F", "-J", "-L", "-R", "-D", "-b", "-c", "-E", "-S"})
    if host is None:
        return "ssh", "ssh"
    if rest and depth < 3:
        inner = " ".join(rest)
        inner, _ = strip_cd(inner)
        p, hd = primary(inner)
        if p is not None:
            f, t = classify(p, hd, depth + 1)
            if f not in ("inspect", "ssh", "codex-js"):
                return "ssh", f"ssh {host} > {t}"
    return "ssh", f"ssh {host}"


def classify_cmd(cmd):
    """(family, target) for a full command string (cd prefix already removed or not)."""
    rest, _ = strip_cd(cmd)
    if JS_START.match(rest) and not re.match(r"^(for|if)\s+[^(]", rest):
        m = re.search(r"tools\.([A-Za-z_]+)", rest)
        return "codex-js", ("tools." + m.group(1)) if m else "js"
    p, hd = primary(rest)
    if p is None:
        return "shell", "(no command)"
    return classify(p, hd)


def parse(cmd, n_text=2000, mask=None):
    """Parse one shell command. `mask(text, n)` masks secrets (default: identity + truncate)."""
    raw = cmd or ""
    rest, cwd = strip_cd(raw)
    try:
        fam, tgt = classify_cmd(raw)
    except Exception:
        fam, tgt = "unparsed", "unparsed"
    mk = mask or (lambda s, n: s[:n])
    return {"cmd_text": mk(rest, n_text), "cmd_cwd": mk(cwd, 300) if cwd else None,
            "cmd_family": mk(fam, 80), "cmd_target": mk(tgt, 200)}
