"""Stream Claude Code transcripts (read only) into daily Parquet event files.

No payload text is stored: only sizes and sha256 of tool_result content.
Dates are UTC. One assistant API message appears several times in a transcript
(one line per content block, usage repeated), so llm_request is de-duplicated
by message.id (max of each usage field).

Every row carries a transcript pointer: src_file (relative to the transcripts root, prefixed with
the source: claude/<project>/<session>.jsonl or codex/<yyyy>/<mm>/<dd>/rollout-*.jsonl), src_line
(1-based physical line in that JSONL) and msg_id where the transcript has one. The roots are
recorded in <out>/sources.json so `obs verify-pointers` can resolve them.

The only command text kept is on shell tool calls: `cmd_text` (up to 2000 characters, a leading `cd <path> &&`
removed into `cmd_cwd`, secret-looking values masked, see mask_secrets / shell_fields) and `detail` (its first
80 characters). cmd_family / cmd_target (obs/cmdparse.py) are on the call and on its tool_result.
"""
import bisect, glob, hashlib, json, os, re, shlex, socket, sys, time, uuid
from datetime import datetime, timedelta, timezone
import pyarrow as pa
import pyarrow.parquet as pq
from . import retain
from .prices import prices

SCHEMA = pa.schema([
    ("event_id", pa.string()), ("ts", pa.timestamp("ms", tz="UTC")), ("host", pa.string()),
    ("agent", pa.string()), ("harness", pa.string()), ("session_id", pa.string()), ("parent_session_id", pa.string()),
    ("project", pa.string()), ("event_type", pa.string()), ("provider", pa.string()),
    ("model", pa.string()), ("input_tokens", pa.int64()), ("output_tokens", pa.int64()),
    ("cache_read_tokens", pa.int64()), ("cache_write_tokens", pa.int64()),
    ("cost_usd", pa.float64()), ("latency_ms", pa.int64()), ("tool_name", pa.string()),
    ("tool_call_id", pa.string()), ("content_bytes", pa.int64()), ("estimated_tokens", pa.int64()),
    ("result_sha256", pa.string()), ("is_error", pa.bool_()), ("truncated", pa.bool_()),
    ("redaction_version", pa.string()),
    ("input_sha256", pa.string()),  # extra: hash of tool_use input, for retry-loop detection
    # transcript pointer
    ("src_file", pa.string()), ("src_line", pa.int64()), ("msg_id", pa.string()),
    # per-call facts
    ("duration_ms", pa.int64()),        # tool_result: result ts - tool_use ts
    ("exit_code", pa.int64()),          # shell results, when the output states it
    ("detail", pa.string()),            # shell call: masked command (80 chars); spawn call: subagent_type
    ("cmd_text", pa.string()),          # shell call: masked command, cd prefix removed, up to 2000 chars
    ("cmd_cwd", pa.string()),           # shell call: path of a leading `cd <path> && `
    ("cmd_family", pa.string()),        # shell call + result: python | node | npm-script | git | test | inspect | ...
    ("cmd_target", pa.string()),        # shell call + result: script path, npm script, git subcommand, tool, ...
    ("file_path", pa.string()),         # file tools + Codex cat/sed/head/tail/view_image: masked path, on call + result
    ("file_ext", pa.string()),          # lowercase extension | none | env | dockerfile | makefile
    ("file_kind", pa.string()),         # code | markdown | json-yaml | log | jsonl-transcript | image | binary | other
    ("skill_name", pa.string()),        # Skill tool call (Claude) or a SKILL.md read (Codex)
    ("model_requested", pa.string()),   # spawn call: explicit model or "default"
    ("spawn_key", pa.string()),         # links a spawn call to the subagent transcript it started
    ("child_session_id", pa.string()),  # spawn result: subagent session id, when reported
    # tool_result size split: content_bytes = text_bytes + image_bytes
    ("text_bytes", pa.int64()), ("image_bytes", pa.int64()), ("image_count", pa.int64()),
    # tail cost: model requests after this tool_result in the same session file (it is re-read by each of them)
    ("reread_turns", pa.int64()), ("reread_tokens", pa.int64()),
    # compaction: segment_index = compactions seen before this row in the session (0-based); compaction_id = "<session>#<n>"
    ("segment_index", pa.int64()), ("compaction_id", pa.string()),
    # llm_request at list prices (obs/prices.yaml); cost_usd stays the harness-reported figure
    ("list_cost_usd", pa.float64()),
    # which rule priced it: yaml | openrouter-exact | openrouter-guess | openrouter-fuzzy | none
    ("price_source", pa.string()),
])
COLS = [f.name for f in SCHEMA]


def parse_since(s):
    m = re.fullmatch(r"(\d+)([dh])", s)
    if not m:
        raise SystemExit("--since must look like 14d or 36h")
    n = int(m.group(1))
    return timedelta(days=n) if m.group(2) == "d" else timedelta(hours=n)


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def content_str(c):
    if isinstance(c, str):
        return c
    try:
        return json.dumps(c, sort_keys=True, ensure_ascii=False)
    except Exception:
        return str(c)


def sha(s):
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()


CMD_KEYS = ("cmd_family", "cmd_target", "cmd_cwd")
FILE_KEYS = ("file_path", "file_ext", "file_kind")
FILE_TOOLS = {"Read": "file_path", "Edit": "file_path", "Write": "file_path", "NotebookEdit": "notebook_path",
              "MultiEdit": "file_path", "Glob": "path", "Grep": "path", "ReadFile": "path", "WriteFile": "path",
              "StrReplaceFile": "path"}
KIND_EXT = {
    "code": "py pyi js jsx mjs cjs ts tsx sh bash zsh fish go rs c h cc cpp hpp java kt swift rb php lua pl r sql css scss "
            "html htm vue svelte dockerfile makefile cs scala ex exs tf gradle",
    "markdown": "md mdx markdown rst",
    "json-yaml": "json jsonl yaml yml toml ini cfg conf env ipynb xml plist lock",
    "log": "log out err trace",
    "image": "png jpg jpeg gif webp bmp svg heic ico tiff avif",
    "binary": "pdf zip gz tgz tar bz2 xz 7z parquet db sqlite sqlite3 so dylib dll exe bin wasm woff woff2 ttf otf mp4 mov mp3 wav "
              "xlsx xls docx pptx pyc o a jar whl",
}
EXT_KIND = {e: k for k, v in KIND_EXT.items() for e in v.split()}
TRANSCRIPT_PATH = re.compile(r"/\.(?:claude[\w-]*/projects|codex/sessions|kimi[\w-]*/sessions)/|/subagents/agent-|/rollout-[\w-]+\.jsonl$")
SPECIAL_NAMES = {"dockerfile": "dockerfile", "makefile": "makefile", "gnumakefile": "makefile"}


def file_ext_kind(path):
    """(file_ext, file_kind) from a path: lowercase extension, `none`; .env* -> env, Dockerfile/Makefile by name."""
    base = os.path.basename(str(path or "").rstrip("/"))
    low = base.lower()
    if low == ".env" or low.startswith(".env.") or low.endswith(".env"):
        ext = "env"
    elif low in SPECIAL_NAMES or low.startswith("dockerfile."):
        ext = SPECIAL_NAMES.get(low, "dockerfile")
    else:
        ext = os.path.splitext(low)[1].lstrip(".") if not (low.startswith(".") and low.count(".") == 1) else "none"
        ext = ext or "none"
        if not re.fullmatch(r"[a-z0-9_+\-]{1,12}", ext):
            ext = "none"
    kind = EXT_KIND.get(ext, "other")
    if ext == "jsonl" and TRANSCRIPT_PATH.search(str(path)):
        kind = "jsonl-transcript"
    return ext, kind


def file_fields(path, ext_from=None):
    """Event fields for a file path (masked, kept). ext_from: derive ext/kind from this instead (Glob pattern)."""
    if not path or not isinstance(path, str):
        return {}
    ext, kind = file_ext_kind(ext_from or path)
    if ext_from and kind == "other" and ext == "none":
        ext, kind = file_ext_kind(path)
    return {"file_path": mask_secrets(path, 400), "file_ext": ext, "file_kind": kind}


READ_CMDS = {"cat", "sed", "head", "tail", "less", "more", "nl", "bat", "tac"}
OPT_ARG = {"head": {"-n", "-c"}, "tail": {"-n", "-c"}, "sed": {"-e", "-f", "--expression", "--file"}, "bat": {"-r", "--line-range", "-l"}}
VIEW_PATH = re.compile(r"""tools\.view_image\s*\(\s*\{[^}]*?path\s*:\s*(?:`([^`]*)`|"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')""")
STR_LIT = re.compile(r"""(?:"((?:[^"\\\n]|\\.)*)"|'((?:[^'\\\n]|\\.)*)')""")


def shell_file(cmd):
    """File read by a shell command whose primary command is cat/sed/head/tail/less/nl/bat: the last file operand, or None."""
    from .cmdparse import primary, strip_cd
    try:
        t, _ = primary(strip_cd(str(cmd or ""))[0])
    except Exception:
        return None
    if not t:
        return None
    name = os.path.basename(t[0])
    if name not in READ_CMDS:
        return None
    args, pos, skip, script_opt = t[1:], [], False, False
    for a in args:
        if skip:
            skip = False; continue
        if a == "--":
            continue
        if a.startswith("-") and len(a) > 1:
            if a in OPT_ARG.get(name, ()):
                skip = True
                script_opt = script_opt or name == "sed"
            elif name == "sed" and a.startswith(("-e", "-f")) and len(a) > 2 or a.startswith("--expression="):
                script_opt = True
            continue
        pos.append(a)
    if name == "sed" and not script_opt:
        pos = pos[1:]  # first operand is the sed script
    pos = [x for x in pos if not re.fullmatch(r"\d+|[\d,$]+[pq]?", x)]
    return pos[-1] if pos else None


def js_image_path(src):
    """Path of a Codex tools.view_image call inside exec JS: its path: literal, else the first image-looking string literal."""
    m = VIEW_PATH.search(src or "")
    if m:
        return next(g for g in m.groups() if g is not None)
    for m in STR_LIT.finditer(src or ""):
        v = next(g for g in m.groups() if g is not None)
        if file_ext_kind(v)[1] == "image":
            return v
    for m in STR_LIT.finditer(src or ""):
        v = next(g for g in m.groups() if g is not None)
        if v.startswith("/") and len(v) > 8:
            return v
    return None


def claude_file(name, d):
    key = FILE_TOOLS.get(name)
    if not key or not isinstance(d, dict):
        return {}
    path = d.get(key) or d.get("file_path") or d.get("path")
    if name == "Glob":
        pat = d.get("pattern")
        return file_fields(str(path or pat or ""), ext_from=pat if pat and "." in os.path.basename(str(pat)) else None)
    if name == "Grep":
        g = d.get("glob")
        return file_fields(str(path or d.get("pattern") or ""), ext_from=g if g and "." in str(g) else None)
    return file_fields(path)


def codex_file(name, raw, cmd):
    """File fields for a Codex shell-ish call: view_image in exec JS, or a cat/sed/head/tail target."""
    if name == "exec" and "view_image" in str(raw or ""):
        p = js_image_path(str(raw))
        f = file_fields(p)
        if f:
            f["file_ext"], f["file_kind"] = (f["file_ext"], "image")
            return f
        return {"file_path": None, "file_ext": "none", "file_kind": "image"}
    return file_fields(shell_file(cmd))

SECRET_KV = re.compile(r"(?i)\b([A-Z0-9_]*(?:token|secret|passw(?:or)?d|passwd|api[_-]?key|apikey|auth|bearer|credential|private[_-]?key)[A-Z0-9_]*)(\s*[=:]\s*)(['\"]?)[^\s'\";&|)]+")
SECRET_BEARER = re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9._~+/\-]{8,}=*")
SECRET_FLAG = re.compile(r"(?i)(--?(?:token|password|passwd|secret|api-key|key|auth)[= ]\s*)(['\"]?)[^\s'\";&|)]+")
SECRET_PREFIX = re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|xox[abpr]-[A-Za-z0-9\-]{8,}|AKIA[0-9A-Z]{12,}|gAAAA[A-Za-z0-9_\-=]{8,}|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-.]+)")
SECRET_URL = re.compile(r"(\w+://)[^/\s:@]+:[^/\s@]+@")
SECRET_LONG = re.compile(r"(?<![\w/.-])(?=[A-Za-z0-9+_\-]*\d)(?=[A-Za-z0-9+_\-]*[A-Za-z])[A-Za-z0-9+_\-]{32,}={0,2}(?![\w/.-])")


def mask_secrets(cmd, n=80, keep_lines=False):
    """Mask secret-looking tokens, collapse whitespace, keep the first n characters.
    keep_lines: collapse runs of blanks only and keep newlines. With n > 80 the obs/scan.py key shapes
    (PEM header, JWT, Telegram bot token, API keys, high-entropy base64 runs) are masked too."""
    if keep_lines:
        c = "\n".join(" ".join(l.split()) for l in str(cmd or "").strip().splitlines())
    else:
        c = " ".join(str(cmd or "").split())
    c = SECRET_URL.sub(r"\1***@", c)
    c = SECRET_PREFIX.sub("***", c)
    c = SECRET_BEARER.sub(r"\1\2***", c)
    c = SECRET_KV.sub(lambda m: m.group(1) + m.group(2) + m.group(3) + "***", c)
    c = SECRET_FLAG.sub(lambda m: m.group(1) + m.group(2) + "***", c)
    c = SECRET_LONG.sub("***", c)
    if n > 80:
        c = _scan_mask(c)
    return c[:n]


SCAN_SECRET_KINDS = ("pem", "jwt", "telegram_bot_token", "api_key", "base64_secret")


def _scan_mask(c):
    from .scan import scan_text  # lazy: scan imports this module
    out, pos = [], 0
    for a, b, kind, _ in scan_text(c):
        if kind in SCAN_SECRET_KINDS:
            out.append(c[pos:a] + "***")
            pos = b
    return "".join(out) + c[pos:] if out else c


def shell_fields(cmd):
    """Event fields for a shell tool_use: detail (80), cmd_text (2000), cmd_cwd, cmd_family, cmd_target.
    Parsed from the raw command; every stored string is masked."""
    from .cmdparse import parse
    p = parse(str(cmd or ""), 2000, lambda s, n: mask_secrets(s, n, keep_lines=True))
    p["detail"] = mask_secrets(cmd)
    return p


EXIT_RE = re.compile(r'(?:exit_code\\?"?\s*:\s*|exited with code\s+|Exit code:?\s+)(-?\d+)')
SKILL_RE = re.compile(r"skills/([A-Za-z0-9_.:\-]+)/SKILL\.md")
CODEX_CMD_RE = re.compile(r"""cmd\s*:\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|`([^`]*)`)""")
SHELL_TOOLS = ("Bash", "exec_command", "shell", "shell_command", "exec", "local_shell")
SPAWN_TOOLS = ("Agent", "Task", "spawn_agent")


LANE_PATH = re.compile(r"/\.world/team/([^/]+)/workspace(?:/|$)")
LANE_SLUG = re.compile(r"-home-[^/-]+--world-team-([^/]+?)-workspace(?:/|$)")


def lane_of(path, fallback=None):
    """project = lane name: <lane> for paths under ~/.world/team/<lane>/workspace or a scratchpad / project
    folder named -home-<user>--world-team-<lane>-workspace; else the basename of path (or fallback)."""
    p = str(path or "")
    m = LANE_PATH.search(p) or LANE_SLUG.search(p + "/" if not p.endswith("/") else p)
    if m:
        return m.group(1)
    if p:
        return os.path.basename(p.rstrip("/")) or fallback
    return fallback


DATA_URL = re.compile(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]*")
IMAGE_TYPES = ("image", "input_image", "image_url", "output_image")
IMAGE_TOKENS = 1500  # approximation per image (provider formulas depend on resolution); see README


def image_parts(x):
    """(image_count, image_bytes) inside a tool result: Claude image blocks (source.data), Codex input_image
    image_url fields and data:image/...;base64 URLs anywhere in strings. Bytes are the base64 / URL length."""
    n = b = 0
    stack = [x]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            if v.get("type") in IMAGE_TYPES:
                src = v.get("source")
                pay = (src.get("data") if isinstance(src, dict) else None) or v.get("image_url") or v.get("url")
                if isinstance(pay, dict):
                    pay = pay.get("url")
                n += 1
                b += len(pay) if isinstance(pay, str) else 0
                continue
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
        elif isinstance(v, str) and "data:image/" in v:
            for m in DATA_URL.finditer(v):
                n += 1
                b += len(m.group(0))
    return n, b


def sizes(raw, s):
    """content_bytes, text_bytes, image_bytes, image_count, estimated_tokens for one tool result.
    estimated_tokens = text_bytes / 3.6 + image_count * IMAGE_TOKENS."""
    total = len(s.encode("utf-8", "replace"))
    n, ib = image_parts(raw)
    ib = min(ib, total)
    tb = total - ib
    return dict(content_bytes=total, text_bytes=tb, image_bytes=ib, image_count=n,
                estimated_tokens=round(tb / 3.6 + n * IMAGE_TOKENS))


def ctx_of(r):
    return int(r.get("input_tokens") or 0) + int(r.get("cache_read_tokens") or 0) + int(r.get("cache_write_tokens") or 0)


def find_boundaries(reqs, marks):
    """Compaction boundaries for one session -> (timestamps, info). Explicit markers win (Claude Code
    type=system/subtype=compact_boundary, compactMetadata.trigger manual|auto; Codex top-level type=compacted,
    no trigger field -> unknown); with none, fall back to a request whose context is <60% of the previous
    request's and the previous was >30k tokens (trigger unknown). info[i] = (trigger, pre_tokens, src_line)."""
    if marks:
        m = sorted(marks, key=lambda x: x[0])
        return [x[0] for x in m], [(x[1], x[2], x[3]) for x in m]
    out, info, prev = [], [], None
    for r in sorted(reqs, key=lambda r: r["ts"]):
        c = ctx_of(r)
        if prev is not None and prev > 30000 and c < 0.6 * prev:
            out.append(r["ts"])
            info.append(("unknown", prev, r.get("src_line")))
        prev = c
    return out, info


def compaction_rows(reqs, bounds, info, sid, sink, stats, cutoff):
    """One event_type='compaction' row per boundary: detail = manual|auto|unknown, input_tokens = context before it."""
    tpl = min(reqs, key=lambda r: r["ts"]) if reqs else None
    if tpl is None:
        return
    for n, (b, (trig, pre, ln)) in enumerate(zip(bounds, info), start=1):
        if b < cutoff:
            continue
        r = {c: None for c in COLS}
        r.update(event_id=str(uuid.uuid4()), ts=b, host=tpl["host"], agent=tpl["agent"], session_id=sid,
                 parent_session_id=tpl.get("parent_session_id"), project=tpl["project"], provider=tpl.get("provider"),
                 event_type="compaction", detail=trig, input_tokens=int(pre or 0), src_file=tpl["src_file"],
                 src_line=ln, segment_index=n, compaction_id=f"{sid}#{n}", redaction_version="0")
        sink.add(r)
        stats["events"] += 1


def tag_requests(reqs, bounds, sid):
    for r in reqs:
        n = bisect.bisect_right(bounds, r["ts"])
        r["segment_index"], r["compaction_id"] = n, f"{sid}#{n}"


def add_tail(rows, reqs, bounds, sink, stats, sid=None):
    """reread_turns = llm_requests after the row's ts in the same session AND the same compaction segment
    (until the next compaction); reread_tokens = estimated_tokens x reread_turns. Hands rows to the sink."""
    req_ts = sorted(r["ts"] for r in reqs)
    for e in rows:
        n = bisect.bisect_right(bounds, e["ts"])
        end = bisect.bisect_left(req_ts, bounds[n]) if n < len(bounds) else len(req_ts)
        k = max(end - bisect.bisect_right(req_ts, e["ts"]), 0)
        e["reread_turns"] = k
        e["reread_tokens"] = int(e.get("estimated_tokens") or 0) * k
        e["segment_index"], e["compaction_id"] = n, f"{sid or e.get('session_id')}#{n}"
        sink.add(e)
        stats["events"] += 1


def set_list_cost(r):
    r["price_source"] = prices().resolve(r.get("model"))[1]
    r["list_cost_usd"] = prices().cost(r.get("model"), r.get("input_tokens"), r.get("cache_read_tokens"),
                                       r.get("cache_write_tokens"), r.get("output_tokens"))


def exit_code_of(s):
    m = EXIT_RE.search(s[:2000])
    return int(m.group(1)) if m else None


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t is not None else None


def coverage_of(name, files, sink):
    """Per-source-root data coverage: transcript file count, bytes, mtime range, first/last event ts."""
    mt, size = [], 0
    for p in files:
        try:
            st = os.stat(p)
            mt.append(st.st_mtime)
            size += st.st_size
        except OSError:
            pass
    ev = lambda t: t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None
    return {"name": name, "files": len(mt), "bytes": size, "mtime_earliest": _iso(min(mt) if mt else None),
            "mtime_latest": _iso(max(mt) if mt else None), "event_first": ev(sink.first), "event_last": ev(sink.last)}


def glob_t(pattern, recursive=False):
    """Transcripts matching a *.jsonl pattern, plus archived *.jsonl.gz ones (unless the plain file still exists)."""
    plain = glob.glob(pattern, recursive=recursive)
    have = set(plain)
    return plain + [g for g in glob.glob(pattern + ".gz", recursive=recursive) if g[:-3] not in have]


def iter_lines(path):
    """(1-based line number, decoded line); splits on \\n only, so numbers match `sed -n Np`."""
    with retain.open_t(path) as fh:
        for n, raw in enumerate(fh, 1):
            yield n, raw.decode("utf-8", "replace")


def ms_between(a, b):
    return int((b - a).total_seconds() * 1000) if a is not None and b is not None else None


def harness_of_key(key):
    """claude -> claude-code; claude-<profile> -> claude-code:<profile>; codex; kimi -> kimi-code.
    claude-restored-* (restored copies of the main ~/.claude/projects) stay plain claude-code."""
    if key == "claude" or key.startswith("claude-restored"):
        return "claude-code"
    if key.startswith("claude-"):
        return "claude-code:" + key[len("claude-"):]
    return {"kimi": "kimi-code"}.get(key, key)


def event_key(row):
    """Identity of an event independent of which copy of a transcript it was read from (64-bit hash)."""
    return hash((row.get("session_id"), row.get("event_type"), row["ts"], row.get("msg_id"), row.get("tool_call_id")))


class Sink:
    def __init__(self, out, suffix="", harness="claude-code"):
        self.harness = harness
        self.dir = os.path.join(out, "events")
        self.host = os.environ.get("OBS_HOST") or socket.gethostname()
        self.fname = self.host + suffix  # one file per source per day, so sources never overwrite each other
        self.writers, self.bufs, self.counts = {}, {}, {}
        self.first = self.last = None  # earliest / latest event ts seen
        self.skip = None  # event keys already emitted by an earlier root in the same run (cross-root de-dup)
        self.keys = None  # set: record this sink's event keys for later roots
        self.dupes = 0

    def add(self, row):
        if self.skip is not None or self.keys is not None:
            k = event_key(row)
            if self.skip is not None and k in self.skip:
                self.dupes += 1
                return
            if self.keys is not None:
                self.keys.add(k)
        row["harness"] = self.harness
        t = row["ts"]
        if self.first is None or t < self.first:
            self.first = t
        if self.last is None or t > self.last:
            self.last = t
        d = row["ts"].strftime("%Y-%m-%d")
        b = self.bufs.setdefault(d, [])
        b.append(row)
        self.counts[d] = self.counts.get(d, 0) + 1
        if len(b) >= 20000:
            self.flush(d)

    def flush(self, d):
        b = self.bufs.get(d)
        if not b:
            return
        if d not in self.writers:
            p = os.path.join(self.dir, d)
            os.makedirs(p, exist_ok=True)
            # write to .tmp and rename on close, so readers globbing *.parquet never see a half-written file
            self.writers[d] = pq.ParquetWriter(os.path.join(p, self.fname + ".parquet.tmp"), SCHEMA, compression="zstd")
        cols = {c: [r.get(c) for r in b] for c in COLS}
        self.writers[d].write_table(pa.Table.from_pydict(cols, schema=SCHEMA))
        self.bufs[d] = []

    def close(self):
        for d in list(self.bufs):
            self.flush(d)
        for d, w in self.writers.items():
            w.close()
            f = os.path.join(self.dir, d, self.fname + ".parquet")
            os.replace(f + ".tmp", f)


def base(host, ts, o, project, agent):
    r = {c: None for c in COLS}
    r.update(event_id=str(uuid.uuid4()), ts=ts, host=host, agent=agent,
             session_id=o.get("sessionId"), project=project, provider="anthropic",
             redaction_version="0")
    return r


def read_meta(path):
    """Subagent side file <agent>.meta.json: toolUseId of the spawning Agent call, agentType, model."""
    try:
        with open(retain.logical(path)[:-6] + ".meta.json") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def process_file(path, cutoff, sink, stats, parent=None):
    """parent: enclosing session id for subagent transcripts (session_id becomes the agent file stem)."""
    gz_path, path = path, retain.logical(path)  # archived .jsonl.gz: names and src_file stay the original .jsonl
    root = ROOT_HINT[0]
    relp = os.path.relpath(path, root) if root else os.path.basename(path)
    rel = relp.split(os.sep) if root else [os.path.basename(os.path.dirname(path))]
    src = SRC_KEY[0] + "/" + relp.replace(os.sep, "/")
    project_dir = rel[0]
    session_fallback = os.path.basename(path)[:-6]
    spawn_key = read_meta(path).get("toolUseId") if parent else None
    cost = None  # last cost-state totalCostUSD in this file
    tool_names, tool_inputs, tool_ts = {}, {}, {}
    cmdinfo = {}  # tool_call_id -> cmd_family / cmd_target / cmd_cwd, copied onto the tool_result row
    msgs = {}  # message.id -> row (dedupe)
    results = []  # tool_result rows, written at the end once reread_turns is known
    marks = []  # compact_boundary timestamps
    for ln, line in iter_lines(gz_path):
        stats["lines"] += 1
        try:
            o = json.loads(line)
            if not isinstance(o, dict):
                raise ValueError
        except Exception:
            stats["skipped"] += 1
            continue
        t = o.get("type")
        if t == "cost-state":
            try:
                cost = float(o["totalCostUSD"])
            except Exception:
                pass
            continue
        if t == "system" and o.get("subtype") == "compact_boundary":
            try:
                cm = o.get("compactMetadata") or {}
                tr = cm.get("trigger") if cm.get("trigger") in ("manual", "auto") else "unknown"
                marks.append((parse_ts(o["timestamp"]), tr, cm.get("preTokens"), ln))
            except Exception:
                pass
            continue
        if t not in ("assistant", "user"):
            continue
        try:
            ts = parse_ts(o["timestamp"])
            msg = o.get("message") or {}
            cwd = o.get("cwd")
            project = lane_of(cwd, None) if cwd else lane_of(project_dir)
            if cwd and project == os.path.basename(cwd.rstrip("/")):
                pl = lane_of(project_dir)  # cwd outside a lane: the project folder may still name one
                project = pl if pl != project_dir else project
            agent = "claude-code-sidechain" if o.get("isSidechain") else "claude-code"
            if parent:
                agent = "subagent"
                o["sessionId"] = session_fallback
            elif not o.get("sessionId"):
                o["sessionId"] = session_fallback

            def mk(**kw):
                e = base(sink.host, ts, o, project, agent)
                e.update(parent_session_id=parent, src_file=src, src_line=ln, spawn_key=spawn_key)
                e.update(kw)
                return e
            content = msg.get("content")
            if t == "assistant":
                mid = msg.get("id") or o.get("uuid")
                u = msg.get("usage") or {}
                if mid not in msgs:
                    msgs[mid] = mk(event_type="llm_request", model=msg.get("model"), msg_id=msg.get("id"),
                                   input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0)
                r = msgs[mid]
                for k, f in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
                             ("cache_read_tokens", "cache_read_input_tokens"),
                             ("cache_write_tokens", "cache_creation_input_tokens")):
                    r[k] = max(r[k], int(u.get(f) or 0))
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "tool_use":
                            tid, name = c.get("id"), c.get("name")
                            inp = c.get("input")
                            raw = content_str(inp)
                            tool_names[tid], tool_ts[tid] = name, ts
                            ih = sha(raw)
                            tool_inputs[tid] = ih
                            if ts >= cutoff:
                                kw = {}
                                d = inp if isinstance(inp, dict) else {}
                                if name == "Bash":
                                    kw.update(shell_fields(d.get("command")))
                                    cmdinfo[tid] = {k: kw[k] for k in CMD_KEYS}
                                elif name in FILE_TOOLS:
                                    kw.update(claude_file(name, d))
                                    cmdinfo[tid] = {k: kw[k] for k in FILE_KEYS if k in kw}
                                elif name == "Skill":
                                    kw["skill_name"] = str(d.get("skill") or "?")
                                elif name in SPAWN_TOOLS:
                                    kw.update(detail=str(d.get("subagent_type") or "general-purpose"),
                                              model_requested=str(d.get("model") or "default"), spawn_key=tid)
                                sink.add(mk(event_type="tool_call", model=msg.get("model"), msg_id=msg.get("id"),
                                            tool_name=name, tool_call_id=tid, input_sha256=ih,
                                            content_bytes=len(raw.encode("utf-8", "replace")), **kw))
                                stats["events"] += 1
            elif isinstance(content, list) and ts >= cutoff:
                tur = o.get("toolUseResult")
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        s = content_str(c.get("content"))
                        sz = sizes(c.get("content"), s)
                        tid = c.get("tool_use_id")
                        name = tool_names.get(tid)
                        err = bool(c.get("is_error"))
                        kw = {}
                        if name == "Bash":
                            kw["exit_code"] = (exit_code_of(s) if err else 0)
                        if name == "Bash" or name in FILE_TOOLS:
                            kw.update(cmdinfo.get(tid, {}))
                        if name in SPAWN_TOOLS and isinstance(tur, dict) and tur.get("agentId"):
                            kw["child_session_id"] = "agent-" + str(tur["agentId"])
                            kw["model"] = tur.get("resolvedModel")
                        e = mk(event_type="tool_result", tool_name=name, tool_call_id=tid,
                               result_sha256=sha(s), **sz,
                               is_error=err, input_sha256=tool_inputs.get(tid),
                               duration_ms=ms_between(tool_ts.get(tid), ts), **kw)
                        results.append(e)
                        if err:
                            x = dict(e, event_id=str(uuid.uuid4()), event_type="error", result_sha256=None)
                            sink.add(x)
                            stats["events"] += 1
        except Exception:
            stats["skipped"] += 1
    # cost_usd: per-session totalCostUSD (last cost-state line) is attached to the LAST llm_request
    # of the file; all other rows stay NULL, so sum(cost_usd) per session is the session total.
    if cost is not None and msgs:
        stats["cost_sessions"] = stats.get("cost_sessions", 0) + 1
        max(msgs.values(), key=lambda r: r["ts"])["cost_usd"] = cost
    sidc = next((r["session_id"] for r in list(msgs.values()) + results), session_fallback)
    bounds, binfo = find_boundaries(list(msgs.values()), marks)
    tag_requests(msgs.values(), bounds, sidc)
    for r in msgs.values():
        r["provider"] = codex_provider(r.get("model"), "anthropic")
    compaction_rows(list(msgs.values()), bounds, binfo, sidc, sink, stats, cutoff)
    add_tail(results, list(msgs.values()), bounds, sink, stats, sidc)
    for r in msgs.values():
        if r["ts"] >= cutoff:
            r["provider"] = codex_provider(r.get("model"), "anthropic")
            set_list_cost(r)
            sink.add(r)
            stats["events"] += 1


ROOT_HINT = [None]
SRC_KEY = ["claude"]


def extra_claude_roots():
    """~/.claude-*/projects dirs (other Claude Code config homes, e.g. .claude-deepseek)."""
    return sorted(glob.glob(os.path.expanduser("~/.claude-*/projects")))


def split_root(arg):
    """'NAME=PATH' -> (claude-NAME, PATH); a bare PATH -> (None, PATH)."""
    name, eq, path = arg.partition("=")
    if eq and name and os.sep not in name and re.fullmatch(r"[\w.-]+", name):
        return "claude-" + name, path
    return None, arg


def root_key(root):
    """sources.json key / src_file prefix for an extra Claude root: claude-<name> from ~/.claude-<name>/projects,
    or an explicit label given as NAME=PATH (e.g. restored-r2=/srv/restore/projects -> claude-restored-r2)."""
    label, root = split_root(root)
    if label:
        return label
    d = os.path.basename(os.path.dirname(os.path.abspath(root)))
    return "claude-" + d[len(".claude-"):] if d.startswith(".claude-") else "claude-" + re.sub(r"\W+", "_", d.strip("."))


def run(since, out, root=None, align_day=False):
    """root: one projects dir, or a list of them. The first (default ~/.claude/projects) is source 'claude';
    extra roots are sources 'claude-<name>' with their own parquet file per day (NAME=PATH sets the name).
    Cross-root de-dup for restored roots (claude-restored-*): an event (session, type, ts, msg id, tool call id)
    already emitted by an earlier root in this run is dropped, so restored copies / prefixes of live transcripts
    never double count. Other profile roots are independent config homes and are not de-duplicated."""
    roots = list(root) if isinstance(root, (list, tuple)) and root else [root if not isinstance(root, (list, tuple)) else None]
    keys = ["claude"] + [root_key(r) for r in roots[1:]]
    restored = [k.startswith("claude-restored") for k in keys]
    rc, seen = 0, set()
    for i, r in enumerate(roots):
        dd = (seen if restored[i] else None, set() if any(restored[i + 1:]) else None)
        if i == 0:
            rc = run_one(since, out, r, align_day, dedup=dd) or rc
        else:
            rc = run_one(since, out, split_root(r)[1], align_day, key=root_key(r), dedup=dd) or rc
        if dd[1]:
            seen |= dd[1]
    return rc


def run_one(since, out, root=None, align_day=False, key="claude", dedup=(None, None)):
    """align_day: start the window at UTC midnight so every day file that gets rewritten is complete."""
    t0 = time.time()
    root = root or os.path.expanduser("~/.claude/projects")
    SRC_KEY[0] = key
    cutoff = datetime.now(timezone.utc) - parse_since(since)
    if align_day:
        cutoff = cutoff.replace(hour=0, minute=0, second=0, microsecond=0)
    ROOT_HINT[0] = root
    write_sources(out, **{key: root})
    files, sub = [], set()
    pats = [(os.path.join(root, "*", "*.jsonl"), False),
            (os.path.join(root, "*", "*", "subagents", "**", "*.jsonl"), True)]
    for pat, is_sub in pats:
        for p in glob_t(pat, recursive=True):
            try:
                if os.path.getmtime(p) >= cutoff.timestamp():
                    files.append(p)
                    if is_sub:
                        sub.add(p)
            except OSError:
                pass
    files = sorted(set(files))
    stats = {"files": 0, "subagent_files": 0, "lines": 0, "skipped": 0, "events": 0, "cost_sessions": 0}
    sink = Sink(out, "" if key == "claude" else "-" + key, harness_of_key(key))
    sink.skip, sink.keys = dedup
    for p in files:
        try:
            parent = os.path.relpath(p, root).split(os.sep)[1] if p in sub else None
            process_file(p, cutoff, sink, stats, parent)
        except OSError as e:
            print("skip file", p, e, file=sys.stderr)
            continue
        stats["files"] += 1
        stats["subagent_files"] += p in sub
        if stats["files"] % 50 == 0:
            print(f"[{stats['files']}/{len(files)}] lines={stats['lines']} events={stats['events']}", flush=True)
    sink.close()
    el = time.time() - t0
    mb = sum(os.path.getsize(f) for f in glob.glob(os.path.join(out, "events", "*", "*.parquet"))) / 1e6
    stats["dup_events_skipped"] = sink.dupes
    stats.update(ingest_s=round(el, 1), parquet_mb=round(mb, 2), days=sorted(sink.counts), coverage=coverage_of(key, files, sink))
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "ingest_stats.json" if key == "claude" else f"ingest_stats_{key}.json"), "w") as f:
        json.dump(stats, f)
    print(json.dumps(stats))
    return 0


# ---------------------------------------------------------------- Codex CLI rollouts
PROVIDERS = (("deepseek", "deepseek"), ("glm", "zhipu"), ("qwen", "alibaba"), ("kimi", "moonshot"))


def codex_provider(model, default="openai"):
    m = (model or "").lower()
    for pre, prov in PROVIDERS:
        if m.startswith(pre) or ("/" + pre) in m:
            return prov
    return default


def codex_command(name, raw):
    """Command text for a Codex shell-ish call (exec_command/shell args, or the cmd inside an exec script)."""
    if name == "exec":
        m = CODEX_CMD_RE.search(raw or "")
        if m:
            v = next(g for g in m.groups() if g is not None)
            try:
                return json.loads('"' + v + '"') if m.group(1) is not None else v
            except ValueError:
                return v
        return raw
    try:
        a = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except ValueError:
        return raw
    if not isinstance(a, dict):
        return raw
    c = a.get("cmd") or a.get("command") or ""
    if isinstance(c, list):
        c = [str(x) for x in c]
        if len(c) >= 3 and os.path.basename(c[0]) in ("bash", "sh", "zsh") and c[1].startswith("-") and "c" in c[1]:
            return c[-1]  # bash -lc '<script>': the script, not the quoted-away join
        return shlex.join(c)
    return str(c)


def process_codex_file(path, cutoff, sink, stats):
    """Codex usage arrives twice (token_usage_record and event_msg/token_count); records are keyed by
    response_id and token_count is only used for files that have no token_usage_record.
    Codex input_tokens INCLUDES cached_input_tokens, so input = input - cached, cache_read = cached."""
    gz_path, path = path, retain.logical(path)
    sid = os.path.basename(path)[:-6].split("-", 6)[-1]
    root = ROOT_HINT_CODEX[0]
    src = "codex/" + (os.path.relpath(path, root) if root else os.path.basename(path)).replace(os.sep, "/")
    parent, project, prov, model, agent, spawn_key = None, None, "openai", None, "codex", None
    names, inputs, call_ts = {}, {}, {}
    cmdinfo = {}  # tool_call_id -> cmd_family / cmd_target / cmd_cwd, copied onto the tool_result row
    recs = {}
    results = []
    marks = []
    for ln, line in iter_lines(gz_path):
        stats["lines"] += 1
        try:
            o = json.loads(line)
            p = o.get("payload") or {}
            t, pt = o.get("type"), p.get("type")
            ts = parse_ts(o["timestamp"])
            if t == "session_meta":
                sid = p.get("id") or sid
                parent = p.get("parent_thread_id")
                prov = p.get("model_provider") or "openai"
                if parent:
                    agent = "subagent"
                    if p.get("agent_path"):
                        spawn_key = f"{parent}:{str(p['agent_path']).rstrip('/').rsplit('/', 1)[-1]}"
                if p.get("cwd"):
                    project = lane_of(p["cwd"])
                continue
            if t == "turn_context":
                model = p.get("model") or model
                continue
            if t == "compacted":
                marks.append((ts, "unknown", None, ln))  # Codex records carry no manual/auto field
                continue
            o2 = {"sessionId": sid}
            def mk(**kw):
                e = base(sink.host, ts, o2, project, agent)
                e.update(parent_session_id=parent, provider=codex_provider(model, prov), model=model,
                         src_file=src, src_line=ln, spawn_key=spawn_key)
                e.update(kw)
                return e
            if t == "token_usage_record" or (t == "event_msg" and pt == "token_count"):
                u = p.get("usage") if t == "token_usage_record" else ((p.get("info") or {}).get("last_token_usage"))
                if not u:
                    continue
                key = p.get("response_id") or f"ord{o.get('ordinal')}"
                kind = "rec" if t == "token_usage_record" else "cnt"
                inp, cr = int(u.get("input_tokens") or 0), int(u.get("cached_input_tokens") or 0)
                r = mk(event_type="llm_request", input_tokens=max(inp - cr, 0), cache_read_tokens=cr,
                       output_tokens=int(u.get("output_tokens") or 0), msg_id=p.get("response_id"),
                       cache_write_tokens=int(u.get("cache_write_input_tokens") or 0))
                recs.setdefault(kind, {}).setdefault(key, r)  # de-dup: first occurrence wins
            elif t == "response_item" and pt in ("function_call", "custom_tool_call"):
                cid, name = p.get("call_id"), p.get("name")
                raw = p.get("arguments") if pt == "function_call" else p.get("input")
                rs = content_str(raw)
                ih = sha(rs)
                names[cid], inputs[cid], call_ts[cid] = name, ih, ts
                if ts >= cutoff:
                    kw = {}
                    if name in SHELL_TOOLS:
                        cmd = codex_command(name, raw)
                        kw.update(shell_fields(cmd))
                        cmdinfo[cid] = {k: kw[k] for k in CMD_KEYS}
                        ff = codex_file(name, rs, cmd)
                        kw.update(ff)
                        cmdinfo[cid].update(ff)
                        m = SKILL_RE.search(rs)
                        if m:
                            kw["skill_name"] = m.group(1)
                    elif name in SPAWN_TOOLS:
                        try:
                            a = json.loads(raw) if isinstance(raw, str) else (raw or {})
                        except ValueError:
                            a = {}
                        a = a if isinstance(a, dict) else {}
                        kw.update(detail=str(a.get("agent_type") or "codex-agent"),
                                  model_requested=str(a.get("model") or "default"),
                                  spawn_key=f"{sid}:{a.get('task_name')}" if a.get("task_name") else cid)
                    sink.add(mk(event_type="tool_call", tool_name=name, tool_call_id=cid, input_sha256=ih,
                                msg_id=p.get("id"), content_bytes=len(rs.encode("utf-8", "replace")), **kw))
                    stats["events"] += 1
            elif t == "response_item" and pt in ("function_call_output", "custom_tool_call_output") and ts >= cutoff:
                cid = p.get("call_id")
                name = names.get(cid)
                s = content_str(p.get("output"))
                sz = sizes(p.get("output"), s)
                kw, err = {}, False
                if name in SHELL_TOOLS:
                    ec = exit_code_of(s)
                    err = (ec is not None and ec != 0) or s[:300].find("Script failed") >= 0
                    kw["exit_code"] = ec
                    kw.update(cmdinfo.get(cid, {}))
                results.append(mk(event_type="tool_result", tool_name=name, tool_call_id=cid, **sz,
                                  result_sha256=sha(s), is_error=err,
                                  input_sha256=inputs.get(cid), duration_ms=ms_between(call_ts.get(cid), ts), **kw))
        except Exception:
            stats["skipped"] += 1
    reqs = list((recs.get("rec") or recs.get("cnt") or {}).values())
    for r in results:
        r["session_id"], r["project"] = sid, project
    bounds, binfo = find_boundaries(reqs, marks)
    tag_requests(reqs, bounds, sid)
    for r in reqs:
        r["session_id"], r["project"] = sid, project
    compaction_rows(reqs, bounds, binfo, sid, sink, stats, cutoff)
    add_tail(results, reqs, bounds, sink, stats, sid)
    for r in reqs:
        set_list_cost(r)
        r["session_id"], r["project"] = sid, project
        if r["ts"] >= cutoff:
            sink.add(r)
            stats["events"] += 1
            stats["models"].add(r["model"])


ROOT_HINT_CODEX = [None]


def write_sources(out, **roots):
    """Record transcript roots so pointers (src_file = <source>/<relative path>) can be resolved later."""
    p = os.path.join(out, "sources.json")
    cur = {}
    try:
        cur = json.load(open(p))
    except (OSError, ValueError):
        pass
    cur.update({k: os.path.abspath(v) for k, v in roots.items()})
    os.makedirs(out, exist_ok=True)
    with open(p + ".tmp", "w") as f:
        json.dump(cur, f)
    os.replace(p + ".tmp", p)


def run_codex(since, out, root=None, align_day=False):
    t0 = time.time()
    root = root or os.path.expanduser("~/.codex/sessions")
    ROOT_HINT_CODEX[0] = root
    write_sources(out, codex=root)
    cutoff = datetime.now(timezone.utc) - parse_since(since)
    if align_day:
        cutoff = cutoff.replace(hour=0, minute=0, second=0, microsecond=0)
    files = []
    for p in glob_t(os.path.join(root, "**", "rollout-*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(p) >= cutoff.timestamp():
                files.append(p)
        except OSError:
            pass
    stats = {"files": 0, "lines": 0, "skipped": 0, "events": 0, "models": set()}
    sink = Sink(out, "-codex", harness_of_key("codex"))
    for p in sorted(files):
        try:
            process_codex_file(p, cutoff, sink, stats)
        except OSError as e:
            print("skip file", p, e, file=sys.stderr)
            continue
        stats["files"] += 1
    sink.close()
    stats.update(models=sorted(m for m in stats["models"] if m), ingest_s=round(time.time() - t0, 1), days=sorted(sink.counts), coverage=coverage_of("codex", files, sink))
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "ingest_stats_codex.json"), "w") as f:
        json.dump(stats, f)
    print(json.dumps(stats))
    return 0


LINEAGE_DEFAULT = "~/.local/state/agent-observatory/lineage.jsonl"
LINEAGE_SCHEMA = pa.schema([
    ("ts", pa.timestamp("ms", tz="UTC")), ("session_id", pa.string()), ("project", pa.string()),
    ("tool_name", pa.string()), ("subagent_type", pa.string()), ("model", pa.string()),
    ("agent_id", pa.string()), ("child_session_id", pa.string()), ("src_line", pa.int64()),
])


def run_lineage(out, path=None):
    """Spawn lineage written by a PreToolUse hook (one JSON line per Agent/Task spawn: ts, session_id, cwd,
    tool_name, subagent_type, model, agentId) -> <out>/lineage/lineage.parquet. The description text is not kept.
    Missing file: nothing is written and any earlier table is left as is."""
    path = os.path.expanduser(path or os.environ.get("OBS_LINEAGE") or LINEAGE_DEFAULT)
    if not os.path.isfile(path):
        print(json.dumps({"lineage": None, "note": f"not found: {path}"}))
        return 0
    rows, skipped = [], 0
    for ln, line in iter_lines(path):
        try:
            o = json.loads(line)
            aid = o.get("agentId") or o.get("agent_id")
            rows.append({"ts": parse_ts(o["ts"]), "session_id": o.get("session_id"), "project": lane_of(o.get("cwd")),
                         "tool_name": o.get("tool_name"), "subagent_type": o.get("subagent_type"),
                         "model": o.get("model"), "agent_id": aid, "child_session_id": f"agent-{aid}" if aid else None,
                         "src_line": ln})
        except Exception:
            skipped += 1
    d = os.path.join(out, "lineage")
    os.makedirs(d, exist_ok=True)
    f = os.path.join(d, "lineage.parquet")
    pq.write_table(pa.Table.from_pylist(rows, schema=LINEAGE_SCHEMA), f + ".tmp", compression="zstd")
    os.replace(f + ".tmp", f)
    write_sources(out, lineage=path)
    print(json.dumps({"lineage": len(rows), "skipped": skipped, "path": path}))
    return 0


def run_all(since, out, claude_root=None, codex_root=None, align_day=False):
    """claude_root/codex_root None means the default home folders (plus ~/.claude-*/projects and ~/.kimi-code/sessions).
    Also refreshes the lineage table."""
    roots = list(claude_root) if isinstance(claude_root, (list, tuple)) else [claude_root]
    if claude_root is None:
        roots += extra_claude_roots()
    run(since, out, roots, align_day)
    rc = run_codex(since, out, codex_root, align_day)
    if claude_root is None:  # real home layout only (synthetic roots have no lineage file)
        run_lineage(out)
        if os.path.isdir(os.path.expanduser("~/.kimi-code/sessions")):
            run_kimi(since, out, None, align_day)
    return rc


# ---------------------------------------------------------------- Kimi Code sessions
# <root>/wd_<dirname>_<hash>/session_<uuid>/agents/<agent>/wire.jsonl ; "main" is the session, other agents are subagents.
# Records carry ms-epoch "time". Request = loop step.end (usage + latency); usage.record (scope turn) is the
# fallback when a file has no step.end usage. Tool events: context.append_loop_event with event.type
# tool.call (name, args, toolCallId) / tool.result (toolCallId, result.output, result.durationMs).
ROOT_HINT_KIMI = [None]
WD_RE = re.compile(r"^wd_(.+)_[0-9a-f]{6,}$")


def kimi_ts(o):
    return datetime.fromtimestamp(o["time"] / 1000, tz=timezone.utc)


def process_kimi_file(path, cutoff, sink, stats):
    root = ROOT_HINT_KIMI[0]
    relp = os.path.relpath(path, root) if root else os.path.basename(path)
    src = "kimi/" + relp.replace(os.sep, "/")
    parts = relp.split(os.sep)
    wd = parts[0] if len(parts) > 3 else ""
    m = WD_RE.match(wd)
    project = m.group(1) if m else (wd or None)
    sess_dir = os.path.dirname(os.path.dirname(os.path.dirname(path)))
    sid = os.path.basename(sess_dir)
    sid = sid[len("session_"):] if sid.startswith("session_") else sid
    try:
        with open(os.path.join(sess_dir, "state.json")) as f:
            cwd = json.load(f).get("cwd")
        if cwd:
            project = lane_of(cwd, project)
    except (OSError, ValueError, AttributeError):
        pass
    aname = os.path.basename(os.path.dirname(path))
    parent, agent = None, "kimi"
    if aname != "main":
        parent, agent = sid, "subagent"
        sid = f"{sid}:{aname}"
    model = None
    names, inputs, call_ts = {}, {}, {}
    cmdinfo = {}  # tool_call_id -> cmd_family / cmd_target / cmd_cwd, copied onto the tool_result row
    steps, usage_recs, results = [], [], []
    for ln, line in iter_lines(path):
        stats["lines"] += 1
        try:
            o = json.loads(line)
            t = o.get("type")
            if t not in ("llm.request", "usage.record", "context.append_loop_event") or "time" not in o:
                continue
            ts = kimi_ts(o)
            o2 = {"sessionId": sid}

            def mk(**kw):
                e = base(sink.host, ts, o2, project, agent)
                e.update(parent_session_id=parent, provider=codex_provider(model, "moonshot"), model=model,
                         src_file=src, src_line=ln)
                e.update(kw)
                return e
            if t == "llm.request":
                model = o.get("model") or model
                continue
            if t == "usage.record":
                u = o.get("usage") or {}
                usage_recs.append(mk(event_type="llm_request", input_tokens=int(u.get("inputOther") or 0),
                                     output_tokens=int(u.get("output") or 0), cache_read_tokens=int(u.get("inputCacheRead") or 0),
                                     cache_write_tokens=int(u.get("inputCacheCreation") or 0)))
                continue
            ev = o.get("event") or {}
            et = ev.get("type")
            if et == "step.end" and ev.get("usage"):
                u = ev["usage"]
                steps.append(mk(event_type="llm_request", msg_id=ev.get("messageId"),
                                input_tokens=int(u.get("inputOther") or 0), output_tokens=int(u.get("output") or 0),
                                cache_read_tokens=int(u.get("inputCacheRead") or 0),
                                cache_write_tokens=int(u.get("inputCacheCreation") or 0),
                                latency_ms=ev.get("llmFirstTokenLatencyMs")))
            elif et == "tool.call":
                cid, name, args = ev.get("toolCallId"), ev.get("name"), ev.get("args")
                rs = content_str(args)
                ih = sha(rs)
                names[cid], inputs[cid], call_ts[cid] = name, ih, ts
                if ts >= cutoff:
                    kw = {}
                    if name in SHELL_TOOLS and isinstance(args, dict):
                        cmd = str(args.get("command") or args.get("cmd") or "")
                        kw.update(shell_fields(cmd))
                        cmdinfo[cid] = {k: kw[k] for k in CMD_KEYS}
                        ff = file_fields(shell_file(cmd))
                        kw.update(ff)
                        cmdinfo[cid].update(ff)
                    elif name in FILE_TOOLS and isinstance(args, dict):
                        kw.update(claude_file(name, args))
                        cmdinfo[cid] = {k: kw[k] for k in FILE_KEYS if k in kw}
                    sink.add(mk(event_type="tool_call", tool_name=name, tool_call_id=cid, input_sha256=ih,
                                msg_id=ev.get("uuid"), content_bytes=len(rs.encode("utf-8", "replace")), **kw))
                    stats["events"] += 1
            elif et == "tool.result" and ts >= cutoff:
                cid = ev.get("toolCallId")
                res = ev.get("result")
                raw = res.get("output") if isinstance(res, dict) else res
                s = content_str(raw)
                sz = sizes(raw, s)
                name = names.get(cid)
                err = bool(isinstance(res, dict) and (res.get("isError") or res.get("error")))
                kw = {}
                if name in SHELL_TOOLS:
                    ec = exit_code_of(s)
                    err = err or (ec is not None and ec != 0)
                    kw["exit_code"] = ec
                if name in SHELL_TOOLS or name in FILE_TOOLS:
                    kw.update(cmdinfo.get(cid, {}))
                dur = res.get("durationMs") if isinstance(res, dict) else None
                results.append(mk(event_type="tool_result", tool_name=name, tool_call_id=cid, **sz,
                                  result_sha256=sha(s), is_error=err, input_sha256=inputs.get(cid),
                                  duration_ms=int(dur) if dur is not None else ms_between(call_ts.get(cid), ts), **kw))
        except Exception:
            stats["skipped"] += 1
    reqs = steps or usage_recs
    for r in reqs + results:
        r["session_id"], r["project"] = sid, project
        if not r.get("model"):
            r["model"] = model
        r["provider"] = codex_provider(r["model"], "moonshot")
    bounds, binfo = find_boundaries(reqs, [])
    tag_requests(reqs, bounds, sid)
    compaction_rows(reqs, bounds, binfo, sid, sink, stats, cutoff)
    add_tail(results, reqs, bounds, sink, stats, sid)
    for r in reqs:
        set_list_cost(r)
        if r["ts"] >= cutoff:
            sink.add(r)
            stats["events"] += 1
            stats["models"].add(r["model"])


def run_kimi(since, out, root=None, align_day=False):
    t0 = time.time()
    root = root or os.path.expanduser("~/.kimi-code/sessions")
    ROOT_HINT_KIMI[0] = root
    write_sources(out, kimi=root)
    cutoff = datetime.now(timezone.utc) - parse_since(since)
    if align_day:
        cutoff = cutoff.replace(hour=0, minute=0, second=0, microsecond=0)
    files = []
    for p in glob.glob(os.path.join(root, "**", "agents", "*", "wire.jsonl"), recursive=True):
        try:
            if os.path.getmtime(p) >= cutoff.timestamp():
                files.append(p)
        except OSError:
            pass
    stats = {"files": 0, "lines": 0, "skipped": 0, "events": 0, "models": set()}
    sink = Sink(out, "-kimi", harness_of_key("kimi"))
    for p in sorted(files):
        try:
            process_kimi_file(p, cutoff, sink, stats)
        except OSError as e:
            print("skip file", p, e, file=sys.stderr)
            continue
        stats["files"] += 1
    sink.close()
    stats.update(models=sorted(m for m in stats["models"] if m), ingest_s=round(time.time() - t0, 1), days=sorted(sink.counts), coverage=coverage_of("kimi", files, sink))
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "ingest_stats_kimi.json"), "w") as f:
        json.dump(stats, f)
    print(json.dumps(stats))
    return 0
