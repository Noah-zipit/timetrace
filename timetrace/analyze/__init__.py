"""timetrace.analyze — analyze ANY public GitHub repo into a temporal graph.

Pipeline (no git binary anywhere — GitHub API only, so it runs inside a
Vercel serverless function):

    parse_repo_url(url)          -> (owner, repo)
    repo_info(owner, repo)       -> size/branch/private checks
    list_commits(owner, repo)    -> chronological [{sha, name, email, date_iso}]
    sample_commits(commits, 40)  -> evenly spread subset (first + last kept)
    fetch_py_files(owner, repo, sha) -> {path: source} from the commit tarball

The API drives this client-stepped: POST /api/analyze/start creates an
AnalyzeJob node in the repo's own graph, POST /api/analyze/step processes
exactly one commit (tarball -> parse -> plan -> write), so no single
serverless call does long work. Jobs + registry live in FalkorDB itself;
planner state is reconstructed from the graph between steps, so a retried
step replays idempotent ops and is safe.

Per-repo graphs keep every repo isolated: graph name "gh_{owner}__{repo}"
(sanitized). The rich demo stays untouched in graph "timetrace".
"""

from timetrace.analyze import github, jobs

__all__ = ["github", "jobs"]
