# SUBMISSION.md — WeMakeDevs Graph Hacks submission pack

Paste-ready answers for the WeMakeDevs submission panel. The panel needs
Ashar's login — do NOT log in as him; he pastes these himself.

Event: **Graph Hacks: Context for AI Agents** (WeMakeDevs × FalkorDB),
Oct 15–18, 2026. Team: solo run (code B8RBK8), leader Ashar Qaisar.

---

## Project name

`timetrace`

## Tagline

A time machine for code: ask "is it safe to change this?" and get a
verdict with proof, at any point in your repo's history.

## Description (long)

timetrace turns a repository's git history into a temporal knowledge graph on
FalkorDB and lets you interrogate it. Every function, class, and module
becomes a node with a `valid_from`/`valid_to` time window; every
calls/imports/renames/authorship relation becomes a timestamped edge. Point at
any module and ask "is it safe to change this today?" — an agent runs
multi-hop temporal Cypher (dependents now vs. at time T, churn/blast radius,
ownership) and returns a verdict plus a proof chain where each claim cites
the exact commit and query behind it. Drag the timeline scrubber back to any
commit and the same question gets a different answer, because the code, its
dependents, and its owners were different then.

Demo: Textualize/rich — 61 sampled commits (Nov 2019 → Apr 2026), 420 modules,
318 classes, 2,576 functions, ~30,000 temporal edges, all queryable as-of any
timestamp.

FalkorDB is load-bearing, not decorative: it is the only data store. Remove
it and the product dies — the agent queries the graph read-only and returns
an honest 503 when the database is unreachable. Judges can read the Cypher in
`timetrace/graph/queries.py`: real multi-hop temporal traversals
(time-windowed dependents, churn aggregation, ownership ranking), not trivial
MATCHes.

## Tracks (entering all three)

1. Connected-data agents
2. Agent memory/coordination
3. Company brain

## Repository

https://github.com/Noah-zipit/timetrace (public)

## Demo video

`ui/qa/timetrace-demo.mp4` in the repo — silent walkthrough with captions:
the app answers "Is it safe to change rich/console.py today?" on the real
demo graph (verdict: RISKY, with proof chain), then the timeline is scrubbed
back to 2020 and the same question returns a different verdict. Upload this
file as the submission's demo video.

## What was built during the event vs foundation

**Foundation (before Oct 15):** Python AST parser, git history sampler,
FastAPI skeleton, UI shell. Deliberately zero FalkorDB code.

**Event week (Oct 15–18):** the entire graph layer — temporal interval
schema, snapshot→interval planner with rename detection, batched FalkorDB
writer, temporal Cypher query library, the proof-chain agent, real UI wiring
with timeline scrubber, the full rich demo ingest, screenshots, and the demo
video.

## AI-assistance disclosure

This project was built with AI assistance: an AI coding agent working under
the author's direction wrote code, ran tests, and produced docs and the demo
video from the author's explicit build orders. The author designed the
product, made the technical decisions, verified the demo against the real
repository, and takes responsibility for the submission.

## Deployment status

Live at **https://timetrace-demo.vercel.app** — fully loaded and verified
as of 2026-10-17. The demo graph lives in FalkorDB Cloud (61 sampled commits
of Textualize/rich, 2,596 functions, 13,384 temporal edges) and was verified
on the deployed site: `/api/stats` (connected:true), `/api/timeline` (61
commits), and `POST /api/query` for "Is it safe to change rich/console.py?"
returns verdict **RISKY** with a 4-claim proof chain. The full interactive
demo (UI + timeline scrubber + verdicts with proof chains) runs against the
live cloud graph — the demo video below shows the same product.
