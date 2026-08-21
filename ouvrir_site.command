#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
PY="$ROOT/.venv/bin/python"
ANALYSE="$ROOT/analyse"
URL="http://127.0.0.1:8002/accueil.html?maj=$(date +%s)"
CONTROLE_URL="http://127.0.0.1:8765/etat?rapide=1"
INDEX_URL="http://127.0.0.1:8002/index.json?maj=$(date +%s)"

unset PYTHONPATH PYTHONHOME
mkdir -p "$ANALYSE"

if [[ ! -x "$PY" ]]; then
  printf 'L’environnement Python est absent. Double-cliquez d’abord sur installer.command.\n'
  read -r -p 'Appuyez sur Entrée pour fermer.' _
  exit 1
fi

cp "$ROOT/index.html" "$ANALYSE/index.html" 2>/dev/null || true
cp "$ROOT/accueil.html" "$ANALYSE/accueil.html" 2>/dev/null || true
cp "$ROOT/fiches.html" "$ANALYSE/fiches.html" 2>/dev/null || true
cp "$ROOT/photos.html" "$ANALYSE/photos.html" 2>/dev/null || true
cp "$ROOT/minimal-theme.css" "$ANALYSE/minimal-theme.css" 2>/dev/null || true

controle_pret() {
  "$PY" - "$CONTROLE_URL" <<'PY'
from urllib.request import urlopen
import json, sys
url = sys.argv[1]
try:
    data = json.loads(urlopen(url, timeout=1).read().decode('utf-8'))
except Exception:
    sys.exit(1)
sys.exit(0 if data.get('ok') else 1)
PY
}

site_pret() {
  "$PY" - "$INDEX_URL" <<'PY'
from urllib.request import urlopen
import sys
url = sys.argv[1]
try:
    with urlopen(url, timeout=1) as r:
        body = r.read(32).decode('utf-8', errors='replace')
except Exception:
    sys.exit(1)
sys.exit(0 if body.startswith('{"genere":') else 1)
PY
}

attendre_port_libre() {
  for _ in {1..30}; do
    if ! lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.2
  done
  return 1
}

if controle_pret; then
  printf 'Serveur de contrôle déjà actif et prêt.\n'
else
  printf 'Serveur de contrôle absent ou non répondant : redémarrage propre…\n'
  pkill -TERM -f "[c]ontrole_analyse.py" >/dev/null 2>&1 || true
  if ! attendre_port_libre; then
    pkill -KILL -f "[c]ontrole_analyse.py" >/dev/null 2>&1 || true
    attendre_port_libre || {
      printf 'Erreur : le port 8765 est encore occupé.\n'
      lsof -nP -iTCP:8765 -sTCP:LISTEN || true
      read -r -p 'Appuyez sur Entrée pour fermer.' _
      exit 1
    }
  fi
  "$PY" "$ROOT/demarrer_detache.py" --cwd "$ROOT" --log "$ROOT/controle_analyse.log" -- "$PY" "$ROOT/controle_analyse.py"
  printf 'Serveur de contrôle lancé : http://127.0.0.1:8765/etat\n'
fi

printf 'Attente du serveur de contrôle…\n'
if ! "$PY" - <<'PY'
from urllib.request import urlopen
import json, sys, time
for _ in range(80):
    try:
        data = json.loads(urlopen('http://127.0.0.1:8765/etat?rapide=1', timeout=1).read().decode('utf-8'))
        if data.get('ok'):
            sys.exit(0)
    except Exception:
        pass
    time.sleep(0.25)
sys.exit(1)
PY
then
  printf 'Erreur : le serveur de contrôle ne répond pas encore. Voir controle_analyse.log\n'
  tail -40 "$ROOT/controle_analyse.log" 2>/dev/null || true
  read -r -p 'Appuyez sur Entrée pour fermer.' _
  exit 1
fi
printf 'Serveur de contrôle prêt.\n'

if site_pret; then
  printf 'Serveur web déjà actif et prêt : http://127.0.0.1:8002\n'
else
  if lsof -nP -iTCP:8002 -sTCP:LISTEN >/dev/null 2>&1; then
    printf 'Serveur web 8002 actif mais incomplet : redémarrage propre…\n'
    pkill -TERM -f "http.server 8002 --bind 127.0.0.1" >/dev/null 2>&1 || true
    sleep 1
    if lsof -nP -iTCP:8002 -sTCP:LISTEN >/dev/null 2>&1; then
      pkill -KILL -f "http.server 8002 --bind 127.0.0.1" >/dev/null 2>&1 || true
      sleep 1
    fi
  fi
  "$PY" "$ROOT/demarrer_detache.py" --cwd "$ANALYSE" --log "$ROOT/site_films_http.log" -- "$PY" -m http.server 8002 --bind 127.0.0.1
  printf 'Serveur web lancé : http://127.0.0.1:8002\n'
fi

printf 'Attente du serveur web…\n'
if ! "$PY" - <<'PY'
from urllib.request import urlopen
import sys, time
for _ in range(60):
    try:
        with urlopen('http://127.0.0.1:8002/index.json?maj=attente', timeout=1) as r:
            body = r.read(32).decode('utf-8', errors='replace')
        if body.startswith('{"genere":'):
            sys.exit(0)
    except Exception:
        pass
    time.sleep(0.25)
sys.exit(1)
PY
then
  printf 'Erreur : le serveur web 8002 ne sert pas encore index.json. Voir site_films_http.log\n'
  tail -40 "$ROOT/site_films_http.log" 2>/dev/null || true
  read -r -p 'Appuyez sur Entrée pour fermer.' _
  exit 1
fi
printf 'Serveur web prêt.\n'

open "$URL" >/dev/null 2>&1 || true
printf '\nPage ouverte : %s\n' "$URL"
printf '\nVous pouvez fermer cette fenêtre. Le site et le contrôle continuent en arrière-plan.\n'
sleep 3
