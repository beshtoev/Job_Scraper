#!/bin/zsh
# Double-click once. After this the triage dashboard is always running at
# http://127.0.0.1:8080/triage.html, starts when you sign in, and restarts
# itself if it ever dies. Run it again any time to reinstall or repair.
set -e

TOOL_DIR="${0:A:h}"
LABEL="com.murat.job-scraper-dashboard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/JobScraperDashboard.log"
PORT=8080

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

# Replace only an older copy of this same dashboard. Refuse to disturb an
# unrelated application that happens to use the same port.
for PID in $(/usr/sbin/lsof -tiTCP:$PORT -sTCP:LISTEN 2>/dev/null); do
  COMMAND=$(/bin/ps -p "$PID" -o command= 2>/dev/null || true)
  if [[ "$COMMAND" == *"http.server"* ]]; then
    /bin/kill "$PID" 2>/dev/null || true
  else
    echo "Port $PORT is being used by another application:"
    echo "  $COMMAND"
    echo "Stop that application and run this installer again."
    echo
    read "?Press Return to close."
    exit 1
  fi
done

# ProgramArguments[0] MUST be the CommandLineTools python, not /usr/bin/python3.
# The Apple stub at /usr/bin/python3 is the TCC-responsible binary when launchd
# spawns it, it can never hold a Full Disk Access grant, and this workspace lives
# in iCloud Drive. Getting this wrong is a silent "Operation not permitted".
# See Career/job-capture/Install Auto-Start.command, which has the same rule.
/bin/rm -f "$PLIST"
/usr/bin/plutil -create xml1 "$PLIST"
/usr/bin/plutil -insert Label -string "$LABEL" "$PLIST"
/usr/bin/plutil -insert ProgramArguments -json \
  "[\"/Library/Developer/CommandLineTools/usr/bin/python3\",\"-m\",\"http.server\",\"$PORT\",\"--bind\",\"127.0.0.1\"]" "$PLIST"
/usr/bin/plutil -insert WorkingDirectory -string "$TOOL_DIR" "$PLIST"
/usr/bin/plutil -insert RunAtLoad -bool true "$PLIST"
/usr/bin/plutil -insert KeepAlive -bool true "$PLIST"
/usr/bin/plutil -insert ProcessType -string Background "$PLIST"
/usr/bin/plutil -insert StandardOutPath -string "$LOG" "$PLIST"
/usr/bin/plutil -insert StandardErrorPath -string "$LOG" "$PLIST"

/bin/launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
/bin/launchctl bootstrap "gui/$UID" "$PLIST"
/bin/launchctl kickstart -k "gui/$UID/$LABEL"

# Confirm it actually serves the dashboard before claiming success.
sleep 2
CODE=$(/usr/bin/curl -s -o /dev/null -w "%{http_code}" --max-time 15 \
       "http://127.0.0.1:$PORT/triage.html" || echo 000)
echo
if [[ "$CODE" == "200" ]]; then
  echo "The dashboard now starts automatically when you sign in."
  echo "Dashboard: http://127.0.0.1:$PORT/triage.html"
  /usr/bin/open "http://127.0.0.1:$PORT/triage.html" 2>/dev/null || true
else
  echo "Installed, but the dashboard did not answer (HTTP $CODE)."
  echo "Check the log: $LOG"
  echo "An 'Operation not permitted' there means Full Disk Access is missing for"
  echo "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app"
  echo "(it shows as \"Python\" in System Settings > Privacy & Security)."
fi
echo "Log: $LOG"
echo "To remove it later: launchctl bootout gui/\$UID/$LABEL && rm \"$PLIST\""
echo
read "?Press Return to close."
