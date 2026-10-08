"""timetrace API skeleton (foundation).

Live endpoint: GET /api/health.
Everything else is a 501 stub naming its event-week build — no graph
database code of any kind (no FalkorDB, no Cypher) lives here or anywhere
in foundation; the temporal graph layer is built live Oct 15-18.

The UI shell in ../../ui is served statically at "/". API routes are
registered BEFORE the static mount so /api/* is never swallowed.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="timetrace", version="0.1.0-foundation")


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "timetrace", "phase": "foundation"}


@app.post("/api/ingest")
def ingest():
    return JSONResponse(
        status_code=501,
        content={
            "detail": "NOT IMPLEMENTED — event week: ingest repo snapshots into the temporal graph"
        },
    )


@app.post("/api/query")
def query():
    return JSONResponse(
        status_code=501,
        content={
            "detail": "NOT IMPLEMENTED — event week: temporal agent query over the graph (question + as-of time)"
        },
    )


@app.get("/api/timeline")
def timeline():
    return JSONResponse(
        status_code=501,
        content={
            "detail": "NOT IMPLEMENTED — event week: snapshot list powering the timeline scrubber"
        },
    )


# UI shell, served last so API routes win. Path resolved from this module's
# location so the app boots the same regardless of CWD.
UI_DIR = Path(__file__).resolve().parent.parent.parent / "ui"
app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")
