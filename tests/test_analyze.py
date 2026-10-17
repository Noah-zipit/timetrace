"""Pure tests for the analyze-any-repo flow (no network, no DB)."""




import pytest

from timetrace.analyze import github as G
from timetrace.analyze import jobs as J
from timetrace.ingest import plan_ingest
from timetrace.ingest.ingest import SnapshotPlanner


# ---------------------------------------------------------------------------
# github.parse_repo_url
# ---------------------------------------------------------------------------


def test_parse_repo_url_shapes():
    assert G.parse_repo_url("https://github.com/tqdm/tqdm") == ("tqdm", "tqdm")
    assert G.parse_repo_url("github.com/psf/requests.git") == ("psf", "requests")
    assert G.parse_repo_url("https://github.com/a/b/tree/main/x") == ("a", "b")
    assert G.parse_repo_url("  https://www.github.com/a-b/c_d  ") == ("a-b", "c_d")


def test_parse_repo_url_rejects():
    for bad in ["", "https://gitlab.com/a/b", "https://github.com/onlyone",
                "https://example.com/a/b", "not a url at all!!!"]:
        with pytest.raises(ValueError):
            G.parse_repo_url(bad)


# ---------------------------------------------------------------------------
# github.sample_commits
# ---------------------------------------------------------------------------


def _commits(n):
    return [{"sha": f"s{i:03d}"} for i in range(n)]


def test_sample_commits_keeps_first_and_last():
    out = G.sample_commits(_commits(100), 10)
    assert len(out) == 10
    assert out[0]["sha"] == "s000"
    assert out[-1]["sha"] == "s099"
    shas = [c["sha"] for c in out]
    assert shas == sorted(shas)  # chronological


def test_sample_commits_small_passthrough():
    cs = _commits(7)
    assert G.sample_commits(cs, 40) == cs


# ---------------------------------------------------------------------------
# jobs naming
# ---------------------------------------------------------------------------


def test_graph_name_for_sanitizes():
    assert J.graph_name_for("tqdm", "tqdm") == "gh_tqdm__tqdm"
    assert J.graph_name_for("My.Org", "cool.repo") == "gh_my_org__cool_repo"
    assert J.repo_name_for("tqdm", "tqdm") == "tqdm/tqdm"


# ---------------------------------------------------------------------------
# SnapshotPlanner: one-shot == stepped, and stepped == resumed-from-registry
# ---------------------------------------------------------------------------


def _ir_node(path, qualname, args=None):
    return {"id": f"func:{path}:{qualname}", "kind": "function",
            "name": qualname.split(".")[-1], "qualname": qualname,
            "module": f"module:{path}", "lineno": 1, "async": False,
            "decorators": [], "args": args or []}


def _ir_mod(path):
    return {"id": f"module:{path}", "kind": "module", "name": path}


def _snap(sha, ts, nodes, edges):
    return {"commit": sha, "author": "A U <a@u.io>", "timestamp": ts,
            "nodes": nodes, "edges": edges}


def _fixture_snaps():
    s1 = _snap("a" * 40, "2024-01-01T00:00:00+00:00",
               [_ir_mod("m.py"), _ir_node("m.py", "f")],
               [{"from": "module:m.py", "to": "func:m.py:f", "kind": "defines"}])
    s2 = _snap("b" * 40, "2024-02-01T00:00:00+00:00",
               [_ir_mod("m.py"), _ir_node("m.py", "f", args=["x"]),
                _ir_node("m.py", "g")],
               [{"from": "module:m.py", "to": "func:m.py:f", "kind": "defines"},
                {"from": "module:m.py", "to": "func:m.py:g", "kind": "defines"},
                {"from": "func:m.py:g", "to": "func:m.py:f",
                 "kind": "calls", "callee": "f"}])
    return [s1, s2]


def test_planner_one_shot_equals_plan_ingest():
    snaps = _fixture_snaps()
    p = SnapshotPlanner("r")
    assert (plan_ingest("r", snaps)
            == p.init_ops + p.add_snapshot(snaps[0])
            + p.add_snapshot(snaps[1]))


def test_planner_resume_with_passed_state():
    """Simulates the analyze API: step 2 resumes from the step-1
    registries (the API reconstructs these from the graph)."""
    snaps = _fixture_snaps()
    p1 = SnapshotPlanner("r")
    ops1 = p1.add_snapshot(snaps[0])
    p2 = SnapshotPlanner("r", open_nodes=p1.open_nodes,
                         open_edges=p1.open_edges, emit_repo_open=False)
    ops2 = p2.add_snapshot(snaps[1])
    assert p1.init_ops + ops1 + ops2 == plan_ingest("r", snaps)
