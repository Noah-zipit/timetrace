"""Tests for the ingest planner (pure; no FalkorDB needed)."""

from timetrace.ingest import FalkorWriter, body_hash, epoch_seconds, \
    plan_ingest, similarity
from timetrace.ingest.ingest import _EDGE_CLOSE, _EDGE_OPEN


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


def _import_edge(path, alias, names=None):
    return {"from": f"module:{path}", "to": f"module:{alias}",
            "kind": "imports", "names": names or []}


def test_imports_edge_resolves_to_real_module():
    repo = "demo"
    ops = plan_ingest(repo, [_snap(
        "c1", T1, [_mod("a.py"), _mod("rich/console.py")],
        [_import_edge("a.py", "rich.console", ["console"])])])
    imp = [o for o in ops if o["op"] == "edge_open"
           and o["kind"] == "IMPORTS"]
    assert len(imp) == 1
    assert imp[0]["from_id"] == "module:demo:a.py"
    assert imp[0]["to_id"] == "module:demo:rich/console.py"
    # uid/props conventions unchanged
    assert imp[0]["props"]["uid"] == (
        "IMPORTS:module:demo:a.py>module:demo:rich/console.py"
        f"@{epoch_seconds(T1)}")
    assert imp[0]["props"]["names"] == ["console"]
    assert imp[0]["to_vf"] == epoch_seconds(T1)
    # no $ext stub node opened for the resolved import
    stubs = [o for o in ops if o["op"] == "node_open"
             and "$ext:" in o["id"]]
    assert stubs == []


def test_imports_edge_resolves_package_init():
    ops = plan_ingest("demo", [_snap(
        "c1", T1, [_mod("a.py"), _mod("pkg/__init__.py")],
        [_import_edge("a.py", "pkg")])])
    imp = [o for o in ops if o["op"] == "edge_open"
           and o["kind"] == "IMPORTS"]
    assert len(imp) == 1
    assert imp[0]["to_id"] == "module:demo:pkg/__init__.py"


def test_imports_edge_external_falls_back_to_stub():
    ops = plan_ingest("demo", [_snap(
        "c1", T1, [_mod("a.py")],
        [_import_edge("a.py", "os", ["path"])])])
    imp = [o for o in ops if o["op"] == "edge_open"
           and o["kind"] == "IMPORTS"]
    assert len(imp) == 1
    assert imp[0]["to_id"] == "module:demo:$ext:os"
    stubs = [o for o in ops if o["op"] == "node_open"
             and o["id"] == "module:demo:$ext:os"]
    assert len(stubs) == 1
    assert stubs[0]["props"]["external"] is True


class _RecordingGraph:
    """Stand-in for FalkorDB's graph object; records (query, params)."""
    def __init__(self):
        self.calls = []

    def query(self, q, params=None):
        self.calls.append((q, params))
        return []


def _writer(batch):
    w = FalkorWriter.__new__(FalkorWriter)  # skip FalkorDB connect
    w.g = _RecordingGraph()
    w.batch = batch
    return w


def test_writer_batches_edge_ops_by_kind():
    batch = 5
    n_calls, n_imports, n_close = 12, 4, 12
    w = _writer(batch)
    ops = []
    for i in range(n_calls):
        ops.append({"op": "edge_open", "kind": "CALLS",
                    "from_id": "module:demo:a.py", "from_vf": 1,
                    "to_id": "module:demo:b.py", "to_vf": 1,
                    "valid_from": 1, "props": {"uid": f"c{i}"}})
    for i in range(n_imports):
        ops.append({"op": "edge_open", "kind": "IMPORTS",
                    "from_id": "module:demo:a.py", "from_vf": 1,
                    "to_id": "module:demo:$ext:os", "to_vf": 1,
                    "valid_from": 1, "props": {"uid": f"i{i}"}})
    for i in range(n_close):
        ops.append({"op": "edge_close", "uid": f"u{i}", "ts": 2})
    counts = w.write_ops(ops)

    def _calls_for(query):
        return [c for c in w.g.calls if c[0] == query]

    calls_q = _EDGE_OPEN % {"kind": "CALLS"}
    imports_q = _EDGE_OPEN % {"kind": "IMPORTS"}
    assert calls_q != imports_q  # each kind renders its own query
    oc = _calls_for(calls_q)
    assert 0 < len(oc) <= -(-n_calls // batch)  # <= ceil(12/5) = 3
    assert all(len(c[1]["rows"]) <= batch for c in oc)
    assert sum(len(c[1]["rows"]) for c in oc) == n_calls
    oi = _calls_for(imports_q)
    assert len(oi) <= -(-n_imports // batch)  # ceil(4/5) = 1
    assert sum(len(c[1]["rows"]) for c in oi) == n_imports
    cc = _calls_for(_EDGE_CLOSE)
    assert 0 < len(cc) <= -(-n_close // batch)
    assert sum(len(c[1]["rows"]) for c in cc) == n_close

    assert counts == {"edge_open:CALLS": n_calls,
                      "edge_open:IMPORTS": n_imports,
                      "edge_close": n_close}


def test_modified_node_repoints_incident_edges():
    repo = "demo"
    m = _mod()
    f1 = _func("a.py", "f", args=["x"])
    h1 = _func("a.py", "h")
    edges = (_defs("a.py", "f") + _defs("a.py", "h")
             + [{"from": h1["id"], "to": f1["id"], "kind": "calls",
                 "callee": "f"}])
    f2 = _func("a.py", "f", args=["x", "y"])  # body changed at T2
    ops = plan_ingest(repo, [
        _snap("c1", T1, [m, f1, h1], edges),
        _snap("c2", T2, [m, f2, h1], edges),
        _snap("c3", T3, [m, f2, h1], edges),
    ])
    e1, e2, e3 = epoch_seconds(T1), epoch_seconds(T2), epoch_seconds(T3)
    hid, fid = f"func:{repo}:a.py:h", f"func:{repo}:a.py:f"
    old_uid = f"CALLS:{hid}>{fid}@{e1}"
    new_uid = f"CALLS:{hid}>{fid}@{e2}"

    # the old CALLS edge is closed when f's version rolls ...
    closes = [o for o in ops if o["op"] == "edge_close"
              and o["uid"] == old_uid]
    assert len(closes) == 1 and closes[0]["ts"] == e2
    # ... and reopened pinned to f's new version, h's version untouched
    opens = [o for o in ops if o["op"] == "edge_open"
             and o["kind"] == "CALLS"]
    assert [o["props"]["uid"] for o in opens] == [old_uid, new_uid]
    repinned = opens[1]
    assert repinned["to_vf"] == e2
    assert repinned["from_vf"] == e1
    assert repinned["valid_from"] == e2
    assert repinned["props"]["callee"] == "f"  # extra props preserved
    assert repinned["props"]["valid_to"] is None

    # DEFINES follows the same rule uniformly
    defs = [o for o in ops if o["op"] == "edge_open"
            and o["kind"] == "DEFINES" and o["to_id"] == fid]
    assert [o["to_vf"] for o in defs] == [e1, e2]
    # IN_REPO was already per-version correct (fresh edge per version)
    inrepo = [o for o in ops if o["op"] == "edge_open"
              and o["kind"] == "IN_REPO" and o["from_id"] == fid]
    assert [o["from_vf"] for o in inrepo] == [e1, e2]

    # fold the op stream: exactly one open CALLS edge at T3
    windows = {}
    for o in ops:
        if o["op"] == "edge_open" and o["kind"] == "CALLS":
            windows[o["props"]["uid"]] = [o["valid_from"], None]
        elif o["op"] == "edge_close" and o["uid"] in windows:
            windows[o["uid"]][1] = o["ts"]
    live = [u for u, (vf, vt) in windows.items()
            if vf <= e3 and (vt is None or vt > e3)]
    assert live == [new_uid], windows
