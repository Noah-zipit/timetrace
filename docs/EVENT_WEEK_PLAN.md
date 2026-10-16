# timetrace — Event-week build plan (Oct 15–18, Graph Hacks)

**BUILD STATUS 2026-10-16 ~15:00 PKT:** the ingest script is written and
tested — `timetrace/ingest/` implements data-model.md §5 (snapshot →
interval) + §6 (rename detection), 24/24 tests pass, and the full pipeline
(real sampler → planner) was verified on a synthetic 4-commit repo: correct
windows, one RENAMED_TO detected, MODIFIED tags created/modified/renamed/
deleted all fire. It is **not** yet run against FalkorDB — that needs the
laptop (`scripts/check_db.py` is the one-command connection test) because
the build VM cannot reach database ports.

Status: FalkorDB Cloud instance "timetrace" is **Running / Healthy** on FalkorDB's
own console (checked 2026-10-15 ~13:31 PKT). Connection details are in `.env`
(chmod 600, gitignored). This machine cannot reach database ports at all, so
the first real connection test must come from **your** laptop — see Day 1.

Deadline note (verified on the live hackathon page, 2026-10-15 ~14:05 PKT):
- Event runs **Oct 15, 12:01 AM IST to Oct 18, 11:59 PM IST**; **submissions
  close 11:59 PM IST Oct 18 = 11:29 PM PKT Oct 18**.
- "Is FalkorDB mandatory? **Yes. FalkorDB must be the primary graph database,
  and it must support a central part of the product.**" — the load-bearing
  rule below is not my opinion; it is the entry criterion.
- "The submission and its main FalkorDB implementation must be **new work
  completed during the event**" — the foundation/event-week split is
  rule-clean.
- "Coding assistants allowed; **you must be able to explain what was built
  and why.**"
- "A **live deployment is preferred**; otherwise complete local setup
  instructions **and a working demo video**."
- Track 01's own brief names "**work through a codebase**" as an example —
  timetrace is a textbook Track 01 fit. The graph as the agent's memory of
  the codebase also fits Track 02; the codebase as team knowledge fits
  Track 03.
Target: **build done by the evening of Oct 17**, demo + submission on Oct 18.

## The win condition (read this first)

The judges' main criterion is that FalkorDB is **load-bearing**. The test we
must pass: *unplug FalkorDB and timetrace answers nothing.* The design rule
that enforces it:

- The agent **never reads snapshot JSON directly.** Every fact in an answer
  comes from a graph query. The JSON files are just the loading dock; the
  graph is the warehouse.
- The proof chain is the receipts: every verdict cites the exact commits,
  diffs, and dependency edges the agent walked. If an answer cannot show its
  chain, it does not ship.

You must be able to explain the architecture yourself (event rule). Rehearse
the two-minute version on Day 4: "snapshots become time-stamped nodes and
edges; asking what the code looked like at time T is a filter on two numbers;
the agent answers by walking the graph, not by reading files."

## Build order (each step unlocks the next)

1. **Ingest** — read sampler snapshots, write timestamped nodes/edges into
   FalkorDB per `docs/data-model.md` (valid_from / valid_to windows; nothing
   is ever deleted, windows close and open).
2. **Core queries** — the three questions everything else stands on:
   - *Code as of time T*: "what did module X look like on Mar 1?" (filter on
     the time windows).
   - *Blast radius*: "what depends on function Y right now?" (walk the calls
     and imports edges forward from Y, at today's time window).
   - *History of a thing*: "who touched function Y, and what changed each
     time?" (all versions of one node, ordered by window).
3. **Agent** — plain question in, verdict + proof chain out. It decides which
   of the three queries to run, runs them against FalkorDB, and assembles the
   answer. The LLM writes words; the **graph supplies facts**.
4. **API wiring** — replace the 501 stubs (`/api/ingest`, `/api/query`,
   `/api/timeline`) with the real thing.
5. **Demo UI** — timeline scrub wired to real answers; tap a node, see its
   numbers, its history, its proof chain.
6. **Demo + submission** — one killer demo, recorded; submission on the
   hackathon page.

## Day by day

### Day 1 — today, Oct 15: connect + ingest
- From **your laptop**: connect to the FalkorDB instance with the `.env`
  credentials (host/port/user in `.env`; password is the one line in that
  file — do not print it anywhere). A simple AUTH + PING success is the green
  light. If it fails, the instance may need a recreate — flag it same day.
- Pick the **demo repo**: one of your own repos with real git history
  (renames, deletions, refactors — that is what makes the time machine
  visible). Candidates: rot-protocol, noah-bot, portfolio.
  - **CORRECTION 2026-10-16: none of those candidates work.** The parser is
    Python-only and noah-bot (20 commits), portfolio (18 commits), and the
    other JS/TS repos contain zero `.py` files — the sampler would ingest
    nothing. Pick a real **Python** project with deep history instead
    (a well-known open-source one is fine — the entry is the tool, not the
    analyzed code). It must show at least one genuine rename/move for the
    RENAMED_TO demo. `pip install falkordb` on the laptop first; the ingest
    CLI is `python -m timetrace.ingest.cli <snapshots_dir> --repo NAME`.
- Build the ingest script: sampler output -> FalkorDB, following
  `docs/data-model.md` exactly. Ingest the demo repo fully. Sanity check: ask
  "code as of time T" for one known commit and compare with `git show`.
- End of day: the graph holds the demo repo's whole history and the
  time-travel question works on at least one module.

### Day 2 — Oct 16: queries + proof chains
- Build the other two core queries (blast radius, history of a thing).
- Assemble proof chains: every answer returns the commits, diffs, and edges
  it walked. Wire these into the API's `/api/query` shape.
- Run the load-bearing test: stop the FalkorDB client and confirm the agent
  genuinely cannot answer. If it can, the design leaked — fix before moving
  on.
- End of day: ask "is it safe to change module X today?" and get a verdict
  with a real proof chain.

### Day 3 — Oct 17: agent + UI + submission check
- Agent: natural-language question -> chooses queries -> verdict + proof
  chain. Keep it to the one demo question family; depth beats breadth.
- Wire the UI timeline scrub to real answers (no mocks).
- **Re-check the hackathon page** for the exact submission window and required
  artifacts (video? repo link? write-up?).
- End of day: full demo runs end-to-end on your laptop, no rehearsal edits.

### Day 4 — Oct 18: demo + submit
- Record the demo (one question, one repo, timeline scrub, proof chain —
  under 3 minutes). No AI-generated filler; real screens only.
- Rehearse the 2-minute architecture explanation out loud once.
- Submit on the hackathon page. Confirm the submission actually landed
  (screenshot the confirmation).

## What not to do
- No second demo question family until the first is airtight.
- No polishing the UI before the queries return real answers.
- No snapshot-JSON shortcuts in the agent — that is how the entry dies on
  the load-bearing criterion.
- No printing `.env` credentials anywhere (chat, logs, commits).
