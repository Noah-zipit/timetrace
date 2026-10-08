# timetrace — Design constraints

## Foundation scope (now)

- Parser: turn Python source files into the IR below (modules, classes,
  functions, defines/calls/imports edges).
- Sampler: walk a git repo's history, run the parser per commit, write one
  snapshot JSON per commit plus a chronological `manifest.json`.
- API skeleton: FastAPI with `/api/health` live; every other endpoint is a
  501 stub that names its event-week build in the response body.
- UI shell: static placeholder page, served by the API.

## Non-goals for foundation

- **No graph database code of any kind.** No FalkorDB imports, no Cypher, no
  graph clients — not even in `requirements.txt`. The graph layer is the
  event-week build (Oct 15–18); judges want to see it built live.
- **No final visual design.** The visual reference comes from Ashar before the
  polish pass. Foundation UI is a plain shell only.
- No deployed demo yet; no agent logic; no Cypher anywhere.

## Workstream ownership map

| Path                    | Owner / workstream         |
|-------------------------|----------------------------|
| `timetrace/parser/`     | parser workstream          |
| `timetrace/sampler/`    | sampler workstream         |
| `timetrace/api/`        | api workstream             |
| `ui/`                   | UI workstream (separate)   |
| `docs/ARCHITECTURE.md`  | docs workstream            |
| `docs/data-model.md`    | docs workstream (placeholder for now) |
| `tests/`                | each workstream with its code |

## Runtime

- Python 3.10+
- `requirements.txt` pins FastAPI, uvicorn, pytest only.

## Stub-endpoint rule

Every stub endpoint returns HTTP 501 with a body of the form:

```json
{"detail": "NOT IMPLEMENTED — event week: <what gets built>"}
```

The `<what>` part names the event-week build precisely (e.g. "snapshot
loading into FalkorDB", "graph-backed question answering", "timeline playback
from the graph").

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
