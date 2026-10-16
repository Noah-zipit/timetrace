"""timetrace.graph.db — FalkorDB connection plumbing.

Connection is a pure .env swap: FALKORDB_HOST / FALKORDB_PORT /
FALKORDB_USER|USERNAME / FALKORDB_PASSWORD|PASS are read from the timetrace
.env (same loader pattern as timetrace.ingest.cli), with real OS
environment variables taking precedence over the file. When no keys are
present anywhere, the local default 127.0.0.1:6380 with no auth is used.

Load-bearing rule: the agent path MUST use ro_query for every read.
``ro_graph()`` returns a handle that physically cannot run a write
(``GRAPH.RO_QUERY`` rejects writes server-side), so "unplug FalkorDB and
the agent answers nothing" holds by construction. ``require_db()`` raises
a clear 503-style error when the instance is unreachable, so failure is
honest, never silent.

The password is never printed, logged, or included in exceptions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6380
DEFAULT_USER = "falkordb"
DEFAULT_GRAPH = "timetrace"


def _dotenv_path() -> str:
    """timetrace/ root .env, resolved from this file's location."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "..", ".env"))


def load_dotenv(path: Optional[str] = None) -> dict[str, str]:
    """Parse a KEY=VALUE .env file. Same pattern as ingest.cli._load_env."""
    env: dict[str, str] = {}
    path = path or _dotenv_path()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    return env


@dataclass(frozen=True)
class FalkorConfig:
    """Resolved connection settings. ``password`` is redacted from repr."""

    host: str
    port: int
    username: str
    password: Optional[str]
    graph: str

    def __repr__(self) -> str:  # never leak the secret
        return (
            f"FalkorConfig(host={self.host!r}, port={self.port!r}, "
            f"username={self.username!r}, password=<redacted>, "
            f"graph={self.graph!r})"
        )

    @property
    def has_auth(self) -> bool:
        return bool(self.password)


def get_config(dotenv_path: Optional[str] = None) -> FalkorConfig:
    """Resolve config.

    Default call (no explicit path): OS env (non-empty) > repo .env > local
    defaults. ``FALKORDB_LOCAL=1`` skips the .env entirely (dev flow).
    ``FALKORDB_PASSWORD=""`` in the env is an explicit "no auth" and must
    NOT fall through to a password stored in the .env.

    Explicit ``dotenv_path`` (tests, fixtures): the file wins over ambient
    OS env, so a pinned fixture file is deterministic.

    Accepts both FALKORDB_USER/FALKORDB_PASSWORD and the variant spellings
    FALKORDB_USERNAME/FALKORDB_PASS found in the cloud .env.
    """
    explicit = dotenv_path is not None
    skip_file = os.environ.get("FALKORDB_LOCAL") == "1"
    file_env = {} if skip_file else load_dotenv(dotenv_path)

    def from_file(*keys: str) -> Optional[str]:
        for key in keys:
            v = file_env.get(key)
            if v:
                return v
        return None

    def from_env(*keys: str) -> Optional[str]:
        for key in keys:
            if key in os.environ:
                v = os.environ[key]
                if v:
                    return v
                if key in ("FALKORDB_PASSWORD", "FALKORDB_PASS"):
                    return ""  # explicit clear: no auth
                return _ABSENT
        return _ABSENT

    _ABSENT: Any = object()  # marker: no source provided this key

    def pick(*keys: str, default: Optional[str] = None) -> Optional[str]:
        # "" is a real answer (explicit no-auth), not a miss — compare
        # against the marker, never truthiness.
        if explicit:
            v = from_file(*keys)
            if v is not None:
                return v
            v = from_env(*keys)
            return default if v is _ABSENT else v
        v = from_env(*keys)
        if v is not _ABSENT:
            return v
        v = from_file(*keys)
        return default if v is None else v

    host = pick("FALKORDB_HOST", default=DEFAULT_HOST)
    port = int(pick("FALKORDB_PORT", default=str(DEFAULT_PORT)) or DEFAULT_PORT)
    username = pick("FALKORDB_USER", "FALKORDB_USERNAME",
                    default=DEFAULT_USER) or DEFAULT_USER
    password = pick("FALKORDB_PASSWORD", "FALKORDB_PASS")
    graph = pick("FALKORDB_GRAPH", default=DEFAULT_GRAPH) or DEFAULT_GRAPH
    return FalkorConfig(host=host, port=port, username=username,
                        password=password, graph=graph)


class DatabaseUnavailableError(RuntimeError):
    """Raised when FalkorDB is unreachable. status_code is for the API.

    Never includes the password in the message.
    """

    status_code = 503

    def __init__(self, config: FalkorConfig, detail: str = "") -> None:
        hostport = f"{config.host}:{config.port}"
        msg = f"FalkorDB unreachable at {hostport} (graph '{config.graph}')."
        if detail:
            msg += f" {detail}"
        super().__init__(msg)


class _ClientCache:
    """One client per (host, port, username, graph); never stores secrets
    in a loggable place — the password lives only on the client object."""

    _cache: dict[tuple, Any] = {}

    @classmethod
    def get(cls, config: FalkorConfig) -> Any:
        from falkordb import FalkorDB

        key = (config.host, config.port, config.username, config.graph)
        client = cls._cache.get(key)
        if client is None:
            kwargs: dict[str, Any] = {"host": config.host,
                                      "port": config.port}
            if config.has_auth:
                kwargs["username"] = config.username
                kwargs["password"] = config.password
            client = FalkorDB(**kwargs)
            cls._cache[key] = client
        return client

    @classmethod
    def clear(cls) -> None:
        cls._cache.clear()


@dataclass
class GraphHandle:
    """Writable handle (ingest path only)."""

    client: Any
    graph: Any
    config: FalkorConfig


class ReadOnlyGraph:
    """Read-only facade: the only way to run a query is ro_query.

    This is the load-bearing rule made structural — any code holding this
    handle cannot mutate the graph even by accident, because the server
    rejects writes on the RO path.
    """

    def __init__(self, graph: Any, config: FalkorConfig) -> None:
        self._graph = graph
        self.config = config

    def ro_query(self, query: str, params: Optional[dict] = None):
        """Run a read-only Cypher query. Writes raise server-side."""
        return self._graph.ro_query(query, params or {})

    @property
    def name(self) -> str:
        return self.config.graph


def get_graph(dotenv_path: Optional[str] = None) -> GraphHandle:
    """Writable client + selected graph. Ingest path only."""
    config = get_config(dotenv_path)
    client = _ClientCache.get(config)
    return GraphHandle(client=client,
                       graph=client.select_graph(config.graph),
                       config=config)


def ro_graph(dotenv_path: Optional[str] = None) -> ReadOnlyGraph:
    """Read-only graph handle. The agent path MUST use this for every read."""
    config = get_config(dotenv_path)
    client = _ClientCache.get(config)
    return ReadOnlyGraph(client.select_graph(config.graph), config)


def require_db(dotenv_path: Optional[str] = None) -> ReadOnlyGraph:
    """Ping the instance; return the read-only handle or raise honestly.

    Raises:
        DatabaseUnavailableError: 503-style, never silent, never leaks
            the password.
    """
    config = get_config(dotenv_path)
    try:
        client = _ClientCache.get(config)
        graph = client.select_graph(config.graph)
        graph.ro_query("RETURN 1 AS ok")
    except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
        raise DatabaseUnavailableError(config) from exc
    return ReadOnlyGraph(graph, config)


__all__ = [
    "DEFAULT_GRAPH", "DEFAULT_HOST", "DEFAULT_PORT",
    "DatabaseUnavailableError", "FalkorConfig", "GraphHandle",
    "ReadOnlyGraph", "get_config", "get_graph", "load_dotenv",
    "require_db", "ro_graph",
]
