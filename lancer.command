#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
PY="$ROOT/.venv/bin/python"
CONFIG="$ROOT/config.json"
MODELE_GLOBAL="${BANC_MODELE_ANALYSE:-}"
MODELE_AFFINAGE="${BANC_MODELE_AFFINAGE:-}"

unset PYTHONPATH PYTHONHOME
clear 2>/dev/null || true
printf '\nAnalyse films et photos — lancement simple\n'
printf '==============================================\n\n'

if [[ ! -x "$PY" ]]; then
  printf 'L’environnement Python est absent. Double-cliquez d’abord sur installer.command.\n'
  read -r -p 'Appuyez sur Entrée pour fermer.' _
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  printf 'ffmpeg est introuvable. Lancez installer.command.\n'
  read -r -p 'Appuyez sur Entrée pour fermer.' _
  exit 1
fi

if ! command -v ollama >/dev/null 2>&1; then
  printf 'Ollama est introuvable. Lancez installer.command.\n'
  read -r -p 'Appuyez sur Entrée pour fermer.' _
  exit 1
fi

if [[ -n "$MODELE_GLOBAL" ]] && ! ollama list | awk 'NR>1 {print $1}' | grep -Fxq "$MODELE_GLOBAL"; then
  printf 'Le modèle global demandé (%s) est absent. Téléchargez-le avec : ollama pull %s\n' "$MODELE_GLOBAL" "$MODELE_GLOBAL"
fi
if [[ -n "$MODELE_AFFINAGE" ]] && ! ollama list | awk 'NR>1 {print $1}' | grep -Fxq "$MODELE_AFFINAGE"; then
  printf 'Le modèle d’affinage demandé (%s) est absent. Téléchargez-le avec : ollama pull %s\n' "$MODELE_AFFINAGE" "$MODELE_AFFINAGE"
fi
if [[ -z "$MODELE_GLOBAL" && -z "$MODELE_AFFINAGE" ]]; then
  printf 'Aucun modèle IA n’est imposé : choisissez vos modèles locaux dans l’interface ou dans config.json.\n'
fi

if [[ ! -f "$CONFIG" ]]; then
  printf '{"dossier_films":"%s/Movies","dossier_photos":"%s/Pictures"}\n' "$HOME" "$HOME" > "$CONFIG"
fi

bash "$ROOT/ouvrir_site.command"

printf '\nTout se pilote maintenant depuis la page d’accueil :\n'
printf '  http://127.0.0.1:8002/accueil.html\n\n'
printf 'Boutons disponibles : catalogue films, fiches, catalogue photo, vérifier, lancer ou arrêter les analyses.\n'
printf 'Vous pouvez fermer cette fenêtre.\n'
sleep 4
