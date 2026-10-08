"""Tests for timetrace.sampler (foundation; no graph code anywhere)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from timetrace.sampler import sample_repo

AUTHOR = ("Test Dev", "dev@example.com")

# Jan 5 2026 (Mon) + Jan 7 2026 (Wed) are the same ISO year-week (week 2);
# Jan 20 2026 (Tue) is a different week (week 4).
COMMITS = [
    ("2026-01-05T10:00:00+05:00", "app.py",
     b"def hello():\n    return 'v1'\n"),
    ("2026-01-07T15:30:00+05:00", "app.py",
     b"def hello():\n    return 'v2'\n\ndef bye():\n    return 'later'\n"),
    ("2026-01-20T09:00:00+05:00", "util.py",
     b"def helper(x):\n    return x * 2\n"),
]


def _git(repo: Path, *args: str, env: dict | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, env=env or os.environ,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _commit(repo: Path, when: str, filename: str, content: bytes, message: str) -> str:
    (repo / filename).write_bytes(content)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": AUTHOR[0],
        "GIT_AUTHOR_EMAIL": AUTHOR[1],
        "GIT_COMMITTER_NAME": AUTHOR[0],
        "GIT_COMMITTER_EMAIL": AUTHOR[1],
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_DATE": when,
    }
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo3(tmp_path: Path) -> tuple[Path, list[str]]:
    """Synthetic repo with 3 commits: two in one ISO week, one in another."""
    repo = tmp_path / "demo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", AUTHOR[0])
    _git(repo, "config", "user.email", AUTHOR[1])
    shas = [
        _commit(repo, when, fname, content, f"commit {i}")
        for i, (when, fname, content) in enumerate(COMMITS)
    ]
    return repo, shas


def _load_snapshot(out_dir: str, sha: str) -> dict:
    with open(os.path.join(out_dir, f"{sha}.json"), encoding="utf-8") as fh:
        return json.load(fh)


def test_every_mode_three_commits(tmp_path, repo3):
    repo, shas = repo3
    out = str(tmp_path / "snaps")
    result = sample_repo(str(repo), mode="every", n=1, out_dir=out)

    manifest = result["manifest"]
    assert len(manifest) == 3
    assert [m["commit"] for m in manifest] == shas  # chronological
    assert [m["timestamp"] for m in manifest] == sorted(m["timestamp"] for m in manifest)

    with open(os.path.join(out, "manifest.json"), encoding="utf-8") as fh:
        assert json.load(fh) == manifest  # on-disk manifest matches

    for entry in manifest:
        snap = _load_snapshot(out, entry["commit"])
        assert "nodes" in snap and "edges" in snap
        meta = snap["snapshot"]
        assert meta["commit"] == entry["commit"]
        assert meta["author"] == f"{AUTHOR[0]} <{AUTHOR[1]}>"
        assert meta["timestamp"] == entry["timestamp"]
        assert meta["repo"] == "demo"
        # real parser found the code
        assert any(n["kind"] == "function" for n in snap["nodes"])


def test_every_mode_step(tmp_path, repo3):
    repo, shas = repo3
    result = sample_repo(str(repo), mode="every", n=2, out_dir=str(tmp_path / "s2"))
    assert [m["commit"] for m in result["manifest"]] == [shas[0], shas[2]]


def test_weekly_keeps_last_commit_per_bucket(tmp_path, repo3):
    repo, shas = repo3
    out = str(tmp_path / "weekly")
    result = sample_repo(str(repo), mode="weekly", out_dir=out)

    manifest = result["manifest"]
    # Jan 5 and Jan 7 share an ISO week -> only the LAST (Jan 7) is kept
    assert [m["commit"] for m in manifest] == [shas[1], shas[2]]
    assert [m["timestamp"] for m in manifest] == sorted(m["timestamp"] for m in manifest)


def test_working_tree_untouched(tmp_path, repo3):
    repo, shas = repo3
    before = _git(repo, "status", "--porcelain")
    head_before = _git(repo, "rev-parse", "HEAD")
    sample_repo(str(repo), mode="every", n=1, out_dir=str(tmp_path / "s3"))
    assert _git(repo, "status", "--porcelain") == before
    assert _git(repo, "rev-parse", "HEAD") == head_before


def test_skips_non_utf8_files(tmp_path):
    repo = tmp_path / "bin"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", AUTHOR[0])
    _git(repo, "config", "user.email", AUTHOR[1])
    _commit(repo, "2026-02-01T10:00:00+05:00", "bad.py",
            b"\xff\xfe\x00bad-bytes-not-utf8", "binary file")
    _commit(repo, "2026-02-02T10:00:00+05:00", "good.py",
            b"def ok():\n    pass\n", "text file")

    out = str(tmp_path / "binout")
    result = sample_repo(str(repo), mode="every", n=1, out_dir=out)
    assert result["count"] == 2
    for entry in result["manifest"]:
        snap = _load_snapshot(out, entry["commit"])
        assert not any("bad.py" in n.get("id", "") for n in snap["nodes"])


def test_missing_parser_gives_clear_error(tmp_path, repo3, monkeypatch):
    repo, _ = repo3
    monkeypatch.delattr("timetrace.parser.parse_files", raising=False)
    with pytest.raises(RuntimeError, match="timetrace.parser.parse_files"):
        sample_repo(str(repo), mode="every", n=1, out_dir=str(tmp_path / "s4"))


def test_invalid_mode_and_n(tmp_path, repo3):
    repo, _ = repo3
    with pytest.raises(ValueError, match="unknown sampling mode"):
        sample_repo(str(repo), mode="monthly", out_dir=str(tmp_path / "s5"))
    with pytest.raises(ValueError, match="n>=1"):
        sample_repo(str(repo), mode="every", n=0, out_dir=str(tmp_path / "s6"))
