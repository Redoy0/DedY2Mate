#!/usr/bin/env bash
# One-time setup: creates .venv and installs dependencies. Ubuntu/Debian.
set -euo pipefail
cd "$(dirname "$0")"

if ! python3 -m venv .venv 2>/dev/null; then
  rm -rf .venv
  echo "Python venv support is missing. Install it with:"
  echo "  sudo apt update && sudo apt install -y python3-venv python3-pip ffmpeg"
  exit 1
fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install --upgrade -r requirements.txt
# Optional: Deno JS runtime from PyPI (YouTube needs a JS runtime; Node.js also works).
.venv/bin/python -m pip install --upgrade deno || echo "Note: deno wheel not available here - install Node.js 20+ instead."

command -v ffmpeg >/dev/null || echo "Note: no system ffmpeg - using the bundled imageio-ffmpeg build (sudo apt install ffmpeg is recommended)."

[ -f videos.txt ] || cp videos.example.txt videos.txt
echo
.venv/bin/python -m ytbulk --check
echo "Setup complete. Put your URLs in videos.txt, then run: ./ytbulk.sh"
