FROM python:3.11-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends xxd ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "pytest==8.3.4"

WORKDIR /app

# The task inputs.  They are produced by build/pg_fixture.py from a real
# PostgreSQL 16 server; see MANIFEST.md for what is and is not solver-facing.
COPY artifacts/heap_pages.bin           /app/heap_pages.bin
COPY artifacts/ledger_entries_heap.bin  /app/ledger_entries_heap.bin
COPY artifacts/account_tags_heap.bin    /app/account_tags_heap.bin
COPY artifacts/table_schema.json        /app/table_schema.json
# The cluster's own (partially stale) commit log and subtransaction map, as
# raw SLRU segments under their real directory names.
COPY artifacts/pg_xact/                 /app/pg_xact/
COPY artifacts/pg_subtrans/             /app/pg_subtrans/

# /app holds exactly what the prompt names - nothing else, and no duplicate
# copies, so what the solver sees matches the prompt one for one.

CMD ["/bin/bash", "-l"]
