#!/bin/bash
# One-time setup on a Mac (about 5 minutes). Run it from Terminal inside this folder:
#     bash tools/setup_mac.sh
# It installs uv (Python for this app only), the app's libraries, and puts a "Reading Challenge" app in the
# Applications folder. Afterwards nobody needs Terminal: double-click the app.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (from astral.sh)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
echo "Setting up Python and the app's libraries..."
[ -d .venv ] || uv venv -q .venv
uv pip install --python .venv/bin/python -q -r requirements.txt
.venv/bin/python -c "import pymupdf, openpyxl, anthropic, keyring" || { echo "Library install failed"; exit 1; }

chmod +x tools/launch.sh
APP="$HOME/Applications/Reading Challenge.app"
mkdir -p "$HOME/Applications"
rm -rf "$APP"
LAUNCH="${REPO//\"/\\\"}/tools/launch.sh"
osacompile -o "$APP" -e "do shell script quoted form of \"$LAUNCH\" & \" > /dev/null 2>&1 &\""
echo
echo "Done. \"Reading Challenge\" is in $HOME/Applications (drag it to the Dock to keep it handy)."
echo "Open it, then in Settings choose the shared data folder and paste the Claude API key."
