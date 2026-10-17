# Vercel serverless entry point for the timetrace live demo.
#
# Vercel's Python runtime auto-detects the ASGI `app` exported here.
# Everything (FastAPI + the temporal graph agent + static UI) runs as one
# function; connection comes from the FALKORDB_* env vars set on the
# Vercel project (same pure-.env-swap as local).

from timetrace.api.main import app
