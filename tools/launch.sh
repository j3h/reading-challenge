#!/bin/bash
# Starts Reading Challenge (or finds it already running) and opens it in the browser.
# The "Reading Challenge" app made by tools/setup_mac.sh runs this; nobody needs to run it by hand.
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PORT=8642
URL="http://127.0.0.1:$PORT/"
LOG="$HOME/Library/Logs/Reading Challenge.log"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

if curl -fsS --max-time 2 "${URL}ping" 2>/dev/null | grep -q rc-app; then
  open "$URL"; exit 0
fi
cd "$REPO" || exit 1
echo "--- $(date) starting" >>"$LOG"
# Fetch updates when this copy came from git and the network is there. Never asks for a password; on any
# problem it just runs the version already here.
if [ -d .git ]; then
  if GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND="ssh -o BatchMode=yes" git pull --ff-only -q >>"$LOG" 2>&1; then
    uv pip install --python .venv/bin/python -q -r requirements.txt >>"$LOG" 2>&1
  fi
fi
nohup .venv/bin/python -W ignore -m rc app --port "$PORT" --no-browser >>"$LOG" 2>&1 &
for _ in $(seq 1 60); do
  curl -fsS --max-time 1 "${URL}ping" >/dev/null 2>&1 && break
  sleep 0.25
done
open "$URL"
