"""CLI: python -m timetrace.sampler.cli <repo_path> [--mode weekly|every] [--n N] [--out out_dir]"""

from __future__ import annotations

import argparse
import json

from .sampler import sample_repo


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Sample a git repo's history into timetrace IR snapshots."
    )
    ap.add_argument("repo_path", help="path to the git repository")
    ap.add_argument("--mode", choices=["weekly", "every"], default="weekly")
    ap.add_argument("--n", type=int, default=1, help="step for mode=every")
    ap.add_argument("--out", default="snapshots", help="output directory")
    args = ap.parse_args()

    result = sample_repo(args.repo_path, mode=args.mode, n=args.n, out_dir=args.out)
    for entry in result["manifest"]:
        snap_path = f"{args.out}/{entry['commit']}.json"
        with open(snap_path, encoding="utf-8") as fh:
            snap = json.load(fh)
        print(
            f"{entry['commit'][:12]} {entry['timestamp']} "
            f"{len(snap['nodes'])} nodes / {len(snap['edges'])} edges"
        )
    print(f"manifest: {result['count']} snapshots -> {args.out}/manifest.json")


if __name__ == "__main__":
    main()
