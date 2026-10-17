"""timetrace API (event week).

Paste ANY public GitHub repo URL and timetrace builds a temporal graph of
it, then answers "can I safely change X?" against that repo's history. The
Textualize/rich demo stays as the bundled default.

Live endpoints over the temporal FalkorDB graphs:
  GET  /api/health    — liveness, no DB needed
  GET  /api/repos     — analyzed repos (rich demo + user-analyzed)
  GET  /api/stats     — repo/commit/node/edge counts + DB connectivity
  GET  /api/timeline  — commit list powering the timeline scrubber
  GET  /api/state     — time_slice at ?t=<epoch> (+ repo stats)
  POST /api/query     — the temporal agent: {question, t?, repo?}
  POST /api/ingest    — run the ingest planner + writer in-process
  POST /api/analyze/start — begin analyzing a GitHub repo (returns job)
  POST /api/analyze/step  — process ONE commit snapshot (client-stepped)
  GET  /api/analyze/status — job progress

All reads go through the read-only graph handle (ro_query only). When
FalkorDB is unreachable the agent/query endpoints return an honest 503 —
the agent answers nothing rather than inventing an answer.

Each analyzed repo lives in its OWN FalkorDB graph ("gh_{owner}__{repo}",
sanitized); the rich demo graph "timetrace" is never touched. The `repo`
parameter routes to the right graph server-side, so every endpoint works
unchanged per repo.

NOTE on POST /api/ingest: the ingest path is idempotent through
FalkorWriter (MERGE on id+valid_from / uid; closes are no-ops when already
closed — see timetrace.ingest.ingest), so re-running a chunk or a step is
safe.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from timetrace import agent as agent_mod
from timetrace.analyze import github as gh_mod
from timetrace.analyze import jobs as jobs_mod
from timetrace.graph import queries as Q
from timetrace.graph.db import (
    DatabaseUnavailableError,
    get_config,
    require_db,
    ro_graph,
)
from timetrace.ingest.ingest import SnapshotPlanner
from timetrace.parser import parse_files

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
    create_indexes=true on the first chunk only. Ops are idempotent
    through FalkorWriter, so re-sending a chunk is safe.
    """
    ops: List[Dict[str, Any]]
    create_indexes: bool = False


class AnalyzeStartRequest(BaseModel):
    repo_url: str = Field(..., min_length=10,
                          description="public GitHub repo URL, "
                                      "e.g. https://github.com/tqdm/tqdm")


class AnalyzeStepRequest(BaseModel):
    repo: str = Field(..., min_length=3,
                      description="analyzed repo name, e.g. 'tqdm/tqdm'")


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


def _resolve_target(repo: Optional[str]
                    ) -> Union[Tuple[str, str], JSONResponse]:
    """(graph_name, repo_name) for a repo selector.

    Omitted or "rich" -> the bundled demo (graph "timetrace"). Any other
    repo routes through the registry of analyzed repos; unknown repos get
    a 404 that points at /api/analyze/start.
    """
    if not repo or repo == jobs_mod.DEMO_REPO:
        return jobs_mod.DEFAULT_GRAPH, jobs_mod.DEMO_REPO
    try:
        graph = jobs_mod.registry_lookup(repo)
    except DatabaseUnavailableError as exc:
        return _db_error(exc)
    if not graph:
        return JSONResponse(
            status_code=404,
            content={"detail": f"unknown repo {repo!r} — analyze it first "
                               "with POST /api/analyze/start "
                               "{\"repo_url\": \"https://github.com/owner/repo\"}"})
    return graph, repo


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


@app.get("/api/repos")
def repos() -> JSONResponse:
    """Every repo the UI can show: the bundled rich demo + analyzed repos."""
    out = [{"repo": jobs_mod.DEMO_REPO, "graph": jobs_mod.DEFAULT_GRAPH,
            "owner": "Textualize", "status": "ready"}]
    try:
        out.extend(jobs_mod.list_registered())
    except DatabaseUnavailableError as exc:
        return _db_error(exc)
    return JSONResponse(status_code=200, content={"repos": out})


@app.get("/api/stats")
def stats(repo: Optional[str] = None) -> JSONResponse:
    """Repo/commit/node/edge counts + DB connectivity status.

    Returns 200 even when the DB is down, with connected=false — the UI
    uses this to render its status pill honestly.
    """
    resolved = _resolve_target(repo)
    if isinstance(resolved, JSONResponse):
        return resolved
    graph, repo_name = resolved
    try:
        g = jobs_mod.require_db_named(graph)
    except DatabaseUnavailableError as exc:
        return JSONResponse(status_code=200, content={
            "connected": False, "error": str(exc), "repo": repo_name,
            "graph": graph})
    payload: Dict[str, Any] = {"connected": True,
                               "graph": g.config.graph,
                               "host": f"{g.config.host}:{g.config.port}"}
    payload.update(Q.repo_stats(g, repo_name))
    return JSONResponse(status_code=200, content=payload)


@app.get("/api/timeline")
def timeline(repo: Optional[str] = None) -> JSONResponse:
    """Commit list for the timeline scrubber (ordered oldest -> newest)."""
    resolved = _resolve_target(repo)
    if isinstance(resolved, JSONResponse):
        return resolved
    graph, repo_name = resolved
    try:
        entries = Q.timeline(jobs_mod.ro_graph_named(graph), repo_name)
    except DatabaseUnavailableError as exc:
        return _db_error(exc)
    return JSONResponse(status_code=200,
                        content={"repo": repo_name, "graph": graph,
                                 "entries": entries})


@app.get("/api/state")
def state(t: Optional[int] = None,
          repo: Optional[str] = None) -> JSONResponse:
    """Time-slice at ?t=<epoch> (default now): open modules + functions."""
    import time as _time

    resolved = _resolve_target(repo)
    if isinstance(resolved, JSONResponse):
        return resolved
    graph, repo_name = resolved
    at = t if t is not None else int(_time.time())
    try:
        g = jobs_mod.ro_graph_named(graph)
        payload = {"repo": repo_name, "graph": graph, "t": at,
                   "slice": Q.time_slice(g, repo_name, at),
                   "stats": Q.repo_stats(g, repo_name)}
    except DatabaseUnavailableError as exc:
        return _db_error(exc)
    return JSONResponse(status_code=200, content=payload)


@app.post("/api/query")
def query(req: QueryRequest) -> JSONResponse:
    """The temporal agent: question (+ optional as-of time) -> verdict,
    summary, proof chain. Every fact comes from graph queries only."""
    resolved = _resolve_target(req.repo)
    if isinstance(resolved, JSONResponse):
        return resolved
    graph, repo_name = resolved
    try:
        result = agent_mod.answer(req.question, t=_parse_t(req.t),
                                  repo=repo_name, graph=graph)
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

    Ingest ops are idempotent through FalkorWriter (MERGE on
    id+valid_from / uid; closes are no-ops when already closed), so
    re-running a chunk or the whole ingest is safe.
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


@app.get("/api/diag-net")
def diag_net() -> JSONResponse:
    """One-shot network diagnostic (no secrets): DNS + raw TCP to the
    configured FalkorDB host:port. Helps distinguish Vercel-side egress
    blocks from instance-side problems."""
    import socket as _socket

    cfg = get_config()
    out: Dict[str, Any] = {"host": cfg.host, "port": cfg.port}
    try:
        out["dns"] = _socket.gethostbyname(cfg.host)
    except Exception as exc:  # noqa: BLE001
        out["dns_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        return JSONResponse(status_code=200, content=out)
    s = _socket.socket()
    s.settimeout(8)
    try:
        s.connect((cfg.host, cfg.port))
        out["tcp"] = "connected"
    except Exception as exc:  # noqa: BLE001
        out["tcp_error"] = f"{type(exc).__name__}: {str(exc)[:150]}"
    finally:
        s.close()
    return JSONResponse(status_code=200, content=out)


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


# ---------------------------------------------------------------------------
# analyze: any public GitHub repo -> temporal graph (client-stepped)
# ---------------------------------------------------------------------------


@app.post("/api/analyze/start")
def analyze_start(req: AnalyzeStartRequest) -> JSONResponse:
    """Begin analyzing a public GitHub repo.

    Lists commits (paginated), samples up to 40 spread across the listed
    history, checks the oldest sampled commit has Python files, then
    creates the repo's own graph + AnalyzeJob + registry row. The UI then
    calls POST /api/analyze/step once per commit until the job is done.

    Honest caps (also in timetrace/analyze/github.py): commit listing
    covers the newest 800 commits; repos over 60 MB are refused; Python
    only (parser is Python-only); unauthenticated GitHub rate limit
    (60 req/hour) applies.
    """
    try:
        owner, repo = gh_mod.parse_repo_url(req.repo_url)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    repo_name = jobs_mod.repo_name_for(owner, repo)

    existing = None
    try:
        graph = jobs_mod.registry_lookup(repo_name)
        if graph:
            existing = jobs_mod.load_job(repo_name, graph)
    except DatabaseUnavailableError as exc:
        return _db_error(exc)
    if existing and existing.get("status") == "done":
        return JSONResponse(status_code=200, content={
            "repo": repo_name, "owner": owner,
            "graph": existing["graph"], "total": existing["total"],
            "done": existing["total"], "status": "done",
            "note": "already analyzed"})
    if existing:  # interrupted job: resume where it left off
        pending = json_loads(existing["pending"])
        return JSONResponse(status_code=200, content={
            "repo": repo_name, "owner": owner, "graph": existing["graph"],
            "total": existing["total"], "done": existing["done"],
            "status": "running", "note": "resumed"})

    try:
        info = gh_mod.repo_info(owner, repo)
    except gh_mod.RepoNotFoundError:
        return JSONResponse(status_code=404, content={
            "detail": f"repo not found on GitHub: {owner}/{repo}"})
    except gh_mod.GitHubError as exc:
        return JSONResponse(status_code=502, content={"detail": str(exc)})
    if info["private"]:
        return JSONResponse(status_code=400, content={
            "detail": "private repos are not supported — paste a public "
                      "GitHub repo URL"})
    if info["size_kb"] > gh_mod.SIZE_CAP_KB:
        return JSONResponse(status_code=400, content={
            "detail": f"repo is {info['size_kb'] // 1024} MB; the serverless "
                      f"analyze flow caps at {gh_mod.SIZE_CAP_KB // 1024} MB"})

    try:
        commits = gh_mod.list_commits(owner, repo)
    except gh_mod.GitHubError as exc:
        return JSONResponse(status_code=502, content={"detail": str(exc)})
    if not commits:
        return JSONResponse(status_code=400, content={
            "detail": f"no commits found for {owner}/{repo}"})
    sampled = gh_mod.sample_commits(commits, gh_mod.SAMPLE_N)

    # Python check on the OLDEST sampled commit (fail fast, clear message).
    try:
        probe = gh_mod.fetch_py_files(owner, repo, sampled[0]["sha"])
    except gh_mod.GitHubError as exc:
        return JSONResponse(status_code=502, content={"detail": str(exc)})
    if len(probe) < gh_mod.MIN_PY_FILES:
        return JSONResponse(status_code=400, content={
            "detail": f"only {len(probe)} Python files at the oldest sampled "
                      "commit — timetrace's parser is Python-only, so this "
                      "repo cannot be analyzed"})

    graph = jobs_mod.graph_name_for(owner, repo)
    try:
        jobs_mod.create_job(owner, repo_name, graph, sampled)
        jobs_mod.register_repo(owner, repo_name, graph,
                               status=f"0/{len(sampled)}")
    except Exception as exc:  # noqa: BLE001 - surfaced as 502 detail
        return JSONResponse(status_code=502, content={
            "detail": f"could not create analyze job: {exc}"})
    return JSONResponse(status_code=200, content={
        "repo": repo_name, "owner": owner, "graph": graph,
        "total": len(sampled), "done": 0, "status": "running",
        "note": f"sampled {len(sampled)} commits across the listed history"})


@app.post("/api/analyze/step")
def analyze_step(req: AnalyzeStepRequest) -> JSONResponse:
    """Process ONE commit snapshot for an analyze job.

    Fetch the commit tarball, parse the Python files, plan ops against
    planner state reconstructed from the graph, write them, advance the
    job. Idempotent ops make a retried step safe.
    """
    repo_name = req.repo
    try:
        graph = jobs_mod.registry_lookup(repo_name)
    except DatabaseUnavailableError as exc:
        return _db_error(exc)
    if not graph:
        return JSONResponse(status_code=404, content={
            "detail": f"unknown repo {repo_name!r} — start with "
                      "POST /api/analyze/start"})
    job = jobs_mod.load_job(repo_name, graph)
    if job is None:
        return JSONResponse(status_code=404, content={
            "detail": f"no analyze job for {repo_name!r}"})
    if job["status"] == "done":
        return JSONResponse(status_code=200, content={
            "repo": repo_name, "done": job["total"], "total": job["total"],
            "status": "done"})
    pending = json_loads(job["pending"])
    if not pending:  # belt-and-braces: job drained without being marked
        jobs_mod.save_job(repo_name, graph, [], job["total"], "done")
        jobs_mod.set_registry_status(repo_name, "ready")
        return JSONResponse(status_code=200, content={
            "repo": repo_name, "done": job["total"], "total": job["total"],
            "status": "done"})

    entry = pending[0]
    owner = job["owner"]
    gh_repo = repo_name.split("/", 1)[1]
    try:
        files = gh_mod.fetch_py_files(owner, gh_repo, entry["sha"])
    except gh_mod.GitHubError as exc:
        return JSONResponse(status_code=502, content={"detail": str(exc)})
    try:
        ir = parse_files(files)
    except Exception as exc:  # noqa: BLE001 - parser is pure; be honest
        return JSONResponse(status_code=502, content={
            "detail": f"parser failed on commit {entry['sha'][:7]}: {exc}"})

    snapshot = {
        "commit": entry["sha"],
        "author": f"{entry['name']} <{entry['email']}>",
        "timestamp": entry["date_iso"],
        "nodes": ir["nodes"],
        "edges": ir["edges"],
    }
    try:
        g = jobs_mod.ro_graph_named(graph)
        open_nodes, open_edges = jobs_mod.reconstruct_planner_state(
            g, repo_name)
        planner = SnapshotPlanner(repo_name, open_nodes, open_edges,
                                  emit_repo_open=False)
        ops = planner.add_snapshot(snapshot)
        written = jobs_mod.writer_named(graph).write_ops(ops)
    except DatabaseUnavailableError as exc:
        return _db_error(exc)
    except Exception as exc:  # noqa: BLE001 - surfaced as 502 detail
        return JSONResponse(status_code=502, content={
            "detail": f"step failed on commit {entry['sha'][:7]}: {exc}"})

    done = job["done"] + 1
    rest = pending[1:]
    status = "done" if not rest else "running"
    jobs_mod.save_job(repo_name, graph, rest, done, status)
    jobs_mod.set_registry_status(
        repo_name, "ready" if status == "done" else f"{done}/{job['total']}")
    return JSONResponse(status_code=200, content={
        "repo": repo_name, "done": done, "total": job["total"],
        "status": status, "ops_written": written})


@app.get("/api/analyze/status")
def analyze_status(repo: str) -> JSONResponse:
    """Job progress for one repo: {repo, done, total, status}."""
    try:
        graph = jobs_mod.registry_lookup(repo)
    except DatabaseUnavailableError as exc:
        return _db_error(exc)
    if not graph:
        return JSONResponse(status_code=404, content={
            "detail": f"unknown repo {repo!r}"})
    job = jobs_mod.load_job(repo, graph)
    if job is None:
        return JSONResponse(status_code=404, content={
            "detail": f"no analyze job for {repo!r}"})
    return JSONResponse(status_code=200, content={
        "repo": repo, "graph": graph, "done": job["done"],
        "total": job["total"], "status": job["status"]})


def json_loads(text: Optional[str]) -> List[Dict[str, Any]]:
    """Pending-commit JSON with a sane default."""
    import json as _json

    if not text:
        return []
    try:
        val = _json.loads(text)
    except (ValueError, TypeError):
        return []
    return val if isinstance(val, list) else []


# UI shell, served last so API routes win. Path resolved from this module's
# location so the app boots the same regardless of CWD.
UI_DIR = Path(__file__).resolve().parent.parent.parent / "ui"
app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")
