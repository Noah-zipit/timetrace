"""End-to-end test against a live FalkorDB instance.

Marked for the local instance (default 127.0.0.1:6380, no auth — honoring
FALKORDB_* env overrides). SKIPS cleanly when the DB is unreachable; it
never fails spuriously.

What it does: builds a 3-commit fixture graph in a throwaway graph
("timetrace_e2e"), then runs every query layer function plus agent.answer
against real Cypher and asserts real values. Cleans the graph up after.
"""

import json

import pytest

from timetrace import agent as A
from timetrace.graph import queries as Q
from timetrace.graph.db import ReadOnlyGraph, get_config
from timetrace.ingest import FalkorWriter, epoch_seconds, plan_ingest

pytestmark = pytest.mark.e2e_local

E2E_GRAPH = "timetrace_e2e"
REPO = "e2e"
AUTHOR = "Test Author <test@example.com>"
C1 = "2026-01-05T00:00:00+00:00"
C2 = "2026-03-12T00:00:00+00:00"
C3 = "2026-06-03T00:00:00+00:00"
T1, T2, T3 = (epoch_seconds(x) for x in (C1, C2, C3))


def _mod(path):
    return {"id": f"module:{path}", "kind": "module", "name": path}


def _func(path, qualname, args=None):
    return {"id": f"func:{path}:{qualname}", "kind": "function",
            "name": qualname.split(".")[-1], "qualname": qualname,
            "module": f"module:{path}", "lineno": 1, "async": False,
            "decorators": [], "args": args or []}


def _defs(path, *qualnames):
    return [{"from": f"module:{path}", "to": f"func:{path}:{q}",
             "kind": "defines"} for q in qualnames]


def _snap(sha, ts, nodes, edges):
    return {"commit": sha, "author": AUTHOR, "timestamp": ts,
            "nodes": nodes, "edges": edges}


def _fixture_snapshots():
    a, b, c = _mod("a.py"), _mod("b.py"), _mod("c.py")
    f1, g1, h1 = (_func("a.py", "f"), _func("a.py", "g"), _func("b.py", "h"))
    s1 = _snap("c1" * 10, C1, [a, b, f1, g1, h1], _defs("a.py", "f", "g") + [
        {"from": "module:b.py", "to": "func:b.py:h", "kind": "defines"},
        {"from": "func:a.py:f", "to": "func:a.py:g",
         "kind": "calls", "callee": "g"},
        {"from": "func:b.py:h", "to": "func:a.py:f",
         "kind": "calls", "callee": "f"},
        {"from": "module:b.py", "to": "module:a",
         "kind": "imports", "names": ["a"]},
    ])
    f2 = _func("a.py", "f", args=["x"])  # body changed at C2
    k2 = _func("c.py", "k")
    s2 = _snap("c2" * 10, C2, [a, b, c, f2, g1, h1, k2],
               _defs("a.py", "f", "g") + _defs("c.py", "k") + [
                   {"from": "module:b.py", "to": "func:b.py:h",
                    "kind": "defines"},
                   {"from": "func:a.py:f", "to": "func:a.py:g",
                    "kind": "calls", "callee": "g"},
                   {"from": "func:b.py:h", "to": "func:a.py:f",
                    "kind": "calls", "callee": "f"},
                   {"from": "func:b.py:h", "to": "func:a.py:g",
                    "kind": "calls", "callee": "g"},
                   {"from": "func:c.py:k", "to": "func:b.py:h",
                    "kind": "calls", "callee": "h"},
                   {"from": "module:b.py", "to": "module:a",
                    "kind": "imports", "names": ["a"]},
               ])
    s3 = _snap("c3" * 10, C3, [a, b, c, f2, h1, k2],  # g deleted
               [e for e in s2["edges"]
                if e.get("to") != "func:a.py:g"])
    return [s1, s2, s3]


def _client_kwargs(cfg):
    kwargs = {"host": cfg.host, "port": cfg.port}
    if cfg.password:
        kwargs["username"] = cfg.username
        kwargs["password"] = cfg.password
    return kwargs


@pytest.fixture(scope="module")
def e2e_graph(tmp_path_factory):
    """Ingest the fixture into a throwaway graph; skip if DB is down."""
    from falkordb import FalkorDB

    cfg = get_config()
    try:
        client = FalkorDB(**_client_kwargs(cfg))
        # write-mode probe: creates the throwaway graph if missing
        # (ro_query on a nonexistent graph raises "empty key")
        client.select_graph(E2E_GRAPH).query("RETURN 1 AS ok")
    except Exception as exc:  # noqa: BLE001 - skip, don't fail
        pytest.skip(f"FalkorDB unreachable at {cfg.host}:{cfg.port}: {exc}")

    writer = FalkorWriter(cfg.host, cfg.port, cfg.username, cfg.password,
                          graph=E2E_GRAPH)
    writer.create_indexes()
    writer.write_ops(plan_ingest(REPO, _fixture_snapshots()))

    handle = ReadOnlyGraph(client.select_graph(E2E_GRAPH), cfg)
    yield handle
    client.select_graph(E2E_GRAPH).query("MATCH (n) DETACH DELETE n")


def test_time_slice_at_c2(e2e_graph):
    out = Q.time_slice(e2e_graph, REPO, T2)
    by_name = {m["name"]: m["n_functions"] for m in out["modules"]}
    assert by_name == {"a.py": 2, "b.py": 1, "c.py": 1}
    assert out["total_functions"] == 4


def test_time_slice_after_delete(e2e_graph):
    out = Q.time_slice(e2e_graph, REPO, T3 + 1)
    by_name = {m["name"]: m["n_functions"] for m in out["modules"]}
    assert by_name["a.py"] == 1  # g is gone


def test_blast_radius_two_hops(e2e_graph):
    import time
    out = Q.blast_radius(e2e_graph, REPO, f"func:{REPO}:a.py:f",
                         int(time.time()))
    assert [c["id"] for c in out["direct_callers"]] == [f"func:{REPO}:b.py:h"]
    assert [c["id"] for c in out["indirect_callers"]] == [f"func:{REPO}:c.py:k"]
    assert [m["name"] for m in out["importing_modules"]] == ["b.py"]
    assert out["totals"]["live_dependents"] == 2


def test_churn_and_owners(e2e_graph):
    ch = Q.churn(e2e_graph, REPO, f"module:{REPO}:a.py", since_days=0)
    assert ch["version_count"] == 1
    assert ch["first_seen"] == T1
    assert [c["change"] for c in ch["changes"]] == [
        "created", "created", "created", "modified", "deleted"]
    own = Q.owners(e2e_graph, REPO, f"module:{REPO}:a.py")
    assert own[0]["email"] == "test@example.com"
    assert own[0]["touches"] == 5


def test_timeline_counts(e2e_graph):
    tl = Q.timeline(e2e_graph, REPO)
    assert [e["short"] for e in tl] == ["c1c1c1c1", "c2c2c2c2", "c3c3c3c3"]
    assert [(e["n_modules_open"], e["n_functions_open"]) for e in tl] == [
        (2, 3), (3, 4), (3, 3)]
    assert tl[0]["timestamp"]["epoch"] == T1
    assert tl[0]["author"] == "Test Author"


def test_resolve_target(e2e_graph):
    hit = Q.resolve_target(e2e_graph, REPO, "a.py")
    assert hit["id"] == f"module:{REPO}:a.py"
    hit = Q.resolve_target(e2e_graph, REPO, "k")
    assert hit["id"] == f"func:{REPO}:c.py:k"


def test_agent_end_to_end(e2e_graph, monkeypatch):
    monkeypatch.setenv("FALKORDB_GRAPH", E2E_GRAPH)
    out = A.answer('is it safe to change "a.py"?', repo=REPO)
    assert out["meta"]["target_id"] == f"module:{REPO}:a.py"
    # 2*2 live deps + 3*1 importer + 0 recent + 2*1 author = 9 -> care
    assert out["verdict"] == "CHANGE WITH CARE"
    assert len(out["proof_chain"]) == 4
    assert "b.py:h" in out["summary"] or "h" in out["summary"]
    assert set(out["meta"]["queries_run"]) >= {
        "resolve_target", "blast_radius", "churn", "owners"}
    # proof chain cites real commits in the required source format
    assert any(s["source"].startswith("commit ") and "test@example.com" in s["source"]
               for s in out["proof_chain"])


def test_api_ingest_endpoint_smoke(e2e_graph, tmp_path, monkeypatch):
    """POST /api/ingest plans real ops (writes go to a scratch graph)."""
    from fastapi.testclient import TestClient
    from falkordb import FalkorDB

    snaps = _fixture_snapshots()
    d = tmp_path / "snaps"
    d.mkdir()
    (d / "manifest.json").write_text(
        json.dumps([{"commit": s["commit"]} for s in snaps]))
    for s in snaps:
        (d / f"{s['commit']}.json").write_text(json.dumps(s))

    monkeypatch.setenv("FALKORDB_GRAPH", "timetrace_e2e_ingest")
    cfg = get_config()
    try:
        from timetrace.api.main import app
        client = TestClient(app)
        r = client.post("/api/ingest",
                        json={"snapshots_dir": str(d), "repo": "e2e2"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["snapshots"] == 3
        assert body["written"]["node_open:Function"] == 5
    finally:
        FalkorDB(**_client_kwargs(cfg)).select_graph(
            "timetrace_e2e_ingest").query("MATCH (n) DETACH DELETE n")
