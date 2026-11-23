#!/usr/bin/env bash
# Runs the tool from its virtualenv. All arguments are passed through.
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ ! -x "$DIR/.venv/bin/python" ]; then
  echo "Not set up yet - run $DIR/setup.sh first." >&2
  exit 2
fi
PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}" exec "$DIR/.venv/bin/python" -m ytbulk "$@"
