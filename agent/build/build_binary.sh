#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYINSTALLER_PYTHON:-python3}"
"$PY" -m pip install --quiet "pyinstaller>=6.0" >/dev/null 2>&1 || { echo "!! install pyinstaller failed" >&2; exit 1; }
rm -rf dist
# hidden-import：paho-mqtt / psutil 为第三方依赖，PyInstaller 对部分动态子模块
# 追踪不到，显式声明避免运行时 ImportError（G3）。
"$PY" -m PyInstaller --onefile --name kk-agent --paths src \
  --hidden-import kk_agent \
  --hidden-import paho.mqtt.client \
  --hidden-import paho.mqtt.packettypes \
  --hidden-import paho.mqtt.properties \
  --hidden-import paho.mqtt.reasoncodes \
  --hidden-import psutil \
  --clean --distpath dist --workpath build src/kk_agent/__main__.py
BIN="dist/kk-agent"; [ -f "$BIN" ] || BIN="dist/kk-agent.exe"
[ -f "$BIN" ] || { echo "!! binary missing: $BIN" >&2; exit 1; }
echo ">> built: $BIN"
