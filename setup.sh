#!/usr/bin/env bash
# WorkPulse — one-command install for macOS.
#   ./setup.sh
# Creates a virtualenv, installs WorkPulse, and sets up the background agents.
set -euo pipefail

cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3.11+ is required but '$PY' was not found." >&2
  echo "Install it from https://www.python.org/downloads/ and re-run." >&2
  exit 1
fi

echo "==> Creating virtualenv (.venv)"
"$PY" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Installing WorkPulse (this can take a minute)"
python -m pip install --upgrade pip >/dev/null
pip install -e ".[mac]"

echo "==> Setting up config, database, and background agents"
workpulse install

cat <<'EOF'

------------------------------------------------------------
WorkPulse is installed. To open the dashboard now:

    source .venv/bin/activate
    workpulse web        # then visit http://127.0.0.1:5700

The background sensors and hourly jobs are already running via launchd.
Check anytime with:  workpulse status
------------------------------------------------------------
EOF
