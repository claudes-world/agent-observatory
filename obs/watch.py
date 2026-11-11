"""Container entry: every OBS_INTERVAL minutes ingest /transcripts -> /data/events, rewrite the
reports for the newest 3 days into /data/reports, and serve the dashboard the whole time.

First cycle backfills OBS_BACKFILL days (default 30); later cycles re-ingest OBS_RECENT days
(default 3), aligned to UTC midnight so every rewritten day file is complete.
"""
import json, os, sys, threading, time, traceback
from datetime import datetime, timezone
from . import ingest, report, retain


def _status(data, **kw):
    p = os.path.join(data, "watch_status.json")
    with open(p + ".tmp", "w") as f:
        json.dump(kw, f)
    os.replace(p + ".tmp", p)


def cycle(transcripts, data, since):
    t0 = time.time()
    cl, cx = os.path.join(transcripts, "claude"), os.path.join(transcripts, "codex")
    if os.path.isdir(cl) or os.path.isdir(cx):
        if os.path.isdir(cl):
            ingest.run(since, data, cl, align_day=True)
        if os.path.isdir(cx):
            ingest.run_codex(since, data, cx, align_day=True)
    else:  # flat legacy layout = Claude projects
        ingest.run(since, data, transcripts, align_day=True)
    if os.environ.get("OBS_LINEAGE"):  # spawn hook log mounted into the container
        ingest.run_lineage(data)
    ev = os.path.join(data, "events")
    days = sorted(d for d in os.listdir(ev) if len(d) == 10 and d[:4].isdigit()) if os.path.isdir(ev) else []
    out = os.path.join(data, "reports")
    for d in days[-3:]:
        report.run(d, data, out)
    return round(time.time() - t0, 1), days[-3:]


_last_retire = [0.0]


def maybe_retire(transcripts, now=None):
    """Once a day (OBS_RETAIN_DAYS, default 30; 0 disables) gzip old transcripts under every layout root."""
    days = int(os.environ.get("OBS_RETAIN_DAYS", "30") or 0)
    now = now or time.time()
    if days <= 0 or now - _last_retire[0] < 86400:
        return None
    _last_retire[0] = now
    out = []
    for r in (os.path.join(transcripts, "claude"), os.path.join(transcripts, "codex")) \
            if os.path.isdir(os.path.join(transcripts, "claude")) or os.path.isdir(os.path.join(transcripts, "codex")) else (transcripts,):
        if os.path.isdir(r):
            out.append(retain.retire(r, f"{days}d"))
    return out


def loop(transcripts, data, interval_min, backfill, recent, stop=None):
    first = True
    while not (stop and stop.is_set()):
        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        since = f"{backfill if first else recent}d"
        try:
            try:
                maybe_retire(transcripts)
            except Exception:  # a read-only mount must not stop the recorder
                traceback.print_exc()
            secs, days = cycle(transcripts, data, since)
            _status(data, last_run=started, seconds=secs, since=since, reports=days, ok=True,
                    interval_min=interval_min)
            first = False
        except BaseException as e:  # keep the recorder alive; the dashboard shows the failure
            if isinstance(e, KeyboardInterrupt):
                raise
            traceback.print_exc()
            _status(data, last_run=started, since=since, ok=False, error=f"{type(e).__name__}: {e}"[:300],
                    interval_min=interval_min)
        if stop:
            stop.wait(interval_min * 60)
        else:
            time.sleep(interval_min * 60)


def run(transcripts, data, interval_min, host, port, backfill=30, recent=3):
    if not os.path.isdir(transcripts):
        raise SystemExit(f"transcripts folder not found: {transcripts}")
    os.makedirs(data, exist_ok=True)
    print(f"observatory: watching {transcripts} every {interval_min} min, data in {data}, "
          f"dashboard on http://{host}:{port}", flush=True)
    t = threading.Thread(target=loop, args=(transcripts, data, interval_min, backfill, recent), daemon=True)
    t.start()
    from .dashboard import serve
    return serve(data, host, port)
