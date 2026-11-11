"""obs redact: allowlist-mode transcript redaction with X masking.

Every redacted character becomes "X" (same length, newlines kept). One output line per input line, so file:line
pointers stay valid; JSON structure, ids, timestamps, models, usage, cwd are kept.
- prose (user / assistant text, tool inputs, system content): all X.
- tool outputs: kept, except spans found by the scanner (obs/scan.py), exact known secrets and names, masked in place.
- api keys / known secrets keep a prefix up to and including the first '-' (or '_'), max 5 chars; emails keep '@' and the domain dots;
  phones, IPs, JWTs, PEM blocks, Telegram tokens, high-entropy strings are all X.
- known secrets: exact values (plus URL-encoded and JSON-escaped forms) read from env files at redact time.
- names: --names / OBS_NAME_LIST, case-insensitive, no word boundary, X per char.
- keyword rule: a record whose text fields contain any keyword (case-insensitive) has ALL its text X-masked.
Image payloads become "A" runs behind their data: prefix. Output file sha256 are recomputed into manifest.json.
"""
import glob, hashlib, json, os, re
from collections import Counter
from urllib.parse import quote, quote_plus

from .ingest import DATA_URL, IMAGE_TYPES
from .scan import scan_text

KEEP_KEYS = {
    "type", "uuid", "parentUuid", "logicalParentUuid", "sessionId", "session_id", "timestamp", "id", "tool_use_id",
    "call_id", "name", "role", "model", "cwd", "version", "gitBranch", "isSidechain", "isMeta", "userType",
    "requestId", "media_type", "agentId", "status", "stop_reason", "stop_sequence", "response_id", "turn_id",
    "parent_thread_id", "agent_path", "model_provider", "toolUseId", "agentType", "resolvedModel", "detail",
    "cli_version", "originator", "source", "subtype", "level", "service_tier", "operation", "kind", "level",
    "promptId", "messageId", "leafUuid", "task_name", "agent_type", "subagent_type", "is_error", "create_time",
}
RESULT_PAYLOADS = ("function_call_output", "custom_tool_call_output")

DEFAULT_NAMES = "Alice,Carol"
DEFAULT_KEYWORDS = "Atlantis,Atlantean,El Dorado"
DEFAULT_SECRET_GLOBS = ["~/.secrets/*.env", "/run/secrets/*.env"]


def _split(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def x(s):
    return "".join("\n" if c == "\n" else "X" for c in s)


def keep_prefix(s):
    """Prefix kept for key-shaped strings: through the first '-' (else '_') if within 5 chars, else 5 chars."""
    n = 0
    for d in "-_":
        i = s.find(d)
        if 0 <= i <= 4:
            n = i + 1
            break
    else:
        n = 5
    return min(n, len(s) // 3)


def mask_span(kind, s):
    if kind in ("api_key", "known_secret"):
        k = keep_prefix(s)
        return s[:k] + x(s[k:])
    if kind == "email":
        loc, at, dom = s.partition("@")
        return "X" * len(loc) + at + "".join("." if c == "." else "X" for c in dom)
    return x(s)


def parse_env(path):
    vals = []
    try:
        for line in open(path, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            line = line[7:] if line.startswith("export ") else line
            v = line.split("=", 1)[1].strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if v:
                vals.append((line.split("=", 1)[0].strip(), v))
    except OSError:
        pass
    return vals


KEY_NAME_RX = re.compile(r"(key|token|secret|password|passwd|pass|auth|bearer|private)", re.I)
_PLAIN_RX = re.compile(r"^(?:[a-z0-9][a-z0-9.\-]*|[a-z][a-z0-9+.\-]*://\S*|~?/[^\s]*)$")


def is_secret_value(key, v):
    """Guard against X-masking ordinary text: short/common env values (ports, true, hostnames) are not secrets."""
    if len(v) < 8:
        return False
    if KEY_NAME_RX.search(key or ""):
        return True
    if len(v) < 16 or re.search(r"\s", v) or _PLAIN_RX.match(v):
        return False
    classes = sum(bool(re.search(c, v)) for c in ("[a-z]", "[A-Z]", "[0-9]", r"[^a-zA-Z0-9]"))
    return classes >= 2


DEFAULT_SECRET_DIRS = ["~/.secrets", "/run/secrets"]
NAME_KEEP_RX = re.compile(r"(token|key|secret|password|credential|\.pem|sa\.json)", re.I)
ENV_NAME_RX = re.compile(r"\.(env|sh)(\.|$)", re.I)
MAX_SECRET_FILE = 64 * 1024
LAST_SOURCES = {}  # file NAME -> {"loaded": n, "skipped": n} from the last load_secrets_ex call


def _pem_parts(v):
    """Body lines and the joined base64 body of a PEM block inside string v ([] if none)."""
    m = re.search(r"-----BEGIN [A-Z ]+-----(.*?)-----END [A-Z ]+-----", v, re.S)
    if not m:
        return []
    lines = [l.strip() for l in m.group(1).replace("\\n", "\n").splitlines() if l.strip()]
    return lines + ["".join(lines)]


def _json_leaves(o, key=""):
    if isinstance(o, str):
        yield key, o
    elif isinstance(o, list):
        for i in o:
            yield from _json_leaves(i, key)
    elif isinstance(o, dict):
        for k, i in o.items():
            yield from _json_leaves(i, str(k))


def _file_values(f):
    """[(keyname, value, always_keep)] for one secret source file."""
    name = os.path.basename(f)
    name_hit = bool(NAME_KEEP_RX.search(name))
    try:
        if os.path.getsize(f) > MAX_SECRET_FILE:
            return []
        raw = open(f, "rb").read()
        if b"\0" in raw:
            return []
        txt = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    if name.endswith(".json"):
        try:
            leaves = list(_json_leaves(json.loads(txt)))
        except ValueError:
            leaves = None
        if leaves is not None:
            out = []
            for k, v in leaves:
                out.append((k or name, v, name_hit))
                out += [(k or name, p, True) for p in _pem_parts(v)]
            return out
    elif ENV_NAME_RX.search(name):
        out = []
        for k, v in parse_env(f):
            out.append((k, v, False))
            out += [(k, p, True) for p in _pem_parts(v)]
        return out
    vals = [txt.strip()] + [l.strip() for l in txt.splitlines()]
    seen, out = set(), []
    for v in vals:
        if v and v not in seen:
            seen.add(v); out.append((name, v, name_hit))
    out += [(name, p, True) for p in _pem_parts(txt)]
    return out


def secret_files(dirs=None):
    """Regular files directly in each dir, plus one level down in <dir>/agents/*/ ."""
    out = []
    for d in dirs or DEFAULT_SECRET_DIRS:
        d = os.path.expanduser(d)
        for pat in (os.path.join(d, "*"), os.path.join(d, "agents", "*", "*")):
            out += [f for f in sorted(glob.glob(pat)) if os.path.isfile(f)]
    return out


def load_secrets_ex(files=None, dirs=None):
    """(regex or None, nfiles, loaded, skipped, {form: KEYNAME}). Values are never printed.
    Per-file counts by file NAME land in LAST_SOURCES."""
    files = list(files or []) 
    if dirs or not files:
        files += secret_files(dirs)
    forms, loaded, skipped, nfiles = {}, 0, 0, 0
    LAST_SOURCES.clear()
    for f in files:
        if not os.access(f, os.R_OK) or not os.path.isfile(f):
            continue
        nfiles += 1
        st = LAST_SOURCES.setdefault(os.path.basename(f), {"loaded": 0, "skipped": 0})
        for k, v, keep in _file_values(f):
            if not ((keep and len(v) >= 8) or is_secret_value(k, v)):
                skipped += 1; st["skipped"] += 1
                continue
            loaded += 1; st["loaded"] += 1
            for form in {v, quote(v, safe=""), quote_plus(v), json.dumps(v, ensure_ascii=False)[1:-1], json.dumps(v)[1:-1]}:
                forms.setdefault(form, k)
    rx = re.compile("|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))) if forms else None
    return rx, nfiles, loaded, skipped, forms


def load_secrets(files=None):
    """(compiled regex or None, number of files read, number of distinct values kept)."""
    rx, nfiles, loaded, _, _ = load_secrets_ex(files)
    return rx, nfiles, loaded


class Redactor:
    def __init__(self, names=None, secrets=None, keywords=None, key_of=None):
        self.key_of = key_of or {}
        self.key_hits = Counter()
        nl = _split(names)
        self.names = re.compile("|".join(re.escape(n) for n in sorted(nl, key=len, reverse=True)), re.I) if nl else None
        self.secrets = secrets
        kl = _split(keywords)
        self.kw = re.compile("|".join(re.escape(k) for k in sorted(kl, key=len, reverse=True)), re.I) if kl else None
        self.c = Counter()
        self.rules = Counter()
        self.force = False

    # -- text
    def mask_text(self, s):
        spans = []
        if self.secrets:
            for m in self.secrets.finditer(s):
                spans.append((m.start(), m.end(), "known_secret")); self.key_hits[self.key_of.get(m.group(0), "?")] += 1
        taken = [(a, b) for a, b, _ in spans]
        # names are matched here (not via scan_text) so they need no word boundary
        if self.names:
            for m in self.names.finditer(s):
                if not any(m.start() < b and a < m.end() for a, b in taken):
                    spans.append((m.start(), m.end(), "name")); taken.append((m.start(), m.end()))
        for a, b, kind, _ in scan_text(s, names=_NONE):
            if not any(a < y and x_ < b for x_, y in taken):
                spans.append((a, b, kind)); taken.append((a, b))
        if not spans:
            return s
        out, pos = [], 0
        for a, b, kind in sorted(spans):
            if a < pos:
                continue
            out.append(s[pos:a]); out.append(mask_span(kind, s[a:b]))
            self.rules[kind] += 1
            self.c["masked_spans"] += 1
            self.c["span_chars_masked"] += b - a
            pos = b
        out.append(s[pos:])
        return "".join(out)

    def literal(self, s):
        """Kept fields (cwd, ids...): known secrets and names only."""
        if self.secrets:
            def _f(m):
                self.key_hits[self.key_of.get(m.group(0), "?")] += 1
                return mask_span("known_secret", m.group(0))
            s = self.secrets.sub(_f, s)
        if self.names:
            s = self.names.sub(lambda m: x(m.group(0)), s)
        return s

    def _pieces(self, s, f):
        """Apply f to the text around data:image URLs; image base64 becomes A runs."""
        if "data:image/" not in s:
            return f(s)
        out, pos = [], 0
        for m in DATA_URL.finditer(s):
            out.append(f(s[pos:m.start()]))
            u = m.group(0); k = u.index(",") + 1
            out.append(u[:k] + "A" * (len(u) - k)); self.c["images_filled"] += 1
            pos = m.end()
        out.append(f(s[pos:]))
        return "".join(out)

    def prose(self, s):
        def f(t):
            n = sum(1 for c in t if c != "\n")
            self.c["prose_chars_masked"] += n
            self.c["prose_strings"] += 1
            return x(t)
        return self._pieces(s, f)

    def fill_image(self, v):
        self.c["images_filled"] += 1
        v = dict(v)
        src = v.get("source")
        if isinstance(src, dict) and isinstance(src.get("data"), str):
            v["source"] = dict(src, data="A" * len(src["data"]))
        for k in ("image_url", "url"):
            u = v.get(k)
            if isinstance(u, str):
                i = u.index(",") + 1 if u.startswith("data:") and "," in u else 0
                v[k] = u[:i] + "A" * (len(u) - i)
            elif isinstance(u, dict) and isinstance(u.get("url"), str):
                uu = u["url"]; i = uu.index(",") + 1 if uu.startswith("data:") and "," in uu else 0
                v[k] = dict(u, url=uu[:i] + "A" * (len(uu) - i))
        return v

    def fill(self, v, key=None):
        """All string leaves become X, except KEEP_KEYS values (which get the literal pass)."""
        if isinstance(v, str):
            return self.literal(v) if key in KEEP_KEYS else self.prose(v)
        if isinstance(v, list):
            return [self.fill(i) for i in v]
        if isinstance(v, dict):
            if v.get("type") in IMAGE_TYPES:
                return self.fill_image(v)
            return {k: self.fill(i, key=k) for k, i in v.items()}
        return v

    def keep(self, v, key=None):
        """Tool output: keep text, mask scanner / secret / name spans in place."""
        if isinstance(v, str):
            return self.literal(v) if key in KEEP_KEYS else self._pieces(v, self.mask_text)
        if isinstance(v, list):
            return [self.keep(i) for i in v]
        if isinstance(v, dict):
            if v.get("type") in IMAGE_TYPES:
                return self.fill_image(v)
            return {k: self.keep(i, key=k) for k, i in v.items()}
        return v

    def result(self, v):
        self.c["tool_results"] += 1
        return self.fill(v) if self.force else self.keep(v)

    # -- records
    def text_leaves(self, v, key=None, acc=None):
        acc = [] if acc is None else acc
        if isinstance(v, str):
            if key not in KEEP_KEYS:
                acc.append(v)
        elif isinstance(v, list):
            for i in v:
                self.text_leaves(i, None, acc)
        elif isinstance(v, dict) and v.get("type") not in IMAGE_TYPES:
            for k, i in v.items():
                self.text_leaves(i, k, acc)
        return acc

    def line(self, o):
        self.force = bool(self.kw and any(self.kw.search(t) for t in self.text_leaves(o)))
        if self.force:
            self.c["keyword_masked_records"] += 1
        if not isinstance(o, dict):
            return self.fill(o)
        out = {}
        for k, v in o.items():
            if k == "message" and isinstance(v, dict):
                out[k] = self.message(v)
            elif k == "toolUseResult":
                out[k] = self.result(v)
            elif k == "payload" and isinstance(v, dict):
                out[k] = self.payload(v)
            else:
                out[k] = v if not isinstance(v, (str, list, dict)) else self.fill(v, key=k)
        return out

    def message(self, m):
        out = {}
        for k, v in m.items():
            if k == "content" and isinstance(v, list):
                items = []
                for c in v:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        c = {kk: (self.result(vv) if kk == "content" else self.fill(vv, key=kk)) for kk, vv in c.items()}
                    elif isinstance(c, dict) and c.get("type") == "tool_use":
                        c = {kk: (self.fill(vv) if kk == "input" else self.fill(vv, key=kk)) for kk, vv in c.items()}
                    else:
                        c = self.fill(c)
                    items.append(c)
                out[k] = items
            elif k == "usage" or not isinstance(v, (str, list, dict)):
                out[k] = v
            else:
                out[k] = self.fill(v, key=k)
        return out

    def payload(self, p):
        t = p.get("type")
        out = {}
        for k, v in p.items():
            if t in RESULT_PAYLOADS and k == "output":
                out[k] = self.result(v)
            elif t in ("function_call", "custom_tool_call") and k in ("arguments", "input"):
                out[k] = self.fill(v)
            elif t == "exec_command_end" and k in ("aggregated_output", "stdout", "stderr", "formatted_output"):
                out[k] = self.result(v)
            elif k in ("usage", "info", "rate_limits") or not isinstance(v, (str, list, dict)):
                out[k] = v
            else:
                out[k] = self.fill(v, key=k)
        return out


class _NoNames:
    def finditer(self, s):
        return iter(())


_NONE = _NoNames()


def redact(src, dst, mode="allowlist", secrets_files=None, names=None, keywords=None, dry_run=False, secrets_dirs=None):
    if dry_run:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            return _redact(src, os.path.join(td, "out"), mode, secrets_files, names, keywords, True, secrets_dirs)
    return _redact(src, dst, mode, secrets_files, names, keywords, False, secrets_dirs)


def _redact(src, dst, mode, secrets_files, names, keywords, dry_run, secrets_dirs=None):
    if mode != "allowlist":
        raise SystemExit("--mode: only allowlist exists")
    src, dst = os.path.abspath(src), os.path.abspath(dst)
    if dst == src or dst.startswith(src + os.sep):
        raise SystemExit("dst must be outside src")
    if names is None:
        names = os.environ.get("OBS_NAME_LIST") or DEFAULT_NAMES
    if keywords is None:
        keywords = os.environ.get("OBS_MASK_KEYWORDS") or DEFAULT_KEYWORDS
    rx, nfiles, nvals, nskip, key_of = load_secrets_ex(secrets_files, secrets_dirs)
    r = Redactor(names, rx, keywords, key_of)
    files = []
    for d, dirs, fs in os.walk(src):
        dirs.sort()
        for f in sorted(fs):
            if f.endswith((".jsonl", ".json")):
                files.append(os.path.join(d, f))
    detail = []
    for p in files:
        rel = os.path.relpath(p, src)
        q = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(q), exist_ok=True)
        h, n = hashlib.sha256(), 0
        with open(p, "rb") as fi, open(q + ".tmp", "wb") as fo:
            chunks = [fi.read()] if p.endswith(".json") else fi
            for raw in chunks:
                n += 1
                r.c["bytes_in"] += len(raw)
                nl = b"\n" if raw.endswith(b"\n") else b""
                txt = raw.decode("utf-8", "replace")
                try:
                    o = json.loads(txt)
                    outl = json.dumps(r.line(o), ensure_ascii=False)
                    r.c["json_lines"] += 1
                except ValueError:
                    outl = x(txt.rstrip("\n"))
                    r.c["non_json_lines_filled"] += 1
                b = outl.encode("utf-8") + nl
                fo.write(b); h.update(b)
                r.c["bytes_out"] += len(b)
        os.replace(q + ".tmp", q)
        r.c["lines"] += n
        detail.append({"file": rel, "lines": n, "sha256": h.hexdigest()})
    r.c.setdefault("keyword_masked_records", 0)
    man = {"mode": "allowlist", "mask_char": "X", "src": src, "files": len(files), **dict(sorted(r.c.items())),
           "rules": dict(sorted(r.rules.items())), "secret_files_read": nfiles, "secret_values_loaded": nvals,
           "secret_values_skipped": nskip, "secret_sources": dict(LAST_SOURCES), "known_secret_hits_by_key": dict(r.key_hits.most_common()),
           "names_count": len(_split(names)), "keywords_count": len(_split(keywords)),
           "note": "pointers (file:line) match the source; hashes are recomputed by ingesting this copy",
           "file_detail": detail}
    os.makedirs(dst, exist_ok=True)
    with open(os.path.join(dst, "manifest.json"), "w") as f:
        json.dump(man, f, indent=1)
    print(json.dumps({k: v for k, v in man.items() if k != "file_detail"}))
    if dry_run:
        print("dry-run: nothing written; known-secret hits by key NAME: " + json.dumps(man["known_secret_hits_by_key"]))
    return 0
