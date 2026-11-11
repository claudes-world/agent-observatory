"""Excerpts: the span behind every ranked report item, written at report time to reports/{date}/excerpts/{item}.txt.

The text is taken from the item's pointer line (read only): the tool_result whose content hashes to payload_hash,
the tool call whose input hashes to input_hash, or the message text of a session's first request line.
At most 4 KB (UTF-8) is kept; images become "[image N bytes]". Every excerpt goes through obs scan before it
is written; findings are replaced by [redacted:<kind>]. The Skill reads excerpts, never raw transcripts.
"""
import json, os
from .ingest import DATA_URL, IMAGE_TYPES, content_str, sha
from . import retain
from .scan import name_pattern, redact_text

LIMIT = 4096
NOT_HERE = "Linked transcript not on this NAS; the report keeps the numbers and the pointer"


def _img(v):
    src = v.get("source")
    pay = (src.get("data") if isinstance(src, dict) else None) or v.get("image_url") or v.get("url")
    if isinstance(pay, dict):
        pay = pay.get("url")
    return f"[image {len(pay) if isinstance(pay, str) else 0} bytes]"


def render(v):
    """Readable text of a tool result / input / message content, images replaced by placeholders."""
    if isinstance(v, str):
        return DATA_URL.sub(lambda m: f"[image {len(m.group(0))} bytes]", v) if "data:image/" in v else v
    if isinstance(v, list):
        return "\n".join(render(x) for x in v)
    if isinstance(v, dict):
        if v.get("type") in IMAGE_TYPES:
            return _img(v)
        for k in ("text", "content", "output"):
            if k in v and isinstance(v[k], (str, list)):
                return render(v[k])
        return render(json.dumps({k: (_img(x) if isinstance(x, dict) and x.get("type") in IMAGE_TYPES else x)
                                  for k, x in v.items()}, ensure_ascii=False))
    return "" if v is None else str(v)


def text_for(o, kind, want):
    """kind: result | input | session. Returns text or None when the line has no matching block."""
    msg = o.get("message") or {}
    c = msg.get("content")
    p = o.get("payload") or {}
    ev = o.get("event") or {}
    if o.get("type") == "context.append_loop_event" and kind in ("result", "input"):  # Kimi Code
        if kind == "result" and ev.get("type") == "tool.result":
            r = ev.get("result")
            raw = r.get("output") if isinstance(r, dict) else r
        elif kind == "input" and ev.get("type") == "tool.call":
            raw = ev.get("args")
        else:
            return None
        return render(content_str(raw)) if sha(content_str(raw)) == want else None
    if kind == "result":
        if isinstance(c, list):
            for x in c:
                if isinstance(x, dict) and x.get("type") == "tool_result" and sha(content_str(x.get("content"))) == want:
                    return render(x.get("content"))
        if p.get("type") in ("function_call_output", "custom_tool_call_output") and sha(content_str(p.get("output"))) == want:
            return render(p.get("output"))
        return None
    if kind == "input":
        if isinstance(c, list):
            for x in c:
                if isinstance(x, dict) and x.get("type") == "tool_use" and sha(content_str(x.get("input"))) == want:
                    return render(content_str(x.get("input")))
        if p.get("type") in ("function_call", "custom_tool_call"):
            raw = p.get("arguments") if p.get("type") == "function_call" else p.get("input")
            if sha(content_str(raw)) == want:
                return render(content_str(raw))
        return None
    if c is not None:
        return render(c)
    if p:
        return f"[no message text on this line: {o.get('type')}/{p.get('type')}]"
    return f"[no message text on this line: {o.get('type')}]"


def clip(s, n=LIMIT):
    b = s.encode("utf-8", "replace")
    return s if len(b) <= n else b[:n].decode("utf-8", "ignore")


class Lines:
    """Reads requested line numbers per file in one pass each (files are opened read only)."""
    def __init__(self, roots):
        self.roots = roots

    def path(self, ptr):
        src, _, rel = ptr["file"].partition("/")
        return os.path.join(self.roots.get(src, ""), rel)

    def fetch(self, ptrs):
        want = {}
        for p in ptrs:
            want.setdefault(self.path(p), set()).add(int(p["line"]))
        got = {}
        for path, lines in want.items():
            real = retain.resolve(path)  # original .jsonl, or its archived .jsonl.gz
            if not real:
                continue
            top = max(lines)
            with retain.open_t(real) as fh:
                for i, raw in enumerate(fh, 1):
                    if i in lines:
                        got[(path, i)] = raw.decode("utf-8", "replace")
                    if i >= top:
                        break
        return got


def write(data, folder, groups, masker=None):
    """groups: [(prefix, kind, hash_key, rows)]. Sets row['excerpt'] (path relative to the reports folder, or None).
    Returns {"written", "missing", "findings_masked"}."""
    try:
        roots = json.load(open(os.path.join(data, "sources.json")))
    except (OSError, ValueError):
        roots = {}
    lr = Lines(roots)
    ptrs = [r["pointer"] for _, _, _, rows in groups for r in rows if r.get("pointer")]
    lines = lr.fetch(ptrs)
    exdir = os.path.join(folder, "excerpts")
    os.makedirs(exdir, exist_ok=True)
    names = name_pattern()
    keep, stats = set(), {"written": 0, "missing": 0, "findings_masked": 0, "source_missing": 0}
    day = os.path.basename(folder.rstrip(os.sep))
    for prefix, kind, key, rows in groups:
        for i, r in enumerate(rows, 1):
            p = r.get("pointer")
            item = f"{prefix}-{i:02d}"
            r["excerpt"] = None
            raw = lines.get((lr.path(p), int(p["line"]))) if p else None
            txt = None
            if raw is not None:
                try:
                    txt = text_for(json.loads(raw), kind, r.get(key))
                except ValueError:
                    txt = None
            if txt is None:
                stats["missing"] += 1
                if p and retain.resolve(lr.path(p)) is None:  # transcript not on this host: clean message, not an error
                    stats["source_missing"] += 1
                    f = os.path.join(exdir, item + ".txt")
                    with open(f, "w", encoding="utf-8") as fh:
                        fh.write(NOT_HERE + "\n")
                    keep.add(item + ".txt")
                    r["excerpt"] = f"{day}/excerpts/{item}.txt"
                continue
            # scan a little past the limit so a secret straddling it is still caught whole, then clip and rescan
            txt, found = redact_text(clip(txt, LIMIT + 512), names)
            txt, found2 = redact_text(clip(txt), names)
            stats["findings_masked"] += len(found) + len(found2)
            if masker:
                txt = masker.excerpt(txt)
            f = os.path.join(exdir, item + ".txt")
            with open(f + ".tmp", "w", encoding="utf-8") as fh:
                fh.write(txt)
            os.replace(f + ".tmp", f)
            keep.add(item + ".txt")
            r["excerpt"] = f"{day}/excerpts/{item}.txt"
            stats["written"] += 1
    for f in os.listdir(exdir):  # drop excerpts of items that are no longer ranked
        if f.endswith(".txt") and f not in keep:
            os.remove(os.path.join(exdir, f))
    return stats
