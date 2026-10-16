"""Laptop connection test for the FalkorDB Cloud 'timetrace' instance.

Run from YOUR laptop (the build VM cannot reach database ports):

    pip install falkordb
    python scripts/check_db.py

Reads creds from ../.env (FALKORDB_HOST / FALKORDB_PORT / FALKORDB_USER /
FALKORDB_PASSWORD). Green light = AUTH + PING + a real graph write/read
round-trip on a throwaway graph. Exits 0 on success, 1 on failure.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(os.path.dirname(HERE), ".env")


def load_env(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main() -> int:
    try:
        from falkordb import FalkorDB
    except ImportError:
        print("FAIL: the 'falkordb' package is not installed.")
        print("      run: pip install falkordb")
        return 1

    env = load_env(ENV_PATH)
    host = env.get("FALKORDB_HOST")
    port = int(env.get("FALKORDB_PORT", "6379"))
    user = env.get("FALKORDB_USER", "falkordb")
    password = env.get("FALKORDB_PASSWORD")
    if not host or not password:
        print(f"FAIL: FALKORDB_HOST / FALKORDB_PASSWORD missing in {ENV_PATH}")
        return 1

    try:
        db = FalkorDB(host=host, port=port, username=user, password=password)
        g = db.select_graph("timetrace_probe")
        g.query("RETURN 1 AS ok")
        g.query("CREATE (:Probe {at: timestamp()})")
        n = g.ro_query(
            "MATCH (p:Probe) RETURN count(p) AS c").result_set[0][0]
        g.query("MATCH (p:Probe) DELETE p")
        print(f"GREEN LIGHT: AUTH + PING + write/read round-trip ok "
              f"(probes seen: {n}). The graph core can connect.")
        return 0
    except Exception as exc:  # noqa: BLE001 - want the raw message
        print(f"FAIL: {exc}")
        print("If this fails, the instance may need a recreate — "
              "flag it today, Day 1 is the day this must be green.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
