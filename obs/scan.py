"""PII / secret scanner (`obs scan`) and transcript redaction (`obs redact`).

scan_text() finds: emails, phone numbers, IPv4 / IPv6 addresses, API-key shapes (sk-..., xox?-, gh?_,
AKIA/ASIA, high-entropy base64 >= 40 chars, PEM headers), JWTs, Telegram bot tokens, and the names listed
in OBS_NAME_LIST (comma-separated, default empty). Loopback / unspecified IPs (127.x, 0.0.0.0, ::1) are
not reported. Overlapping matches keep the most specific kind.

redact(src, dst, ...) lives in obs/redact.py (allowlist mode only, X masking); re-exported here.
"""
import hashlib, ipaddress, json, math, os, re, sys
from collections import Counter

from .ingest import DATA_URL, IMAGE_TYPES

# (kind, regex). Order matters: earlier kinds win overlaps.
PATTERNS = [
    ("pem", re.compile(r"-----BEGIN [A-Z0-9 ]{3,40}-----(?:[\s\S]{0,20000}?-----END [A-Z0-9 ]{3,40}-----)?")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("telegram_bot_token", re.compile(r"(?<![\w:])\d{8,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])")),
    ("api_key", re.compile(r"(?<![A-Za-z0-9_-])(?:sk-[A-Za-z0-9_-]{20,}|xox[abposr]-[A-Za-z0-9-]{10,}"
                           r"|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|(?:AKIA|ASIA)[0-9A-Z]{16})(?![A-Za-z0-9])")),
    ("email", re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}(?![\w-])")),
    ("ipv6", re.compile(r"(?<![\w:.])(?=[0-9A-Fa-f:]*:[0-9A-Fa-f]*:)[0-9A-Fa-f:]{3,39}(?![\w:])")),
    ("ipv4", re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")),
    ("phone", re.compile(r"(?<![\w+])\+\d{1,3}[ .-]?\(?\d{1,4}\)?(?:[ .-]?\d{2,4}){2,4}(?![\w])"
                         r"|(?<![\w(])\(\d{3}\) ?\d{3}[ .-]\d{4}(?!\w)|(?<![\w.-])\d{3}[.-]\d{3}[.-]\d{4}(?![\w.-])")),
    ("base64_secret", re.compile(r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_-]{32,}={0,2}(?![A-Za-z0-9+/_=-])")),
]

# Known id shapes, skipped before the base64 rule: (label, regex). "image" is counted separately.
SKIP_PATTERNS = [
    ("image", re.compile(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=_-]*")),
    ("image", re.compile(r"(?<![A-Za-z0-9+/_=-])(?:iVBORw0KGgo|/9j/|R0lGODl|UklGR)[A-Za-z0-9+/=_-]{60,}")),
    ("id", re.compile(r"(?<![A-Za-z0-9])(?:toolu|msg|req|call|resp|chatcmpl)_[A-Za-z0-9]+")),
    ("id", re.compile(r"(?<![A-Za-z0-9])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![A-Za-z0-9])")),
    ("id", re.compile(r"(?<![A-Za-z0-9])(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{40})(?![A-Za-z0-9])")),
    ("id", re.compile(r"(?<![A-Za-z0-9])agent-[0-9a-f]+")),
    ("id", re.compile(r"(?<![A-Za-z0-9])rollout-[A-Za-z0-9_.-]+")),
    ("id", re.compile(r"(?i)sha256[=:]\s*[A-Za-z0-9+/_=-]+")),
    ("id", re.compile(r"(?i)(?:attachment_file_id|file_unique_id|file_id)(?:\\*[\"'])?\s*[=:]\s*\\*[\"']?[A-Za-z0-9_-]+")),
    ("id", re.compile(r"(?:signature|encrypted_content|thought_signature)(?:\\*[\"'])?\s*[=:]\s*\\*[\"']?[A-Za-z0-9+/_=-]+")),
    ("url_path", re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s\"'<>?#\\]+")),
]


def entropy(s):
    c = Counter(s)
    n = len(s)
    return -sum(v / n * math.log2(v / n) for v in c.values())


def _valid(kind, m):
    if kind == "ipv4":
        parts = [int(x) for x in m.split(".")]
        if any(p > 255 for p in parts):
            return False
        return not (parts[0] in (0, 127) or m == "255.255.255.255")
    if kind == "ipv6":
        try:
            a = ipaddress.IPv6Address(m)
        except ValueError:
            return False
        return not (a.is_loopback or a.is_unspecified) and m.count(":") >= 2
    if kind == "phone":
        return 9 <= sum(ch.isdigit() for ch in m) <= 15
    if kind == "base64_secret":
        core = m.rstrip("=")
        if not (any(ch.isupper() for ch in core) and any(ch.islower() for ch in core)
                and any(ch.isdigit() for ch in core) and entropy(core) >= 4.5):
            return False
        # paths and slugs (/code/app-wt/game/Plugins/..., build-ground-20261108T024355Z-...) are not secrets:
        # random base64 rarely has long lowercase word runs or many short separator-delimited segments
        words = sum(len(w) for w in WORD_RUN.findall(core))
        segs = [len(x) for x in re.split(r"[/_-]", core)]
        short = len(segs) >= 3 and sorted(segs)[len(segs) // 2] < 12
        return words / len(core) < 0.35 and not short
    if kind == "email":
        local, _, dom = m.partition("@")
        tld = dom.rsplit(".", 1)[-1]
        return dom == dom.lower() and local.lower() not in ("git", "noreply", "no-reply") and tld not in FILE_EXT
    return True


# a "TLD" that is really a file extension: systemd instances (unit@name.conf), files named user@host.json
FILE_EXT = {"conf", "service", "timer", "socket", "target", "mount", "json", "jsonl", "yaml", "yml", "toml", "md",
            "txt", "py", "js", "ts", "sh", "log", "lock", "png", "jpg", "jpeg", "gif", "svg", "html", "css", "csv"}


WORD_RUN = re.compile(r"[A-Z]?[a-z]{4,}")


def name_pattern():
    names = [n.strip() for n in (os.environ.get("OBS_NAME_LIST") or "").split(",") if n.strip()]
    if not names:
        return None
    return re.compile(r"(?i)(?<!\w)(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")(?!\w)")


def skip_spans(text):
    out = []
    for label, rx in SKIP_PATTERNS:
        for m in rx.finditer(text):
            out.append((m.start(), m.end(), label))
    return out


def scan_text(text, names=None, skipped=None):
    """[(start, end, kind, matched_text)], non-overlapping, sorted by start.
    skipped: optional Counter; gets one count per base64-shaped match dropped as an id / image / URL path."""
    if not text:
        return []
    found, taken = [], []
    skips = None
    pats = list(PATTERNS)
    np_ = names if names is not None else name_pattern()
    if np_ is not None:
        pats.append(("name", np_))
    for kind, rx in pats:
        for m in rx.finditer(text):
            a, b = m.span()
            if any(a < y and x < b for x, y in taken):
                continue
            if kind == "base64_secret":
                if skips is None:
                    skips = skip_spans(text)
                hit = next((l for x, y, l in skips if a < y and x < b), None)
                if hit:
                    if skipped is not None:
                        skipped[hit] += 1
                    continue
            if not _valid(kind, m.group(0)):
                continue
            found.append((a, b, kind, m.group(0)))
            taken.append((a, b))
    return sorted(found)


def mask(s):
    """Masked match for tables: first 6 and last 4 characters, then the length."""
    return f"{s[:6]}***{s[-4:]}({len(s)})" if len(s) > 12 else f"***({len(s)})"


def redact_text(text, names=None):
    """Replace every finding with [redacted:<kind>]; returns (text, findings)."""
    f = scan_text(text, names)
    if not f:
        return text, f
    out, pos = [], 0
    for a, b, kind, _ in f:
        out.append(text[pos:a])
        out.append(f"[redacted:{kind}]")
        pos = b
    out.append(text[pos:])
    return "".join(out), f


SKIP_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".parquet", ".gz", ".zip", ".pdf", ".ico", ".woff", ".woff2")


def iter_files(path):
    if os.path.isfile(path):
        yield path
        return
    for d, dirs, files in os.walk(path):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        for f in sorted(files):
            if not f.lower().endswith(SKIP_EXT):
                yield os.path.join(d, f)


def scan_paths(paths, skipped=None):
    names = name_pattern()
    rows, nfiles = [], 0
    for root in paths:
        for p in iter_files(root):
            nfiles += 1
            try:
                with open(p, "rb") as fh:
                    for i, raw in enumerate(fh, 1):
                        for a, b, kind, m in scan_text(raw.decode("utf-8", "replace"), names, skipped):
                            rows.append((p, i, kind, mask(m)))
            except OSError as e:
                print(f"skip {p}: {e}", file=sys.stderr)
    return rows, nfiles


def run_scan(paths, limit=200, report=False):
    skipped = Counter()
    rows, nfiles = scan_paths(paths, skipped)
    if report:
        by = Counter(r[2] for r in rows)
        print("| kind | count |\n|---|---|")
        for k, n in by.most_common():
            print(f"| {k} | {n} |")
        for k, n in by.most_common():
            print(f"\ntop 10 {k} (masked):")
            for m, c in Counter(r[3] for r in rows if r[2] == k).most_common(10):
                print(f"  {c:5d}  {m}")
        print("\nskipped as known shapes:", json.dumps(dict(skipped)))
        print(json.dumps({"files": nfiles, "findings": len(rows), "by_kind": dict(by.most_common()),
                          "skipped": dict(skipped), "images": skipped.get("image", 0)}))
        return 1 if rows else 0
    if rows:
        print("| file | line | kind | match |\n|---|---|---|---|")
        for p, i, kind, m in rows[:limit]:
            print(f"| {p} | {i} | {kind} | {m} |")
        if len(rows) > limit:
            print(f"... {len(rows) - limit} more findings not shown")
    by = Counter(r[2] for r in rows)
    print(json.dumps({"files": nfiles, "findings": len(rows), "by_kind": dict(by.most_common())}))
    return 1 if rows else 0


def redact(*a, **k):
    from . import redact as _r
    return _r.redact(*a, **k)
