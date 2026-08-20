#!/bin/bash
# Terminal Bench test entrypoint.  Evaluates ONLY /app/recovered.csv.
set -uo pipefail
python -m pytest -q /app/tests/test_outputs.py
