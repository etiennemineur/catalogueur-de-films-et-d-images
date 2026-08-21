#!/usr/bin/env bash
set -euo pipefail

LABEL="com.etiennemineur.films.site"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
ROOT="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$ROOT/ouvrir_site.command</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>WorkingDirectory</key>
  <string>$ROOT</string>
  <key>StandardOutPath</key>
  <string>$ROOT/site_films_launchagent.log</string>
  <key>StandardErrorPath</key>
  <string>$ROOT/site_films_launchagent.err.log</string>
</dict>
</plist>
PLIST

launchctl unload "$PLIST" >/dev/null 2>&1 || true
launchctl load "$PLIST"

printf 'Lancement automatique installé.\n'
printf 'Le site sera lancé à chaque ouverture de session Mac.\n'
printf 'Page : http://localhost:8002/accueil.html\n'
printf '\nPour désinstaller :\n'
printf 'launchctl unload %s && rm %s\n' "$PLIST" "$PLIST"
read -r -p 'Appuyez sur Entrée pour fermer.' _
