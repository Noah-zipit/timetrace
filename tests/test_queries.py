"""Unit tests for timetrace.graph.queries — no DB needed.

A scripted fake graph stands in for FalkorDB so the row -> dict mapping
and the Cypher shapes (window predicate on every read, no writes) are
tested without a live instance.
"""

import pytest

from timetrace.graph import queries as Q


class ScriptedGraph:
    """Fake read-only graph: ro_query consults a handler(query, params)."""

    def __init__(self, handler):
        self._handler = handler
        self.seen = []  # (query, params) for every call

    def ro_query(self, query, params=None):
        self.seen.append((query, params or {}))

        class _R:
            pass

        r = _R()
        r.result_set = self._handler(query, params or {})
        return r


def _blast_handler(query, params):
    if "e2:CALLS" in query:  # indirect branch (check first: contains e1 too)
        return [
            ["func:r:c.py:k", "k", "module:c.py", 2],
            ["func:r:b.py:h", "h", "module:b.py", 2],  # also direct: dedup
        ]
    if "<-[e1:CALLS]-(c1:Function)" in query:
        return [["func:r:b.py:h", "h", "module:b.py", 1]]
    if "IMPORTS]->(m:Module" in query:
        return [["module:r:b.py", "b.py", ["a"]]]
    if "DEFINES" in query:
        return [["func:r:a.py:f", "f"]]
    return []


def test_window_predicate_fragment():
    frag = Q._win("n")
    assert frag == ("n.valid_from <= $t "
                    "AND (n.valid_to IS NULL OR n.valid_to > $t)")


def test_label_for_and_module_id_for():
    assert Q._label_for("module:r:a.py") == "Module"
    assert Q._label_for("func:r:a.py:C.m") == "Function"
    assert Q._label_for("class:r:a.py:C") == "Class"
    with pytest.raises(ValueError):
        Q._label_for("bogus:x")
    assert Q._module_id_for("r", "func:r:a/b.py:C.m") == "module:r:a/b.py"
    assert Q._module_id_for("r", "module:r:a.py") == "module:r:a.py"
    assert Q._short_module("module:a/b.py") == "a/b.py"
    assert Q._short_module("") == ""


def test_blast_radius_mapping_and_dedup():
    g = ScriptedGraph(_blast_handler)
    out = Q.blast_radius(g, "r", "module:r:a.py", t=999)
    assert out["target_label"] == "Module"
    assert [c["id"] for c in out["direct_callers"]] == ["func:r:b.py:h"]
    assert [c["id"] for c in out["indirect_callers"]] == ["func:r:c.py:k"]
    assert out["direct_callers"][0]["module"] == "b.py"  # prefix stripped
    assert [m["id"] for m in out["importing_modules"]] == ["module:r:b.py"]
    assert out["totals"] == {"direct_callers": 1, "indirect_callers": 1,
                             "importing_modules": 1, "defined_functions": 1,
                             "live_dependents": 2}


def test_blast_radius_function_target_skips_defined():
    g = ScriptedGraph(_blast_handler)
    out = Q.blast_radius(g, "r", "func:r:a.py:f", t=999)
    assert out["target_label"] == "Function"
    assert out["defined_functions"] == []
    assert out["totals"]["direct_callers"] == 1


def test_every_blast_query_is_windowed_and_readonly():
    g = ScriptedGraph(_blast_handler)
    Q.blast_radius(g, "r", "module:r:a.py", t=999)
    assert g.seen, "expected queries to run"
    for query, params in g.seen:
        if "$t" in query:
            assert "valid_from <= $t" in query, query[:120]
        upper = query.upper()
        for verb in ("CREATE ", "MERGE ", "DELETE ", "DETACH "):
            assert verb not in upper, f"write verb in read query: {query[:120]}"


def test_time_slice_mapping():
    def h(query, params):
        return [["module:r:a.py", "a.py", 2], ["module:r:b.py", "b.py", 0]]

    g = ScriptedGraph(h)
    out = Q.time_slice(g, "r", t=123)
    assert out["modules"] == [
        {"id": "module:r:a.py", "name": "a.py", "n_functions": 2},
        {"id": "module:r:b.py", "name": "b.py", "n_functions": 0},
    ]
    assert out["total_functions"] == 2
    (query, params), = g.seen
    assert params == {"t": 123, "repo": "r"}
    assert "valid_from <= $t" in query


def test_resolve_target_best_and_none():
    def h(query, params):
        if params.get("q") == "nope.py":
            return []
        return [[1, "module:r:app/auth.py", "Module", "app/auth.py", 5, True]]

    g = ScriptedGraph(h)
    hit = Q.resolve_target(g, "r", '"app/auth.py"')
    assert hit == {"id": "module:r:app/auth.py", "label": "Module",
                   "name": "app/auth.py"}
    assert Q.resolve_target(g, "r", "nope.py") is None
    assert Q.resolve_target(g, "r", "   ") is None


def test_owners_aggregation():
    def h(query, params):  # three UNION branches, same person twice
        return [["Ashar", "a@x.com", 2], ["Ashar", "a@x.com", 3],
                ["Zed", "z@x.com", 1]]

    g = ScriptedGraph(h)
    out = Q.owners(g, "r", "func:r:a.py:f")
    assert out[0] == {"name": "Ashar", "email": "a@x.com", "touches": 5}
    assert out[1]["email"] == "z@x.com"


def test_churn_shapes():
    calls = {"n": 0}

    def h(query, params):
        calls["n"] += 1
        if "MODIFIED" in query:
            return [[100, "c1" * 10, "Ashar", "a@x.com", "created",
                     "func:r:a.py:f"]]
        if "valid_from AS vf" in query:
            return [[50, 150], [150, None]]
        return [["func:r:a.py:f"]]  # subtree ids

    g = ScriptedGraph(h)
    out = Q.churn(g, "r", "func:r:a.py:f", since_days=0)
    assert out["version_count"] == 2
    assert out["first_seen"] == 50
    assert out["still_open"] is True
    assert out["n_changes"] == 1
    assert out["changes"][0]["change"] == "created"


def test_db_config_defaults_and_secret_hygiene(monkeypatch):
    from timetrace.graph import db as D

    monkeypatch.delenv("FALKORDB_HOST", raising=False)
    monkeypatch.delenv("FALKORDB_PORT", raising=False)
    monkeypatch.delenv("FALKORDB_PASSWORD", raising=False)
    monkeypatch.delenv("FALKORDB_PASS", raising=False)
    cfg = D.get_config(dotenv_path="/nonexistent/.env")
    assert (cfg.host, cfg.port) == ("127.0.0.1", 6380)
    assert cfg.password is None
    assert "secret" not in repr(cfg).lower()

    monkeypatch.setenv("FALKORDB_HOST", "db.internal")
    monkeypatch.setenv("FALKORDB_PORT", "9999")
    monkeypatch.setenv("FALKORDB_PASSWORD", "s3cret")
    cfg = D.get_config(dotenv_path="/nonexistent/.env")
    assert (cfg.host, cfg.port, cfg.password) == ("db.internal", 9999,
                                                  "s3cret")
    assert "s3cret" not in repr(cfg)
    err = D.DatabaseUnavailableError(cfg)
    assert err.status_code == 503
    assert "s3cret" not in str(err)
    assert "db.internal:9999" in str(err)


def test_db_config_accepts_cloud_env_spellings(tmp_path, monkeypatch):
    from timetrace.graph import db as D

    env = tmp_path / ".env"
    env.write_text("FALKORDB_HOST=cloud.example\n"
                   "FALKORDB_PORT=64621\n"
                   "FALKORDB_USERNAME=falkordb\n"
                   "FALKORDB_PASS=topsecret\n")
    monkeypatch.delenv("FALKORDB_HOST", raising=False)
    monkeypatch.delenv("FALKORDB_LOCAL", raising=False)  # else .env is skipped
    cfg = D.get_config(dotenv_path=str(env))
    assert cfg.host == "cloud.example"
    assert cfg.port == 64621
    assert cfg.username == "falkordb"
    assert cfg.password == "topsecret"
