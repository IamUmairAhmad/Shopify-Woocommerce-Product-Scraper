#!/bin/bash
# Double-click this file to start the Shopify Product Exporter on a Mac.
# First run sets everything up (about a minute). After that it starts in seconds
# and opens the tool in Chrome. Close this window, or press Control+C, to stop it.

cd "$(dirname "$0")" || exit 1

pause_and_exit() {
  echo
  read -r -p "Press Return to close this window." _
  exit 1
}

echo "Shopify Product Exporter"
echo "------------------------"

# 1. Find a working Python 3.9+. Macs without developer tools have a placeholder
#    /usr/bin/python3 that only pops up an install dialog, so it is handled separately.
PY=""
good_python() {
  [ -x "$1" ] && "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1
}
for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/Current/bin/python3; do
  if good_python "$candidate"; then PY="$candidate"; break; fi
done
if [ -z "$PY" ]; then
  PATH_PY="$(command -v python3 2>/dev/null)"
  if [ "$PATH_PY" = "/usr/bin/python3" ] && command -v xcode-select >/dev/null 2>&1 \
      && ! xcode-select -p >/dev/null 2>&1; then
    xcode-select --install >/dev/null 2>&1
    echo "Your Mac needs Apple's developer tools (they include Python)."
    echo
    echo "A dialog should have appeared. Click Install, agree, and wait until it says"
    echo "the installation finished (it can take 5-15 minutes)."
    echo "Then double-click launch-mac.command again."
    echo
    echo "Prefer not to wait? Install Python from https://www.python.org/downloads/"
    echo "(download the macOS installer, open it, finish it), then double-click this file again."
    pause_and_exit
  elif [ -n "$PATH_PY" ] && good_python "$PATH_PY"; then
    PY="$PATH_PY"
  else
    echo "Python 3.9 or newer was not found."
    echo "Download it from https://www.python.org/downloads/ (macOS installer), install it,"
    echo "then double-click this file again."
    pause_and_exit
  fi
fi
echo "Using Python: $("$PY" --version 2>&1)"

# 2. One-time setup: private environment plus dependencies.
if [ ! -x ".venv/bin/python" ]; then
  echo "First run: creating environment..."
  "$PY" -m venv .venv || { echo "Could not create the Python environment."; pause_and_exit; }
fi
if [ ! -f ".venv/.deps-installed" ] || [ "requirements.txt" -nt ".venv/.deps-installed" ]; then
  echo "Installing dependencies (needs internet)..."
  .venv/bin/python -m pip install --quiet --upgrade pip
  if ! .venv/bin/python -m pip install --quiet -r requirements.txt; then
    echo "Installing dependencies failed. Check your internet connection and try again."
    pause_and_exit
  fi
  touch ".venv/.deps-installed"
fi

# 3. Pick a free port (5000 is often taken by AirPlay on macOS).
PORT=$(.venv/bin/python - <<'EOF'
import socket
for port in range(5001, 5100):
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            continue
        print(port)
        break
EOF
)
if [ -z "$PORT" ]; then
  echo "Could not find a free port."
  pause_and_exit
fi
URL="http://127.0.0.1:$PORT"

# 4. Open the browser as soon as the server answers.
(
  for _ in $(seq 1 60); do
    if curl -s -o /dev/null "$URL"; then
      open -a "Google Chrome" "$URL" 2>/dev/null || open "$URL"
      exit 0
    fi
    sleep 0.5
  done
) &

echo
echo "Starting at $URL"
echo "Keep this window open while you use the tool. Close it to stop."
echo

PORT="$PORT" .venv/bin/python app.py
echo
echo "The exporter has stopped."
pause_and_exit
