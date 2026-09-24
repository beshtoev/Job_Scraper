#!/bin/bash
# Install the local job-scraper runner as a background job on this Mac.
# What it does and why: docs/local-runner.md.  Undo it: scripts/uninstall_local_runner.sh
#
# Creates (all under your home folder, outside iCloud):
#   ~/.job-scraper/venv     Python 3.10+ with the scrapers' dependencies
#   ~/.job-scraper/repo     a disposable clone the scrapers run in
#   ~/.job-scraper/config.json, state.json, logs/
#   ~/Library/LaunchAgents/com.murat.job-scraper-runner.plist   (runs every 5 minutes)
#
# If no Python 3.10+ is found it installs Python 3.11 (about 40 MB from GitHub's
# python-build-standalone releases, via the `uv` tool) into ~/.job-scraper/python.
set -euo pipefail

LABEL="com.murat.job-scraper-runner"
HOME_DIR="${JOB_SCRAPER_HOME:-$HOME/.job-scraper}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
REPO_URL="https://github.com/beshtoev/Job_Scraper.git"
DASHBOARD_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TICK_SECONDS=300
START=1

while [ $# -gt 0 ]; do
  case "$1" in
    --dashboard-dir) DASHBOARD_DIR="$2"; shift 2 ;;
    --repo-url)      REPO_URL="$2"; shift 2 ;;
    --no-start)      START=0; shift ;;
    -h|--help)       sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
[ -d "$DASHBOARD_DIR/output" ] || { say "No output/ folder in $DASHBOARD_DIR - pass --dashboard-dir."; exit 1; }
command -v git >/dev/null || { say "git is required."; exit 1; }
[ "$(uname)" = "Darwin" ] || { say "This installer is for macOS (launchd)."; exit 1; }

mkdir -p "$HOME_DIR/logs" "$HOME/Library/LaunchAgents"

# 1. The disposable clone the scrapers run in
if [ ! -d "$HOME_DIR/repo/.git" ]; then
  say "Cloning $REPO_URL ..."
  git clone --quiet --depth 1 "$REPO_URL" "$HOME_DIR/repo"
fi
git -C "$HOME_DIR/repo" config user.name  "Job Scraper (local runner)"
git -C "$HOME_DIR/repo" config user.email "job-scraper-local@users.noreply.github.com"

# 2. Python 3.10+ and dependencies
VENV="$HOME_DIR/venv"
pick_python() {
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    p="$(command -v "$c" 2>/dev/null)" || continue
    if "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then echo "$p"; return 0; fi
  done
  return 1
}
if [ ! -x "$VENV/bin/python" ]; then
  if PY="$(pick_python)"; then
    say "Creating Python environment with $PY ..."
    "$PY" -m venv "$VENV"
    "$VENV/bin/python" -m pip install --quiet --upgrade pip
    "$VENV/bin/python" -m pip install --quiet -r "$HOME_DIR/repo/requirements.txt"
  else
    say "No Python 3.10+ found: installing Python 3.11 (about 40 MB) with uv into $HOME_DIR/python ..."
    "$(command -v python3)" -m venv "$HOME_DIR/bootstrap"
    "$HOME_DIR/bootstrap/bin/python" -m pip install --quiet --upgrade pip uv
    UV_PYTHON_INSTALL_DIR="$HOME_DIR/python" "$HOME_DIR/bootstrap/bin/uv" venv --quiet --python 3.11 "$VENV"
    "$HOME_DIR/bootstrap/bin/uv" pip install --quiet --python "$VENV/bin/python" -r "$HOME_DIR/repo/requirements.txt"
  fi
fi
"$VENV/bin/python" -c 'import jobspy' 2>/dev/null || { say "python-jobspy did not install; see $HOME_DIR"; exit 1; }

# 3. Config (an existing one keeps its own intervals)
"$VENV/bin/python" - "$HOME_DIR/config.json" "$DASHBOARD_DIR" "$VENV/bin/python" "$REPO_URL" <<'PY'
import json, pathlib, sys
path, dashboard, python, url = sys.argv[1:5]
p = pathlib.Path(path)
cfg = json.loads(p.read_text()) if p.exists() else {}
cfg.update({"dashboard_dir": dashboard, "python": python, "repo_url": url})
p.write_text(json.dumps(cfg, indent=2) + "\n")
PY

# 4. The background job. git pushes authenticate through the gh login (see ~/.gitconfig), so gh's
#    folder must be on the job's PATH.
GH_DIR=""; command -v gh >/dev/null 2>&1 && GH_DIR="$(dirname "$(command -v gh)"):"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV/bin/python</string>
    <string>$HOME_DIR/repo/scripts/local_runner.py</string>
    <string>tick</string>
  </array>
  <key>StartInterval</key><integer>$TICK_SECONDS</integer>
  <key>RunAtLoad</key><true/>
  <key>WorkingDirectory</key><string>$HOME_DIR</string>
  <key>StandardOutPath</key><string>$HOME_DIR/logs/runner.log</string>
  <key>StandardErrorPath</key><string>$HOME_DIR/logs/runner.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>${GH_DIR}/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin</string>
    <key>HOME</key><string>$HOME</string>
    <key>JOB_SCRAPER_HOME</key><string>$HOME_DIR</string>
  </dict>
</dict>
</plist>
PLIST
plutil -lint "$PLIST" >/dev/null

if [ "$START" = "1" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  launchctl enable "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  say "Started. The first LinkedIn scrape begins now."
else
  say "Installed but not started (--no-start). Start with: launchctl bootstrap gui/$(id -u) $PLIST"
fi

say ""
say "Check on it:   $VENV/bin/python $HOME_DIR/repo/scripts/local_runner.py status"
say "Watch the log: tail -f $HOME_DIR/logs/runner.log"
say "Remove it:     $HOME_DIR/repo/scripts/uninstall_local_runner.sh"
