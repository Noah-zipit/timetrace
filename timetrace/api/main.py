"""timetrace API (event week).

Live endpoints over the temporal FalkorDB graph:
  GET  /api/health    — liveness, no DB needed
  GET  /api/stats     — repo/commit/node/edge counts + DB connectivity
  GET  /api/timeline  — commit list powering the timeline scrubber
  GET  /api/state     — time_slice at ?t=<epoch> (+ repo stats)
  POST /api/query     — the temporal agent: {question, t?, repo?}
  POST /api/ingest    — run the ingest planner + writer in-process

All reads go through the read-only graph handle (ro_query only). When
FalkorDB is unreachable the agent/query endpoints return an honest 503 —
the agent answers nothing rather than inventing an answer.

NOTE on POST /api/ingest: re-ingesting the same snapshots duplicates
CREATE-based edges (edge opens are not idempotent — see
timetrace.ingest.ingest.FalkorWriter), so the demo graph is ingested once.
Nodes are safe (MERGE on id+valid_from); edges are not.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from timetrace import agent as agent_mod
from timetrace.graph import queries as Q
from timetrace.graph.db import (
    DatabaseUnavailableError,
    get_config,
    require_db,
    ro_graph,
)

app = FastAPI(title="timetrace", version="0.1.0-eventweek")


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=3)
    t: Optional[Union[int, str]] = Field(
        default=None,
        description="as-of time: epoch seconds or ISO-8601; default = now")
    repo: Optional[str] = None


class IngestRequest(BaseModel):
    snapshots_dir: str
    repo: Optional[str] = None


class IngestOpsRequest(BaseModel):
    """Pre-planned ops (from plan_ingest) executed in chunks.

    Exists so the graph can be built against a remote FalkorDB from
    environments with bounded request time (e.g. serverless): the planner
    is pure and runs wherever, and each POST executes one chunk of ops.
    create_indexes=true on the first chunk only. Ops are NOT idempotent
    for edge opens — run each chunk exactly once per fresh graph.
    """
    ops: List[Dict[str, Any]]
    create_indexes: bool = False


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _db_error(exc: DatabaseUnavailableError) -> JSONResponse:
    detail = str(exc)
    cause = exc.__cause__
    if cause is not None:
        # Underlying driver errors never carry the password; surfacing the
        # class + first 200 chars is what makes remote debugging possible.
        detail += (f" (cause: {type(cause).__name__}: "
                   f"{str(cause)[:200]})")
    return JSONResponse(status_code=exc.status_code,
                        content={"detail": detail})


def _resolve_repo(repo: Optional[str]) -> Union[str, JSONResponse]:
    """Explicit repo, else the single repo in the graph, else a 400."""
    if repo:
        return repo
    try:
        found = Q.default_repo(ro_graph())
    except DatabaseUnavailableError as exc:
        return _db_error(exc)
    if not found:
        return JSONResponse(
            status_code=400,
            content={"detail": "no repository specified and the graph holds "
                               "zero or several repositories; pass ?repo="})
    return found


def _parse_t(t: Optional[Union[int, str]]) -> Optional[int]:
    if t is None:
        return None
    if isinstance(t, int):
        return t
    text = t.strip()
    if text.isdigit():
        return int(text)
    return agent_mod._iso_to_epoch(text)  # reuse the agent's ISO parser


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "service": "timetrace", "phase": "eventweek"}


@app.get("/api/stats")
def stats(repo: Optional[str] = None) -> JSONResponse:
    """Repo/commit/node/edge counts + DB connectivity status.

    Returns 200 even when the DB is down, with connected=false — the UI
    uses this to render its status pill honestly.
    """
    try:
        g = require_db()
    except DatabaseUnavailableError as exc:
        return JSONResponse(status_code=200, content={
            "connected": False, "error": str(exc), "repo": repo})
    repo_name = repo or Q.default_repo(g)
    payload: Dict[str, Any] = {"connected": True,
                               "graph": g.config.graph,
                               "host": f"{g.config.host}:{g.config.port}"}
    if repo_name:
        payload.update(Q.repo_stats(g, repo_name))
    else:
        payload["repo"] = None
    return JSONResponse(status_code=200, content=payload)


@app.get("/api/timeline")
def timeline(repo: Optional[str] = None) -> JSONResponse:
    """Commit list for the timeline scrubber (ordered oldest -> newest)."""
    resolved = _resolve_repo(repo)
    if isinstance(resolved, JSONResponse):
        return resolved
    try:
        entries = Q.timeline(ro_graph(), resolved)
    except DatabaseUnavailableError as exc:
        return _db_error(exc)
    return JSONResponse(status_code=200,
                        content={"repo": resolved, "entries": entries})


@app.get("/api/state")
def state(t: Optional[int] = None,
          repo: Optional[str] = None) -> JSONResponse:
    """Time-slice at ?t=<epoch> (default now): open modules + functions."""
    import time as _time

    resolved = _resolve_repo(repo)
    if isinstance(resolved, JSONResponse):
        return resolved
    at = t if t is not None else int(_time.time())
    try:
        g = ro_graph()
        payload = {"repo": resolved, "t": at,
                   "slice": Q.time_slice(g, resolved, at),
                   "stats": Q.repo_stats(g, resolved)}
    except DatabaseUnavailableError as exc:
        return _db_error(exc)
    return JSONResponse(status_code=200, content=payload)


@app.post("/api/query")
def query(req: QueryRequest) -> JSONResponse:
    """The temporal agent: question (+ optional as-of time) -> verdict,
    summary, proof chain. Every fact comes from graph queries only."""
    try:
        result = agent_mod.answer(req.question, t=_parse_t(req.t),
                                  repo=req.repo)
    except DatabaseUnavailableError as exc:
        return _db_error(exc)
    except ValueError as exc:  # e.g. unresolvable target id shape
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    return JSONResponse(status_code=200, content=result)


@app.post("/api/ingest")
def ingest(req: IngestRequest) -> JSONResponse:
    """Run the ingest planner + FalkorDB writer in-process.

    Body: {"snapshots_dir": "<dir with manifest.json + <sha>.json>",
           "repo": "<name, default: dir name>"}.
    Returns planned op counts and written counts.

    WARNING: re-ingesting the same snapshots duplicates CREATE-based
    edges (MODIFIED/DEFINES/CALLS/... opens are plain CREATEs, not
    idempotent). Ingest the demo graph once; for a re-run, point at a
    fresh graph name via the FALKORDB_GRAPH env key.
    """
    import json
    import os

    from timetrace.ingest import plan_ingest, FalkorWriter

    snaps_dir = req.snapshots_dir
    manifest_path = os.path.join(snaps_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        return JSONResponse(
            status_code=400,
            content={"detail": f"no manifest.json in {snaps_dir!r}"})
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        snapshots: List[Dict[str, Any]] = []
        for entry in manifest:
            path = os.path.join(snaps_dir, f"{entry['commit']}.json")
            with open(path, encoding="utf-8") as fh:
                snapshots.append(json.load(fh))
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        return JSONResponse(status_code=400,
                            content={"detail": f"bad snapshots dir: {exc}"})

    repo = req.repo or os.path.basename(os.path.abspath(snaps_dir.rstrip("/")))
    ops = plan_ingest(repo, snapshots)
    planned: Dict[str, int] = {}
    for op in ops:
        key = op["op"] + (":" + op.get("kind", op.get("label", ""))).rstrip(":")
        planned[key] = planned.get(key, 0) + 1

    cfg = get_config()
    try:
        writer = FalkorWriter(cfg.host, cfg.port, cfg.username, cfg.password,
                              graph=cfg.graph)
        writer.create_indexes()
        written = writer.write_ops(ops)
    except Exception as exc:  # noqa: BLE001 - surfaced as 502 detail
        return JSONResponse(
            status_code=502,
            content={"detail": f"ingest failed against "
                               f"{cfg.host}:{cfg.port}: {exc}"})

    return JSONResponse(status_code=200, content={
        "repo": repo, "graph": cfg.graph,
        "snapshots": len(snapshots),
        "ops_planned": len(ops), "planned_by_kind": planned,
        "written": written,
        "note": "Nodes are idempotent (MERGE on id+valid_from); edge opens "
                "are CREATEs — re-ingesting the same snapshots duplicates edges.",
    })


@app.post("/api/ingest-ops")
def ingest_ops(req: IngestOpsRequest) -> JSONResponse:
    """Execute one chunk of pre-planned ingest ops.

    Body: {"ops": [...], "create_indexes": true|false}.
    See IngestOpsRequest for the why: planning is pure and happens off-
    server; each chunk runs through FalkorWriter.write_ops in UNWIND
    batches against the configured FalkorDB.
    """
    from timetrace.ingest import FalkorWriter

    cfg = get_config()
    try:
        writer = FalkorWriter(cfg.host, cfg.port, cfg.username,
                              cfg.password, graph=cfg.graph)
        if req.create_indexes:
            writer.create_indexes()
        written = writer.write_ops(req.ops)
    except Exception as exc:  # noqa: BLE001 - surfaced as 502 detail
        return JSONResponse(
            status_code=502,
            content={"detail": f"ingest-ops failed against "
                               f"{cfg.host}:{cfg.port}: {exc}"})
    return JSONResponse(status_code=200, content={
        "graph": cfg.graph, "ops_received": len(req.ops),
        "written": written})


# UI shell, served last so API routes win. Path resolved from this module's
# location so the app boots the same regardless of CWD.
UI_DIR = Path(__file__).resolve().parent.parent.parent / "ui"
app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")
