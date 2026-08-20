FROM python:3.11-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends xxd ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "pytest==8.3.4"

WORKDIR /app

# The three task inputs.  They are produced by build/pg_fixture.py from a real
# PostgreSQL 16 server; see MANIFEST.md for what is and is not solver-facing.
COPY artifacts/heap_pages.bin    /app/heap_pages.bin
COPY artifacts/tx_status.csv     /app/tx_status.csv
COPY artifacts/table_schema.json /app/table_schema.json

# /app holds exactly the three files the prompt names - nothing else, and no
# duplicate copies, so what the solver sees matches the prompt one for one.

CMD ["/bin/bash", "-l"]
