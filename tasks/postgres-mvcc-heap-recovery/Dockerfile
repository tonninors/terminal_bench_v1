FROM python:3.11-slim-bookworm

RUN apt-get update  && apt-get install -y --no-install-recommends xxd ca-certificates  && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "pytest==8.3.4"

WORKDIR /app

# The three task inputs.  They are produced by build/pg_fixture.py from a real
# PostgreSQL 16 server; see MANIFEST.md for what is and is not solver-facing.
COPY artifacts/heap_pages.bin    /app/heap_pages.bin
COPY artifacts/tx_status.csv     /app/tx_status.csv
COPY artifacts/table_schema.json /app/table_schema.json

# Read-only copies, so that a partial write or an in-place edit cannot destroy
# the evidence.  Byte-identical to the files above; they reveal nothing extra.
RUN mkdir -p /app/evidence  && cp /app/heap_pages.bin /app/tx_status.csv /app/table_schema.json /app/evidence/  && chmod 0444 /app/evidence/*

CMD ["/bin/bash", "-l"]
