"""Unit tests for timetrace.parser (IR v1 contract)."""

import subprocess
import sys

from timetrace.parser import parse_files


def _nodes_by_id(ir):
    return {n["id"]: n for n in ir["nodes"]}


def test_nested_functions():
    ir = parse_files(
        {
            "pkg/mod.py": "def outer():\n"
            "    def inner():\n"
            "        return 1\n"
            "    return inner()\n"
        }
    )
    nodes = _nodes_by_id(ir)
    outer = nodes["func:pkg/mod.py:outer"]
    inner = nodes["func:pkg/mod.py:outer.inner"]
    assert outer["name"] == "outer"
    assert outer["qualname"] == "outer"
    assert inner["name"] == "inner"
    assert inner["qualname"] == "outer.inner"
    defines = {(e["from"], e["to"]) for e in ir["edges"] if e["kind"] == "defines"}
    assert ("module:pkg/mod.py", "func:pkg/mod.py:outer") in defines
    assert ("func:pkg/mod.py:outer", "func:pkg/mod.py:outer.inner") in defines


def test_async_def():
    ir = parse_files(
        {"a.py": "import asyncio\n\nasync def fetch(url):\n    return await asyncio.sleep(0)\n"}
    )
    fn = _nodes_by_id(ir)["func:a.py:fetch"]
    assert fn["async"] is True
    assert fn["args"] == ["url"]
    assert fn["decorators"] == []
    # sync function must be marked async=False
    ir2 = parse_files({"b.py": "def plain():\n    pass\n"})
    assert _nodes_by_id(ir2)["func:b.py:plain"]["async"] is False


def test_decorators():
    ir = parse_files(
        {
            "d.py": "@app.route('/x')\n"
            "@functools.lru_cache\n"
            "def view():\n"
            "    pass\n"
        }
    )
    fn = _nodes_by_id(ir)["func:d.py:view"]
    assert fn["decorators"] == ["app.route", "functools.lru_cache"]


def test_class_methods_and_bases():
    ir = parse_files(
        {
            "c.py": "class Server(Base, Mixin):\n"
            "    def start(self, *args, **kwargs):\n"
            "        pass\n"
            "    @staticmethod\n"
            "    async def stop(reason):\n"
            "        pass\n"
        }
    )
    nodes = _nodes_by_id(ir)
    cls = nodes["class:c.py:Server"]
    assert cls["kind"] == "class"
    assert cls["bases"] == ["Base", "Mixin"]
    start = nodes["func:c.py:Server.start"]
    assert start["args"] == ["self", "*args", "**kwargs"]
    assert start["async"] is False
    assert start["decorators"] == []
    stop = nodes["func:c.py:Server.stop"]
    assert stop["async"] is True
    assert stop["decorators"] == ["staticmethod"]
    defines = {(e["from"], e["to"]) for e in ir["edges"] if e["kind"] == "defines"}
    assert ("class:c.py:Server", "func:c.py:Server.start") in defines
    assert ("class:c.py:Server", "func:c.py:Server.stop") in defines


def test_imports_all_three_forms():
    ir = parse_files(
        {
            "main.py": "import os\n"
            "import sys as system\n"
            "from pathlib import Path, PurePath as PP\n"
            "from . import sibling\n"
        }
    )
    imports = [e for e in ir["edges"] if e["kind"] == "imports"]
    by_to = {e["to"]: e for e in imports}
    assert by_to["module:os"]["names"] == ["os"]
    assert by_to["module:sys"]["names"] == ["system"]
    assert by_to["module:pathlib"]["names"] == ["Path", "PP"]
    assert by_to["module:."]["names"] == ["sibling"]
    assert all(e["from"] == "module:main.py" for e in imports)


def test_calls_resolved_and_unresolved():
    ir = parse_files(
        {
            "m.py": "def helper():\n"
            "    return 1\n\n"
            "class Server:\n"
            "    def start(self):\n"
            "        helper()\n"
            "        self.stop()\n"
            "        nowhere.missing()\n\n"
            "    def stop(self):\n"
            "        pass\n"
        }
    )
    calls = [e for e in ir["edges"] if e["kind"] == "calls"]
    by_callee = {e["callee"]: e for e in calls}
    assert by_callee["helper"]["from"] == "func:m.py:Server.start"
    assert by_callee["helper"]["to"] == "func:m.py:helper"
    assert by_callee["self.stop"]["from"] == "func:m.py:Server.start"
    assert by_callee["self.stop"]["to"] == "func:m.py:Server.stop"
    assert by_callee["nowhere.missing"]["to"] is None
    assert by_callee["nowhere.missing"]["callee"] == "nowhere.missing"


def test_unparseable_file_skipped():
    ir = parse_files(
        {
            "good.py": "def ok():\n    pass\n",
            "bad.py": "def broken(:\n    pass\n(((  broken syntax",
        }
    )
    nodes = _nodes_by_id(ir)
    assert "func:good.py:ok" in nodes
    assert not any(n["id"].startswith("module:bad.py:") for n in ir["nodes"])
    assert not any(n.get("module") == "module:bad.py" for n in ir["nodes"])
    assert "module:bad.py" not in nodes


def test_module_node_shape():
    ir = parse_files({"x/y.py": "X = 1\n"})
    mod = _nodes_by_id(ir)["module:x/y.py"]
    assert mod == {"id": "module:x/y.py", "kind": "module", "name": "x/y.py"}


def test_cli_on_repo_tree(tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / "app.py").write_text("import os\n\ndef main():\n    os.getcwd()\n")
    out = tmp_path / "out.json"
    r = subprocess.run(
        [sys.executable, "-m", "timetrace.parser.cli", str(repo), "--out", str(out)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    import json

    snap = json.loads(out.read_text())
    assert snap["snapshot"]["repo"] == "demo"
    assert snap["snapshot"]["commit"] is None
    assert snap["snapshot"]["timestamp"]
    nodes = {n["id"] for n in snap["nodes"]}
    assert "module:app.py" in nodes
    assert "func:app.py:main" in nodes
    imports = [e for e in snap["edges"] if e["kind"] == "imports"]
    assert any(e["to"] == "module:os" for e in imports)
