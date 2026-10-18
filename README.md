# timetrace — a time machine for code

**One-liner:** paste **any public GitHub repo URL** and timetrace builds a
temporal graph of its history in FalkorDB, then answers **"is it safe to
change this module today?"** with a verdict backed by a proof chain you can
inspect. Drag the timeline back to any commit and the answer changes —
because the code, its dependencies, and its owners were different then.

Bundled demo: **Textualize/rich** — 61 sampled commits (Nov 2019 → Apr 2026),
420 modules, 318 classes, 2,576 functions, ~30,000 temporal edges.

## How it works (plain language)

1. **Sampler** walks the repo's git history and checks out N commits.
2. **Parser** reads every Python file at each commit and extracts modules,
   classes, functions, who-calls-whom, and who-imports-what — plain JSON.
3. **Planner** turns consecutive snapshots into *interval operations*: a
   function that existed from March to June becomes one node with
   `valid_from`/`valid_to` timestamps. Unchanged things are not rewritten.
4. **FalkorDB** stores it all. Every node and edge carries a time window, so
   the graph can be queried *as of any moment*: "who depended on console.py
   in 2021?"
5. **Agent** answers questions with multi-hop temporal Cypher — dependents
   now vs. at time T, churn (blast radius), ownership — and returns a verdict
   (SAFE / RISKY / …) plus a proof chain: each claim cites the exact commit
   and the query that produced it.
6. **UI** is one screen: repo timeline scrubber, code state at time T, ask box,
   verdict card, proof chain.

**Why FalkorDB is load-bearing:** remove it and the product dies. There is no
other database, no cache, no fallback. The agent's only data source is Cypher
against the temporal graph (`timetrace/graph/queries.py`); with the database
unreachable the API returns an honest 503 instead of a fake answer.

## Graph map

Every node and every edge carries `valid_from` / `valid_to` timestamps. A
fact is true at time T when `valid_from <= T < valid_to` (`valid_to = null`
means still true). Nothing is ever deleted — when code changes, the old
window closes and a new one opens.

```mermaid
graph LR
    R[Repository] -->|IN_REPO| M[Module]
    M -->|DEFINES| C[Class]
    M -->|DEFINES| F[Function]
    C -->|DEFINES| F
    F -->|CALLS| F
    M -->|IMPORTS| M
    P[Person] -->|AUTHORED| M
    P -->|MODIFIED| F
    F -->|RENAMED_TO| F
```

**Nodes:** `Repository` (one per analyzed repo) · `Module` (one Python file)
· `Class` · `Function` — each with a deterministic id
(`module:server.py`, `class:server.py:Server`) and its time window ·
`Person` (commit authors).

**Edges:** `DEFINES` (module → class/function, class → method) · `CALLS`
(function → function) · `IMPORTS` (module → module) · `AUTHORED`
(person → commit) · `MODIFIED` (person → node, via a commit) · `RENAMED_TO`
(old version → new version, so a rename reads as one story).

**How the agent queries it** — every question becomes a time-windowed
traversal. Example, "which modules imported `rich/console.py` at time T"
(from `timetrace/graph/queries.py`):

```cypher
MATCH (imp:Module)-[im:IMPORTS]->(m:Module)
WHERE m.name = 'rich/console.py'
  AND im.valid_from <= $t AND (im.valid_to IS NULL OR $t < im.valid_to)
RETURN imp.name
```

The verdict's proof chain is built the same way — dependents, churn, and
ownership are all multi-hop temporal traversals, and each claim cites the
commit and the query behind it. Full model: `docs/data-model.md`.

## Screenshots

![Fresh load — timeline and module list](ui/qa/01-fresh-load.png)
![Verdict with proof chain](ui/qa/02-verdict-now.png)
![Same question, asked in 2020 — different verdict](ui/qa/03-verdict-scrubbed.png)

## Live demo — paste any GitHub repo

**https://timetrace-demo.vercel.app** — the app, live.

Paste any public GitHub repo URL into the repo bar and timetrace analyzes
it: commits are listed via the GitHub API (no git binary needed), up to 40
are sampled across the listed history, each commit's tarball is parsed for
Python files, and the temporal graph is built one commit per serverless
step — the UI shows real progress (n/40). Every repo gets its own FalkorDB
graph (`gh_{owner}__{repo}`); the bundled rich demo stays untouched, and
the repo switcher flips between them. Honest caps: sampling covers the
repo's newest 800 commits, repos over 60 MB are refused, Python files only
(the parser is Python-only), and the unauthenticated GitHub rate limit
(60 requests/hour) applies.

Status 2026-10-17: **fully live and verified.** The FalkorDB Cloud instance
was restored, and all 20,257 pre-planned ingest ops in 14 chunks (under
`~/workspace/graph-hacks/ops-chunks`, built from the verified local snapshots
in `demo-data/rich-snapshots`) were loaded into it through
`POST /api/ingest-ops` on the live app — first chunk with
`create_indexes: true`. Live verification on the deployed site:
`/api/stats` returns connected:true, repo rich, 61 commits, 2,596 functions;
`/api/timeline` returns 61 commits; and `POST /api/query` with
`{"question": "Is it safe to change rich/console.py?"}` returns verdict
**RISKY** with a 4-claim proof chain (58 direct callers, 119 importing
modules, most-touched by Will McGugan). One chunk needed a retry on a
dropped connection; edge opens are MERGE-keyed by stable uid since commit
`d03a6df`, so the re-send was a verified no-op with zero duplicate edges.

## Demo video

[`ui/qa/timetrace-demo.mp4`](ui/qa/timetrace-demo.mp4) — silent walkthrough with
captions: the app answers a real question on the real demo graph, then the
timeline is scrubbed back to 2020 and the verdict changes.

## Local setup (complete, from clone to running)

Prerequisites: Python 3.10+, git, and a FalkorDB server. Two options:

**Option A — FalkorDB Cloud (the submission's production target).**
Create a free instance at falkordb.com, then copy the connection into `.env`:

```bash
cp .env.example .env   # then fill in FALKORDB_HOST / PORT / USERNAME / PASSWORD
```

**Option B — local FalkorDB (what the demo was built and verified against).**
The repo ships a start script that runs the FalkorDB Redis module locally —
no Docker needed:

```bash
bash scripts/start_local_falkordb.sh   # starts 127.0.0.1:6380, no auth
FALKORDB_LOCAL=1 python scripts/check_db.py   # expect: GREEN LIGHT
```

Then, either way:

```bash
# 1. install
pip install -r requirements.txt

# 2. build the demo graph (samples rich's git history → parses → plans → writes)
#    (or point the sampler at any Python repo with git history)
python -m timetrace.sampler --repo /path/to/repo --out demo-data/repo-snapshots --mode every --n 74
FALKORDB_LOCAL=1 python -m timetrace.ingest.cli demo-data/repo-snapshots \
    --repo rich --graph timetrace --host 127.0.0.1 --port 6380 --password ""

# 3. run the API (serves the UI at http://localhost:8123)
FALKORDB_LOCAL=1 uvicorn timetrace.api.main:app --port 8123
```

Environment variables (`.env` or exported): `FALKORDB_HOST`, `FALKORDB_PORT`,
`FALKORDB_USERNAME`, `FALKORDB_PASSWORD`. `FALKORDB_LOCAL=1` forces the local
defaults (127.0.0.1:6380, no auth) and skips `.env` — handy for tests.
The cloud connection is a pure `.env` swap: no code changes.

Run the tests: `FALKORDB_LOCAL=1 pytest` (69 tests, incl. 8 end-to-end
against a live local FalkorDB).

## API

| Method | Path            | What it does                                          |
|--------|-----------------|-------------------------------------------------------|
| GET    | `/`             | the UI                                                |
| GET    | `/api/health`   | service status + build phase                          |
| GET    | `/api/stats`    | node/edge counts for the repo graph                   |
| GET    | `/api/timeline` | commit list for the scrubber                          |
| GET    | `/api/state?t=` | modules + functions open at epoch `t`                 |
| POST   | `/api/query`    | `{"question": "..."}` → verdict + proof chain         |
| POST   | `/api/ingest`   | run the pipeline for a snapshot directory            |
| GET    | `/api/repos`    | analyzed repos (rich demo + user-analyzed)            |
| POST   | `/api/analyze/start` | `{"repo_url": "https://github.com/o/r"}` → analyze job |
| POST   | `/api/analyze/step`  | `{"repo": "o/r"}` → process ONE commit (repeat to 40) |
| GET    | `/api/analyze/status?repo=o/r` | job progress                       |

## Project layout

```
timetrace/
  timetrace/
    parser/        Python AST → JSON IR (foundation)
    sampler/       git history → per-commit snapshots (foundation)
    ingest/        snapshot diff → interval ops → FalkorDB writer (event week)
    graph/         db.py (config/connection), queries.py (temporal Cypher)
    agent.py       question → verdict + proof chain (read-only graph access)
    api/           FastAPI: health/stats/timeline/state/query/ingest
  ui/              one-screen app (index.html, app.js, styles.css)
    qa/            screenshots + demo video (this README's images)
  scripts/         start_local_falkordb.sh, check_db.py, demo_capture.py
  tests/           69 tests (unit + E2E against local FalkorDB)
  docs/            ARCHITECTURE.md, data-model.md, TEMPORAL_CHEAT_SHEET.md, …
  SUBMISSION.md    exact answers for the WeMakeDevs submission form
```

## Foundation vs event week

Built for **Graph Hacks (WeMakeDevs × FalkorDB, Oct 15–18, 2026)**, entering
all three tracks: **connected-data agents**, **agent memory/coordination**,
**company brain**.

- **Foundation (before the event):** AST parser, git sampler, API skeleton,
  UI shell. Deliberately zero FalkorDB code.
- **Event week (Oct 15–18, built during the hackathon):** the entire temporal
  graph layer — interval schema, planner, FalkorDB writer, temporal Cypher
  queries, the proof-chain agent, the real UI wiring, demo ingest of rich,
  screenshots, and the demo video.

## AI-assistance disclosure

This project was built with AI assistance (an AI coding agent working under
the author's direction throughout design and implementation). The author
designed the product, made the technical decisions, verified the demo, and
takes responsibility for the submission. AI wrote code, ran tests, and
produced docs/video from the author's explicit build orders — including this
README.

## License

MIT — see LICENSE.
