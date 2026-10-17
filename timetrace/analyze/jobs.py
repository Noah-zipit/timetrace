"""timetrace.analyze.jobs — per-repo graphs, repo registry, analyze jobs.

Every analyzed repo gets its OWN FalkorDB graph ("gh_{owner}__{repo}",
sanitized), so the rich demo graph "timetrace" is never touched. The
registry (repo -> graph) lives in the default graph as :RepoGraph nodes;
each analyze job lives in its repo's own graph as an :AnalyzeJob node
holding the pending commit list as JSON.

Planner state between /analyze/step calls is RECONSTRUCTED FROM THE GRAPH
(open_nodes / open_edges registries), never kept in server memory — which
is also what makes a retried step safe: the ops SnapshotPlanner emits are
idempotent through FalkorWriter (MERGE on id+valid_from / uid; closes are
no-ops when already closed).
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple

from timetrace.graph.db import (
    DatabaseUnavailableError,
    FalkorConfig,
    ReadOnlyGraph,
    _ClientCache,
    get_config,
)
from timetrace.ingest.ingest import _edge_key

DEFAULT_GRAPH = "timetrace"   # the rich demo graph; never written by analyze
DEMO_REPO = "rich"            # repo name inside the demo graph

_DIFF_KINDS = ("DEFINES", "CALLS", "IMPORTS")


def sanitize(text: str) -> str:
    """'psf' -> 'psf'; 'my.repo' -> 'my_repo'. Never empty."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "repo"


def graph_name_for(owner: str, repo: str) -> str:
    """Deterministic per-repo graph name: 'gh_psf__requests'."""
    return f"gh_{sanitize(owner)}__{sanitize(repo)}"[:64]


def repo_name_for(owner: str, repo: str) -> str:
    """The repo name stored in the graph: 'psf/requests'."""
    return f"{owner}/{repo}"


def config_for_graph(graph_name: str) -> FalkorConfig:
    cfg = get_config()
    return FalkorConfig(host=cfg.host, port=cfg.port,
                        username=cfg.username, password=cfg.password,
                        graph=graph_name)


def ro_graph_named(graph_name: str) -> ReadOnlyGraph:
    """Read-only handle for an arbitrary graph (same auth as default)."""
    config = config_for_graph(graph_name)
    client = _ClientCache.get(config)
    return ReadOnlyGraph(client.select_graph(graph_name), config)


def require_db_named(graph_name: str) -> ReadOnlyGraph:
    """Ping the instance on a named graph; honest 503 when unreachable."""
    config = config_for_graph(graph_name)
    try:
        client = _ClientCache.get(config)
        graph = client.select_graph(graph_name)
        graph.ro_query("RETURN 1 AS ok")
    except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
        raise DatabaseUnavailableError(config) from exc
    return ReadOnlyGraph(graph, config)


def writer_named(graph_name: str):
    """Writable FalkorWriter for a named graph (lazy falkordb import)."""
    from timetrace.ingest import FalkorWriter

    cfg = get_config()
    return FalkorWriter(cfg.host, cfg.port, cfg.username, cfg.password,
                        graph=graph_name)


# ---------------------------------------------------------------------------
# registry (lives in the default graph)
# ---------------------------------------------------------------------------


def register_repo(owner: str, repo: str, graph: str,
                  status: str = "running") -> None:
    w = writer_named(DEFAULT_GRAPH)
    w._run(
        "MERGE (rg:RepoGraph {repo: $repo}) "
        "SET rg.graph = $graph, rg.owner = $owner, rg.status = $status",
        repo=repo, graph=graph, owner=owner, status=status)


def registry_lookup(repo: str) -> Optional[str]:
    """repo name -> graph name, or None when never analyzed."""
    g = ro_graph_named(DEFAULT_GRAPH)
    rows = g.ro_query(
        "MATCH (rg:RepoGraph {repo: $repo}) RETURN rg.graph AS graph",
        {"repo": repo})
    vals = [r[0] for r in rows.result_set]
    return vals[0] if vals else None


def list_registered() -> List[Dict]:
    """All analyzed repos: [{repo, graph, owner, status}]."""
    g = ro_graph_named(DEFAULT_GRAPH)
    rows = g.ro_query(
        "MATCH (rg:RepoGraph) "
        "RETURN rg.repo AS repo, rg.graph AS graph, "
        "       rg.owner AS owner, rg.status AS status "
        "ORDER BY rg.repo")
    return [{"repo": r[0], "graph": r[1], "owner": r[2],
             "status": r[3]} for r in rows.result_set]


def set_registry_status(repo: str, status: str) -> None:
    w = writer_named(DEFAULT_GRAPH)
    w._run("MATCH (rg:RepoGraph {repo: $repo}) SET rg.status = $status",
           repo=repo, status=status)


# ---------------------------------------------------------------------------
# jobs (live in the repo's own graph)
# ---------------------------------------------------------------------------


def create_job(owner: str, repo: str, graph: str,
               commits: List[Dict]) -> Dict:
    """Create the AnalyzeJob node (idempotent MERGE) and the Repository node.

    The repository's :Repository node is opened here so /api/stats works
    even mid-analysis; the first /analyze/step emits no repo_open of its own.
    """
    w = writer_named(graph)
    w.create_indexes()
    job_id = f"job:{repo}"
    w._run(
        "MERGE (j:AnalyzeJob {id: $id}) "
        "SET j.repo = $repo, j.owner = $owner, j.graph = $graph, "
        "    j.total = $total, j.done = 0, j.status = 'running', "
        "    j.pending = $pending",
        id=job_id, repo=repo, owner=owner, graph=graph,
        total=len(commits), pending=json.dumps(commits))
    w._run(
        "MERGE (r:Repository {name: $repo}) "
        "SET r.id = 'repo:' + $repo, "
        "    r.valid_from = coalesce(r.valid_from, timestamp()), "
        "    r.valid_to = NULL",
        repo=repo)
    return {"id": job_id, "repo": repo, "owner": owner, "graph": graph,
            "total": len(commits), "done": 0, "status": "running"}


def load_job(repo: str, graph: str) -> Optional[Dict]:
    g = ro_graph_named(graph)
    rows = g.ro_query(
        "MATCH (j:AnalyzeJob {id: $id}) "
        "RETURN j.repo AS repo, j.owner AS owner, j.graph AS graph, "
        "       j.total AS total, j.done AS done, j.status AS status, "
        "       j.pending AS pending",
        {"id": f"job:{repo}"})
    if not rows.result_set:
        return None
    r = rows.result_set[0]
    return {"repo": r[0], "owner": r[1], "graph": r[2], "total": r[3],
            "done": r[4], "status": r[5], "pending": r[6]}


def save_job(repo: str, graph: str, pending: List[Dict],
             done: int, status: str) -> None:
    w = writer_named(graph)
    w._run(
        "MATCH (j:AnalyzeJob {id: $id}) "
        "SET j.pending = $pending, j.done = $done, j.status = $status",
        id=f"job:{repo}", pending=json.dumps(pending),
        done=done, status=status)


# ---------------------------------------------------------------------------
# planner-state reconstruction
# ---------------------------------------------------------------------------


def reconstruct_planner_state(g: ReadOnlyGraph,
                              repo: str) -> Tuple[dict, dict]:
    """Rebuild SnapshotPlanner's open_nodes/open_edges from the graph.

    open_nodes: id -> {valid_from, hash, label, qualname, fields{...}}
    open_edges: edge_key -> {uid, valid_from, kind, from, to, from_vf,
                             to_vf, props}
    (Shapes mirror SnapshotPlanner's in-memory registries exactly.)
    """
    marker = f":{repo}:"

    open_nodes: Dict[str, dict] = {}
    node_rows = g.ro_query(
        "MATCH (n) "
        "WHERE n.valid_to IS NULL AND n.id CONTAINS $marker "
        "  AND (n:Module OR n:Class OR n:Function) "
        "RETURN labels(n)[0] AS label, n.id AS id, "
        "       n.valid_from AS vf, n.body_hash AS bh, n.name AS name, "
        "       n.qualname AS qualname, n.args AS args, "
        "       n.decorators AS decs, n.bases AS bases, "
        "       n.async AS async, n.external AS ext",
        {"marker": marker})
    for row in node_rows.result_set:
        (label, nid, vf, bh, name, qualname, args, decs, bases,
         async_, ext) = row
        open_nodes[nid] = {
            "valid_from": vf,
            "hash": bh or "",
            "label": label,
            "qualname": "" if ext else (qualname or ""),
            "fields": {
                "name": name or "",
                "args": list(args or []),
                "decorators": list(decs or []),
                "bases": list(bases or []),
                "async": bool(async_),
            },
        }

    open_edges: Dict[tuple, dict] = {}
    edge_rows = g.ro_query(
        "MATCH (a)-[e]->(b) "
        "WHERE e.valid_to IS NULL AND type(e) IN $kinds "
        "  AND a.id CONTAINS $marker AND b.id CONTAINS $marker "
        "RETURN type(e) AS kind, a.id AS frm, a.valid_from AS frv, "
        "       b.id AS dto, b.valid_from AS tov, e.uid AS uid, "
        "       e.valid_from AS evf, e.callee AS callee, e.names AS names",
        {"kinds": list(_DIFF_KINDS), "marker": marker})
    for row in edge_rows.result_set:
        kind, frm, frv, dto, tov, uid, evf, callee, names = row
        key = _edge_key(kind, frm, dto, callee or "")
        props: Dict = {}
        if kind == "CALLS":
            props["callee"] = callee or ""
        elif kind == "IMPORTS":
            props["names"] = list(names or [])
        open_edges[key] = {
            "uid": uid, "valid_from": evf, "kind": kind,
            "from": frm, "to": dto, "from_vf": frv, "to_vf": tov,
            "props": props,
        }
    return open_nodes, open_edges
