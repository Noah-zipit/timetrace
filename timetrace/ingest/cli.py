"""CLI: python -m timetrace.ingest.cli <snapshots_dir> [--repo NAME]

Reads the sampler's snapshots/ dir (manifest.json + <sha>.json) and writes
the temporal graph into FalkorDB. Connection details come from the
timetrace .env (FALKORDB_HOST / FALKORDB_PORT / FALKORDB_USER /
FALKORDB_PASSWORD); flags override.

Run this from your laptop — the build VM cannot reach database ports.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def _load_env(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Ingest timetrace snapshots into FalkorDB.")
    ap.add_argument("snapshots_dir", help="dir with manifest.json + <sha>.json")
    ap.add_argument("--repo", default=None, help="repo name (default: dir name)")
    ap.add_argument("--env", default=".env", help="path to .env with creds")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--graph", default="timetrace")
    ap.add_argument("--dry-run", action="store_true",
                    help="plan only; print op counts, write nothing")
    args = ap.parse_args()

    from timetrace.ingest import plan_ingest, FalkorWriter

    with open(os.path.join(args.snapshots_dir, "manifest.json"),
              encoding="utf-8") as fh:
        manifest = json.load(fh)
    snapshots = []
    for entry in manifest:
        with open(os.path.join(args.snapshots_dir, f"{entry['commit']}.json"),
                  encoding="utf-8") as fh:
            snapshots.append(json.load(fh))

    repo = args.repo or os.path.basename(
        os.path.abspath(args.snapshots_dir.rstrip("/")))
    ops = plan_ingest(repo, snapshots)
    counts: dict[str, int] = {}
    for op in ops:
        k = op["op"] + (":" + op.get("kind", op.get("label", ""))).rstrip(":")
        counts[k] = counts.get(k, 0) + 1
    print(f"planned {len(ops)} ops for {len(snapshots)} snapshots:")
    for k in sorted(counts):
        print(f"  {k}: {counts[k]}")

    if args.dry_run:
        return

    env = _load_env(args.env)
    host = args.host if args.host is not None else env.get("FALKORDB_HOST")
    port = (args.port if args.port is not None
            else int(env.get("FALKORDB_PORT", "6379")))
    user = (args.user if args.user is not None
            else env.get("FALKORDB_USER", env.get("FALKORDB_USERNAME",
                                                 "falkordb")))
    if args.password is not None:
        password = args.password  # "" = explicit no-auth (local instance)
    else:
        password = env.get("FALKORDB_PASSWORD", env.get("FALKORDB_PASS"))
    if not host:
        sys.exit("need FALKORDB_HOST (.env or --host)")

    writer = FalkorWriter(host, port, user, password, graph=args.graph)
    writer.create_indexes()
    written = writer.write_ops(ops)
    print("written:", json.dumps(written, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
