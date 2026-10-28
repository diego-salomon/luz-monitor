#!/bin/bash
# Instala (o reinstala) la tarea recurrente del monitor de luz con launchd.
# Se ejecuta cada día a las 9:30; el propio script decide si toca el informe mensual
# o nada. Si el Mac está dormido a esa hora, corre al despertar.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.diego.luzgas"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -f "$DIR/config.json" ]; then
  echo "Falta config.json. Haz primero los pasos 1 a 4 del README." >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$DIR/logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$DIR/luzgas.py</string>
    <string>--modo</string>
    <string>auto</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>30</integer></dict>
  <key>StandardOutPath</key><string>$DIR/logs/luzgas.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/luzgas.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Instalado. Se ejecutará cada día a las 9:30 (modo auto)."
echo "Log: $DIR/logs/luzgas.log"
echo "Para quitarlo: launchctl bootout gui/\$(id -u)/$LABEL && rm \"$PLIST\""
