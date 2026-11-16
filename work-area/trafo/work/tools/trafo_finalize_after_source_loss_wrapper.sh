#!/bin/bash
set -uo pipefail

state=/mnt/volume/iss-archive-work/trafo/work/accession-runs/trafo-full-002/loss-resolution-001
runner=/mnt/volume/iss-archive-work/trafo/work/tools/trafo_finalize_after_source_loss.py

mkdir -p "$state"
/usr/bin/python3 -B "$runner"
code=$?
printf '%s\n' "$code" > "$state/runner.exit"
exit "$code"
