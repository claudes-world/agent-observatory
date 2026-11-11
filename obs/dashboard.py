"""Read-only LAN dashboard over the Parquet events (FastAPI + DuckDB).

Every /api/<panel> endpoint returns {"rows": [...], "hint": "...", ...} for one panel; the computations
live in obs/panels.py and are shared with the self-contained daily report.html.
No outbound network calls: the chart library is vendored in obs/static/.
Days are UTC, matching the ingest partitioning.
"""
import glob, json, os
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from .panels import PANELS, PanelError, Store, View, human_bytes, human_tokens, short  # noqa: F401 (re-exported)

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def create_app(data):
    st = Store(data, shared=True)  # one in-memory DuckDB for all requests: no parquet handles per request
    app = FastAPI(title="Agent Observatory", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(os.path.join(STATIC, "index.html"))

    @app.get("/api/health")
    def health():
        return {"ok": True, "parquet_files": len(st.file_list())}

    @app.get("/api/pointer")
    def pointer(file: str, line: int = 1):
        """Resolve a transcript pointer; a clean message when the transcript is not on this host."""
        from . import excerpts
        try:
            roots = json.load(open(os.path.join(data, "sources.json")))
        except (OSError, ValueError):
            roots = {}
        got = excerpts.Lines(roots).fetch([{"file": file, "line": line}])
        if not got:
            return {"available": False, "message": excerpts.NOT_HERE, "file": file, "line": line}
        return {"available": True, "file": file, "line": line}

    @app.get("/api/{name}")
    def panel(name: str, days: int = Query(14), harness: list[str] = Query(default=[])):
        f = PANELS.get(name)
        if not f:
            raise HTTPException(404, f"unknown panel {name}; one of {sorted(PANELS)}")
        try:
            return JSONResponse(f(View(st, days=days, harness=[x for h in harness for x in h.split(",")])))
        except PanelError as e:
            raise HTTPException(e.status, e.msg)

    return app


def serve(data, host="127.0.0.1", port=8787):
    import uvicorn
    uvicorn.run(create_app(data), host=host, port=port, log_level="warning")
    return 0
