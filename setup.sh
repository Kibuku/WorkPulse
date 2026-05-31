#!/usr/bin/env bash
# setup.sh — one-command WorkPulse install for macOS.
#
# Usage:
#   bash setup.sh                    # interactive (prompts for name, optional keys)
#   bash setup.sh --yes              # answer "yes" to all optional prompts
#   bash setup.sh --skip-secrets     # don't prompt for API key / SMTP
#   bash setup.sh --skip-launch      # don't ask to launch the tray at end
#   bash setup.sh --import-config X  # copy an existing config.yaml from elsewhere
#
# What it does:
#   1. Verify Python 3.12+ on PATH (or fail with a clear message)
#   2. Create a .venv and install requirements.txt + requirements-mac.txt
#   3. Bootstrap config/ files if missing (config.yaml, identity.yaml)
#   4. Offer to set up the API key and SMTP password (both fully skippable)
#   5. Install the LaunchAgent so the tray launches at every login
#   6. Tell the user about the macOS Accessibility-permission step
#
# Re-running setup.sh is safe — it only creates missing pieces.

set -euo pipefail

ROOT="$( cd -- "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd )"
cd "$ROOT"

YES=0
SKIP_SECRETS=0
SKIP_LAUNCH=0
IMPORT_CONFIG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --yes|-y)         YES=1 ;;
        --skip-secrets)   SKIP_SECRETS=1 ;;
        --skip-launch)    SKIP_LAUNCH=1 ;;
        --import-config)  IMPORT_CONFIG="${2:-}"; shift ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

step() { echo; echo "[$1] $2"; }
confirm_yn() {
    local msg="$1" default="${2:-y}"
    if [ "$YES" -eq 1 ]; then return 0; fi
    local hint="[Y/n]"; [ "$default" = "n" ] && hint="[y/N]"
    read -r -p "$msg $hint " ans
    [ -z "$ans" ] && ans="$default"
    [[ "$ans" =~ ^[Yy] ]]
}

# ── 1. Python ────────────────────────────────────────────────────────────────
step 1 "Checking for Python 3.12 or later..."
PYTHON=""
for cmd in python3.14 python3.13 python3.12 python3; do
    if command -v "$cmd" >/dev/null 2>&1; then
        ver=$("$cmd" --version 2>&1)
        if [[ "$ver" =~ Python\ 3\.([0-9]+) ]] && [ "${BASH_REMATCH[1]}" -ge 12 ]; then
            PYTHON="$cmd"
            echo "  Found: $ver  (using '$cmd')"
            break
        fi
    fi
done
if [ -z "$PYTHON" ]; then
    cat >&2 <<EOF
  ERROR: Python 3.12+ not found on PATH.

  Install with one of:
      brew install python@3.13          (recommended — uses Homebrew)
      https://www.python.org/downloads  (official installer)

  Then re-run: bash setup.sh
EOF
    exit 1
fi

# ── 2. Virtual environment + deps ────────────────────────────────────────────
step 2 "Setting up .venv and installing dependencies..."
if [ ! -x .venv/bin/python ]; then
    "$PYTHON" -m venv .venv
    echo "  Created .venv"
else
    echo "  .venv already exists; reusing"
fi
.venv/bin/pip install --upgrade pip --quiet
.venv/bin/pip install -r requirements.txt --quiet
echo "  Installed core dependencies"
.venv/bin/pip install -r requirements-mac.txt --quiet
echo "  Installed macOS dependencies (PyObjC)"

# ── 3. Config bootstrap ──────────────────────────────────────────────────────
step 3 "Bootstrapping config/..."
mkdir -p config

if [ ! -f config/config.yaml ]; then
    if [ -n "$IMPORT_CONFIG" ] && [ -f "$IMPORT_CONFIG" ]; then
        cp "$IMPORT_CONFIG" config/config.yaml
        echo "  Imported config from $IMPORT_CONFIG"
    elif [ -f config/config.example.yaml ]; then
        cp config/config.example.yaml config/config.yaml
        echo "  Created config/config.yaml from template (edit it to define your streams)"
    else
        echo "  WARNING: no config.example.yaml found; create config.yaml by hand"
    fi
else
    echo "  config/config.yaml already exists; leaving as-is"
fi

if [ ! -f config/identity.yaml ]; then
    actor_label="$USER"
    if [ "$YES" -eq 0 ]; then
        read -r -p "  Your name (free text, default: $USER): " input
        [ -n "$input" ] && actor_label="$input"
    fi
    org=""
    if [ "$YES" -eq 0 ]; then
        read -r -p "  Organization or team (optional, press Enter to skip): " org
    fi
    actor_id="wp-$(.venv/bin/python -c 'import secrets; print(secrets.token_hex(6))')"
    created_at=$(date +"%Y-%m-%dT%H:%M:%S%z" | sed 's/\(..\)$/:\1/')
    cat > config/identity.yaml <<EOF
# Identity file for this WorkPulse install. Local-only; never sent anywhere.
# The actor_id is reserved for the v3 Fingerprint Engine's attribution hash;
# it does NOT get stamped on raw activity logs.
actor_label:  "$actor_label"
organization: "$org"
hostname:     "$(hostname)"
created_at:   "$created_at"
actor_id:     "$actor_id"
EOF
    echo "  Created config/identity.yaml (actor_id: $actor_id)"
else
    echo "  config/identity.yaml already exists; leaving as-is"
fi

# ── 4. Optional secrets (SKIPPABLE — these are NOT required to use WorkPulse) ─
step 4 "Optional API key / email setup..."
echo "  WorkPulse runs fine without these. AI auto-tagging and email reports"
echo "  stay off until you configure them. You can add them anytime from"
echo "  the dashboard Settings page (top-right corner) — no terminal needed."

if [ "$SKIP_SECRETS" -eq 0 ]; then
    if confirm_yn "  Add an Anthropic API key now? (enables smart auto-tagging)" n; then
        echo "  Get one from: https://console.anthropic.com/settings/keys"
        read -r -s -p "  Paste your API key (input hidden): " key; echo
        if [ -n "$key" ]; then
            .venv/bin/python -c "import sys; sys.path.insert(0,'scripts'); import wp_secrets as s; s.set_('anthropic_key', sys.argv[1])" "$key"
            echo "  Saved."
        fi
    else
        echo "  Skipped. Add later from the dashboard."
    fi
    if confirm_yn "  Set up Gmail email reports now? (off by default)" n; then
        echo "  You need a Gmail App Password (not your normal password)."
        echo "  Create one at: https://myaccount.google.com/apppasswords"
        read -r -s -p "  Paste your App Password (input hidden): " smtp; echo
        if [ -n "$smtp" ]; then
            .venv/bin/python -c "import sys; sys.path.insert(0,'scripts'); import wp_secrets as s; s.set_('smtp_password', sys.argv[1])" "$smtp"
            echo "  Saved. Toggle email on from the dashboard Settings page."
        fi
    else
        echo "  Skipped. Add later from the dashboard."
    fi
else
    echo "  Skipped (--skip-secrets). Configure from the dashboard later."
fi

# ── 5. LaunchAgent (autostart at login) ──────────────────────────────────────
step 5 "Installing the LaunchAgent so WorkPulse launches at every login..."
bash "$ROOT/scripts/install_launchagent.sh"

# ── 6. Done ──────────────────────────────────────────────────────────────────
step 6 "Done."
cat <<EOF

WorkPulse is installed. Dashboard will be at http://127.0.0.1:5700/

╔════════════════════════════════════════════════════════════════════════╗
║  IMPORTANT macOS step you must do ONCE, by hand:                       ║
║                                                                        ║
║  Grant Accessibility permission so WorkPulse can read window titles.   ║
║                                                                        ║
║  1. Open: System Settings → Privacy & Security → Accessibility         ║
║  2. Click the [+] button                                               ║
║  3. Add this exact file:                                               ║
║       $ROOT/.venv/bin/python
║  4. Toggle it on.                                                      ║
║                                                                        ║
║  Without this, WorkPulse still tracks which app you're in — but        ║
║  window titles will be blank. Apple gates this by design; there is no  ║
║  way to grant it from a script.                                        ║
╚════════════════════════════════════════════════════════════════════════╝

EOF

if [ "$SKIP_LAUNCH" -eq 0 ] && confirm_yn "  Launch WorkPulse now?" y; then
    launchctl start com.workpulse.tray 2>/dev/null || true
    echo "  Launched. Look for the WorkPulse icon in your menu bar (top right)."
fi
