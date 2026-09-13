#!/usr/bin/env bash
set -euo pipefail
if [[ $# -lt 3 ]]; then
  echo "usage: $0 CONFIG STATE_DIR OUTPUT.tar.gz" >&2
  exit 2
fi
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$ROOT/src"
exec python3 -m asterun --config "$1" --state-dir "$2" state-backup --output "$3"
