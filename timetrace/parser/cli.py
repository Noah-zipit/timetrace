"""CLI: snapshot a repo's current Python files into IR JSON.

Usage:
    cd ~/workspace/graph-hacks/timetrace
    python -m timetrace.parser.cli <repo_path> [--out out.json]

Walks <repo_path> for *.py files, parses them, and wraps the IR with a
snapshot header: {"snapshot": {"commit": null, "author": null,
"timestamp": <now UTC ISO>, "repo": <basename>}, "nodes": [...], "edges": [...]}.
Prints the JSON to stdout unless --out is given.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

from .parser import parse_files


def _collect_py_files(repo: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(repo.rglob("*.py")):
        if any(part.startswith(".") for part in path.parts):
            continue  # skip hidden dirs (.git, .venv, ...)
        rel = path.relative_to(repo).as_posix()
        try:
            files[rel] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="timetrace parser: repo -> IR JSON")
    ap.add_argument("repo_path", help="path to the repository to scan")
    ap.add_argument("--out", default=None, help="write JSON to this file (default: stdout)")
    args = ap.parse_args(argv)

    repo = Path(args.repo_path)
    if not repo.is_dir():
        print(f"error: not a directory: {repo}", file=sys.stderr)
        return 2

    ir = parse_files(_collect_py_files(repo))
    snapshot = {
        "snapshot": {
            "commit": None,
            "author": None,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "repo": repo.name,
        },
        "nodes": ir["nodes"],
        "edges": ir["edges"],
    }
    text = json.dumps(snapshot, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
