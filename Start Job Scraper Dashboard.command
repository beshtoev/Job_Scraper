#!/bin/zsh
# Double-click to serve the triage dashboard and open it.
# triage.html reads its job data over HTTP, so opening the file directly in a
# browser shows an empty dashboard. It has to be served, which is all this does.
cd "${0:A:h}" || exit 1

PORT=8080
URL="http://127.0.0.1:$PORT/triage.html"

# If something is already on the port, only reuse it when it is this same
# dashboard. Never kill an unrelated application that happens to hold the port.
RUNNING=""
for PID in $(/usr/sbin/lsof -tiTCP:$PORT -sTCP:LISTEN 2>/dev/null); do
  COMMAND=$(/bin/ps -p "$PID" -o command= 2>/dev/null || true)
  if [[ "$COMMAND" == *"http.server"* ]]; then
    RUNNING="$PID"
  else
    echo "Port $PORT is being used by another application:"
    echo "  $COMMAND"
    echo "Stop that application and run this again."
    echo
    read "?Press Return to close."
    exit 1
  fi
done

if [[ -n "$RUNNING" ]]; then
  echo "Dashboard is already running (pid $RUNNING)."
  echo "Opening $URL"
  /usr/bin/open "$URL" 2>/dev/null || true
  echo
  read "?Press Return to close."
  exit 0
fi

echo "Job Scraper Dashboard"
echo "Dashboard: $URL"
echo "Serving:   $PWD"
echo "Keep this window open while you use the dashboard. Ctrl-C stops it."
echo

# Give the server a moment to bind, then open the browser.
( sleep 1; /usr/bin/open "$URL" 2>/dev/null || true ) &

exec /usr/bin/python3 -m http.server "$PORT" --bind 127.0.0.1
