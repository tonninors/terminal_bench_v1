FROM python:3.11-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends sqlite3 xxd ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "pytest==8.3.4"

WORKDIR /app

# The two crash artifacts are the only task input.  Both are produced from
# scratch by build/generate_case.py; see MANIFEST.md.
COPY artifacts/ledger.db      /app/ledger.db
COPY artifacts/ledger.db-wal  /app/ledger.db-wal

# Pristine copies, so that accidentally opening the originals with a database
# tool does not destroy the evidence.  They are byte-identical to the files
# above and reveal nothing extra.
RUN mkdir -p /app/evidence \
 && cp /app/ledger.db /app/evidence/ledger.db \
 && cp /app/ledger.db-wal /app/evidence/ledger.db-wal \
 && chmod 0444 /app/evidence/ledger.db /app/evidence/ledger.db-wal

CMD ["/bin/bash", "-l"]
