#!/bin/bash
# Stop and remove the local job-scraper runner's background job.
# Keeps ~/.job-scraper (config, logs, backups) unless you pass --purge.
set -euo pipefail

LABEL="com.murat.job-scraper-runner"
HOME_DIR="${JOB_SCRAPER_HOME:-$HOME/.job-scraper}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
rm -f "$PLIST"
echo "Background job removed."

if [ "${1:-}" = "--purge" ]; then
  rm -rf "$HOME_DIR"
  echo "Deleted $HOME_DIR (including its clone, logs and backups)."
else
  echo "Left $HOME_DIR in place (use --purge to delete it)."
fi
