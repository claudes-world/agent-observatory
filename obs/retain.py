"""Retention: gzip old transcripts in place and keep a per-root index.

`obs retire --root R --older-than 30d` turns <file>.jsonl into <file>.jsonl.gz (mtime kept) for files whose newest
line timestamp OR mtime is older than the cutoff. The original is
removed only after the gz is read back and its size + sha256 match. Rows go to <R>/.observatory-index.jsonl.
Readers use open_t()/resolve(), so src_file pointers (always the original .jsonl path) keep resolving.
"""
import glob, gzip, hashlib, json, os, re, shutil, sys, time
from datetime import datetime, timezone

INDEX = ".observatory-index.jsonl"


def resolve(path):
    """The readable file for a (possibly archived) transcript path, or None."""
    if os.path.isfile(path):
        return path
    if os.path.isfile(path + ".gz"):
        return path + ".gz"
    return None


def open_t(path):
    """Binary read handle; gzip-transparent."""
    return gzip.open(path, "rb") if path.endswith(".gz") else open(path, "rb")


def logical(path):
    return path[:-3] if path.endswith(".gz") else path


def _days(s):
    m = re.fullmatch(r"(\d+)\s*d?", str(s).strip())
    if not m:
        raise SystemExit(f"bad --older-than {s!r} (use e.g. 30d)")
    return int(m.group(1))


def _scan(path):
    """(lines, bytes, sha256, first_ts, last_ts, sessions) for one plain file."""
    h, n, size, first, last, sess = hashlib.sha256(), 0, 0, None, None, set()
    with open(path, "rb") as fh:
        for raw in fh:
            h.update(raw)
            n += 1
            size += len(raw)
            if b'"timestamp"' in raw or b'"sessionId"' in raw:
                try:
                    o = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(o, dict):
                    continue
                ts = o.get("timestamp")
                if isinstance(ts, str):
                    first = ts if first is None or ts < first else first
                    last = ts if last is None or ts > last else last
                sid = o.get("sessionId")
                if o.get("type") == "session_meta":
                    sid = (o.get("payload") or {}).get("id")
                if sid:
                    sess.add(sid)
    return n, size, h.hexdigest(), first, last, sorted(sess)


def _ts_epoch(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _gz_check(gz, size, digest):
    h, n = hashlib.sha256(), 0
    with gzip.open(gz, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return n == size and h.hexdigest() == digest


def _load_index(root):
    rows = {}
    try:
        for line in open(os.path.join(root, INDEX)):
            try:
                r = json.loads(line)
                rows[r["path"]] = r
            except (ValueError, KeyError):
                pass
    except OSError:
        pass
    return rows


def retire(root, older_than="30d", dry_run=False, now=None):
    now = now or time.time()
    cutoff = now - _days(older_than) * 86400
    index = _load_index(root)
    res = {"root": root, "candidates": 0, "retired": 0, "kept_recent": 0, "failed": 0, "bytes_before": 0, "bytes_after": 0}
    for p in sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)):
        if not os.path.isfile(p):
            continue
        st = os.stat(p)
        n, size, digest, first, last, sess = _scan(p)
        le = _ts_epoch(last) if last else None
        if st.st_mtime >= cutoff and (le is None or le >= cutoff):  # retire when EITHER is older than the cutoff
            res["kept_recent"] += 1
            continue
        res["candidates"] += 1
        if dry_run:
            print(f"would retire {p} ({size} bytes, {n} lines, last {last})")
            continue
        gz, tmp = p + ".gz", p + ".gz.tmp"
        try:
            with open(p, "rb") as src, open(tmp, "wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=6) as out:
                    shutil.copyfileobj(src, out, 1 << 20)
            if not _gz_check(tmp, size, digest):
                raise OSError("round-trip size/sha mismatch")
            os.utime(tmp, (st.st_atime, st.st_mtime))
            os.replace(tmp, gz)
            os.remove(p)
        except OSError as e:
            print(f"FAILED {p}: {e}", file=sys.stderr)
            if os.path.exists(tmp):
                os.remove(tmp)
            res["failed"] += 1
            continue
        index[p] = {"path": p, "gz": gz, "lines": n, "bytes": size, "gz_bytes": os.path.getsize(gz), "sha256": digest,
                    "first_ts": first, "last_ts": last, "sessions": sess,
                    "retired_at": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        res["retired"] += 1
        res["bytes_before"] += size
        res["bytes_after"] += index[p]["gz_bytes"]
    if not dry_run and res["retired"]:
        ip = os.path.join(root, INDEX)
        with open(ip + ".tmp", "w") as f:
            for k in sorted(index):
                f.write(json.dumps(index[k]) + "\n")
        os.replace(ip + ".tmp", ip)
    return res


def run(root, older_than="30d", dry_run=False):
    if not os.path.isdir(root):
        raise SystemExit(f"root not found: {root}")
    res = retire(root, older_than, dry_run)
    print(json.dumps(res))
    return 1 if res["failed"] else 0
