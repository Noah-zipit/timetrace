"""timetrace.ingest — git snapshots -> temporal graph (event-week build).

Implements docs/data-model.md sections 5 (snapshot -> interval conversion)
and 6 (rename detection), emitting Cypher per docs/TEMPORAL_CHEAT_SHEET.md.

Conventions:
- valid_from / valid_to are UNIX epoch seconds (int); valid_to=None = open.
- Node ids are repo-prefixed per data-model.md section 8, e.g.
  "func:requests:requests/api.py:request".
- MERGE is always on (id, valid_from) — never on id alone — so history is
  append-only. Closing a window is a property stamp, never a delete.
- The planner (plan_ingest) is pure and DB-free: it turns snapshots into an
  op list. The writer (FalkorWriter) executes ops in UNWIND batches.

Two deliberate, documented extensions of data-model.md:
- Commit nodes (:Commit, id=sha) exist so AUTHORED edges have a target and
  proof chains can cite exact commits. They carry valid_from=valid_to=ts
  (event semantics, like the doc's AUTHORED/MODIFIED edges).
- Every version node gets an IN_REPO edge to the :Repository node (doc
  section 8 requires "a link up to its Repository node").
- IMPORTS edges target real in-repo modules when the dotted alias resolves
  to one in the current snapshot: "a/b.py" is tried first, then
  "a/b/__init__.py". Only unresolvable aliases (stdlib, third-party)
  land on "module:<repo>:$ext:<alias>" stub nodes, which keeps the
  "who imports this module" evidence on the real Module node.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Pure planning layer (no DB imports here)
# ---------------------------------------------------------------------------

NODE_LABELS = {"module": "Module", "class": "Class", "func": "Function"}
RENAME_SIMILARITY_THRESHOLD = 0.8  # data-model.md section 6 default


def epoch_seconds(iso_ts: str) -> int:
    """ISO-8601 timestamp -> UNIX epoch seconds."""
    dt = datetime.fromisoformat(iso_ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp())


def split_author(author: str) -> tuple[str, str]:
    """'Name <email>' -> (name, email)."""
    author = author.strip()
    if "<" in author and author.endswith(">"):
        name, email = author.rsplit("<", 1)
        return name.strip(), email[:-1].strip()
    return author, author  # fallback: whole string as both


def _rest(parser_id: str) -> str:
    """Strip the 'kind:' prefix from a parser id ('module:path' -> 'path')."""
    return parser_id.split(":", 1)[1]


def rekey_node_id(parser_id: str, repo: str) -> tuple[str, str]:
    """Parser id -> (label, repo-prefixed id), e.g. 'func:p.py:f' -> the
    Function node id 'func:requests:p.py:f'."""
    kind, rest = parser_id.split(":", 1)
    label = NODE_LABELS[kind]
    return label, f"{kind}:{repo}:{rest}"


def qualname_of(node: dict) -> str:
    """Qualname for a node; class nodes lack the field, so derive it."""
    if "qualname" in node:
        return node["qualname"]
    # class id is 'class:<path>:<qualname>'
    return _rest(node["id"]).split(":", 1)[1] if ":" in _rest(node["id"]) else node["name"]


def body_hash(node: dict) -> str:
    """Normalized identity of a node's *content* — the 'changed' test in
    data-model.md section 5. Excludes lineno (line shifts are not changes);
    edges are diffed separately, so calls/imports are not part of this."""
    kind = node["kind"]
    if kind == "module":
        canon = ("module", node.get("name", ""))
    elif kind == "class":
        canon = (
            "class",
            node.get("name", ""),
            qualname_of(node),
            tuple(sorted(node.get("bases", []))),
        )
    else:  # function
        canon = (
            "function",
            node.get("name", ""),
            qualname_of(node),
            tuple(node.get("args", [])),
            bool(node.get("async", False)),
            tuple(sorted(node.get("decorators", []))),
        )
    return hashlib.sha256(json.dumps(canon, sort_keys=True).encode()).hexdigest()


def _features(node: dict) -> set[str]:
    """Feature set for rename similarity (data-model.md section 6)."""
    feats: set[str] = {f"name={node.get('name', '')}"}
    for a in node.get("args", []):
        feats.add(f"arg={a}")
    for d in node.get("decorators", []):
        feats.add(f"dec={d}")
    for b in node.get("bases", []):
        feats.add(f"base={b}")
    if node.get("async"):
        feats.add("async=1")
    return feats


def similarity(a: dict, b: dict) -> float:
    """Jaccard similarity over node features; 0 when both are featureless."""
    fa, fb = _features(a), _features(b)
    if not fa and not fb:
        return 0.0
    return len(fa & fb) / len(fa | fb)


def _edge_key(kind: str, frm: str, to: str, callee: str = "") -> tuple:
    if kind == "calls":
        return ("calls", frm, callee or "")
    return (kind, frm, to or "")


def plan_ingest(repo: str, snapshots: list[dict]) -> list[dict]:
    """Turn chronological snapshots into a DB-ready op list.

    Each snapshot: {"commit", "author", "timestamp" (ISO), "nodes", "edges"}
    where nodes/edges are the parser IR dicts.

    Ops (all DB-agnostic dicts):
      repo_open      {"repo"}
      node_open      {"label","id","valid_from","props"}
      node_close     {"label","id","valid_from","ts"}
      edge_open      {"kind","from_id","from_vf","to_id","to_vf",
                      "valid_from","props"}   (props carry uid)
      edge_close     {"uid","ts"}
    """
    ops: list[dict] = [{"op": "repo_open", "repo": repo}]
    open_nodes: dict[str, dict] = {}  # id -> {valid_from, hash, label}
    open_edges: dict[tuple, dict] = {}  # edge_key -> {uid, valid_from, ...}

    for snap in snapshots:
        meta = snap.get("snapshot", snap)  # sampler nests meta under "snapshot"
        ts = epoch_seconds(meta["timestamp"])
        sha = meta["commit"]
        name, email = split_author(meta["author"])

        # Person + Commit + AUTHORED (event edges, valid_from == valid_to)
        ops.append(
            {"op": "person_open", "email": email, "name": name, "ts": ts}
        )
        ops.append(
            {
                "op": "node_open",
                "label": "Commit",
                "id": sha,
                "valid_from": ts,
                "props": {
                    "timestamp": ts,
                    "author": meta["author"],
                    "valid_to": ts,  # event node: closed at birth
                },
            }
        )
        ops.append(
            {
                "op": "edge_open",
                "kind": "AUTHORED",
                "from_id": email,
                "from_vf": None,  # person matched by email, not version
                "to_id": sha,
                "to_vf": ts,
                "valid_from": ts,
                "props": {"uid": f"AUTHORED:{email}>{sha}",
                          "commit": sha, "timestamp": ts,
                          "valid_to": ts},
            }
        )

        # --- re-key nodes ------------------------------------------------
        cur: dict[str, dict] = {}
        for n in snap["nodes"]:
            label, nid = rekey_node_id(n["id"], repo)
            cur[nid] = {**n, "_nid": nid, "_label": label}
        def _resolve_import(alias: str) -> str | None:
            """Dotted import alias -> real in-repo module id, or None.

            Resolution order: "<a/b>.py", then "<a/b>/__init__.py",
            against this snapshot's module node ids.
            """
            stem = alias.replace(".", "/")
            for candidate in (stem + ".py", stem + "/__init__.py"):
                mid = f"module:{repo}:{candidate}"
                if mid in cur:
                    return mid
            return None

        ext_ids: dict[str, dict] = {}
        for e in snap["edges"]:
            if e["kind"] == "imports":
                raw_to = e["to"]  # 'module:<alias.name>'
                alias = _rest(raw_to)
                if _resolve_import(alias) is not None:
                    continue  # edge targets the real module; no stub needed
                eid = f"module:{repo}:$ext:{alias}"
                if eid not in ext_ids and eid not in cur:
                    stub = {
                        "id": raw_to, "kind": "module", "name": alias,
                        "_nid": eid, "_label": "Module", "_external": True,
                    }
                    ext_ids[eid] = stub
                    cur[eid] = stub

        created: dict[str, dict] = {}
        deleted: dict[str, dict] = {}
        reopened: set[str] = set()  # closed + reopened in the same snapshot
        for nid, n in cur.items():
            o = open_nodes.get(nid)
            h = body_hash(n)
            if o is None:
                created[nid] = n
            elif o["hash"] != h:
                ops.append({"op": "node_close", "label": o["label"],
                            "id": nid, "valid_from": o["valid_from"], "ts": ts})
                created[nid] = n
                reopened.add(nid)
        for nid, o in list(open_nodes.items()):
            if nid not in cur:
                deleted[nid] = {"label": o["label"],
                                "valid_from": o["valid_from"],
                                "qualname": o["qualname"],
                                "fields": o["fields"]}

        # --- rename detection (data-model.md section 6) -----------------
        # renames: old_id -> (new_id, similarity)
        renames: dict[str, tuple[str, float]] = {}
        if deleted and created:
            taken: set[str] = set()
            for old_id, dold in list(deleted.items()):
                best: tuple[str, float] | None = None
                for new_id, n in created.items():
                    if new_id in taken or n.get("_external"):
                        continue
                    if dold["label"] != n["_label"]:
                        continue
                    if dold["qualname"] != qualname_of(n):
                        continue  # the doc's rule: same qualname
                    sim = similarity(dold["fields"], n)
                    if (sim >= RENAME_SIMILARITY_THRESHOLD
                            and (best is None or sim > best[1])):
                        best = (new_id, sim)
                if best is not None:
                    renames[old_id] = best
                    taken.add(best[0])
                    del deleted[old_id]
        # close the old versions of renames (they open as new versions below)
        renamed_new: dict[str, tuple[str, float]] = {}  # new_id -> (old, sim)
        for old_id, (new_id, sim) in renames.items():
            dold = open_nodes[old_id]
            ops.append({"op": "node_close", "label": dold["label"],
                        "id": old_id, "valid_from": dold["valid_from"],
                        "ts": ts})
            renamed_new[new_id] = (old_id, sim)

        # close deleted (non-renamed)
        for nid, dold in deleted.items():
            ops.append({"op": "node_close", "label": dold["label"],
                        "id": nid, "valid_from": dold["valid_from"], "ts": ts})
            ops.append(
                {"op": "edge_open", "kind": "MODIFIED",
                 "from_id": email, "from_vf": None,
                 "to_id": nid, "to_vf": dold["valid_from"],
                 "valid_from": ts,
                 "props": {"uid": f"MODIFIED:{email}>{nid}@{ts}",
                           "commit": sha, "timestamp": ts,
                           "change": "deleted", "valid_to": ts}})

        # open created (and record in registry)
        for nid, n in created.items():
            label = n["_label"]
            props = {
                "name": n.get("name", ""),
                "module": n.get("module", ""),
                "lineno": n.get("lineno"),
                "valid_to": None,
            }
            if label == "Function":
                props.update({
                    "qualname": qualname_of(n),
                    "args": n.get("args", []),
                    "async": bool(n.get("async", False)),
                    "decorators": n.get("decorators", []),
                })
            elif label == "Class":
                props.update({
                    "qualname": qualname_of(n),
                    "bases": n.get("bases", []),
                })
            if n.get("_external"):
                props["external"] = True
            ops.append({"op": "node_open", "label": label, "id": nid,
                        "valid_from": ts, "props": props})
            ops.append({"op": "edge_open", "kind": "IN_REPO",
                        "from_id": nid, "from_vf": ts,
                        "to_id": f"repo:{repo}", "to_vf": None,
                        "valid_from": ts,
                        "props": {"uid": f"IN_REPO:{nid}@{ts}"}})
            open_nodes[nid] = {
                "valid_from": ts, "hash": body_hash(n), "label": label,
                "qualname": ("" if n.get("_external") else qualname_of(n)),
                "fields": {"name": n.get("name", ""),
                           "args": n.get("args", []),
                           "decorators": n.get("decorators", []),
                           "bases": n.get("bases", []),
                           "async": bool(n.get("async", False))},
            }
            if nid in renamed_new:
                change = "renamed"
            elif nid in reopened:
                change = "modified"
            else:
                change = "created"
            ops.append(
                {"op": "edge_open", "kind": "MODIFIED",
                 "from_id": email, "from_vf": None,
                 "to_id": nid, "to_vf": ts,
                 "valid_from": ts,
                 "props": {"uid": f"MODIFIED:{email}>{nid}@{ts}",
                           "commit": sha, "timestamp": ts,
                           "change": change, "valid_to": ts}})
            if nid in renamed_new:
                old_id, sim = renamed_new[nid]
                old_vf = next(
                    (op["valid_from"] for op in reversed(ops)
                     if op["op"] == "node_close" and op["id"] == old_id),
                    None)
                ops.append(
                    {"op": "edge_open", "kind": "RENAMED_TO",
                     "from_id": old_id, "from_vf": old_vf,
                     "to_id": nid, "to_vf": ts,
                     "valid_from": ts,
                     "props": {"uid": f"RENAMED_TO:{old_id}>{nid}@{ts}",
                               "commit": sha, "timestamp": ts,
                               "similarity": round(sim, 3),
                               "valid_to": ts}})

        # --- diff edges --------------------------------------------------
        # Edges open as this snapshot starts; only these are candidates for
        # version re-pointing below (edges opened/closed by the diff itself
        # already pin the right versions).
        pre_open_keys = set(open_edges)
        cur_edges: dict[tuple, dict] = {}
        for e in snap["edges"]:
            kind = e["kind"]
            _, frm = rekey_node_id(e["from"], repo)
            if kind == "defines":
                _, to = rekey_node_id(e["to"], repo)
                key = _edge_key(kind, frm, to)
                cur_edges[key] = {"kind": "DEFINES", "from": frm,
                                  "to": to, "props": {}}
            elif kind == "calls":
                if not e.get("to"):
                    continue  # unresolved callee: no target node; skip
                _, to = rekey_node_id(e["to"], repo)
                key = _edge_key(kind, frm, to, e.get("callee", ""))
                cur_edges[key] = {"kind": "CALLS", "from": frm, "to": to,
                                  "props": {"callee": e.get("callee", "")}}
            elif kind == "imports":
                alias = _rest(e["to"])
                to = (_resolve_import(alias)
                      or f"module:{repo}:$ext:{alias}")
                key = _edge_key(kind, frm, to)
                cur_edges[key] = {"kind": "IMPORTS", "from": frm,
                                  "to": to,
                                  "props": {"names": e.get("names", [])}}

        for key, oe in list(open_edges.items()):
            if key not in cur_edges:
                ops.append({"op": "edge_close", "uid": oe["uid"], "ts": ts})
                del open_edges[key]
        for key, ce in cur_edges.items():
            if key not in open_edges:
                frm_v = open_nodes.get(ce["from"], {}).get("valid_from")
                to_v = open_nodes.get(ce["to"], {}).get("valid_from")
                uid = f"{ce['kind']}:{ce['from']}>{ce['to']}@{ts}"
                props = {"uid": uid, "valid_from": ts,
                         "valid_to": None, **ce["props"]}
                ops.append({"op": "edge_open", "kind": ce["kind"],
                            "from_id": ce["from"], "from_vf": frm_v,
                            "to_id": ce["to"], "to_vf": to_v,
                            "valid_from": ts, "props": props})
                open_edges[key] = {"uid": uid, "valid_from": ts,
                                   "kind": ce["kind"],
                                   "from": ce["from"], "to": ce["to"],
                                   "from_vf": frm_v, "to_vf": to_v,
                                   "props": ce["props"]}

        # --- re-point edges after node-version rollover ------------------
        # A modified node keeps its id but opens a new version
        # (valid_from=ts). Edges pinned to the old version would otherwise
        # outlive it, so close each incident open edge and reopen it on
        # the new version: same kind, same other endpoint, new @ts uid,
        # valid_from=ts, edge-specific props (callee, names) preserved.
        # Renames are excluded: they change ids, so the diff loop above
        # already closes/reopens the affected edges.
        if reopened:
            for key in list(pre_open_keys):
                oe = open_edges.get(key)
                if oe is None:
                    continue  # dropped by the edge diff above
                new_from_vf = ts if oe["from"] in reopened else oe["from_vf"]
                new_to_vf = ts if oe["to"] in reopened else oe["to_vf"]
                if (new_from_vf == oe["from_vf"]
                        and new_to_vf == oe["to_vf"]):
                    continue  # untouched by any rollover
                ops.append({"op": "edge_close", "uid": oe["uid"], "ts": ts})
                new_uid = f"{oe['kind']}:{oe['from']}>{oe['to']}@{ts}"
                props = {"uid": new_uid, "valid_from": ts, "valid_to": None,
                         **oe["props"]}
                ops.append({"op": "edge_open", "kind": oe["kind"],
                            "from_id": oe["from"], "from_vf": new_from_vf,
                            "to_id": oe["to"], "to_vf": new_to_vf,
                            "valid_from": ts, "props": props})
                open_edges[key] = {**oe, "uid": new_uid, "valid_from": ts,
                                   "from_vf": new_from_vf,
                                   "to_vf": new_to_vf}

    return ops


# ---------------------------------------------------------------------------
# Writer layer (needs falkordb; import lazily so the planner stays pure)
# ---------------------------------------------------------------------------

INDEXES = [
    ("Function", "valid_from"),
    ("Function", "id"),
    ("Module", "valid_from"),
    ("Module", "id"),
    ("Class", "valid_from"),
    ("Class", "id"),
    ("Person", "email"),
    ("Repository", "name"),
    ("Commit", "id"),
]

_NODE_OPEN = """\
CYPHER rows=$rows
UNWIND $rows AS row
MERGE (n:%(label)s {id: row.id, valid_from: row.valid_from})
SET n += row.props"""

_PERSON_OPEN = """\
CYPHER rows=$rows
UNWIND $rows AS row
MERGE (p:Person {email: row.email})
SET p.id = row.email, p.name = row.name,
    p.valid_from = coalesce(p.valid_from, row.ts),
    p.last_seen_at = row.ts, p.valid_to = NULL"""

_NODE_CLOSE = """\
CYPHER rows=$rows
UNWIND $rows AS row
MATCH (n:%(label)s {id: row.id, valid_from: row.valid_from})
WHERE n.valid_to IS NULL
SET n.valid_to = row.ts"""

_EDGE_OPEN = """\
CYPHER rows=$rows
UNWIND $rows AS row
MATCH (a {id: row.from_id}), (b {id: row.to_id})
WHERE (row.from_vf IS NULL OR a.valid_from = row.from_vf)
  AND (row.to_vf IS NULL OR b.valid_from = row.to_vf)
CREATE (a)-[e:%(kind)s]->(b)
SET e = row.props"""

_EDGE_CLOSE = """\
CYPHER rows=$rows
UNWIND $rows AS row
MATCH ()-[e]->() WHERE e.uid = row.uid AND e.valid_to IS NULL
SET e.valid_to = row.ts"""

_REPO_OPEN = """\
CYPHER repo=$repo
MERGE (r:Repository {name: $repo})
SET r.id = 'repo:' + $repo,
    r.valid_from = coalesce(r.valid_from, timestamp()),
    r.valid_to = NULL"""


class FalkorWriter:
    """Executes planner ops against a FalkorDB instance in UNWIND batches."""

    def __init__(self, host: str, port: int, username: str, password: str,
                 graph: str = "timetrace", batch: int = 800):
        from falkordb import FalkorDB

        self.db = FalkorDB(host=host, port=port, username=username,
                           password=password)
        self.g = self.db.select_graph(graph)
        self.batch = batch

    def create_indexes(self) -> None:
        for label, prop in INDEXES:
            try:
                self.g.query(
                    f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
            except Exception as exc:  # already exists -> fine
                if "already exists" not in str(exc).lower():
                    raise

    def _run(self, query: str, **params) -> None:
        self.g.query(query, params)

    def write_ops(self, ops: list[dict]) -> dict:
        counts: dict[str, int] = {}
        by_kind: dict[str, list[dict]] = {}
        for op in ops:
            by_kind.setdefault(op["op"], []).append(op)

        for op in by_kind.get("repo_open", []):
            self._run(_REPO_OPEN, repo=op["repo"])
            counts["repo_open"] = counts.get("repo_open", 0) + 1

        for op in by_kind.get("person_open", []):
            self._run(_PERSON_OPEN, rows=[{
                "email": op["email"], "name": op["name"], "ts": op["ts"]}])
            counts["person_open"] = counts.get("person_open", 0) + 1

        node_opens: dict[str, list[dict]] = {}
        for op in by_kind.get("node_open", []):
            node_opens.setdefault(op["label"], []).append({
                "id": op["id"], "valid_from": op["valid_from"],
                "props": {**op["props"], "id": op["id"],
                          "valid_from": op["valid_from"]}})
        for label, rows in node_opens.items():
            for i in range(0, len(rows), self.batch):
                self._run(_NODE_OPEN % {"label": label},
                          rows=rows[i:i + self.batch])
            counts[f"node_open:{label}"] = len(rows)

        node_closes: dict[str, list[dict]] = {}
        for op in by_kind.get("node_close", []):
            node_closes.setdefault(op["label"], []).append({
                "id": op["id"], "valid_from": op["valid_from"], "ts": op["ts"]})
        for label, rows in node_closes.items():
            for i in range(0, len(rows), self.batch):
                self._run(_NODE_CLOSE % {"label": label},
                          rows=rows[i:i + self.batch])
            counts[f"node_close:{label}"] = len(rows)

        edge_opens: dict[str, list[dict]] = {}
        for op in by_kind.get("edge_open", []):
            edge_opens.setdefault(op["kind"], []).append({
                "from_id": op["from_id"], "from_vf": op["from_vf"],
                "to_id": op["to_id"], "to_vf": op["to_vf"],
                "props": op["props"]})
        for kind, rows in edge_opens.items():
            for i in range(0, len(rows), self.batch):
                self._run(_EDGE_OPEN % {"kind": kind},
                          rows=rows[i:i + self.batch])
            counts[f"edge_open:{kind}"] = len(rows)

        edge_closes = [{"uid": op["uid"], "ts": op["ts"]}
                       for op in by_kind.get("edge_close", [])]
        for i in range(0, len(edge_closes), self.batch):
            self._run(_EDGE_CLOSE, rows=edge_closes[i:i + self.batch])
        counts["edge_close"] = len(edge_closes)

        return counts
