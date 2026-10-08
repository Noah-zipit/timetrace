"""Python-source parser for timetrace.

Pure stdlib (`ast`) only. No graph-database code, no Cypher, no graph
clients — that layer is built live during event week (Oct 15-18).

Contract (IR v1):
    parse_files(files: dict[str, str]) -> {"nodes": [...], "edges": [...]}

Node kinds: module / class / function.
Edge kinds: defines / calls / imports.
"""

from __future__ import annotations

import ast

__all__ = ["parse_files"]


def _dotted(node: ast.AST | None) -> str:
    """Best-effort dotted name for an ast expression (callee, decorator, base)."""
    if node is None:
        return ""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    if isinstance(node, ast.Subscript):
        return _dotted(node.value)
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    if isinstance(node, ast.Starred):
        return _dotted(node.value)
    # Constant / literal / lambda / other expressions -> ""
    return ""


def _arg_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    args = fn.args
    names: list[str] = []
    for a in list(args.posonlyargs) + list(args.args):
        names.append(a.arg)
    if args.vararg:
        names.append("*" + args.vararg.arg)
    for a in args.kwonlyargs:
        names.append(a.arg)
    if args.kwarg:
        names.append("**" + args.kwarg.arg)
    return names


class _DefsCollector(ast.NodeVisitor):
    """First pass: record every function/class definition's qualname and id.

    Runs before any call is resolved, so forward references (e.g. calling
    a method defined later in the class) still resolve.
    """

    def __init__(self, path: str):
        self.path = path
        self.scope: list[str] = []
        self.defined: dict[str, str] = {}  # short name -> node id
        self.qualname_index: dict[str, str] = {}  # full qualname -> node id

    def _qualname(self, name: str) -> str:
        if self.scope:
            return ".".join(self.scope) + "." + name
        return name

    def _record(self, name: str, node_id: str) -> None:
        qualname = self._qualname(name)
        self.qualname_index[qualname] = node_id
        if name not in self.defined:
            self.defined[name] = node_id

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._record(node.name, f"class:{self.path}:{self._qualname(node.name)}")
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record(node.name, f"func:{self.path}:{self._qualname(node.name)}")
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)


class _ModuleVisitor(ast.NodeVisitor):
    """Collects structure, calls, and imports for a single module."""

    def __init__(self, path: str, defs: _DefsCollector):
        self.path = path
        self.module_id = f"module:{path}"
        self.defined = defs.defined
        self.qualname_index = defs.qualname_index
        self.nodes: list[dict] = []
        self.edges: list[dict] = []
        self.scope: list[tuple[str, str]] = []  # ("class"|"func", qualname)
        self.func_stack: list[str] = []  # enclosing func node ids (for calls edges)

    # -- helpers ---------------------------------------------------------
    def _qualname(self, name: str) -> str:
        if self.scope:
            return ".".join(part[1] for part in self.scope) + "." + name
        return name

    def _owner_id(self) -> str:
        """Node that 'defines' the next thing: innermost class/func, else module."""
        if self.scope:
            kind, qualname = self.scope[-1]
            prefix = "class" if kind == "class" else "func"
            return f"{prefix}:{self.path}:{qualname}"
        return self.module_id

    def _emit_defines(self, child_id: str) -> None:
        self.edges.append(
            {"from": self._owner_id(), "to": child_id, "kind": "defines"}
        )

    def _record_symbol(self, name: str, node_id: str, qualname: str) -> None:
        # Kept for API stability; the symbol table now comes from the
        # _DefsCollector pre-pass passed to __init__.
        self.qualname_index[qualname] = node_id
        if name not in self.defined:
            self.defined[name] = node_id

    # -- definitions -----------------------------------------------------
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualname = self._qualname(node.name)
        node_id = f"class:{self.path}:{qualname}"
        bases = [
            b for b in (_dotted(base) for base in node.bases) if b
        ]
        self.nodes.append(
            {
                "id": node_id,
                "kind": "class",
                "name": node.name,
                "module": self.module_id,
                "lineno": node.lineno,
                "bases": bases,
            }
        )
        self._emit_defines(node_id)
        self.scope.append(("class", node.name))
        self.generic_visit(node)
        self.scope.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qualname = self._qualname(node.name)
        node_id = f"func:{self.path}:{qualname}"
        decorators = [
            d for d in (_dotted(dec) for dec in node.decorator_list) if d
        ]
        self.nodes.append(
            {
                "id": node_id,
                "kind": "function",
                "name": node.name,
                "qualname": qualname,
                "module": self.module_id,
                "lineno": node.lineno,
                "async": isinstance(node, ast.AsyncFunctionDef),
                "decorators": decorators,
                "args": _arg_names(node),
            }
        )
        self._emit_defines(node_id)
        self.scope.append(("func", node.name))
        self.func_stack.append(node_id)
        self.generic_visit(node)
        self.func_stack.pop()
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    # -- calls -----------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        callee = _dotted(node.func)
        if callee and self.func_stack:
            resolved = self._resolve_callee(callee)
            self.edges.append(
                {
                    "from": self.func_stack[-1],
                    "to": resolved,
                    "kind": "calls",
                    "callee": callee,
                }
            )
        self.generic_visit(node)

    def _resolve_callee(self, callee: str) -> str | None:
        """Best-effort resolution of a raw callee name to a known func id."""
        # Direct: full dotted path matches a defined qualname.
        if callee in self.qualname_index:
            return self.qualname_index[callee]
        # Method via self/attribute chain: try matching the tail onto any
        # qualname that ends with that tail (e.g. "self.start" -> "Server.start").
        parts = callee.split(".")
        for depth in range(1, len(parts) + 1):
            tail = ".".join(parts[-depth:])
            for qualname, node_id in self.qualname_index.items():
                if qualname == tail or qualname.endswith("." + tail):
                    return node_id
        # Last resort: bare name match.
        if parts[-1] in self.defined:
            return self.defined[parts[-1]]
        return None

    # -- imports ---------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        names = []
        for alias in node.names:
            names.append(alias.asname or alias.name)
        for alias in node.names:
            self.edges.append(
                {
                    "from": self.module_id,
                    "to": f"module:{alias.name}",
                    "kind": "imports",
                    "names": names,
                }
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        # Keep relative-import markers so the edge target stays informative.
        if node.level:
            module = "." * node.level + module
        names = [alias.asname or alias.name for alias in node.names]
        self.edges.append(
            {
                "from": self.module_id,
                "to": f"module:{module}",
                "kind": "imports",
                "names": names,
            }
        )

    def finalize(self) -> None:
        self.nodes.insert(
            0, {"id": self.module_id, "kind": "module", "name": self.path}
        )


def _parse_one(path: str, source: str) -> tuple[list[dict], list[dict]]:
    """Parse one file; return ([], []) if it fails to parse. Never raises."""
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return [], []
    defs = _DefsCollector(path)
    defs.visit(tree)
    visitor = _ModuleVisitor(path, defs)
    visitor.visit(tree)
    visitor.finalize()
    return visitor.nodes, visitor.edges


def parse_files(files: dict[str, str]) -> dict[str, list[dict]]:
    """Parse every file in `files` into IR nodes/edges.

    Files that fail to parse are skipped silently (record nothing, no crash).
    """
    nodes: list[dict] = []
    edges: list[dict] = []
    for path, source in files.items():
        file_nodes, file_edges = _parse_one(path, source)
        nodes.extend(file_nodes)
        edges.extend(file_edges)
    return {"nodes": nodes, "edges": edges}
