#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYINSTALLER_PYTHON:-python3}"
"$PY" -m pip install --quiet "pyinstaller>=6.0" >/dev/null 2>&1 || { echo "!! install pyinstaller failed" >&2; exit 1; }
rm -rf dist
"$PY" -m PyInstaller --onefile --name kk-agent --paths src --hidden-import kk_agent --clean --distpath dist --workpath build src/kk_agent/__main__.py
BIN="dist/kk-agent"; [ -f "$BIN" ] || BIN="dist/kk-agent.exe"
[ -f "$BIN" ] || { echo "!! binary missing: $BIN" >&2; exit 1; }
echo ">> built: $BIN"
