#!/usr/bin/env python3
"""Rapport quotidien du site local d’analyse des films."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / 'config.json'
try:
    _config = json.loads(CONFIG.read_text(encoding='utf-8')) if CONFIG.exists() else {}
except Exception:
    _config = {}
FILMS = Path(_config.get('dossier_films') or (Path.home() / 'Movies')).expanduser()
ANALYSE = ROOT / 'analyse'
EXTENSIONS = {'.mkv', '.mp4', '.mov', '.avi', '.m4v', '.webm', '.mpg', '.ts'}


def videos_source() -> list[Path]:
    if not FILMS.exists():
        return []
    return sorted(p for p in FILMS.iterdir() if p.is_file() and p.suffix.lower() in EXTENSIONS)


def index_data() -> dict:
    p = ANALYSE / 'index.json'
    if not p.exists():
        return {'films': [], 'plans': []}
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return {'films': [], 'plans': []}


def process_lines(pattern: str) -> list[str]:
    r = subprocess.run(['pgrep', '-af', pattern], text=True, capture_output=True, check=False)
    lignes = [line.strip() for line in r.stdout.splitlines() if line.strip()]
    if any(' ' in line for line in lignes):
        return lignes
    detaillees = []
    for pid in lignes:
        ps = subprocess.run(['ps', '-p', pid, '-o', 'pid=,etime=,command='], text=True, capture_output=True, check=False)
        detaillees.extend(line.strip() for line in ps.stdout.splitlines() if line.strip())
    return detaillees or lignes


def controle() -> dict | None:
    try:
        return json.loads(urlopen('http://127.0.0.1:8765/etat', timeout=3).read().decode('utf-8'))
    except Exception:
        return None


def progressions() -> list[tuple[str, int, int]]:
    out = []
    for p in sorted(ANALYSE.glob('*/plans.json')):
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            continue
        plans = data.get('plans', [])
        done = 0
        for plan in plans:
            a = plan.get('analyse') or {}
            if a.get('echelle') and a.get('lieu') and a.get('description') and a.get('mots_cles'):
                done += 1
        out.append((p.parent.name, done, len(plans)))
    return out


def main() -> None:
    idx = index_data()
    films = idx.get('films', [])
    plans = idx.get('plans', [])
    machines = sum(1 for p in plans if p.get('machine'))
    dialogues = sum(1 for p in plans if p.get('dialogue'))
    affines = sum(1 for p in plans if p.get('affinage'))
    mouvements_camera = sum(1 for p in plans if p.get('mouvement_camera'))
    src = videos_source()
    analyse_active = process_lines(r'analyse_plans.py .*--mode (triage|complet)')
    controle_state = controle()
    progs = progressions()
    incomplets = [(n, d, t) for n, d, t in progs if t and d < t]

    print('Rapport quotidien · site d’analyse des films')
    print(time.strftime('%Y-%m-%d %H:%M:%S'))
    print()
    print('Pages à utiliser')
    print(f'- Accueil : http://localhost:8002/accueil.html')
    print(f'- Catalogue : http://localhost:8002/index.html')
    print(f'- Contrôle local : http://127.0.0.1:8765/etat')
    print(f'- Fichier à double-cliquer après redémarrage : {ROOT / "ouvrir_site.command"}')
    print()
    print('État du corpus')
    print(f'- Films dans le dossier source : {len(src)}')
    print(f'- Films visibles dans l’index : {len(films)}')
    print(f'- Plans indexés : {len(plans)}')
    print(f'- Plans avec machine : {machines}')
    print(f'- Plans avec dialogue indexé : {dialogues}')
    print(f'- Plans affinés par second module IA : {affines}')
    print(f'- Plans avec mouvement caméra mesuré : {mouvements_camera}')
    print(f'- Index généré : {idx.get("genere") or "non daté"}')
    print()
    print('Automatisation')
    modele_global = (controle_state or {}).get('modele_analyse') or 'à choisir dans config.json / interface'
    modele_affinage = (controle_state or {}).get('modele_affinage') or 'à choisir dans config.json / interface'
    print(f'- Modèle analyse globale : {modele_global}')
    print(f'- Modèle affinage visuel : {modele_affinage}')
    if controle_state:
        print(f'- Serveur de contrôle : actif')
        print(f'- Surveillance : {"active" if controle_state.get("surveillance") else "inactive ou serveur absent"}')
        print(f'- Analyse active : {"oui" if controle_state.get("analyse_active") else "non"}')
        print(f'- Son/dialogues actifs : {"oui" if controle_state.get("audio_active") else "non"}')
        print(f'- Message son/dialogues : {controle_state.get("dernier_audio_message") or "—"}')
        print(f'- Dernier message : {controle_state.get("dernier_message") or "—"}')
        print(f'- Dernière indexation automatique : {controle_state.get("derniere_indexation") or "—"}')
        print(f'- Message index : {controle_state.get("dernier_index_message") or "—"}')
        manquants = controle_state.get('films_sans_analyse') or []
        if manquants:
            print(f'- Films présents sans analyse : {len(manquants)}')
            for nom in manquants[:10]:
                print(f'  · {nom}')
    else:
        print('- Serveur de contrôle : non joignable')
    print()
    print('Processus d’analyse')
    if analyse_active:
        for line in analyse_active[:5]:
            print(f'- {line}')
    else:
        print('- aucune analyse longue active')
    print()
    print('Films incomplets ou en cours')
    if incomplets:
        for name, done, total in incomplets[:12]:
            pct = round(done / total * 100) if total else 0
            print(f'- {name}: {done} / {total} ({pct} %)')
    else:
        print('- aucun film incomplet détecté dans les dossiers déjà indexés')
    print()
    print('Codes utiles')
    print(f'- Ouvrir le site : double-clic sur {ROOT / "ouvrir_site.command"}')
    print('- Refaire l’index maintenant : bouton « Mettre à jour l’index maintenant » sur la page d’accueil')
    print('- Lancer une analyse : bouton « Lancer l’analyse » sur la page d’accueil')
    print('- Activer la surveillance : bouton « Activer la surveillance du dossier » sur la page d’accueil')
    print('- Optimisations : schéma JSON contraint, banc comparer_modeles.py, tri « Relecture prioritaire »')
    print('- Dialogues : analyse_son_dialogues.py indexe les sous-titres intégrés puis utilise Whisper local automatiquement quand aucun sous-titre exploitable n’est trouvé')
    print('- Affinage : analyse_affinage.py relit seulement les plans douteux ou incomplets')
    print('- Mouvements caméra : analyse_mouvements.py mesure fixe, zoom, panoramique, tilt, rotation et caméra portée')


if __name__ == '__main__':
    main()
