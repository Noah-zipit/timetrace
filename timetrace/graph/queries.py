"""timetrace.graph.queries — pure temporal query functions.

Every function here takes a read-only graph handle and runs real
multi-hop Cypher with the window predicate on every node AND every edge:

    n.valid_from <= $t AND (n.valid_to IS NULL OR n.valid_to > $t)

These are pure in the database sense: no writes, no snapshot JSON, no
side effects. The agent (timetrace/agent.py) builds all its answers from
these functions alone.

FalkorDB notes (docs/TEMPORAL_CHEAT_SHEET.md):
- No regex operator: CONTAINS / ENDS WITH / STARTS WITH only.
- No label disjunctions ``(n:Function|Class)``: separate branches + UNION.
- <> filters are not index-accelerated: avoided in hot paths.
- Variable-length CALLS paths are kept to 1..2 hops, bounded.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from timetrace.graph.db import ReadOnlyGraph

__all__ = [
    "time_slice", "blast_radius", "churn", "owners", "timeline",
    "resolve_target", "repo_stats", "target_versions", "default_repo",
]

# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _win(alias: str) -> str:
    """Window predicate fragment for node/edge alias, using the $t param."""
    return (f"{alias}.valid_from <= $t "
            f"AND ({alias}.valid_to IS NULL OR {alias}.valid_to > $t)")


def _rows(g: ReadOnlyGraph, cypher: str,
          params: Optional[Dict[str, Any]] = None) -> List[List[Any]]:
    """Run a read-only query, return raw rows (list of lists)."""
    result = g.ro_query(cypher, params or {})
    return [list(row) for row in result.result_set]


def _label_for(target_id: str) -> str:
    """Deterministic label from the id prefix (ID FORMATS in the build brief)."""
    if target_id.startswith("module:"):
        return "Module"
    if target_id.startswith("func:"):
        return "Function"
    if target_id.startswith("class:"):
        return "Class"
    raise ValueError(f"cannot determine node label from id {target_id!r}")


def _short_module(module_prop: Any) -> str:
    """'module:a/b.py' -> 'a/b.py'; passes through anything else."""
    if isinstance(module_prop, str) and module_prop.startswith("module:"):
        return module_prop[len("module:"):]
    return module_prop or ""


def _module_id_for(repo: str, target_id: str) -> Optional[str]:
    """Module id owning a function/class id: 'func:{repo}:{path}:{q}' -> 'module:{repo}:{path}'.

    Splits on the LAST colon; qualnames never contain one (planner rekeys
    on the first colon, so path/qualname boundary is the last).
    """
    label = _label_for(target_id)
    if label == "Module":
        return target_id
    rest = target_id.split(":", 2)[2]  # strip 'func:{repo}:' / 'class:{repo}:'
    path, _, _qualname = rest.rpartition(":")
    if not path:
        return None
    return f"module:{repo}:{path}"


# ---------------------------------------------------------------------------
# repository helpers
# ---------------------------------------------------------------------------


def default_repo(g: ReadOnlyGraph) -> Optional[str]:
    """The single repository in the graph, or None if there isn't exactly one."""
    rows = _rows(g, "MATCH (r:Repository) RETURN r.name AS name")
    names = sorted({r[0] for r in rows if r[0]})
    return names[0] if len(names) == 1 else None


def repo_stats(g: ReadOnlyGraph, repo: str) -> Dict[str, Any]:
    """Node/edge counts for one repository (for /api/stats)."""
    out: Dict[str, Any] = {"repo": repo}
    for label in ("Module", "Class", "Function", "Person"):
        rows = _rows(
            g,
            f"CYPHER repo=$repo MATCH (r:Repository {{name: $repo}})"
            f"<-[:IN_REPO]-(n:{label}) RETURN count(n) AS c",
            {"repo": repo},
        )
        out[f"n_{label.lower()}"] = rows[0][0] if rows else 0
    rows = _rows(
        g,
        "CYPHER repo=$repo "
        "MATCH (r:Repository {name: $repo})<-[:IN_REPO]-(n)"
        "<-[mod:MODIFIED]-(:Person)-[:AUTHORED]->(c:Commit) "
        "RETURN count(DISTINCT c) AS c",
        {"repo": repo},
    )
    out["n_commits"] = rows[0][0] if rows else 0
    rows = _rows(
        g,
        "CYPHER repo=$repo "
        "MATCH (r:Repository {name: $repo})<-[:IN_REPO]-(a)-[e]->(b) "
        "RETURN type(e) AS kind, count(e) AS c",
        {"repo": repo},
    )
    out["edges"] = {r[0]: r[1] for r in rows}
    out["n_edges"] = sum(out["edges"].values())
    return out


# ---------------------------------------------------------------------------
# time_slice
# ---------------------------------------------------------------------------


def time_slice(g: ReadOnlyGraph, repo: str, t: int) -> Dict[str, Any]:
    """State of the repo at time t: open modules + their open functions.

    Returns {"modules": [{"id", "name", "n_functions"}], "total_functions"}.
    Functions include methods (Module->Class->Function DEFINES chains).
    """
    cypher = f"""
CYPHER t=$t, repo=$repo
MATCH (r:Repository {{name: $repo}})<-[:IN_REPO]-(m:Module)
WHERE {_win("m")}
OPTIONAL MATCH (m)-[d:DEFINES]->(f)
WHERE {_win("d")} AND f:Function AND {_win("f")}
WITH m, count(DISTINCT f.id) AS direct
OPTIONAL MATCH (m)-[d1:DEFINES]->(c:Class)-[d2:DEFINES]->(mf:Function)
WHERE {_win("d1")} AND {_win("c")} AND {_win("d2")} AND {_win("mf")}
RETURN m.id AS id, m.name AS name, direct + count(DISTINCT mf.id) AS n_functions
ORDER BY m.name
"""
    rows = _rows(g, cypher, {"t": t, "repo": repo})
    modules = [{"id": r[0], "name": r[1], "n_functions": r[2]} for r in rows]
    return {"modules": modules,
            "total_functions": sum(m["n_functions"] for m in modules)}


# ---------------------------------------------------------------------------
# blast_radius
# ---------------------------------------------------------------------------


def _defined_functions(g: ReadOnlyGraph, repo: str, module_id: str,
                       t: int) -> List[Dict[str, str]]:
    """Functions a module defines at t (direct + via classes)."""
    wm, wd, wf = _win("m"), _win("d"), _win("f")
    wd1, wc, wd2 = _win("d1"), _win("c"), _win("d2")
    cypher = (
        "CYPHER t=$t, mid=$mid\n"
        "MATCH (m:Module {id: $mid})-[d:DEFINES]->(f:Function)\n"
        f"WHERE {wm} AND {wd} AND {wf}\n"
        "RETURN DISTINCT f.id AS id, f.qualname AS qualname\n"
        "UNION\n"
        "MATCH (m:Module {id: $mid})-[d1:DEFINES]->(c:Class)-[d2:DEFINES]->(f:Function)\n"
        f"WHERE {wm} AND {wd1} AND {wc} AND {wd2} AND {wf}\n"
        "RETURN DISTINCT f.id AS id, f.qualname AS qualname"
    )
    rows = _rows(g, cypher, {"t": t, "mid": module_id})
    seen: Dict[str, str] = {}
    for fid, qualname in rows:
        seen.setdefault(fid, qualname or "")
    return [{"id": fid, "qualname": q} for fid, q in sorted(seen.items())]


def _callers(g: ReadOnlyGraph, func_ids: List[str], t: int
             ) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """Reverse CALLS walk, genuinely 2-hop: (direct, indirect) callers.

    Two bounded patterns (1-hop, 2-hop) rather than a variable-length path
    so every node and edge on the walk carries the window predicate.
    """
    direct_q = f"""
CYPHER t=$t, fids=$fids
UNWIND $fids AS fid
MATCH (f:Function {{id: fid}})<-[e1:CALLS]-(c1:Function)
WHERE {_win("f")} AND {_win("e1")} AND {_win("c1")}
RETURN c1.id AS id, c1.qualname AS qualname, c1.module AS module, 1 AS hops
"""
    indirect_q = f"""
CYPHER t=$t, fids=$fids
UNWIND $fids AS fid
MATCH (f:Function {{id: fid}})<-[e1:CALLS]-(c1:Function)<-[e2:CALLS]-(c2:Function)
WHERE {_win("f")} AND {_win("e1")} AND {_win("c1")} AND {_win("e2")} AND {_win("c2")}
RETURN c2.id AS id, c2.qualname AS qualname, c2.module AS module, 2 AS hops
"""
    if not func_ids:
        return [], []
    best: Dict[str, Dict[str, str]] = {}
    for q in (direct_q, indirect_q):
        for cid, qualname, module, hops in _rows(g, q, {"t": t, "fids": func_ids}):
            prev = best.get(cid)
            if prev is None or hops < prev["hops"]:
                best[cid] = {"id": cid, "qualname": qualname or "",
                             "module": _short_module(module), "hops": hops}
    direct = sorted((v for v in best.values() if v["hops"] == 1),
                    key=lambda v: (v["module"], v["qualname"]))
    indirect = sorted((v for v in best.values() if v["hops"] == 2),
                      key=lambda v: (v["module"], v["qualname"]))
    for v in direct + indirect:
        v.pop("hops", None)
    return direct, indirect


def _importers(g: ReadOnlyGraph, repo: str, module_id: str,
               t: int) -> List[Dict[str, Any]]:
    """Modules with an open IMPORTS edge into the module at t.

    The planner targets IMPORTS edges at the real in-repo module when the
    dotted alias resolves to one in the snapshot ("a/b.py", then
    "a/b/__init__.py"), falling back to an ``$ext:`` stub node only for
    truly external imports — so this matches edges into the module id
    itself AND into its stub. The stub is identified by the module's
    dotted import name.
    """
    path = module_id.split(":", 2)[2]
    dotted = path[:-3].replace("/", ".") if path.endswith(".py") else path
    top = dotted.split(".")[0]
    cypher = f"""
CYPHER t=$t, mid=$mid, pfxext=$pfxext, dotted=$dotted, top=$top
MATCH (imp:Module)-[im:IMPORTS]->(m:Module)
WHERE (m.id = $mid
       OR (m.id STARTS WITH $pfxext AND (m.name = $dotted OR m.name = $top)))
  AND {_win("imp")} AND {_win("im")} AND {_win("m")}
  AND NOT imp.id CONTAINS '$ext:'
RETURN DISTINCT imp.id AS id, imp.name AS name, im.names AS names
ORDER BY imp.name
"""
    params = {"t": t, "mid": module_id, "pfxext": f"module:{repo}:$ext:",
              "dotted": dotted, "top": top}
    return [{"id": r[0], "name": r[1], "names": r[2] or []}
            for r in _rows(g, cypher, params)]


def blast_radius(g: ReadOnlyGraph, repo: str, target_id: str, t: int,
                 hops: int = 2) -> Dict[str, Any]:
    """What depends on the target at time t.

    Resolves a Module or Function target, then walks:
      - reverse CALLS up to 2 hops (windows on every hop),
      - IMPORTS edges into the owning module,
      - DEFINES edges out of the module (module targets only).

    Returns {"direct_callers", "indirect_callers", "importing_modules",
    "defined_functions", "totals"}.
    """
    label = _label_for(target_id)
    module_id = _module_id_for(repo, target_id)
    defined: List[Dict[str, str]] = []

    if label == "Module":
        defined = _defined_functions(g, repo, target_id, t)
        func_ids = [f["id"] for f in defined]
    else:
        func_ids = [target_id]
        if module_id is None:
            raise ValueError(f"cannot find owning module for {target_id!r}")

    direct, indirect = ([], [])
    if hops >= 1:
        direct, indirect = _callers(g, func_ids, t)
        if hops < 2:
            indirect = []

    importing = _importers(g, repo, module_id, t) if module_id else []

    return {
        "target_id": target_id,
        "target_label": label,
        "direct_callers": direct,
        "indirect_callers": indirect,
        "importing_modules": importing,
        "defined_functions": defined,
        "totals": {
            "direct_callers": len(direct),
            "indirect_callers": len(indirect),
            "importing_modules": len(importing),
            "defined_functions": len(defined),
            "live_dependents": len(direct) + len(indirect),
        },
    }


# ---------------------------------------------------------------------------
# churn
# ---------------------------------------------------------------------------


def _subtree_ids(g: ReadOnlyGraph, repo: str, target_id: str) -> List[str]:
    """target id + every function id it ever defined (any version, no time
    filter — churn is about the whole life, not one slice)."""
    label = _label_for(target_id)
    ids = [target_id]
    if label == "Module":
        cypher = (
            "CYPHER mid=$mid\n"
            "MATCH (m:Module {id: $mid})-[:DEFINES]->(f:Function)\n"
            "RETURN DISTINCT f.id AS id\n"
            "UNION\n"
            "MATCH (m:Module {id: $mid})-[:DEFINES]->(:Class)-[:DEFINES]->(f:Function)\n"
            "RETURN DISTINCT f.id AS id"
        )
        for (fid,) in _rows(g, cypher, {"mid": target_id}):
            if fid not in ids:
                ids.append(fid)
    return ids


def _modified_changes(g: ReadOnlyGraph, ids: List[str],
                      since: Optional[int]) -> List[Dict[str, Any]]:
    """MODIFIED events over ids, ordered by time. One UNION branch per label
    (FalkorDB has no label disjunctions); each branch rides the id index."""
    branches = []
    for label in ("Module", "Class", "Function"):
        since_pred = " AND mod.timestamp >= $since" if since is not None else ""
        branches.append(
            "UNWIND $ids AS nid\n"
            f"MATCH (p:Person)-[mod:MODIFIED]->(n:{label})\n"
            f"WHERE n.id = nid{since_pred}\n"
            "RETURN mod.timestamp AS ts, mod.commit AS commit, p.name AS author,\n"
            "       p.email AS email, mod.change AS change, nid AS target")
    params: Dict[str, Any] = {"ids": ids}
    if since is not None:
        params["since"] = since
    cypher = ("CYPHER ids=$ids" + (", since=$since" if since is not None else "")
              + "\n" + "\nUNION\n".join(branches))
    rows = _rows(g, cypher, params)
    changes = [{"ts": r[0], "commit": r[1], "author": r[2] or "",
                "email": r[3] or "", "change": r[4], "target": r[5]}
               for r in rows]
    changes.sort(key=lambda c: (c["ts"] or 0, c["commit"] or ""))
    return changes


def target_versions(g: ReadOnlyGraph, target_id: str) -> Dict[str, Any]:
    """Version windows for one node id: count, first/last seen.

    Used by the agent's proof chain step 1 ("locate the target").
    """
    label = _label_for(target_id)
    rows = _rows(
        g,
        f"CYPHER tid=$tid MATCH (n:{label} {{id: $tid}}) "
        "RETURN n.valid_from AS vf, n.valid_to AS vt ORDER BY vf",
        {"tid": target_id},
    )
    windows = [(r[0], r[1]) for r in rows]
    first = min((vf for vf, _ in windows), default=None)
    still_open = any(vt is None for _, vt in windows)
    closed = [vt for _, vt in windows if vt is not None]
    last_seen = max(closed) if closed else None  # None when still open
    return {
        "id": target_id,
        "label": label,
        "version_count": len(windows),
        "first_seen": first,
        "last_seen": last_seen,          # None when still open
        "still_open": still_open,
        "windows": windows,
    }


def churn(g: ReadOnlyGraph, repo: str, target_id: str,
          since_days: int = 90) -> Dict[str, Any]:
    """Change history for the target and every function it (ever) defined.

    Returns {"version_count", "first_seen", "last_seen", "changes":
    [{ts, commit, author, email, change, target}]} ordered by time.
    """
    import time as _time

    versions = target_versions(g, target_id)
    ids = _subtree_ids(g, repo, target_id)
    since = int(_time.time()) - since_days * 86400 if since_days else None
    changes = _modified_changes(g, ids, since)
    return {
        "target_id": target_id,
        "version_count": versions["version_count"],
        "first_seen": versions["first_seen"],
        "last_seen": versions["last_seen"],
        "still_open": versions["still_open"],
        "changes": changes,
        "n_changes": len(changes),
    }


# ---------------------------------------------------------------------------
# owners
# ---------------------------------------------------------------------------


def owners(g: ReadOnlyGraph, repo: str, target_id: str) -> List[Dict[str, Any]]:
    """Top authors by MODIFIED edge count over the target subtree."""
    ids = _subtree_ids(g, repo, target_id)
    branches = []
    for label in ("Module", "Class", "Function"):
        branches.append(
            "UNWIND $ids AS nid\n"
            f"MATCH (p:Person)-[mod:MODIFIED]->(n:{label})\n"
            "WHERE n.id = nid\n"
            "RETURN p.name AS name, p.email AS email, count(mod) AS touches")
    cypher = "CYPHER ids=$ids\n" + "\nUNION\n".join(branches)
    rows = _rows(g, cypher, {"ids": ids})
    agg: Dict[str, Dict[str, Any]] = {}
    for name, email, touches in rows:
        key = email or name or "?"
        entry = agg.setdefault(key, {"name": name or "", "email": email or "",
                                    "touches": 0})
        entry["touches"] += touches or 0
        if name and not entry["name"]:
            entry["name"] = name
    ranked = sorted(agg.values(), key=lambda e: e["touches"], reverse=True)
    return ranked[:10]


# ---------------------------------------------------------------------------
# timeline
# ---------------------------------------------------------------------------


def _repo_commits(g: ReadOnlyGraph, repo: str) -> List[Dict[str, Any]]:
    """Commits that touched this repo, via MODIFIED edges into its subtree."""
    cypher = """
CYPHER repo=$repo
MATCH (r:Repository {name: $repo})<-[:IN_REPO]-(n)
      <-[mod:MODIFIED]-(p:Person)-[:AUTHORED]->(c:Commit)
RETURN DISTINCT c.id AS commit, c.timestamp AS ts, p.name AS author, p.email AS email
ORDER BY ts
"""
    return [{"commit": r[0], "ts": r[1], "author": r[2] or "",
             "email": r[3] or ""} for r in _rows(g, cypher, {"repo": repo})]


def _label_windows(g: ReadOnlyGraph, repo: str,
                   label: str) -> List[Tuple[int, Optional[int]]]:
    """All (valid_from, valid_to) windows of a label inside the repo."""
    cypher = (f"CYPHER repo=$repo MATCH (r:Repository {{name: $repo}})"
              f"<-[:IN_REPO]-(n:{label}) "
              "RETURN n.valid_from AS vf, n.valid_to AS vt")
    return [(r[0], r[1]) for r in _rows(g, cypher, {"repo": repo})]


def timeline(g: ReadOnlyGraph, repo: str) -> List[Dict[str, Any]]:
    """One entry per commit: sha, author, timestamp, open module/function
    counts at that instant.

    Counts are batched: two window scans (Module, Function) aggregated in
    Python over the commit timestamps, instead of N per-commit queries.
    """
    from datetime import datetime, timezone

    commits = _repo_commits(g, repo)
    if not commits:
        return []
    mod_windows = _label_windows(g, repo, "Module")
    fun_windows = _label_windows(g, repo, "Function")

    def open_at(windows: List[Tuple[int, Optional[int]]], t: int) -> int:
        return sum(1 for vf, vt in windows
                   if vf is not None and vf <= t and (vt is None or vt > t))

    out = []
    for c in commits:
        ts = c["ts"]
        out.append({
            "commit": c["commit"],
            "short": (c["commit"] or "")[:8],
            "author": c["author"],
            "email": c["email"],
            "timestamp": {
                "epoch": ts,
                "iso": (datetime.fromtimestamp(ts, tz=timezone.utc)
                        .isoformat().replace("+00:00", "Z") if ts else None),
            },
            "n_modules_open": open_at(mod_windows, ts),
            "n_functions_open": open_at(fun_windows, ts),
        })
    return out


# ---------------------------------------------------------------------------
# resolve_target
# ---------------------------------------------------------------------------


def resolve_target(g: ReadOnlyGraph, repo: str,
                   text: str) -> Optional[Dict[str, str]]:
    """Fuzzy target lookup for the agent: exact id/path/qualname first,
    then suffix and substring matches. No regex (FalkorDB lacks the operator).

    Returns {"id", "label", "name"} for the best match, or None.
    Ranking: exact id > exact module path > exact qualname > path suffix >
    qualname suffix > function name > qualname substring. Ties prefer the
    open version, then the newest.
    """
    q = (text or "").strip().strip("'\"")
    if not q:
        return None
    pfx_m = f"module:{repo}:"
    pfx_f = f"func:{repo}:"
    pfx_c = f"class:{repo}:"

    def branch(pattern: str, where: str, rank: int, label: str) -> str:
        return (f"MATCH {pattern} WHERE {where} "
                f"RETURN {rank} AS r, n.id AS id, '{label}' AS label, "
                f"n.name AS name, n.valid_from AS vf, (n.valid_to IS NULL) AS open")

    branches = [
        # 0: exact node id (verbatim — covers ids pasted from anywhere)
        branch("(n:Module)", "n.id = $q", 0, "Module"),
        branch("(n:Function)", "n.id = $q", 0, "Function"),
        branch("(n:Class)", "n.id = $q", 0, "Class"),
        # 1: exact module path
        branch("(n:Module)", "n.name = $q AND n.id STARTS WITH $pfxm", 1, "Module"),
        # 2: exact function / class qualname
        branch("(n:Function)", "n.qualname = $q AND n.id STARTS WITH $pxf", 2, "Function"),
        branch("(n:Class)", "n.qualname = $q AND n.id STARTS WITH $pxc", 2, "Class"),
        # 3: module path suffix ("auth.py" -> "app/auth.py")
        branch("(n:Module)", "n.name ENDS WITH $q AND n.id STARTS WITH $pfxm", 3, "Module"),
        # 4: qualname suffix ("login" -> "Authenticator.login")
        branch("(n:Function)", "n.qualname ENDS WITH $q AND n.id STARTS WITH $pxf", 4, "Function"),
        # 5: bare function name
        branch("(n:Function)", "n.name = $q AND n.id STARTS WITH $pxf", 5, "Function"),
        # 6: qualname substring, last resort
        branch("(n:Function)", "n.qualname CONTAINS $q AND n.id STARTS WITH $pxf", 6, "Function"),
    ]
    cypher = ("CYPHER q=$q, pfxm=$pfxm, pxf=$pxf, pxc=$pxc\n"
              + "\nUNION\n".join(branches)
              + "\nORDER BY r, open DESC, vf DESC\nLIMIT 1")
    rows = _rows(g, cypher, {"q": q, "pfxm": pfx_m, "pxf": pfx_f, "pxc": pfx_c})
    if not rows:
        return None
    _, nid, label, name, _vf, _open = rows[0]
    return {"id": nid, "label": label, "name": name or ""}
