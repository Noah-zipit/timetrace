"""Connection test for a FalkorDB 'timetrace' instance.

Works two ways:
  * Local (default): 127.0.0.1:6380, no auth — the dev instance the infra
    agent stands up on this machine.
  * Cloud: creds from ../.env (FALKORDB_HOST / FALKORDB_PORT /
    FALKORDB_USER|USERNAME / FALKORDB_PASSWORD|PASS), as before.

Precedence: real environment variables (FALKORDB_HOST etc.) override the
.env file, which overrides the local defaults. Never prints the password.

    pip install falkordb
    python scripts/check_db.py

Green light = AUTH (when configured) + PING + a real graph write/read
round-trip on a throwaway graph. Exits 0 on success, 1 on failure.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(os.path.dirname(HERE), ".env")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6380
DEFAULT_USER = "falkordb"


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


def resolve_config() -> tuple[str, int, str, "str | None"]:
    """OS env > .env file > local defaults (127.0.0.1:6380, no auth).

    Set FALKORDB_LOCAL=1 to ignore the ../.env file entirely (handy when
    the .env holds production cloud creds but you want to test the local
    dev instance — the connection code path is identical either way).
    """
    file_env = {} if os.environ.get("FALKORDB_LOCAL", "").strip().lower() in (
        "1", "true", "yes") else load_env(ENV_PATH)

    def pick(*keys: str, default: "str | None" = None) -> "str | None":
        for key in keys:
            value = os.environ.get(key) or file_env.get(key)
            if value:
                return value
        return default

    host = pick("FALKORDB_HOST", default=DEFAULT_HOST) or DEFAULT_HOST
    port = int(pick("FALKORDB_PORT", default=str(DEFAULT_PORT))
               or DEFAULT_PORT)
    user = pick("FALKORDB_USER", "FALKORDB_USERNAME",
                default=DEFAULT_USER) or DEFAULT_USER
    password = pick("FALKORDB_PASSWORD", "FALKORDB_PASS")
    return host, port, user, password


def main() -> int:
    try:
        from falkordb import FalkorDB
    except ImportError:
        print("FAIL: the 'falkordb' package is not installed.")
        print("      run: pip install falkordb")
        return 1

    host, port, user, password = resolve_config()
    auth_note = "no-auth" if not password else f"user {user}"

    try:
        kwargs: dict = {"host": host, "port": port}
        if password:
            kwargs["username"] = user
            kwargs["password"] = password
        db = FalkorDB(**kwargs)
        g = db.select_graph("timetrace_probe")
        g.query("RETURN 1 AS ok")
        g.query("CREATE (:Probe {at: timestamp()})")
        n = g.ro_query(
            "MATCH (p:Probe) RETURN count(p) AS c").result_set[0][0]
        g.query("MATCH (p:Probe) DELETE p")
        print(f"GREEN LIGHT: {host}:{port} ({auth_note}) — AUTH + PING + "
              f"write/read round-trip ok (probes seen: {n}). "
              f"The graph core can connect.")
        return 0
    except Exception as exc:  # noqa: BLE001 - want the raw message
        print(f"FAIL: {host}:{port} ({auth_note}): {exc}")
        print("If this fails, the instance may need a recreate — "
              "flag it today, Day 1 is the day this must be green.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
