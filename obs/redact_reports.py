"""obs redact-reports: same span masking as `obs redact`, applied to report ledgers and excerpts.

Used two ways: `redact-reports SRC DST` copies a reports tree with every .csv/.md/.json/.html/excerpts/*.txt masked, and
`obs report` calls LedgerMasker at write time (cmd_text cells and excerpt text) so the ledgers on disk are already masked.
Spans: known secrets (env files), names, emails (keep @), keys (keep prefix), IPs/phones/JWT/PEM/Telegram/high-entropy -> X.
Keyword rule: a CSV row whose text cells contain a keyword has those cells X-masked; an excerpt file with one is all X.
CSV structure (header, column count, row count) is unchanged; hashes are computed at ingest, never from the ledger.
"""
import csv, hashlib, io, json, os
from collections import Counter

from . import redact as R

TEXT_COLS = {"cmd_text", "cmd_target", "cmd_cwd"}
EXTS = (".csv", ".md", ".json", ".html", ".txt")


class LedgerMasker:
    def __init__(self, names=None, secrets_files=None, keywords=None, secrets_dirs=None):
        if names is None:
            names = os.environ.get("OBS_NAME_LIST") or R.DEFAULT_NAMES
        if keywords is None:
            keywords = os.environ.get("OBS_MASK_KEYWORDS") or R.DEFAULT_KEYWORDS
        rx, self.nfiles, self.nvals, self.nskip, key_of = R.load_secrets_ex(secrets_files, secrets_dirs)
        self.r = R.Redactor(names, rx, keywords, key_of)
        self.names_n, self.kw_n = len(R._split(names)), len(R._split(keywords))
        self.by_type = Counter()
        self.kw_rows = 0

    def text(self, s):
        return self.r.mask_text(s)

    def excerpt(self, s):
        if self.r.kw and self.r.kw.search(s):
            self.r.c["keyword_masked_records"] += 1
            return R.x(s)
        return self.r.mask_text(s)

    def row(self, header, row, only_text=False):
        """Mask one CSV row in place of text; same number of cells."""
        idx = [i for i, h in enumerate(header) if h in TEXT_COLS and i < len(row)]
        kw = self.r.kw and any(self.r.kw.search(row[i]) for i in idx)
        out = []
        for i, v in enumerate(row):
            if kw and i in idx:
                out.append(R.x(v)); self.r.c["keyword_masked_records"] += 1 if i == idx[0] else 0
            elif only_text and i not in idx:
                out.append(v)
            else:
                out.append(self.r.mask_text(v))
        return out

    def csv_text(self, s):
        rows = list(csv.reader(io.StringIO(s, newline="")))
        if not rows:
            return s
        buf = io.StringIO(newline="")
        w = csv.writer(buf)
        w.writerow(rows[0])
        for r in rows[1:]:
            w.writerow(self.row(rows[0], r))
        return buf.getvalue()


def redact_reports(src, dst, secrets_files=None, names=None, keywords=None, dry_run=False, secrets_dirs=None):
    if dry_run:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            return _redact_reports(src, os.path.join(td, "out"), secrets_files, names, keywords, True, secrets_dirs)
    return _redact_reports(src, dst, secrets_files, names, keywords, False, secrets_dirs)


def _redact_reports(src, dst, secrets_files, names, keywords, dry_run, secrets_dirs=None):
    src, dst = os.path.abspath(src), os.path.abspath(dst)
    if dst == src or dst.startswith(src + os.sep):
        raise SystemExit("dst must be outside src")
    m = LedgerMasker(names, secrets_files, keywords, secrets_dirs)
    detail = []
    for d, dirs, fs in os.walk(src):
        dirs.sort()
        for f in sorted(fs):
            p = os.path.join(d, f)
            rel = os.path.relpath(p, src)
            q = os.path.join(dst, rel)
            os.makedirs(os.path.dirname(q), exist_ok=True)
            ext = os.path.splitext(f)[1].lower()
            in_ex = os.sep + "excerpts" + os.sep in os.sep + rel
            if ext == ".txt" and not in_ex or ext not in EXTS:
                kind = "skipped"
                with open(p, "rb") as fi:
                    b = fi.read()
                m.r.c["files_copied_unmasked"] += 1
            else:
                kind = "excerpt" if in_ex else ext[1:]
                txt = open(p, encoding="utf-8", errors="replace", newline="").read()
                before = sum(m.r.rules.values())
                out = m.csv_text(txt) if ext == ".csv" else m.excerpt(txt) if in_ex else m.text(txt)
                m.by_type[kind] += sum(m.r.rules.values()) - before
                b = out.encode("utf-8")
                m.r.c["files_masked"] += 1
            with open(q + ".tmp", "wb") as fo:
                fo.write(b)
            os.replace(q + ".tmp", q)
            detail.append({"file": rel, "type": kind, "bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()})
    man = {"mode": "reports", "mask_char": "X", "src": src, "files": len(detail), **dict(sorted(m.r.c.items())),
           "rules": dict(sorted(m.r.rules.items())), "spans_by_file_type": dict(sorted(m.by_type.items())),
           "secret_files_read": m.nfiles, "secret_values_loaded": m.nvals,
           "secret_values_skipped": m.nskip, "secret_sources": dict(R.LAST_SOURCES), "known_secret_hits_by_key": dict(m.r.key_hits.most_common()), "names_count": m.names_n,
           "keywords_count": m.kw_n, "file_detail": detail}
    os.makedirs(dst, exist_ok=True)
    with open(os.path.join(dst, "manifest.json"), "w") as f:
        json.dump(man, f, indent=1)
    print(json.dumps({k: v for k, v in man.items() if k != "file_detail"}))
    if dry_run:
        print("dry-run: nothing written; known-secret hits by key NAME: " + json.dumps(man["known_secret_hits_by_key"]))
    return 0
