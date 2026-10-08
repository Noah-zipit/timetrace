# timetrace — Architecture (plain language)

## System overview

```
Repo ──► Parser ──► IR snapshots ──► [event week: FalkorDB] ──► Agent ──► API ──► UI
```

In plain words: we take a git repository, read each old version of its code,
write down what the code looked like (a "snapshot"), store all snapshots as a
time-stamped graph during event week, and let an agent answer questions by
walking that graph. The API serves the answers, and the UI shows them.

## Data flow, step by step

1. **Sample.** The sampler walks the repo's git history, commit by commit, in
   chronological order.
2. **Parse.** For each commit, the parser reads the Python source files and
   extracts modules, classes, functions, and how they connect (calls,
   imports) — producing one IR snapshot.
3. **Store.** Each snapshot is written as `out_dir/<sha>.json`, and a
   `manifest.json` lists every snapshot in order. (Event week: snapshots are
   loaded into FalkorDB as timestamped nodes and edges.)
4. **Ask.** The user asks a question like "is it safe to change this module
   today?" The agent (event week) queries the graph and walks back through
   time — who changed this code, what depends on it, what broke before.
5. **Prove.** The answer comes back with a **proof chain**: the exact commits,
   diffs, and dependencies behind the verdict, shown in the UI with a
   timeline you can scrub through like a video.

## IR contract (v1)

- Parser exposes `parse_files(files: dict[str,str]) -> {"nodes": [...], "edges": [...]}` where `files` maps repo-relative path -> source text.
- Snapshot IR = `{"snapshot": {"commit","author","timestamp" (ISO-8601 UTC),"repo"}, "nodes": [...], "edges": [...]}`.
- Node: `{"id","kind","name", ...}`. kinds: module/class/function. IDs deterministic: `"module:<path>"`, `"class:<path>:<Class>"`, `"func:<path>:<qualname>"` (qualname e.g. `"Server.start"`, nested `"outer.inner"`).
- Function fields: name, qualname, module, lineno, async (bool), decorators (list of str), args (list of str).
- Class fields: name, module, lineno, bases (list of str).
- Module fields: name (= path).
- Edge: `{"from","to","kind",...}`. kinds: defines (module->class/function, class->method), calls (function->function, with raw "callee" dotted name AND best-effort resolved "to" id or null), imports (module->module, with "names" list).
- Sampler output: `out_dir/<sha>.json` per snapshot (full snapshot IR) + `out_dir/manifest.json` (chronological `[{"commit","author","timestamp"}]`).
- API contract: `GET /api/health` -> 200 `{"status":"ok","service":"timetrace","phase":"foundation"}`; `POST /api/ingest`, `POST /api/query {"question","at"}`, `GET /api/timeline` -> all 501 with `{"detail":"NOT IMPLEMENTED — event week: <what>"}`; UI served statically at `/`.

## What exists now vs what gets built Oct 15–18

**Now (foundation):**
- Parser + sampler contracts defined; IR format locked at v1.
- API skeleton with a live health endpoint; everything else 501s.
- UI shell placeholder.
- This document.

**Event week (built live, Oct 15–18):**
- The FalkorDB temporal graph layer: loading snapshots as timestamped
  nodes/edges, Cypher queries for time-travel.
- The agent: natural-language questions answered with proof chains.
- Real timeline-scrub demo in the UI, wired to the graph.

## Glossary

- **node** — one thing in the code graph: a module, a class, or a function.
- **edge** — one connection between nodes: "this file defines this class",
  "this function calls that function", "this module imports that module".
- **snapshot** — the complete IR of the repo at one commit: who it was,
  when, and every node and edge the parser found.
- **temporal validity** — the time window during which a fact was true: a
  function existed from commit A (created) to commit C (deleted), so its
  validity is A→C. This is what makes it a *time machine*.
- **proof chain** — the answer's receipts: the chain of commits, diffs, and
  dependency edges the agent walked to reach its verdict, shown so you can
  check every step instead of trusting it blindly.
