# timetrace — Temporal Data-Model Design

This document describes how timetrace turns a pile of per-commit snapshots
into a **time machine**: a graph where every fact carries the time window
during which it was true. It is a design document only — no implementation
here, no query-language code. If you can re-explain each section below in
your own words, you can defend the data model to the judges.

**The one-sentence version:** every node and edge in the graph has a
`valid_from` and a `valid_to` timestamp; a fact is "true" at time T when
`valid_from <= T < valid_to`; asking "what did the code look like at time T"
is just filtering on those two numbers.

---

## 1. The big idea, in plain language

A normal code graph says "module `server.py` defines function `start`."
timetrace's graph says "module `server.py` defines function `start`, **and
this was true from Jan 5 (when it was created) until Jun 3 (when it was
deleted)**."

Nothing is ever deleted from our graph. When code changes, the old version's
time window closes and a new version's window opens. That is the entire
trick: history is not lost, it is just filed under an older time window. The
timeline scrubber works because "show me the code on Mar 1" is the same
question as "show me every fact whose time window contains Mar 1."

### A small picture

```
Jan 5              Mar 12              Jun 3             today
 |                   |                   |                  |
 v                   v                   v                  v
 [====== Server.start v1 ======][======== Server.start v2 =========)
                                        x
                                (v1 ends here)         (v2 still true;
                                                         valid_to = null)
```

Reading the timeline left to right: v1 is true from Jan 5 up to (but not
including) Mar 12; v2 is true from Mar 12 onward and has no end yet.

---

## 2. Node types

Five kinds of things live in the graph. Fields are listed per kind.

### Repository
One node per repository analyzed. It is the container everything else hangs
off, and it keeps different repos from ever mixing (see section 8).

- `name` — e.g. `"noah-zipit/win-if-u-can"`
- `path` — the local path it was sampled from (or the clone URL)
- `first_sampled_at`, `last_sampled_at` — when we ingested history
- `valid_from`, `valid_to` — the repository itself is always valid
  (`valid_from` = first commit timestamp, `valid_to` = null)

### Module
One Python file in the repo. (Modules come straight from the parser IR,
kind `module`.)

- `name` — repo-relative path, e.g. `"server.py"`
- `id` — deterministic: `"module:server.py"`
- `lineno` — null (a module has no single line)
- `valid_from`, `valid_to`

### Class
One class in a module. (Parser IR kind `class`.)

- `name` — e.g. `"Server"`
- `qualname` — the dotted path inside its module, e.g. `"Server"`
- `module` — which module defines it, e.g. `"server.py"`
- `lineno` — where the `class` statement starts
- `bases` — list of base-class names, e.g. `["Thread"]`
- `id` — `"class:server.py:Server"`
- `valid_from`, `valid_to`

### Function
One function or method. This is the workhorse of the model — most questions
are about functions. (Parser IR kind `function`.)

- `name` — e.g. `"start"`
- `qualname` — full dotted path inside the module, e.g. `"Server.start"`,
  nested `"outer.inner"`
- `module` — which module defines it, e.g. `"server.py"`
- `lineno` — first line of the `def`
- `args` — list of argument names
- `async` — true/false
- `decorators` — list of decorator names
- `id` — `"func:server.py:Server.start"`
- `valid_from`, `valid_to`

### Person
A code author. (From the snapshot's `author` field — not in the parser IR,
the graph layer adds this at ingest time.)

- `name` — e.g. `"Ashar Qaisar"`
- `email` — e.g. `"noahext994@gmail.com"` (the de-duplication key — git
  identifies authors by email)
- `first_seen_at`, `last_seen_at`
- `valid_from`, `valid_to` — a person is always valid once seen

**A note on "versions":** when a function changes, we do NOT overwrite its
node. The old node gets its `valid_to` stamped (its window closes) and a NEW
node — same identity fields (`id`), different body — begins with
`valid_from` = the change commit's timestamp. So there can be several nodes
with the same `id` but non-overlapping windows. The `id` is "which function";
the window is "when this version was the truth."

---

## 3. Edge types

Edges carry `valid_from`/`valid_to` exactly like nodes do. An edge is true
only while its window contains the time in question — so "function A called
function B" can be true in January and false by June, and the graph remembers
both.

### DEFINES — module → class/function, class → method
"This file contains this class/function; this class contains this method."
Comes from the parser IR (`defines` edges).

- From: Module (to a class or function), or Class (to a method)
- To: Class or Function
- Window semantics: true while the definition exists in the source.

### CALLS — function → function
"This function calls that function." Comes from the parser IR (`calls`
edges). The parser records both the raw dotted name it saw (`callee`) and a
best-effort resolved target (`to`, or null if it could not resolve).

- From / To: Function
- Fields: `callee` (raw name as written), `to` (resolved target id or null)
- Window semantics: true while the caller function's body contains that
  call. If the caller is rewritten to stop calling it, this edge's window
  closes — even though both functions still exist. This is the edge that
  makes dependency history possible: "who depended on this function in March?"

### IMPORTS — module → module
"This module imports that module." Comes from the parser IR (`imports`
edges).

- From / To: Module
- Fields: `names` (what was imported, e.g. `["os", "sys"]`)
- Window semantics: true while the import statement exists in the file.

### AUTHORED — person → commit (snapshot)
"This person wrote this commit." Not in the parser IR — the graph layer adds
it at ingest time, one edge per commit, from the snapshot's `author` field
to the snapshot/commit node.

- From: Person, To: the commit's snapshot
- Fields: `commit` (the sha), `timestamp`
- Window semantics: a commit happened at one instant — `valid_from` =
  `valid_to` = the commit timestamp. (A zero-length window: it is true only
  *at* that instant. That is deliberate — it marks an event, not a state.)

### MODIFIED — person → node, via a commit
"This person changed this function/class/module in this commit." Also added
at ingest time. While AUTHORED says "who made the commit," MODIFIED says
"whose commit touched this particular piece of code" — it is how we answer
"who keeps breaking this function?"

- From: Person, To: Module / Class / Function (the NEW version node)
- Fields: `commit` (the sha), `timestamp`, and a `change` tag: one of
  `created`, `modified`, `deleted`, `renamed`
- Window semantics: like AUTHORED, an event — `valid_from` = `valid_to` =
  the commit timestamp. But the *version node* it points to carries the
  normal state window, so "who modified it" and "when was it true" are both
  answerable from one walk.

### RENAMED_TO — old version → new version
"A function with the same body moved from path A to path B." See section 6.
It is an event-week refinement, kept intentionally simple.

- From / To: two version nodes of (logically) the same function
- Fields: `commit`, `timestamp`, `similarity` (0–1, how alike the bodies were)
- Window semantics: an event — true only at the commit timestamp.

---

## 4. Temporal validity: the rule everything rests on

**Every node and every edge carries `valid_from` and `valid_to`.**
A fact is "true" at time T if and only if:

```
valid_from <= T  AND  (valid_to is null  OR  valid_to > T)
```

In words: the fact became true at or before T, and it has not stopped being
true yet. A `valid_to` of **null means "still true right now"** — the window
is open.

Three consequences worth being able to say out loud:

1. **Nothing is ever updated in place.** A change closes the old window
   (stamps `valid_to`) and opens a new one (fresh `valid_from`). The old
   version stays in the graph forever, retrievable by asking about an older T.
2. **Time T = "now" works automatically.** Asking about the present is just
   asking for everything whose window is still open (`valid_to is null`).
   No special case needed.
3. **The scrubber is one rule.** "What did the code look like on Mar 1?" is
   answered by keeping every node and edge whose window contains Mar 1 and
   throwing away the rest. The UI timeline is a slider over T; the graph is
   filtered by this rule on every tick.

---

## 5. From snapshots to intervals: how windows get built

The sampler emits one snapshot per commit, each stamped with the commit's
timestamp. Snapshots are **states**; the graph needs **intervals**. The
ingest step converts states to intervals with a simple rule, applied in
commit order:

- **First seen** (in commit A, not present before): a new version node begins.
  `valid_from` = A's timestamp, `valid_to` = null. MODIFIED edge tagged
  `created`.
- **Unchanged** (in commit B, same as before): nothing happens. The window
  stays open — B contributes no new node. This is why a quiet function costs
  us almost nothing.
- **Changed** (in commit C, body differs from the open version): the open
  version's window closes — `valid_to` = C's timestamp — and a new version
  begins with `valid_from` = C's timestamp. MODIFIED edge tagged `modified`.
- **Gone** (in commit F, absent from the snapshot): the open version's window
  closes with `valid_to` = F's timestamp. No new version. MODIFIED edge
  tagged `deleted`, pointing at the version that just ended.

"Changed" is decided by comparing the parsed body — same `id` (path +
qualname) but different content (we compare a hash of the normalized body, so
whitespace-only noise does not count as a change). Edges (calls, imports)
are diffed the same way: an edge present in A but missing in C gets
`valid_to` = C's timestamp; one that appears in C gets `valid_from` = C's.

The language-agnostic sketch of the whole conversion:

```
for each commit in chronological order:
    snapshot = parse(commit)
    for each node in snapshot:
        open = currently-open version of node.id   # valid_to is null
        if open is null:
            open new version(node, valid_from = commit.timestamp)
        else if body_hash(node) != body_hash(open):
            close open with valid_to = commit.timestamp
            open new version(node, valid_from = commit.timestamp)
        else:
            leave open untouched                   # unchanged: free
    for each node.id with an open version missing from snapshot:
        close it with valid_to = commit.timestamp  # deleted
    same diffing logic for edges
```

### Worked example: `Server.start`

A function `Server.start` in `server.py`, created Jan 5, body changed Mar 12,
deleted Jun 3. Commits happen weekly, but nothing else touches it. Its
validity intervals:

| Version | id                        | valid_from | valid_to   | What happened              |
|---------|---------------------------|------------|------------|----------------------------|
| v1      | `func:server.py:Server.start` | 2026-01-05 | 2026-03-12 | created Jan 5              |
| v2      | `func:server.py:Server.start` | 2026-03-12 | 2026-06-03 | body changed Mar 12        |
| —       | —                         | —          | —          | deleted Jun 3; no v3, window ends |

Asking "what did `Server.start` look like on Feb 1?" → v1 (Jan 5 ≤ Feb 1 <
Mar 12). Asking "on Apr 20?" → v2 (Mar 12 ≤ Apr 20 < Jun 3). Asking "today?"
→ nothing (no open window) — correctly, because it no longer exists.

Notice the boundary rule in action: on **exactly** Mar 12, v1 is already
false (`valid_to > T` fails) and v2 is true (`valid_from <= T` holds). There
is never a moment where both versions claim to be true, and never a gap
where neither does.

---

## 6. Renames and moves (event-week refinement)

**The problem:** a developer moves `Server.start` from `server.py` to
`engine.py`. Naively the diffing rule in section 5 sees "function deleted in
`server.py`" plus "function created in `engine.py`" — two unrelated facts —
and the history breaks. The timeline would show a death and a birth instead
of a move.

**Detection (kept simple on purpose):** when ingest sees a deletion and a
creation in the *same commit*, and both share the same `qualname` (here
`Server.start`) and their bodies are similar enough (normalized-body
similarity above a threshold, e.g. 0.8 — exact number tuned in event week),
it treats them as one logical function that moved.

**Representation:** the old version node closes normally (`valid_to` = the
commit timestamp), the new version node opens normally (`valid_from` = the
commit timestamp) — the core model does not change at all — and additionally
a **RENAMED_TO** edge is drawn from the old version to the new version,
stamped with the commit and the similarity score. Timeline queries follow
that edge to stitch the history back together: "show me `Server.start`
across its whole life" walks v1 → RENAMED_TO → v2 as one story.

**What is deliberately out of scope:** rename-with-heavy-rewrite (similarity
below threshold — treated as delete + create, which is honest), cross-repo
moves, and class-rename cascades. Marked as event-week refinement: if time is
short, ship without it; the core time-slice model is complete and correct
without it.

---

## 7. Indexing notes (conceptual — no vendor syntax here)

The query in section 4's rule — "keep facts whose window contains T" — must
be fast, because the timeline scrubber runs it on every slider tick. Two
index families make that true:

1. **Range index on (`valid_from`, `valid_to`).** A time-slice query is a
   two-sided range condition: `valid_from <= T` AND (`valid_to IS NULL` OR
   `valid_to > T`). A range index on the two timestamp fields lets the engine
   skip everything whose window cannot contain T instead of scanning the
   whole graph. Since the vast majority of facts are "old versions whose
   windows closed long ago," this pruning is what keeps scrubbing smooth on
   large histories.

2. **Lookup index on (`kind`, `name`) — and on `id`.** Questions like "find
   the function named `start`" or "find node `func:server.py:Server.start`"
   are exact lookups, not time queries. A plain lookup index on kind+name
   (and on the deterministic `id`) answers them without a scan. The time
   filter is applied after: find the candidate nodes by identity, then keep
   the versions whose windows contain T.

**Why time-slice queries stay fast, in one paragraph:** the expensive part
of "state at time T" is not the filtering — comparing two timestamps per
fact is cheap — it is *finding* the candidate facts without touching the
rest. The range index narrows the universe to "facts alive at T" and the
lookup index narrows it to "facts about this thing," so the actual work per
query is proportional to the answer size, not the repo's total history. That
is the property the scrubber depends on, and it holds no matter how many
commits the repo has.

---

## 8. Per-repo isolation: one graph, many namespaces

Every node carries the repository it belongs to (via its `id` prefix and a
link up to its Repository node), and every ingest run is scoped to one
repository. Two repos analyzed by timetrace live in the same physical graph
but never share nodes or edges — a function `main` in repo A and a function
`main` in repo B have different ids (`func:repoA/...` vs `func:repoB/...`)
and sit under different Repository roots, so no query can accidentally mix
them. Practically: the agent always starts its walk from the Repository node
the user asked about, and everything reachable from there — and only that —
is the answer's universe. Adding a second repo later is just another ingest
run; nothing existing moves or renames.

---

## How this maps to the build (for event week)

- **Sampler/parser (foundation):** already produce per-commit snapshots with
  `commit`, `author`, `timestamp` — everything sections 5 and 6 need as
  input. No changes required there.
- **Ingest (event week):** implements the snapshot → interval conversion in
  section 5 (plus rename detection from section 6), creating Repository and
  Person nodes and AUTHORED/MODIFIED edges that do not exist in the parser
  IR. One ingest pass per repo, chronological.
- **Queries (event week):** every query begins with the section 4 rule
  ("window contains T"), then walks DEFINES/CALLS/IMPORTS edges —
  themselves time-filtered — to build answers and proof chains.
- **UI scrubber (event week):** a slider over T; each tick re-runs the
  time-slice filter from section 4, backed by the indexes in section 7.

## Glossary recap

- **valid_from / valid_to** — the time window during which a node or edge
  was true. `valid_to = null` means "still true now."
- **version** — one window of a node's life; same `id`, different window.
- **snapshot** — the full state of the repo at one commit (input).
- **interval** — a `valid_from`/`valid_to` window (output).
- **time-slice / "state at time T"** — all facts whose windows contain T;
  the single operation behind the timeline scrubber.
- **proof chain** — the commits, diffs, and edges walked to answer a
  question; made possible because nothing is ever deleted from the graph.
