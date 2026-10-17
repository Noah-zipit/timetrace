"""timetrace.agent — the temporal Q&A agent.

AGENT RULE (load-bearing): every fact reported by this module comes from a
graph query through ro_query (timetrace.graph.queries). This file MUST NEVER
read snapshot JSON, manifests, or any other off-graph source. If the graph
is unreachable, answer() raises DatabaseUnavailableError (honest 503) —
the agent answers nothing rather than inventing an answer.

Pipeline: parse question -> resolve target -> presence at t ->
blast_radius -> churn -> owners -> deterministic verdict + proof chain.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from timetrace.graph import queries as Q
from timetrace.graph.db import ReadOnlyGraph, require_db
from timetrace.analyze.jobs import require_db_named

# ---------------------------------------------------------------------------
# question parsing (pure — unit tested)
# ---------------------------------------------------------------------------

_QUOTED = re.compile(r"""["'`]([^"'`]{2,120})["'`]""")
_PATH_PY = re.compile(r"\b([\w][\w./\-]*\.py)\b")
_DOTTED = re.compile(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)\b")
_AS_OF_DATE = re.compile(
    r"as of\s+(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}(?::\d{2})?))?", re.IGNORECASE)
_AS_OF_EPOCH = re.compile(r"as of\s+(\d{10})(?!\d)", re.IGNORECASE)


def _iso_to_epoch(text: str) -> Optional[int]:
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


def parse_question(question: str) -> Dict[str, Any]:
    """Extract the target mention and optional as-of time from a question.

    Target priority: quoted text > *.py path > dotted qualname.
    Returns {"target_text": str|None, "t": int|None, "repo": None}.
    Unparseable target -> target_text None (the agent asks for
    clarification; it never answers a different question than asked).
    """
    q = question or ""
    target: Optional[str] = None

    m = _QUOTED.search(q)
    if m:
        target = m.group(1).strip()
    if target is None:
        m = _PATH_PY.search(q)
        if m:
            target = m.group(1)
    if target is None:
        for cand in _DOTTED.finditer(q):
            text = cand.group(1)
            # skip abbreviations like "e.g." / "i.e."
            if all(len(seg) <= 2 for seg in text.split(".")):
                continue
            target = text
            break

    t: Optional[int] = None
    m = _AS_OF_DATE.search(q)
    if m:
        iso = m.group(1) + ("T" + m.group(2) if m.group(2) else "")
        t = _iso_to_epoch(iso)
    if t is None:
        m = _AS_OF_EPOCH.search(q)
        if m:
            t = int(m.group(1))

    return {"target_text": target, "t": t, "repo": None}


# ---------------------------------------------------------------------------
# verdict heuristic (pure — unit tested)
# ---------------------------------------------------------------------------


def risk_score(live_dependent_functions: int, importing_modules: int,
               changes_in_30d: int, author_count: int) -> int:
    """Deterministic, explainable risk score for changing the target."""
    return (2 * live_dependent_functions + 3 * importing_modules
            + changes_in_30d + 2 * author_count)


def verdict_tier(score: int) -> str:
    # Deliberate simplification, tuned for demo legibility rather than
    # statistical rigor: dependents dominate (they break), imports are
    # weighted heaviest per module (a broken import poisons a whole file),
    # recent churn and author spread add noise. Thresholds 6/15 were picked
    # so a quiet utility lands SAFE and a hot shared module lands RISKY.
    if score <= 6:
        return "SAFE TO CHANGE"
    if score <= 15:
        return "CHANGE WITH CARE"
    return "RISKY"


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------


def _fmt_date(ts: Optional[int]) -> str:
    if not ts:
        return "unknown date"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _commit_source(change: Dict[str, Any]) -> str:
    """'commit 9f3a1c2e · 2024-03-01 · Jane Doe <j@x>'"""
    short = (change.get("commit") or "")[:8]
    author = change.get("author") or "unknown author"
    email = change.get("email") or "?"
    return f"commit {short} · {_fmt_date(change.get('ts'))} · {author} <{email}>"


def _names(items: List[Dict[str, Any]], key: str = "qualname",
           limit: int = 4) -> str:
    vals = [i.get(key) or i.get("name") or i.get("id", "") for i in items]
    vals = [v for v in vals if v]
    if not vals:
        return "none"
    shown = ", ".join(f"`{v}`" for v in vals[:limit])
    if len(vals) > limit:
        shown += f", and {len(vals) - limit} more"
    return shown


def _clarify(summary: str, meta_extra: Optional[Dict[str, Any]] = None
             ) -> Dict[str, Any]:
    meta = {"target_id": None, "t": None, "repo": None, "graph": None,
            "queries_run": []}
    if meta_extra:
        meta.update(meta_extra)
    return {"verdict": "NEED MORE INFO", "summary": summary,
            "proof_chain": [], "meta": meta}


# ---------------------------------------------------------------------------
# answer
# ---------------------------------------------------------------------------


def answer(question: str, t: Optional[int] = None,
           repo: Optional[str] = None,
           graph: Optional[str] = None) -> Dict[str, Any]:
    """Answer a temporal code question from the graph only.

    ``graph`` selects the per-repo FalkorDB graph (default: the rich demo
    graph). Every fact still comes from graph queries only.

    Returns {"verdict", "summary", "proof_chain": [{title, evidence,
    source}], "meta": {target_id, t, repo, queries_run}}.
    """
    parsed = parse_question(question)
    if t is None:
        t = parsed["t"]
    now_t = t if t is not None else int(time.time())
    queries_run: List[str] = []

    target_text = parsed["target_text"]
    if not target_text:
        return _clarify(
            "I could not tell which module or function you mean. "
            "Name it in quotes or as a path, for example: "
            "is it safe to change \"rich/console.py\"?",
            {"t": now_t, "repo": repo})

    # honest 503 when unreachable
    g: ReadOnlyGraph = (require_db_named(graph) if graph
                        else require_db())

    if repo is None:
        repo = Q.default_repo(g)
        queries_run.append("default_repo")
    if not repo:
        return _clarify(
            "This graph holds several repositories and the question does not "
            "name one. Which repository should I look at?",
            {"t": now_t})

    resolved = Q.resolve_target(g, repo, target_text)
    queries_run.append("resolve_target")
    if resolved is None:
        return _clarify(
            f"I could not find anything matching {target_text!r} in "
            f"repository {repo!r}. Check the path or function name and ask again.",
            {"t": now_t, "repo": repo})

    target_id = resolved["id"]
    label = resolved["label"]

    versions = Q.target_versions(g, target_id)
    queries_run.append("target_versions")
    open_at_t = any(vf is not None and vf <= now_t and (vt is None or vt > now_t)
                    for vf, vt in versions["windows"])

    blast = Q.blast_radius(g, repo, target_id, now_t)
    queries_run.append("blast_radius")
    # Full-life churn: the 30/90-day windows are derived in Python so the
    # proof chain can always cite the target's real first/last commits.
    ch = Q.churn(g, repo, target_id, since_days=0)
    queries_run.append("churn")
    own = Q.owners(g, repo, target_id)
    queries_run.append("owners")

    totals = blast["totals"]
    month_ago = now_t - 30 * 86400
    quarter_ago = now_t - 90 * 86400
    recent = [c for c in ch["changes"]
              if c["ts"] and month_ago <= c["ts"] <= now_t]
    last90 = [c for c in ch["changes"]
              if c["ts"] and quarter_ago <= c["ts"] <= now_t]
    score = risk_score(totals["live_dependents"], totals["importing_modules"],
                       len(recent), len(own))
    verdict = verdict_tier(score)

    # ---- summary: 2-3 plain sentences naming the real dependents ---------
    target_short = target_id.split(":", 2)[-1]
    parts = []
    if not open_at_t:
        parts.append(
            f"{target_short} does not exist in the graph at {_fmt_date(now_t)} "
            f"(no open version then), so there are no live dependents at that time.")
    else:
        dep_bits = []
        if totals["direct_callers"]:
            dep_bits.append(
                f"{totals['direct_callers']} direct caller(s): "
                f"{_names(blast['direct_callers'])}")
        if totals["indirect_callers"]:
            dep_bits.append(
                f"{totals['indirect_callers']} indirect caller(s): "
                f"{_names(blast['indirect_callers'])}")
        if totals["importing_modules"]:
            dep_bits.append(
                f"imported by {totals['importing_modules']} module(s): "
                f"{_names(blast['importing_modules'], key='name')}")
        dep_text = "; ".join(dep_bits) if dep_bits else "no live dependents"
        parts.append(f"Changing {target_short} looks {verdict.lower()}. "
                     f"Right now it has {dep_text}.")
    if recent:
        who = ", ".join(sorted({c["author"] for c in recent if c["author"]})) or "unknown"
        parts.append(
            f"It changed {len(recent)} time(s) in the last 30 days "
            f"({who}), which is where the risk comes from.")
    elif own:
        parts.append(
            f"It has been quiet lately; historically {own[0]['name'] or own[0]['email']} "
            f"touched it most ({own[0]['touches']} changes).")
    summary = " ".join(parts)

    # ---- proof chain ------------------------------------------------------
    chain: List[Dict[str, str]] = []

    first_c = ch["changes"][0] if ch["changes"] else None
    presence = ("still open" if versions["still_open"]
                else f"last seen {_fmt_date(versions['last_seen'])}")
    chain.append({
        "title": "Locate the target in the graph",
        "evidence": (
            f"{target_id} ({label}) has {versions['version_count']} version(s): "
            f"first seen {_fmt_date(versions['first_seen'])}, {presence}. "
            f"{'It is open at the as-of time.' if open_at_t else 'It is NOT open at the as-of time.'}"),
        "source": (_commit_source(first_c)
                    if first_c else f"node {target_id}"),
    })

    chain.append({
        "title": "Walk the blast radius (reverse CALLS, 2 hops + IMPORTS)",
        "evidence": (
            f"{totals['direct_callers']} direct caller(s) "
            f"[{_names(blast['direct_callers'])}], "
            f"{totals['indirect_callers']} indirect caller(s) "
            f"[{_names(blast['indirect_callers'])}], "
            f"{totals['importing_modules']} importing module(s) "
            f"[{_names(blast['importing_modules'], key='name')}]."
            + (f" The module defines {totals['defined_functions']} function(s)."
               if label == "Module" else "")),
        "source": (f"edge walk at t={now_t} ({_fmt_date(now_t)}): "
                   f"CALLS*1..2 + IMPORTS, windows applied on every hop"),
    })

    if recent:
        newest = recent[-1]
        chain.append({
            "title": "Churn and ownership",
            "evidence": (
                f"{len(recent)} change(s) in the 30 days before the as-of time, "
                f"latest: {newest['change']} by {newest['author']}. "
                f"Top owners overall: "
                + (", ".join(f"{o['name'] or o['email']} ({o['touches']})"
                             for o in own[:3]) or "none recorded")),
            "source": _commit_source(newest),
        })
    else:
        chain.append({
            "title": "Churn and ownership",
            "evidence": (
                f"No changes in the 30 days before the as-of time "
                f"({len(last90)} in the last 90, {len(ch['changes'])} total). Top owners: "
                + (", ".join(f"{o['name'] or o['email']} ({o['touches']})"
                             for o in own[:3]) or "none recorded")),
            "source": (_commit_source(ch["changes"][-1])
                        if ch["changes"] else "MODIFIED edges: none"),
        })

    chain.append({
        "title": "Verdict derivation",
        "evidence": (
            f"risk = 2*{totals['live_dependents']} live dependents "
            f"+ 3*{totals['importing_modules']} importing modules "
            f"+ {len(recent)} recent changes + 2*{len(own)} authors = {score} "
            f"-> {verdict} (thresholds: <=6 safe, <=15 care, else risky)."),
        "source": "deterministic heuristic; see verdict_tier docstring",
    })

    return {
        "verdict": verdict,
        "summary": summary,
        "proof_chain": chain,
        "meta": {"target_id": target_id, "t": now_t, "repo": repo,
                 "graph": graph, "queries_run": queries_run},
    }


__all__ = ["answer", "parse_question", "risk_score", "verdict_tier"]
