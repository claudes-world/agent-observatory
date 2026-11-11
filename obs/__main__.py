import argparse, sys
import os
from . import ingest, report, summary, synth, verify


def main(argv=None):
    p = argparse.ArgumentParser(prog="obs")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("ingest")
    i.add_argument("source", choices=["claude", "codex", "kimi", "all", "lineage"])
    i.add_argument("--since", default="30d", help="window, aligned to UTC midnight (default 30d)")
    i.add_argument("--out", default="data/")
    i.add_argument("--path", default=None, help="lineage: spawn hook log (default $OBS_LINEAGE or ~/.local/state/agent-observatory/lineage.jsonl)")
    i.add_argument("--root", action="append", default=None, help="override ~/.claude/projects (repeatable for claude: first = live, extras NAME=PATH -> source claude-NAME, cross-root de-dup), ~/.codex/sessions or ~/.kimi-code/sessions (for all: a dir holding claude/ and codex/sessions/)")
    y = sub.add_parser("synth", help="write deterministic SYNTHETIC transcripts (claude/ and codex/sessions/)")
    y.add_argument("--out", required=True)
    y.add_argument("--days", type=int, default=14)
    y.add_argument("--seed", type=int, default=7)
    y.add_argument("--start", default=None, help="first UTC day YYYY-MM-DD (default: today - days + 1)")
    r = sub.add_parser("report")
    r.add_argument("--date", required=True)
    r.add_argument("--data", default="data/")
    r.add_argument("--out", default="reports/")
    r.add_argument("--no-mask", action="store_true", help="do not span-mask cmd_text and excerpts at write time")
    m = sub.add_parser("summary")
    m.add_argument("--since", default="30d")
    m.add_argument("--data", default="data/")
    m.add_argument("--out", default=None)
    vp = sub.add_parser("verify-pointers", help="re-open sampled report pointers and check line + hash")
    vp.add_argument("--date", required=True)
    vp.add_argument("--data", default="data/")
    vp.add_argument("--reports", default="reports/")
    vp.add_argument("-n", type=int, default=5)
    vp.add_argument("--allow-missing", action="store_true", help="pointers whose transcript is not on this host are reported as missing-source, not failures")
    vp.add_argument("--seed", type=int, default=None)
    sc = sub.add_parser("scan", help="PII/secret scan of text files; findings table, exit 1 on any finding")
    sc.add_argument("paths", nargs="+")
    sc.add_argument("--limit", type=int, default=200, help="rows shown (all are counted)")
    sc.add_argument("--report", action="store_true", help="counts by kind + top 10 masked matches per kind")
    rd = sub.add_parser("redact", help="copy transcripts with text replaced by same-length X masks")
    rd.add_argument("src")
    rd.add_argument("dst")
    rd.add_argument("--mode", choices=["allowlist"], default="allowlist")
    rd.add_argument("--secrets-file", action="append", default=None, help="env file of known secrets (repeatable; default ~/.secrets/*.env and /run/secrets/*.env)")
    rd.add_argument("--secrets-dir", action="append", default=None, help="dir of secret files (repeatable; default ~/.secrets and /run/secrets)")
    rd.add_argument("--names", default=None, help="comma-separated names (default $OBS_NAME_LIST or Alice,Carol)")
    rd.add_argument("--mask-message-keywords", default=None, help="records containing any keyword get all text X-masked (default $OBS_MASK_KEYWORDS or Atlantis,Atlantean,El Dorado)")
    rd.add_argument("--dry-run", action="store_true", help="write nothing; print counts incl. known-secret hits by key name")
    rr = sub.add_parser("redact-reports", help="copy a reports tree with ledgers, md/json/html and excerpts span-masked")
    rr.add_argument("src")
    rr.add_argument("dst")
    rr.add_argument("--secrets-file", action="append", default=None)
    rr.add_argument("--secrets-dir", action="append", default=None)
    rr.add_argument("--names", default=None)
    rr.add_argument("--mask-message-keywords", default=None)
    rr.add_argument("--dry-run", action="store_true", help="write nothing; print counts incl. known-secret hits by key name")
    rt = sub.add_parser("retire", help="gzip transcripts older than N days in place (+ .observatory-index.jsonl)")
    rt.add_argument("--root", required=True)
    rt.add_argument("--older-than", default="30d")
    rt.add_argument("--dry-run", action="store_true")
    pp = sub.add_parser("prices", help="price tables")
    ps = pp.add_subparsers(dest="pcmd", required=True)
    fo = ps.add_parser("fetch-openrouter", help="write OpenRouter list prices YAML")
    fo.add_argument("--out", default=None, help="default obs/prices-openrouter.yaml")
    v = sub.add_parser("serve", help="dashboard only")
    v.add_argument("--data", default="data/")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8787)
    w = sub.add_parser("watch", help="ingest + reports every N minutes, and serve the dashboard")
    w.add_argument("--transcripts", default=os.environ.get("OBS_TRANSCRIPTS", "/transcripts"))
    w.add_argument("--data", default=os.environ.get("OBS_DATA", "/data"))
    w.add_argument("--interval", type=float, default=float(os.environ.get("OBS_INTERVAL", "10")), help="minutes")
    w.add_argument("--backfill", type=int, default=int(os.environ.get("OBS_BACKFILL", "30")), help="days, first cycle")
    w.add_argument("--recent", type=int, default=int(os.environ.get("OBS_RECENT", "3")), help="days, later cycles")
    w.add_argument("--host", default=os.environ.get("OBS_BIND", "0.0.0.0"))
    w.add_argument("--port", type=int, default=int(os.environ.get("OBS_PORT", "8787")))
    a = p.parse_args(argv)
    if a.cmd == "prices":
        from . import prices as _pr
        n = _pr.fetch_openrouter(a.out or _pr.OR_PATH)
        print(f"wrote {n} models to {a.out or _pr.OR_PATH}")
        return 0
    if a.cmd == "serve":
        from . import dashboard
        return dashboard.serve(a.data, a.host, a.port)
    if a.cmd == "watch":
        from . import watch
        return watch.run(a.transcripts, a.data, a.interval, a.host, a.port, a.backfill, a.recent)
    if a.cmd == "retire":
        from . import retain
        return retain.run(a.root, a.older_than, a.dry_run)
    if a.cmd == "scan":
        from . import scan
        return scan.run_scan(a.paths, a.limit, a.report)
    if a.cmd == "redact-reports":
        from . import redact_reports
        return redact_reports.redact_reports(a.src, a.dst, a.secrets_file, a.names, a.mask_message_keywords, a.dry_run, a.secrets_dir)
    if a.cmd == "redact":
        from . import scan
        return scan.redact(a.src, a.dst, a.mode, a.secrets_file, a.names, a.mask_message_keywords, a.dry_run, a.secrets_dir)
    if a.cmd == "synth":
        return synth.run(a.out, a.days, a.seed, a.start)
    if a.cmd == "ingest":
        # windows start at UTC midnight so every day file that gets rewritten is complete
        if a.source == "lineage":
            return ingest.run_lineage(a.out, a.path)
        root1 = a.root[0] if a.root else None
        if a.source == "codex":
            return ingest.run_codex(a.since, a.out, root1, align_day=True)
        if a.source == "kimi":
            return ingest.run_kimi(a.since, a.out, root1, align_day=True)
        if a.source == "all":
            if a.root:  # synth layout: <root>/claude and <root>/codex/sessions
                return ingest.run_all(a.since, a.out, os.path.join(root1, "claude"), os.path.join(root1, "codex", "sessions"), align_day=True)
            return ingest.run_all(a.since, a.out, align_day=True)
        return ingest.run(a.since, a.out, a.root, align_day=True)  # list: first = claude, others claude-<name>
    if a.cmd == "verify-pointers":
        return verify.run(a.date, a.data, a.reports, a.n, a.seed, a.allow_missing)
    if a.cmd == "summary":
        return summary.run(a.data, a.since, a.out)
    return report.run(a.date, a.data, a.out, mask=not a.no_mask)


if __name__ == "__main__":
    sys.exit(main())
