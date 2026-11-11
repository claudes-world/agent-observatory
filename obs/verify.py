"""obs verify-pointers: re-open a random sample of the transcript pointers in a daily report and check them.

For each pointer the line must exist, parse as JSON, and contain the item it points at:
- largest_tool_outputs / repeated_payloads: a tool result in that line whose content hashes to payload_hash
- retry_loops: a tool call in that line whose input hashes to input_hash
- per_session: the session id (or the subagent file stem) on that line
Transcripts are opened read-only; roots come from <data>/sources.json.
"""
import json, os, random
from .ingest import content_str, sha
from . import retain


NOT_HERE = "Linked transcript not on this NAS; the report keeps the numbers and the pointer"


def read_line(path, n):
    with retain.open_t(path) as fh:
        for i, raw in enumerate(fh, 1):
            if i == n:
                return raw.decode("utf-8", "replace")
    return None


def hashes(o):
    """(result hashes, input hashes, session ids) found in one transcript line, Claude or Codex."""
    res, inp, sids = set(), set(), {o.get("sessionId")}
    msg = o.get("message") or {}
    c = msg.get("content")
    if isinstance(c, list):
        for x in c:
            if isinstance(x, dict) and x.get("type") == "tool_result":
                res.add(sha(content_str(x.get("content"))))
            if isinstance(x, dict) and x.get("type") == "tool_use":
                inp.add(sha(content_str(x.get("input"))))
    ev = o.get("event") or {}
    if o.get("type") == "context.append_loop_event":  # Kimi Code
        if ev.get("type") == "tool.result":
            r = ev.get("result")
            res.add(sha(content_str(r.get("output") if isinstance(r, dict) else r)))
        if ev.get("type") == "tool.call":
            inp.add(sha(content_str(ev.get("args"))))
    p = o.get("payload") or {}
    if p.get("type") in ("function_call_output", "custom_tool_call_output"):
        res.add(sha(content_str(p.get("output"))))
    if p.get("type") in ("function_call", "custom_tool_call"):
        inp.add(sha(content_str(p.get("arguments") if p.get("type") == "function_call" else p.get("input"))))
    return res, inp, sids


def run(d, data, reports, n=5, seed=None, allow_missing=False):
    roots = json.load(open(os.path.join(data, "sources.json")))
    agg = json.load(open(os.path.join(reports, d + ".json")))
    items = []
    for sec, key, kind in (("largest_tool_outputs", "payload_hash", "result"), ("repeated_payloads", "payload_hash", "result"),
                           ("retry_loops", "input_hash", "input"), ("per_session", "session_id", "session")):
        for r in agg.get(sec) or []:
            if r.get("pointer"):
                items.append((sec, kind, r.get(key), r["pointer"]))
    if not items:
        print(json.dumps({"date": d, "checked": 0, "ok": 0, "note": "no pointers in this report"}))
        return 1
    rng = random.Random(seed if seed is not None else d)
    sample = rng.sample(items, min(n, len(items)))
    ok = missing = 0
    for sec, kind, want, p in sample:
        src, _, rel = p["file"].partition("/")
        path = os.path.join(roots.get(src, ""), rel)
        path = retain.resolve(path)  # original .jsonl, or its archived .jsonl.gz
        line = read_line(path, p["line"]) if path else None
        if path is None and allow_missing:
            missing += 1
            print(f"{'missing-source':14} {sec:22} {p['file']}:{p['line']}  {NOT_HERE}")
            continue
        status = "missing line" if line is None else ""
        if line is not None:
            try:
                o = json.loads(line)
                res, inp, sids = hashes(o)
                stem = os.path.basename(rel)[:-6]
                good = (want in res) if kind == "result" else (want in inp) if kind == "input" else \
                       (want in sids or want == stem or stem.endswith(str(want)))
                status = "ok" if good else "hash mismatch"
            except ValueError:
                status = "line is not JSON"
        ok += status == "ok"
        print(f"{status:14} {sec:22} {p['file']}:{p['line']}  {str(want)[:12]}")
    print(json.dumps({"date": d, "checked": len(sample) - missing, "ok": ok, "missing-source": missing, "pointers_in_report": len(items)}))
    return 0 if ok == len(sample) - missing else 1
