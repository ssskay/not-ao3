#!/bin/bash
# Double-click on a Mac: builds the archive from shelf/ (or the demo shelf) and opens it in your browser.
# It rebuilds by itself when you add files, and stops after 3 hours with no page loads.
cd "$(dirname "$0")"
PY=$(command -v python3 || echo /usr/bin/python3)
"$PY" -c "import markdown, pypdf" 2>/dev/null || "$PY" -m pip install --user --quiet markdown pypdf 2>/dev/null \
  || "$PY" -m pip install --quiet --break-system-packages markdown pypdf
exec "$PY" notao3.py serve
