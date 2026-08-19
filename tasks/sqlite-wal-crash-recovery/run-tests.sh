#!/bin/bash
# Terminal Bench test entrypoint.  Evaluates ONLY /app/recovered.db.
set -uo pipefail
python -m pytest -q /app/tests/test_outputs.py
