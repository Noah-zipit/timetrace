"""timetrace.analyze.github — GitHub API access without a git binary.

Pure stdlib (urllib + tarfile). Requests respect proxy environment
variables, which is what lets this work from inside Vercel's function
egress (direct TLS from such sandboxes is typically intercepted).

Unauthenticated GitHub API calls are limited to 60/hour per IP — an
/analyze/start costs ~2 requests plus one per commit-listing page, so a
busy hour can hit the ceiling; the error surfaces as RateLimitError with
the reset time, not a silent failure.
"""

from __future__ import annotations

import io
import json
import re
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Tuple

UA = {"User-Agent": "timetrace-demo (https://github.com/Noah-zipit/timetrace)"}
API = "https://api.github.com"
CODELOAD = "https://codeload.github.com"

# Honest caps for the serverless analyze flow.
MAX_PAGES = 8            # commit listing: 8 x 100 = newest 800 commits max
SAMPLE_N = 40            # commits actually analyzed per repo
SIZE_CAP_KB = 60 * 1024  # repos larger than 60 MB are refused up front
TARBALL_CAP_BYTES = 40 * 1024 * 1024  # single-commit tarball ceiling
MIN_PY_FILES = 5         # fewer .py files -> "Python-only parser" refusal


class GitHubError(RuntimeError):
    """Base for GitHub-side failures (message is safe to show users)."""


class RepoNotFoundError(GitHubError):
    pass


class RateLimitError(GitHubError):
    def __init__(self, reset: str = "") -> None:
        super().__init__(
            "GitHub API rate limit reached (unauthenticated: 60 requests/hour"
            + (f"; resets at {reset}" if reset else "")
            + "). Try again in a little while.")


class TooLargeError(GitHubError):
    pass


def _get(url: str, timeout: int = 25) -> Tuple[int, dict, bytes]:
    """GET -> (status, headers, body), raising typed errors."""
    req = urllib.request.Request(url, headers=UA)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(urllib.request.getproxies()))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RepoNotFoundError(
                f"GitHub returned 404 for {url}") from exc
        if exc.code == 403:
            reset = ""
            if exc.headers:
                reset = exc.headers.get("X-RateLimit-Reset", "")
            raise RateLimitError(reset) from exc
        raise GitHubError(
            f"GitHub API error {exc.code} for {url}") from exc
    except Exception as exc:  # network/timeout — surfaced, not swallowed
        raise GitHubError(
            f"could not reach GitHub ({type(exc).__name__})") from exc


def parse_repo_url(url: str) -> Tuple[str, str]:
    """'https://github.com/owner/repo[.git][/...]' -> (owner, repo).

    Raises ValueError on anything that is not a github.com repo path.
    """
    text = (url or "").strip()
    if not text:
        raise ValueError("empty repo URL")
    if "://" not in text:
        text = "https://" + text
    parts = urllib.parse.urlparse(text)
    if parts.netloc.lower() not in ("github.com", "www.github.com"):
        raise ValueError(
            f"not a github.com URL: {url!r} (only public GitHub repos "
            "are supported)")
    segs = [s for s in parts.path.split("/") if s]
    if len(segs) < 2:
        raise ValueError(
            f"not a repo URL (need github.com/<owner>/<repo>): {url!r}")
    owner, repo = segs[0], segs[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+", repo):
        raise ValueError(f"invalid owner/repo in URL: {url!r}")
    return owner, repo


def repo_info(owner: str, repo: str) -> Dict:
    """Repo metadata: full_name, default_branch, size_kb, private, archived."""
    _, _, body = _get(f"{API}/repos/{owner}/{repo}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise GitHubError("GitHub returned non-JSON for repo info") from exc
    return {
        "full_name": data.get("full_name", f"{owner}/{repo}"),
        "default_branch": data.get("default_branch", "main"),
        "size_kb": data.get("size") or 0,
        "private": bool(data.get("private")),
        "archived": bool(data.get("archived")),
        "description": data.get("description") or "",
    }


def list_commits(owner: str, repo: str,
                 max_pages: int = MAX_PAGES) -> List[Dict]:
    """All listed commits, oldest first.

    Walks up to ``max_pages`` of 100 newest-first commits (the API paginates
    newest first), then reverses. Cap stated honestly: sampling spreads
    across the repo's most recent 800 commits when history is longer.
    """
    collected: List[Dict] = []
    for page in range(1, max_pages + 1):
        url = (f"{API}/repos/{owner}/{repo}/commits"
               f"?per_page=100&page={page}")
        _, _, body = _get(url, timeout=30)
        try:
            entries = json.loads(body)
        except json.JSONDecodeError as exc:
            raise GitHubError("GitHub returned non-JSON for commits") from exc
        if not entries:
            break
        for e in entries:
            author = (e.get("commit") or {}).get("author") or {}
            collected.append({
                "sha": e.get("sha", ""),
                "name": author.get("name") or "unknown",
                "email": author.get("email") or "unknown",
                "date_iso": author.get("date") or "",
            })
        if len(entries) < 100:
            break
    collected.reverse()  # oldest first
    return collected


def sample_commits(commits: List[Dict], n: int = SAMPLE_N) -> List[Dict]:
    """Evenly-spread subset of ``n`` commits; first and last always kept."""
    if len(commits) <= n:
        return list(commits)
    if n == 1:
        return [commits[-1]]
    idx = {round(i * (len(commits) - 1) / (n - 1)) for i in range(n)}
    return [commits[i] for i in sorted(idx)]


def fetch_py_files(owner: str, repo: str, sha: str,
                   cap_bytes: int = TARBALL_CAP_BYTES) -> Dict[str, str]:
    """{relpath: source} for every .py file in the commit tarball.

    Downloads via codeload (not counted against the API rate limit),
    extracts in memory with a byte cap. Skips non-UTF-8 files like the
    git-based sampler does.
    """
    url = f"{CODELOAD}/{owner}/{repo}/tar.gz/{sha}"
    req = urllib.request.Request(url, headers=UA)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(urllib.request.getproxies()))
    try:
        with opener.open(req, timeout=90) as resp:
            chunks: List[bytes] = []
            total = 0
            while True:
                piece = resp.read(1 << 20)
                if not piece:
                    break
                total += len(piece)
                if total > cap_bytes:
                    raise TooLargeError(
                        f"commit tarball exceeds {cap_bytes // 1048576} MB cap")
                chunks.append(piece)
            data = b"".join(chunks)
    except TooLargeError:
        raise
    except urllib.error.HTTPError as exc:
        raise GitHubError(
            f"could not download commit {sha[:7]} (HTTP {exc.code})") from exc
    except Exception as exc:
        raise GitHubError(
            f"could not download commit {sha[:7]} "
            f"({type(exc).__name__})") from exc

    files: Dict[str, str] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                name = member.name
                # strip the single top-level directory codeload wraps in
                rel = name.split("/", 1)[1] if "/" in name else name
                if not rel.endswith(".py"):
                    continue
                fh = tar.extractfile(member)
                if fh is None:
                    continue
                try:
                    files[rel] = fh.read().decode("utf-8")
                except (UnicodeDecodeError, ValueError):
                    continue  # same skip rule as the git sampler
    except (tarfile.TarError, EOFError) as exc:
        raise GitHubError(
            f"could not unpack commit {sha[:7]} tarball") from exc
    return files
