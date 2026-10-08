"""timetrace.sampler — git history -> IR snapshot series (foundation, IR v1).

Foundation-only build: walks a repo's git history, runs the parser on each
sampled commit, and writes one snapshot JSON per commit plus a chronological
manifest. NO graph-database code, no Cypher, no graph clients anywhere —
the graph layer is built live during event week (Oct 15-18).

The working tree is NEVER checked out: history is read with
``git log`` / ``git ls-tree`` / ``git show`` only, so sampling a repo never
churns its files.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone

__all__ = ["sample_repo"]

_FIELD_SEP = "\x1f"


def _git(repo_path: str, *args: str, binary: bool = False) -> str | bytes:
    """Run a read-only git command against ``repo_path``."""
    result = subprocess.run(
        ["git", "-C", repo_path, *args],
        capture_output=True,
        check=False,
        text=not binary,
    )
    if result.returncode != 0:
        err = result.stderr.decode(errors="replace") if binary else result.stderr
        raise RuntimeError(
            f"git {' '.join(args)} failed in {repo_path}: {err.strip()}"
        )
    return result.stdout


def _utc_iso(commit_iso: str) -> str:
    """Convert a ``%cI`` committer timestamp to ISO-8601 UTC."""
    dt = datetime.fromisoformat(commit_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _list_commits(repo_path: str) -> list[dict]:
    """All commits, oldest first, as dicts {sha, author, timestamp, week}."""
    raw = _git(
        repo_path,
        "log",
        f"--format=%H{_FIELD_SEP}%aN{_FIELD_SEP}%aE{_FIELD_SEP}%cI",
        "--reverse",
        "--date-order",
    )
    commits = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        sha, name, email, cdate = line.split(_FIELD_SEP, 3)
        dt = datetime.fromisoformat(cdate)
        year, week, _ = dt.isocalendar()
        commits.append(
            {
                "sha": sha,
                "author": f"{name} <{email}>",
                "timestamp": _utc_iso(cdate),
                "week": (year, week),
                "_dt": dt,  # sort key only; stripped before output
            }
        )
    commits.sort(key=lambda c: c["_dt"])  # belt-and-braces chronological order
    return commits


def _select_commits(commits: list[dict], mode: str, n: int) -> list[dict]:
    if mode == "weekly":
        buckets: dict[tuple, dict] = {}
        for commit in commits:  # chronological; overwrite keeps LAST per bucket
            buckets[commit["week"]] = commit
        return list(buckets.values())  # insertion order == chronological
    if mode == "every":
        if n < 1:
            raise ValueError(f"mode='every' requires n>=1, got {n}")
        return [c for i, c in enumerate(commits) if i % n == 0]
    raise ValueError(f"unknown sampling mode: {mode!r} (expected 'weekly' or 'every')")


def _read_commit_files(repo_path: str, sha: str) -> dict[str, str]:
    """{relpath: source} for every .py file at ``sha``, read into memory."""
    tree = _git(repo_path, "ls-tree", "-r", "--name-only", sha)
    files: dict[str, str] = {}
    for path in tree.splitlines():
        path = path.strip()
        if not path or not path.endswith(".py"):
            continue
        raw = _git(repo_path, "show", f"{sha}:{path}", binary=True)
        try:
            files[path] = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue  # skip files that are not valid UTF-8
    return files


def _parse(files: dict[str, str]) -> dict:
    """Run the parser workstream's parse_files (imported lazily)."""
    try:
        from timetrace.parser import parse_files
    except ImportError as exc:
        raise RuntimeError(
            "timetrace.parser.parse_files is not available — the parser "
            "workstream must land it before the sampler can run."
        ) from exc
    return parse_files(files)


def sample_repo(
    repo_path: str, mode: str = "weekly", n: int = 1, out_dir: str = "snapshots"
) -> dict:
    """Sample a repo's git history into IR snapshots.

    Args:
        repo_path: path to the git repository (read-only; never checked out).
        mode: "weekly" (last commit per ISO year-week) or "every"
            (every n-th commit).
        n: step for mode="every" (must be >= 1).
        out_dir: snapshots + manifest are written here (created if missing).

    Returns:
        {"manifest": [...], "count": int, "out_dir": str} where "manifest" is
        the chronological [{"commit","author","timestamp"}] list.
    """
    commits = _list_commits(repo_path)
    selected = _select_commits(commits, mode, n)

    os.makedirs(out_dir, exist_ok=True)
    repo_name = os.path.basename(os.path.abspath(repo_path))
    manifest: list[dict] = []

    for commit in selected:
        sha = commit["sha"]
        files = _read_commit_files(repo_path, sha)
        ir = _parse(files)
        snapshot = {
            "snapshot": {
                "commit": sha,
                "author": commit["author"],
                "timestamp": commit["timestamp"],
                "repo": repo_name,
            },
            "nodes": ir["nodes"],
            "edges": ir["edges"],
        }
        with open(os.path.join(out_dir, f"{sha}.json"), "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, ensure_ascii=False, indent=2)
        manifest.append(
            {
                "commit": sha,
                "author": commit["author"],
                "timestamp": commit["timestamp"],
            }
        )

    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    return {"manifest": manifest, "count": len(manifest), "out_dir": out_dir}
