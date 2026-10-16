#!/usr/bin/env bash
# Start a LOCAL FalkorDB instance for timetrace dev/testing.
# redis-server + the FalkorDB module, listening on 127.0.0.1:6380, NO auth.
#
# Idempotent: if something is already listening on 127.0.0.1:6380, do nothing.
#
# Production is a pure .env swap: FALKORDB_HOST/FALKORDB_PORT/FALKORDB_USER/
# FALKORDB_PASSWORD point at the cloud instance instead (never commit them).
set -u

HOST=127.0.0.1
PORT=6380
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODULE="$DIR/.falkordb/falkordb.so"
BIN="$DIR/.falkordb/bin/redis-server"   # project-local redis 8.x (system apt has 7.x, too old for FalkorDB 4.22)
LOG="$DIR/.falkordb/redis-6380.log"
DATA="$DIR/.falkordb/data"

if (echo > /dev/tcp/$HOST/$PORT) 2>/dev/null; then
  echo "already listening on $HOST:$PORT - nothing to do"
  exit 0
fi

if [ ! -f "$MODULE" ]; then
  echo "ERROR: falkordb module not found at $MODULE" >&2
  echo "Download it first:" >&2
  echo "  curl -L -o '$MODULE' https://github.com/FalkorDB/FalkorDB/releases/download/v4.22.1/falkordb-x64.so" >&2
  exit 1
fi

mkdir -p "$DATA"
if [ -x "$BIN" ]; then
  REDIS_SERVER="$BIN"
elif command -v redis-server >/dev/null 2>&1; then
  REDIS_SERVER="$(command -v redis-server)"
else
  echo "ERROR: no redis-server found" >&2; exit 1
fi

setsid "$REDIS_SERVER" \
  --port "$PORT" \
  --bind "$HOST" \
  --protected-mode yes \
  --save "" \
  --appendonly no \
  --dir "$DATA" \
  --loadmodule "$MODULE" \
  >"$LOG" 2>&1 < /dev/null &

for i in $(seq 1 30); do
  if (echo > /dev/tcp/$HOST/$PORT) 2>/dev/null; then
    echo "FalkorDB module listening on $HOST:$PORT (log: $LOG)"
    python3 - "$HOST" "$PORT" <<'EOF'
import socket, sys
s = socket.create_connection((sys.argv[1], int(sys.argv[2])), 5)
s.sendall(b"*2\r\n$9\r\nGRAPH.LIST\r\n$1\r\n*\r\n")
resp = s.recv(64)
print("module check: GRAPH.LIST answered:", resp.decode(errors='replace').strip()[:40])
EOF
    exit 0
  fi
  sleep 0.5
done

echo "ERROR: server did not come up on $HOST:$PORT - see $LOG" >&2
tail -20 "$LOG" >&2
exit 1
