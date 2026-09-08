#!/usr/bin/env bash
# One-container entrypoint for a hosted deployment (Hugging Face Spaces / Render / Fly / a VPS):
# runs the mock bank on :5050 internally and the live console on $PORT publicly. Discovered
# capabilities are written to $CAPABILITIES_DIR — point that at a volume to persist them.
set -euo pipefail

# Hugging Face Spaces set SPACE_ID and expect the app on port 7860; everywhere else default 5055.
if [ -n "${SPACE_ID:-}" ]; then
  export PORT="${PORT:-7860}"
else
  export PORT="${PORT:-5055}"
fi
export TARGET_BASE="${TARGET_BASE:-http://localhost:5050}"
export CAPABILITIES_DIR="${CAPABILITIES_DIR:-/data/capabilities}"

mkdir -p "$CAPABILITIES_DIR"
# first boot on an empty volume: seed it with the capabilities shipped in the image
if [ -z "$(ls -A "$CAPABILITIES_DIR" 2>/dev/null || true)" ]; then
  cp /app/capabilities/*.json "$CAPABILITIES_DIR"/ 2>/dev/null || true
fi

# seed + start the mock bank in the background
( cd /app/app && python -c "import models; models.init_db(); models.seed()" && exec python app.py ) &

# wait for it to answer before starting the console
for _ in $(seq 1 40); do
  if python -c "import urllib.request; urllib.request.urlopen('http://localhost:5050/search').read()" 2>/dev/null; then
    break
  fi
  sleep 1
done

exec python -m webconsole.server
