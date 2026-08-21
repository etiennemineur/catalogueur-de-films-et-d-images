#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
unset PYTHONPATH PYTHONHOME

clear 2>/dev/null || true
printf '\ncatalogueur de films et d‘images — installation\n'
printf '================================================\n\n'

if ! command -v python3 >/dev/null 2>&1; then
  printf 'Python 3 est introuvable. Installez-le puis relancez ce fichier.\n'
  read -r -p 'Appuyez sur Entrée pour fermer.' _
  exit 1
fi

if ! command -v brew >/dev/null 2>&1; then
  printf 'Homebrew est introuvable.\n'
  printf 'Installez Homebrew depuis https://brew.sh puis relancez ce fichier.\n'
  read -r -p 'Appuyez sur Entrée pour fermer.' _
  exit 1
fi

printf '1/4 Vérification de ffmpeg…\n'
if ! command -v ffmpeg >/dev/null 2>&1; then
  brew install ffmpeg
else
  printf '    ffmpeg est déjà installé.\n'
fi

printf '\n2/4 Vérification d’Ollama…\n'
if ! command -v ollama >/dev/null 2>&1; then
  brew install ollama
else
  printf '    Ollama est déjà installé.\n'
fi

printf '\n3/4 Création de l’environnement Python local…\n'
python3 -m venv .venv
".venv/bin/python" -m pip install --upgrade pip
".venv/bin/python" -m pip install -r requirements.txt

printf '\n4/4 Modèles IA locaux…\n'
printf 'Aucun modèle n’est imposé par l’installation.\n'
printf 'Installez vos modèles avec Ollama puis choisissez-les dans config.json ou dans l’interface.\n'
printf 'Modèles Ollama actuellement visibles :\n'
ollama list | awk 'NR==1 || NR>1 {print "    " $0}' || true

chmod +x installer.command lancer.command 2>/dev/null || true

printf '\nInstallation terminée.\n'
printf 'Ouvrez maintenant « lancer.command » pour choisir le dossier de films et travailler.\n\n'
read -r -p 'Appuyez sur Entrée pour fermer.' _
