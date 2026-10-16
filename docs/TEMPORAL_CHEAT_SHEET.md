# timetrace — FalkorDB implementation cheat sheet (temporal model → Cypher)

Verified against the live FalkorDB docs (docs.falkordb.com) on 2026-10-16.
Companion to `docs/data-model.md` — that doc is the theory; this is what you
paste tonight. FalkorDB Cloud is unreachable from the build VM, so everything
below runs from **your laptop** with the creds in `.env`.

## 0. The one decision that saves you: epoch integers

Store `valid_from` / `valid_to` as **UNIX epoch seconds (64-bit int)**,
`valid_to = null` for open windows. Not DateTime objects.

Why: FalkorDB's range indexes accelerate exact-match and comparison filters
on numbers; the window rule stays a two-number comparison exactly as
data-model.md writes it; no timezone traps on the Mar-12 boundary. (FalkorDB
does have DateTime/Date/Duration types, but ints are simpler, faster, and
index-friendly for the scrubber.)

## 1. Connect (from your laptop)

```python
from falkordb import FalkorDB

db = FalkorDB(host=HOST, port=PORT, username='falkordb', password=PASSWORD)  # from .env
g = db.select_graph('timetrace')
g.query("RETURN 1")  # green light
```

Agent reads should go through the read-only path (`g.ro_query(...)`) —
`GRAPH.RO_QUERY` errors on writes, so the agent can never mutate the graph
by accident. This also enforces the plan's load-bearing rule: kill the
connection and the agent must genuinely go silent.

## 2. Indexes — create once, before ingest (creation is async)

```cypher
CREATE INDEX FOR (n:Function) ON (n.valid_from)
CREATE INDEX FOR (n:Function) ON (n.id)
CREATE INDEX FOR (n:Module)   ON (n.valid_from)
CREATE INDEX FOR (n:Module)   ON (n.id)
CREATE INDEX FOR (n:Class)    ON (n.valid_from)
CREATE INDEX FOR (n:Class)    ON (n.id)
```

- Verify with `GRAPH.EXPLAIN <query>` — you want `Index Scan`; `Node By Label Scan`
  means the index is missing (or still building).
- `valid_from <= T` rides the range index; the `IS NULL` half of the OR is a
  cheap residual filter after pruning. Put the `valid_from` predicate first.
- `<>` (not-equal) filters are **not** index-accelerated — avoid them in hot
  queries.
- Relationship-property indexes are not documented for FalkorDB: always start
  from an indexed **node**, then filter its edges. (Edges carry windows too —
  filter them in WHERE, don't expect an index.)

## 3. The window predicate — paste verbatim into every time query

```cypher
CYPHER t=$t
MATCH (n:Function)
WHERE n.valid_from <= $t AND (n.valid_to IS NULL OR n.valid_to > $t)
RETURN n
```

NULL semantics: when `valid_to` is missing, `n.valid_to > $t` evaluates to
NULL (not true) — the explicit `IS NULL` branch is **required**, not optional.
"Now" needs no special case: any T past all `valid_from`s with open windows.

## 4. Ingest — batch with UNWIND; MERGE on (id, valid_from)

**The trap:** one logical function has many version nodes sharing one `id`.
`MERGE` on `id` alone matches the old version and overwrites it — silently
destroying append-only history. MERGE on the composite key:

```cypher
CYPHER nodes=$nodes
UNWIND $nodes AS row
MERGE (f:Function {id: row.id, valid_from: row.valid_from})
SET f += row.props
```

Closing a window is a targeted property stamp, never a delete:

```cypher
CYPHER uid=$uid, ts=$ts
MATCH ()-[e:CALLS]->() WHERE e.uid = $uid AND e.valid_to IS NULL
SET e.valid_to = $ts
```

- Give every edge a deterministic `uid` (e.g. `caller_id + '>' + callee_id +
  str(valid_from)`) so closing it later is an exact lookup, not a scan.
- To clear a property: `SET n.prop = NULL` (FalkorDB Cypher has no property
  REMOVE).
- Batch ~500–1000 rows per query. Hundreds of single-row round-trips will
  make ingest crawl.
- Never concatenate user input into a query — always `CYPHER k=$v` params;
  the plan cache reuses parameterized queries.

## 5. The three core queries

**Time-slice** — "what did module X look like on T?" (the scrubber):

```cypher
CYPHER t=$t, mid=$mid
MATCH (m:Module {id: $mid})-[d:DEFINES]->(f)
WHERE m.valid_from <= $t AND (m.valid_to IS NULL OR m.valid_to > $t)
  AND d.valid_from <= $t AND (d.valid_to IS NULL OR d.valid_to > $t)
  AND f.valid_from <= $t AND (f.valid_to IS NULL OR f.valid_to > $t)
RETURN f.id, f.qualname, f.valid_from, f.valid_to
```

**Blast radius** — "what depends on function Y right now?":

```cypher
CYPHER t=$t, fid=$fid
MATCH (f:Function {id: $fid})<-[e:CALLS]-(caller:Function)
WHERE f.valid_from <= $t AND (f.valid_to IS NULL OR f.valid_to > $t)
  AND e.valid_from <= $t AND (e.valid_to IS NULL OR e.valid_to > $t)
  AND caller.valid_from <= $t AND (caller.valid_to IS NULL OR caller.valid_to > $t)
RETURN DISTINCT caller.id, caller.module
```

For 2-hop, use `(f)<-[e:CALLS*1..2]-(caller)` — keep depth bounded; the demo
question only needs 1–2 hops.

**History** — "who touched function Y, and what changed each time?":

```cypher
CYPHER fid=$fid
MATCH (p:Person)-[mod:MODIFIED]->(f:Function {id: $fid})
RETURN f.valid_from, f.valid_to, p.email, mod.change, mod.commit
ORDER BY f.valid_from
```

No time filter here — the whole point is the full life. Follow the
`RENAMED_TO` edge to stitch a move into one story.

## 6. Gotchas that would cost you an hour

1. MERGE on `id` alone corrupts the model — always `(id, valid_from)`.
2. No regex operator — use `STARTS WITH` / `ENDS WITH` / `CONTAINS`.
3. No label disjunctions: `(n:Function|Class)` is unsupported — query each
   label separately and `UNION` the results.
4. Index creation is async — confirm with `GRAPH.EXPLAIN` before blaming the
   query.
5. Edge windows are real — a CALLS edge whose window closed must be excluded,
   or "who depended on this in March?" lies.
6. The demo's killer line stays: "the agent never reads the snapshot JSON —
   every fact comes from a graph query."

## 7. Sanity checks before calling it done

- Time-slice at a known commit vs `git show <sha>:<file>` — must match.
- Open versions at now == current parser output for the demo repo.
- `GRAPH.EXPLAIN` on the time-slice query shows `Index Scan`.

Sources: FalkorDB docs `/cypher/cypher-support`, `/cypher/cheat-sheet`,
`/cypher/index` (indexing FAQ), and the falkordb-py client README — all
checked 2026-10-16.
