"""Tests for the ingest planner (pure; no FalkorDB needed)."""

from timetrace.ingest import body_hash, epoch_seconds, plan_ingest, similarity


def _mod(path="a.py"):
    return {"id": f"module:{path}", "kind": "module", "name": path}


def _func(path, qualname, args=None, decorators=None, async_=False):
    return {
        "id": f"func:{path}:{qualname}",
        "kind": "function",
        "name": qualname.split(".")[-1],
        "qualname": qualname,
        "module": f"module:{path}",
        "lineno": 10,
        "async": async_,
        "decorators": decorators or [],
        "args": args or [],
    }


def _snap(sha, ts, nodes, edges, author="Ashar <a@x.com>"):
    return {"commit": sha, "author": author, "timestamp": ts,
            "nodes": nodes, "edges": edges}


def _defs(path, qualname):
    f = _func(path, qualname)
    return [{"from": f"module:{path}", "to": f["id"], "kind": "defines"}]


T1, T2, T3 = "2026-01-05T00:00:00+00:00", "2026-03-12T00:00:00+00:00", \
    "2026-06-03T00:00:00+00:00"


def _windows(ops, nid):
    """(label, [(valid_from, valid_to-or-None)]) for one node id."""
    opens = [o for o in ops if o["op"] == "node_open" and o["id"] == nid]
    closes = {(o["id"], o["valid_from"]): o["ts"]
              for o in ops if o["op"] == "node_close"}
    out = []
    for o in opens:
        vf = o["valid_from"]
        vt = closes.get((o["id"], vf), o["props"].get("valid_to"))
        out.append((vf, vt))
    return sorted(out)


def test_epoch_and_hash_stable():
    assert epoch_seconds(T1) == 1767571200
    f = _func("a.py", "f", args=["x"])
    assert body_hash(f) == body_hash(_func("a.py", "f", args=["x"]))
    assert body_hash(f) != body_hash(_func("a.py", "f", args=["y"]))
    # lineno shifts are not changes
    g = _func("a.py", "f", args=["x"]); g["lineno"] = 999
    assert body_hash(f) == body_hash(g)


def test_create_then_modify_then_delete():
    repo = "demo"
    m = _mod()
    f1 = _func("a.py", "f", args=["x"])
    s1 = _snap("c1", T1, [m, f1], _defs("a.py", "f"))
    f2 = _func("a.py", "f", args=["x", "y"])  # changed
    s2 = _snap("c2", T2, [m, f2], _defs("a.py", "f"))
    s3 = _snap("c3", T3, [m], [])  # f gone
    ops = plan_ingest(repo, [s1, s2, s3])

    fid = "func:demo:a.py:f"
    wins = _windows(ops, fid)
    e1, e2 = epoch_seconds(T1), epoch_seconds(T2)
    assert wins == [(e1, e2), (e2, epoch_seconds(T3))], wins

    changes = [o["props"]["change"] for o in ops
               if o["op"] == "edge_open" and o["kind"] == "MODIFIED"
               and o["to_id"] == fid]
    assert changes == ["created", "modified", "deleted"], changes


def test_unchanged_costs_nothing():
    m, f = _mod(), _func("a.py", "f")
    ops = plan_ingest("demo", [
        _snap("c1", T1, [m, f], _defs("a.py", "f")),
        _snap("c2", T2, [m, f], _defs("a.py", "f")),
    ])
    fid = "func:demo:a.py:f"
    opens = [o for o in ops if o["op"] == "node_open" and o["id"] == fid]
    assert len(opens) == 1


def test_rename_links_versions():
    repo = "demo"
    m1, m2 = _mod("a.py"), _mod("b.py")
    f_old = _func("a.py", "Server.start", args=["self", "x"])
    f_new = _func("b.py", "Server.start", args=["self", "x"])
    ops = plan_ingest(repo, [
        _snap("c1", T1, [m1, f_old], _defs("a.py", "Server.start")),
        _snap("c2", T2, [m2, f_new], _defs("b.py", "Server.start")),
    ])
    ren = [o for o in ops if o["op"] == "edge_open"
           and o["kind"] == "RENAMED_TO"]
    assert len(ren) == 1
    assert ren[0]["from_id"] == "func:demo:a.py:Server.start"
    assert ren[0]["to_id"] == "func:demo:b.py:Server.start"
    assert ren[0]["props"]["similarity"] >= 0.8
    changes = sorted(o["props"]["change"] for o in ops
                     if o["op"] == "edge_open" and o["kind"] == "MODIFIED"
                     and o["to_id"].endswith("Server.start"))
    assert changes == ["created", "renamed"], changes


def test_rename_not_triggered_by_rewrite():
    m1, m2 = _mod("a.py"), _mod("b.py")
    f_old = _func("a.py", "f", args=["x"])
    f_new = _func("b.py", "f", args=["a", "b", "c", "d"],
                  decorators=["d1", "d2"])
    ops = plan_ingest("demo", [
        _snap("c1", T1, [m1, f_old], _defs("a.py", "f")),
        _snap("c2", T2, [m2, f_new], _defs("b.py", "f")),
    ])
    ren = [o for o in ops if o["op"] == "edge_open"
           and o["kind"] == "RENAMED_TO"]
    assert ren == []  # heavy rewrite -> honest delete + create


def test_no_overlapping_windows():
    m = _mod()
    snaps = []
    for i, ts in enumerate(["2026-01-0%dT00:00:00+00:00" % d
                            for d in range(1, 8)]):
        f = _func("a.py", "f", args=[f"a{i}"])
        snaps.append(_snap(f"c{i}", ts, [m, f], _defs("a.py", "f")))
    ops = plan_ingest("demo", snaps)
    wins = _windows(ops, "func:demo:a.py:f")
    assert len(wins) == 7
    for (vf1, vt1), (vf2, vt2) in zip(wins, wins[1:]):
        assert vt1 == vf2, "windows must be contiguous, no gaps/overlaps"
    assert wins[-1][1] is None  # latest still open


def test_calls_edges_follow_caller():
    repo = "demo"
    m = _mod()
    fa = _func("a.py", "main")
    fb = _func("a.py", "helper")
    e1 = _defs("a.py", "main") + _defs("a.py", "helper") + [
        {"from": fa["id"], "to": fb["id"], "kind": "calls",
         "callee": "helper"}]
    e2 = _defs("a.py", "main") + _defs("a.py", "helper")  # call removed
    ops = plan_ingest(repo, [
        _snap("c1", T1, [m, fa, fb], e1),
        _snap("c2", T2, [m, fa, fb], e2),
    ])
    opened = [o for o in ops if o["op"] == "edge_open"
              and o["kind"] == "CALLS"]
    closed = [o for o in ops if o["op"] == "edge_close"]
    assert len(opened) == 1 and len(closed) == 1
    assert closed[0]["uid"] == opened[0]["props"]["uid"]
    assert closed[0]["ts"] == epoch_seconds(T2)


def test_similarity_bounds():
    a = {"name": "f", "args": ["x"], "decorators": [], "bases": [],
         "async": False}
    assert similarity(a, a) == 1.0
    b = dict(a, args=["y"])
    assert 0.0 < similarity(a, b) < 1.0
