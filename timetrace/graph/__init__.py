"""timetrace.graph — FalkorDB connection + temporal query layer.

Load-bearing rule: every read in this package (and therefore every fact
the agent reports) goes through ro_query only. The writable handle is
exposed solely for the ingest path.
"""
