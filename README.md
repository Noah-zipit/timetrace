# timetrace — a time machine for code

**What timetrace is.** timetrace reads a repository's git history and builds a
timeline of how the code evolved — which functions existed, which called
which, which imports changed, commit by commit. With that timeline you can
point at any module and ask *"is it safe to change this today?"* and get an
answer backed by proof: here is what this code looked like over time, here is
who touches it, here is what depends on it. It is a time machine for code,
with receipts.

## Foundation vs event week

timetrace is being built in two phases for the Graph Hacks hackathon
(WeMakeDevs × FalkorDB, Oct 15–18, 2026).

**Foundation (now, before the event):**
- `timetrace/parser/` — reads Python source files, extracts modules, classes,
  functions, calls and imports into a plain-JSON "IR" (intermediate
  representation)
- `timetrace/sampler/` — walks a repo's git history, runs the parser on each
  commit, and saves one JSON snapshot per commit plus a chronological manifest
- `timetrace/api/` — FastAPI skeleton: a health endpoint plus stub endpoints
  that return 501 until event week
- `ui/` — static UI shell (served from the API)

**Event week (Oct 15–18, built live during the hackathon):**
- The temporal graph layer on **FalkorDB** — every snapshot becomes
  timestamped nodes and edges in the graph
- The agent — answers questions ("is it safe to change this module today?")
  by querying the graph and returning a **proof chain** you can inspect
- The demo UI wired to real answers, plus the timeline-scrub view

Nothing graph-database related exists yet on purpose: the graph layer is the
event-week build, and the judges will see it happen.

## How to run each piece

```bash
# 1. Set up (Python 3.10+)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. API (serves the UI too)
uvicorn timetrace.api:app --reload

# 3. Sampler (snapshot a repo's history — implemented in event week,
#    output contract is in DESIGN.md)
python -m timetrace.sampler --repo <path> --out <out_dir>

# 4. Tests
pytest
```

Endpoints while in foundation phase:

| Method | Path            | Result                                              |
|--------|-----------------|-----------------------------------------------------|
| GET    | `/`             | UI shell (static)                                   |
| GET    | `/api/health`   | 200 `{"status":"ok","service":"timetrace","phase":"foundation"}` |
| POST   | `/api/ingest`   | 501 — event week: snapshot loading into FalkorDB    |
| POST   | `/api/query`    | 501 — event week: graph-backed question answering   |
| GET    | `/api/timeline` | 501 — event week: timeline playback from the graph  |

## Project layout

```
timetrace/
  README.md                 this file
  DESIGN.md                 constraints, workstreams, IR contract
  requirements.txt          FastAPI, uvicorn, pytest — no graph deps
  .gitignore
  docs/
    ARCHITECTURE.md         system overview in plain language
    data-model.md           (placeholder — authored by the docs workstream)
  tests/                    pytest tests
  timetrace/                Python package
    parser/                 git-history-aware source parsing → IR
    sampler/                git sampling → per-commit snapshot JSON + manifest
    api/                    FastAPI skeleton
  ui/
    index.html              UI shell (placeholder — separate UI workstream)
```
