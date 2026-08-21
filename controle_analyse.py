#!/usr/bin/env python3
"""Petit serveur local de contrôle pour l’analyse des films.

Il permet à la page accueil.html de demander :
- le lancement d’une analyse complète ;
- l’activation d’une surveillance du dossier films.

Le serveur n’est accessible que sur localhost.
"""

from __future__ import annotations

import json
import io
import os
import re
import shlex
import signal
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
PY = ROOT / ".venv" / "bin" / "python"
CONFIG = ROOT / "config.json"
ANALYSE = ROOT / "analyse"
PHOTO_ANALYSE = ANALYSE / "photos"
MODELE = os.environ.get("BANC_MODELE_ANALYSE", "").strip()
MODELE_AFFINAGE = os.environ.get("BANC_MODELE_AFFINAGE", MODELE).strip()
MODELE_MLX_VLM = os.environ.get("BANC_MODELE_MLX", "").strip()
MLX_VLM_URL = os.environ.get("BANC_MLX_URL", "http://127.0.0.1:8080")
MLX_VLM_CONCURRENCE = int(os.environ.get("BANC_MLX_CONCURRENCE", "6") or "6")
MODELE_MOUVEMENTS_VIDEO = os.environ.get("BANC_MODELE_MOUVEMENTS_VIDEO", "").strip()
MODELES_ANALYSE = {}
_CACHE_MODELES_OLLAMA = {"ts": 0.0, "noms": set()}
MODELES_MOUVEMENTS_VIDEO = (
    {"videomae": {"nom": MODELE_MOUVEMENTS_VIDEO, "label": f"VideoMAE · {MODELE_MOUVEMENTS_VIDEO}"}}
    if MODELE_MOUVEMENTS_VIDEO else {}
)
LARGEUR_ANALYSE = 896
PORT = 8765
EXTENSIONS = {".mkv", ".mp4", ".mov", ".avi", ".m4v", ".webm", ".mpg", ".ts"}
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".heic", ".heif"}
INDEX_INTERVALLE = 60
AUDIO_INTERVALLE = 180
MOUVEMENTS_INTERVALLE = 240
MODELE_WHISPER = "base"
PAGES_SITE = ("index.html", "accueil.html", "fiches.html", "film.html", "photos.html")
SYNC_PAGES_COMMANDE = " && ".join(f"cp {nom} analyse/{nom}" for nom in PAGES_SITE)
PREVIEW_DIR = "apercus_video"
LECTEUR_DIR = "lecteur_film"
FORMATS_VIDEO_DIRECTS = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".ogv": "video/ogg",
    ".mov": "video/quicktime",
}
PREVIEW_DUREE_MIN = 0.20
PREVIEW_LARGEUR = 640
PREVIEW_FPS = 12.0
PREVIEW_MAX_IMAGES = 900
PREVIEW_ZOOM_LARGEUR = 1280
PREVIEW_ZOOM_FPS = 24.0
PREVIEW_ZOOM_MAX_IMAGES = 2400
PREVIEW_ZOOM_COUPE_FIN_IMAGES = 4

etat = {
    "surveillance": False,
    "analyse_toggle_on": True,
    "dernier_message": "Serveur de contrôle prêt.",
    "analyse_pid": None,
    "index_en_cours": False,
    "derniere_indexation": None,
    "dernier_index_message": "Index non reconstruit par le serveur de contrôle.",
    "audio_auto": True,
    "audio_pid": None,
    "dernier_audio_message": "Indexation son/dialogues en attente.",
    "dernier_audio_index_genere": None,
    "musique_pid": None,
    "dernier_musique_message": "Analyse musique globale en attente.",
    "scenes_pid": None,
    "dernier_scenes_message": "Scènes/séquences en attente.",
    "mouvements_auto": True,
    "mouvements_pid": None,
    "dernier_mouvements_message": "Mesure des mouvements de caméra en attente.",
    "mouvements_video_pid": None,
    "dernier_mouvements_video_message": "Classification VideoMAE des mouvements en attente.",
    "affinage_pid": None,
    "dernier_affinage_message": "Analyse fine IA en attente.",
    "photos_pid": None,
    "dernier_photos_message": "Analyse photo en attente.",
}
verrou = threading.Lock()
verrou_index = threading.Lock()
cache_index_resume = {"mtime_ns": None, "data": None}
cache_films_progression = {"lu": 0.0, "data": None}
cache_processus = {"lu": 0.0, "data": [], "erreur": None}
verrou_processus = threading.Lock()
PROCESSUS_CACHE_TTL = 1.5


def env_nettoye() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return env


def lire_config() -> dict:
    if not CONFIG.exists():
        return {}
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def ecrire_config(data: dict) -> None:
    CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


etat["analyse_toggle_on"] = bool(lire_config().get("analyse_toggle_on", etat.get("analyse_toggle_on", True)))


def memoriser_toggle_analyse(actif: bool) -> bool:
    actif = bool(actif)
    with verrou:
        etat["analyse_toggle_on"] = actif
    config = lire_config()
    config["analyse_toggle_on"] = actif
    ecrire_config(config)
    return actif


def analyse_toggle_active() -> bool:
    return bool(etat.get("analyse_toggle_on", True))


def modeles_analyse_disponibles() -> dict[str, dict[str, str]]:
    """Modèles vision proposés dans l’interface.

    Chaque entrée peut préciser un `moteur` (`ollama` ou `mlx`). Les futurs
    modèles installés peuvent être ajoutés dans `config.json` via
    `modeles_analyse_disponibles`.
    """
    modeles = {cle: dict(info) for cle, info in MODELES_ANALYSE.items()}
    extras = lire_config().get("modeles_analyse_disponibles")
    if isinstance(extras, dict):
        for cle, info in extras.items():
            if isinstance(info, dict):
                nom = str(info.get("nom") or info.get("model") or "").strip()
                label = str(info.get("label") or nom).strip()
                moteur = str(info.get("moteur") or info.get("backend") or "").strip().lower()
            else:
                nom = str(info or "").strip()
                label = nom
                moteur = ""
            if nom:
                if moteur not in {"mlx", "ollama"}:
                    moteur = "mlx" if nom.startswith("mlx-") or nom.startswith("mlx-community/") else "ollama"
                modeles[slug(str(cle or nom))] = {"nom": nom, "label": label or nom, "moteur": moteur}
    elif isinstance(extras, list):
        for item in extras:
            if isinstance(item, dict):
                nom = str(item.get("nom") or item.get("model") or "").strip()
                label = str(item.get("label") or nom).strip()
                moteur = str(item.get("moteur") or item.get("backend") or "").strip().lower()
            else:
                nom = str(item or "").strip()
                label = nom
                moteur = ""
            if nom:
                if moteur not in {"mlx", "ollama"}:
                    moteur = "mlx" if nom.startswith("mlx-") or nom.startswith("mlx-community/") else "ollama"
                modeles[slug(nom)] = {"nom": nom, "label": label or nom, "moteur": moteur}
    installes = modeles_ollama_installes()
    for nom in sorted(installes):
        modeles.setdefault(slug(nom), {"nom": nom, "label": f"Ollama · {nom}", "moteur": "ollama"})
    if MODELE:
        modeles.setdefault(slug(MODELE), {"nom": MODELE, "label": f"Ollama · {MODELE}", "moteur": "ollama"})
    if MODELE_AFFINAGE:
        modeles.setdefault(slug(MODELE_AFFINAGE), {"nom": MODELE_AFFINAGE, "label": f"Ollama · {MODELE_AFFINAGE}", "moteur": "ollama"})
    if MODELE_MLX_VLM:
        modeles.setdefault(slug(MODELE_MLX_VLM), {"nom": MODELE_MLX_VLM, "label": f"Apple MLX · {MODELE_MLX_VLM}", "moteur": "mlx"})
    if installes:
        modeles = {
            cle: info for cle, info in modeles.items()
            if (info.get("moteur") or "ollama") != "ollama" or info.get("nom") in installes
        }
    return modeles


def modeles_ollama_installes() -> set[str]:
    """Retourne les modèles présents dans Ollama, avec un petit cache pour /etat."""
    maintenant = time.time()
    if maintenant - float(_CACHE_MODELES_OLLAMA.get("ts") or 0) < 20:
        return set(_CACHE_MODELES_OLLAMA.get("noms") or set())
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=0.8) as reponse:
            data = json.loads(reponse.read().decode("utf-8", errors="replace"))
        noms = {str(m.get("name") or m.get("model") or "").strip() for m in data.get("models", [])}
        noms = {nom for nom in noms if nom}
        _CACHE_MODELES_OLLAMA["ts"] = maintenant
        _CACHE_MODELES_OLLAMA["noms"] = noms
        return noms
    except Exception:
        _CACHE_MODELES_OLLAMA["ts"] = maintenant
        _CACHE_MODELES_OLLAMA["noms"] = set()
        return set()


def infos_modele_analyse(modele: str) -> dict[str, str]:
    for info in modeles_analyse_disponibles().values():
        if modele == info.get("nom"):
            return info
    return {"nom": modele, "label": modele, "moteur": "ollama"}


def moteur_pour_modele(modele: str) -> str:
    return infos_modele_analyse(modele).get("moteur") or "ollama"


def modeles_ollama_analyse() -> list[str]:
    return [
        info["nom"] for info in modeles_analyse_disponibles().values()
        if (info.get("moteur") or "ollama") == "ollama"
    ]


def concurrence_mlx() -> int:
    try:
        return max(1, int(lire_config().get("mlx_vlm_concurrence") or MLX_VLM_CONCURRENCE))
    except Exception:
        return MLX_VLM_CONCURRENCE


def etat_mlx_vlm(timeout: float = 0.8) -> dict:
    try:
        with urllib.request.urlopen(f"{MLX_VLM_URL}/health", timeout=timeout) as reponse:
            data = json.loads(reponse.read().decode("utf-8", errors="replace"))
        return {
            "actif": True,
            "url": MLX_VLM_URL,
            "modele": data.get("loaded_model") or data.get("model") or MODELE_MLX_VLM,
            "apc_enabled": data.get("apc_enabled"),
            "health": data,
        }
    except Exception as exc:
        return {
            "actif": False,
            "url": MLX_VLM_URL,
            "modele": MODELE_MLX_VLM,
            "apc_enabled": False,
            "erreur": f"{type(exc).__name__}: {exc}",
        }


def normaliser_modele_analyse(valeur: str | None) -> str:
    texte = str(valeur or "").strip()
    if not texte:
        disponibles = modeles_ollama_analyse() or [info["nom"] for info in modeles_analyse_disponibles().values()]
        if disponibles:
            return disponibles[0]
        raise ValueError("Aucun modèle IA local configuré. Ajoutez un modèle Ollama dans config.json ou installez-en un avec Ollama.")
    for cle, info in modeles_analyse_disponibles().items():
        if texte == cle or texte == info["nom"]:
            return info["nom"]
    autorises = ", ".join(info["nom"] for info in modeles_analyse_disponibles().values())
    raise ValueError(f"Modèle d’analyse non autorisé : {texte}. Choisir parmi : {autorises}.")


def normaliser_modele_mouvements_video(valeur: str | None) -> str:
    texte = str(valeur or "").strip()
    if not texte or texte in {"auto", "videomae"}:
        return MODELE_MOUVEMENTS_VIDEO
    for cle, info in MODELES_MOUVEMENTS_VIDEO.items():
        if texte == cle or texte == info["nom"]:
            return info["nom"]
    autorises = ", ".join(info["nom"] for info in MODELES_MOUVEMENTS_VIDEO.values())
    raise ValueError(f"Modèle de mouvement vidéo non autorisé : {texte}. Choisir parmi : {autorises}.")


def normaliser_modele_ollama_analyse(valeur: str | None, defaut: str = MODELE) -> str:
    modele = normaliser_modele_analyse(valeur or defaut)
    if moteur_pour_modele(modele) != "ollama":
        raise ValueError(
            f"Le modèle {modele} utilise MLX. Cette passe n’est pas encore branchée sur MLX ; "
            "choisissez un modèle Ollama local dans l’interface ou dans config.json."
        )
    return modele


def analyses_film_disponibles() -> dict[str, dict]:
    modeles_vision = [info["nom"] for info in modeles_analyse_disponibles().values()]
    modeles_ollama = modeles_ollama_analyse() or ([MODELE] if MODELE else [])
    return {
        "plans": {
            "label": "Plans image",
            "description": "Découpage, vignettes et analyse visuelle complète du film.",
            "modeles": modeles_vision,
            "refaire_possible": True,
        },
        "scenes": {
            "label": "Scènes / séquences",
            "description": "Regroupement narratif léger des plans contigus déjà analysés.",
            "modeles": ["local-scenes"],
            "refaire_possible": True,
        },
        "dialogue": {
            "label": "Dialogue",
            "description": "Indexation locale des sous-titres, sans Whisper automatique par défaut.",
            "modeles": ["local-dialogue"],
            "refaire_possible": True,
        },
        "musique": {
            "label": "Musique",
            "description": "Analyse sonore par séquences, encore branchée sur Ollama.",
            "modeles": modeles_ollama,
            "refaire_possible": True,
        },
        "mouvements": {
            "label": "Mouvements caméra",
            "description": "Mesure mécanique locale par optical flow.",
            "modeles": ["local-mecanique"],
            "refaire_possible": True,
        },
        "mouvements-video": {
            "label": "Mouvements vidéo",
            "description": "Classification VideoMAE complémentaire des mouvements caméra.",
            "modeles": [MODELE_MOUVEMENTS_VIDEO],
            "refaire_possible": True,
        },
        "affinage": {
            "label": "Affinage IA",
            "description": "Deuxième passe ciblée sur les plans douteux ou incomplets, encore branchée sur Ollama.",
            "modeles": modeles_ollama,
            "refaire_possible": False,
        },
    }


def normaliser_etape_film(valeur: str | None) -> str:
    texte = str(valeur or "plans").strip().lower()
    aliases = {
        "plan": "plans",
        "plans": "plans",
        "image": "plans",
        "images": "plans",
        "scene": "scenes",
        "scène": "scenes",
        "scenes": "scenes",
        "scènes": "scenes",
        "sequence": "scenes",
        "séquence": "scenes",
        "sequences": "scenes",
        "séquences": "scenes",
        "dialogue": "dialogue",
        "dialogues": "dialogue",
        "son": "dialogue",
        "audio": "dialogue",
        "musique": "musique",
        "music": "musique",
        "mouvement": "mouvements",
        "mouvements": "mouvements",
        "mouvements-camera": "mouvements",
        "mouvements_video": "mouvements-video",
        "mouvements-video": "mouvements-video",
        "videomae": "mouvements-video",
        "affinage": "affinage",
    }
    if texte not in aliases:
        autorises = ", ".join(analyses_film_disponibles().keys())
        raise ValueError(f"Analyse film inconnue : {texte}. Choisir parmi : {autorises}.")
    return aliases[texte]


def modele_analyse_defaut() -> str:
    try:
        return normaliser_modele_analyse(lire_config().get("modele_analyse"))
    except Exception:
        return MODELE


def enregistrer_modele_analyse(modele: str, film_id: str | None = None) -> str:
    modele = normaliser_modele_analyse(modele)
    config = lire_config()
    if film_id:
        overrides = config.get("modeles_analyse_films")
        if not isinstance(overrides, dict):
            overrides = {}
        overrides[film_id] = modele
        config["modeles_analyse_films"] = overrides
    else:
        config["modele_analyse"] = modele
    ecrire_config(config)
    return modele


def modele_analyse_film(film_id: str | None = None) -> str:
    config = lire_config()
    if film_id:
        overrides = config.get("modeles_analyse_films")
        if isinstance(overrides, dict) and film_id in overrides:
            try:
                return normaliser_modele_analyse(overrides.get(film_id))
            except Exception:
                pass
    return modele_analyse_defaut()


def options_analyse_film(film_id: str | None = None) -> dict:
    if not film_id:
        return {}
    config = lire_config()
    options = config.get("options_analyse_films")
    if not isinstance(options, dict):
        return {}
    film = options.get(film_id)
    return dict(film) if isinstance(film, dict) else {}


def dossier_films() -> Path:
    data = lire_config()
    dossier = data.get("dossier_films")
    if dossier:
        return Path(dossier).expanduser()
    raise RuntimeError("Aucun dossier de films configuré.")


def dossier_photos() -> Path:
    data = lire_config()
    dossier = data.get("dossier_photos") or str(Path.home() / "Pictures")
    return Path(dossier).expanduser()


def films_criteres() -> list[str]:
    data = lire_config()
    criteres = data.get("films_criteres")
    if isinstance(criteres, list) and criteres:
        return [str(c) for c in criteres if str(c).strip()]
    return [
        "description factuelle du plan",
        "décor et architecture visibles",
        "objets, machines et interfaces",
        "personnages, gestes et postures",
        "cadrage, lumière et couleur",
        "texte visible et typographie",
        "prudence sur les éléments hors champ",
        "vocabulaire utile pour le catalogue",
    ]


def films_contexte() -> str:
    return str(lire_config().get("films_contexte") or "")


def photos_criteres() -> list[str]:
    data = lire_config()
    criteres = data.get("photos_criteres")
    if isinstance(criteres, list) and criteres:
        return [str(c) for c in criteres if str(c).strip()]
    return [
        "sujets et objets visibles",
        "composition et cadrage",
        "lumière et couleur",
        "lieu ou décor",
        "personnes sans identification nominative",
        "texte visible dans l’image",
        "qualité technique",
        "usage possible dans un catalogue",
    ]


def photos_contexte() -> str:
    return str(lire_config().get("photos_contexte") or "")


def modele_photos() -> str:
    """Modèle Ollama utilisé par l’analyse photo."""
    data = lire_config()
    return normaliser_modele_ollama_analyse(
        data.get("photos_modele_analyse") or data.get("modele_analyse") or MODELE,
        defaut=MODELE,
    )


def modeles_photos_disponibles() -> list[str]:
    return modeles_ollama_analyse() or ([MODELE] if MODELE else [])


def signature_videos(dossier: Path) -> list[tuple[str, int, int]]:
    lignes = []
    for fichier in sorted(dossier.rglob("*")):
        if fichier.is_file() and fichier.suffix.lower() in EXTENSIONS and not fichier.name.startswith("."):
            st = fichier.stat()
            lignes.append((str(fichier.relative_to(dossier)), st.st_size, int(st.st_mtime)))
    return lignes


def compter_videos_source() -> int:
    try:
        return len(signature_videos(dossier_films()))
    except Exception:
        return 0


def videos_source_details() -> list[dict]:
    try:
        dossier = dossier_films()
    except Exception:
        return []
    details = []
    for fichier in sorted(dossier.rglob("*")):
        if not fichier.is_file() or fichier.suffix.lower() not in EXTENSIONS or fichier.name.startswith("."):
            continue
        film_id = slug(fichier.stem)
        details.append({
            "id": film_id,
            "nom": fichier.name,
            "source": fichier.name,
            "chemin": str(fichier),
            "analyse_presente": (ANALYSE / film_id / "plans.json").exists(),
            "modele_analyse": modele_analyse_film(film_id),
        })
    return details


def video_source_par_id(film_id: str) -> Path:
    for info in videos_source_details():
        if info.get("id") == film_id:
            return Path(info["chemin"])
    raise FileNotFoundError(f"Vidéo source introuvable pour le film : {film_id}")


def chemin_proxy_film(film_id: str) -> Path:
    return ANALYSE / film_id / LECTEUR_DIR / "film.mp4"


def video_lecteur_par_id(film_id: str) -> tuple[Path, str]:
    source = video_source_par_id(film_id)
    ext = source.suffix.lower()
    if ext in FORMATS_VIDEO_DIRECTS:
        return source, FORMATS_VIDEO_DIRECTS[ext]
    sortie = chemin_proxy_film(film_id)
    sortie.parent.mkdir(parents=True, exist_ok=True)
    try:
        if sortie.exists() and sortie.stat().st_size > 0 and sortie.stat().st_mtime >= source.stat().st_mtime:
            return sortie, "video/mp4"
    except OSError:
        pass
    tmp = sortie.with_suffix(".tmp.mp4")
    tmp.unlink(missing_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-map", "0:v:0", "-map", "0:a?",
        "-sn", "-dn",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "160k",
        str(tmp),
    ]
    r = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size <= 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError((r.stderr or r.stdout or "Transcodage vidéo impossible.").strip())
    tmp.replace(sortie)
    return sortie, "video/mp4"


def synchroniser_fiches_films_sources() -> dict:
    """Crée et enrichit les fiches de films source avant analyse complète.

    Objectif : faire apparaître les nouveaux films dans « Fiches des films » dès
    leur détection, avec poster et contexte narratif si disponibles.
    """
    try:
        from enrichir_fiches_wikipedia import (
            FICHE_MODELE,
            fiche_depuis_wikipedia,
            merge_prudent,
            titre_annee,
        )
    except Exception as exc:
        return {"ok": False, "created": 0, "updated": 0, "message": f"Import enrichissement impossible : {exc}"}

    created = 0
    updated = 0
    errors: list[str] = []

    for info in videos_source_details():
        video = Path(info["chemin"])
        base = ANALYSE / info["id"]
        base.mkdir(parents=True, exist_ok=True)
        fichier = base / "fiche.json"

        fiche = dict(FICHE_MODELE)
        avant = ""
        if fichier.exists():
            try:
                fiche.update(json.loads(fichier.read_text(encoding="utf-8")))
                avant = json.dumps(fiche, ensure_ascii=False, sort_keys=True)
            except Exception:
                fiche = dict(FICHE_MODELE)
        else:
            created += 1

        titre, annee = titre_annee(video.name)
        if not fiche.get("titre"):
            fiche["titre"] = titre
        if not fiche.get("annee") and annee:
            fiche["annee"] = annee
        if fiche.get("annee") and not fiche.get("date_sortie"):
            fiche["date_sortie"] = str(fiche["annee"])

        manque_poster = not str(fiche.get("poster_url") or "").strip()
        manque_pitch = not str(fiche.get("pitch") or "").strip()
        pitch_suspect = pitch_fiche_suspect(fiche)
        manque_scenario = not str(fiche.get("scenario") or "").strip()

        if manque_poster or manque_pitch or pitch_suspect or manque_scenario:
            try:
                nouveau = fiche_depuis_wikipedia(video)
                fiche = merge_prudent(fiche, nouveau)
                if (manque_pitch or pitch_suspect) and str(nouveau.get("pitch") or "").strip():
                    fiche["pitch"] = str(nouveau.get("pitch") or "").strip()
                if (not str(fiche.get("synopsis") or "").strip() or pitch_suspect) and str(nouveau.get("synopsis") or "").strip():
                    fiche["synopsis"] = str(nouveau.get("synopsis") or "").strip()
                if not str(fiche.get("pitch") or "").strip() and str(fiche.get("synopsis") or "").strip():
                    fiche["pitch"] = str(fiche.get("synopsis") or "").strip()
            except Exception as exc:
                errors.append(f"{video.name}: {type(exc).__name__}: {exc}")

        apres = json.dumps(fiche, ensure_ascii=False, sort_keys=True)
        if apres != avant:
            fichier.write_text(json.dumps(fiche, ensure_ascii=False, indent=1), encoding="utf-8")
            updated += 1

    return {
        "ok": True,
        "created": created,
        "updated": updated,
        "errors": errors[:10],
    }


def signature_photos(dossier: Path) -> list[tuple[str, int, int]]:
    lignes = []
    if not dossier.exists():
        return lignes
    for fichier in sorted(dossier.rglob("*")):
        if fichier.is_file() and fichier.suffix.lower() in PHOTO_EXTENSIONS and not fichier.name.startswith("."):
            st = fichier.stat()
            lignes.append((str(fichier.relative_to(dossier)), st.st_size, int(st.st_mtime)))
    return lignes


def compter_photos_source() -> int:
    try:
        return len(signature_photos(dossier_photos()))
    except Exception:
        return 0


def slug(texte: str) -> str:
    t = unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-zA-Z0-9]+", "-", t).strip("-").lower()
    return t or "film"


def pitch_fiche_suspect(fiche: dict) -> bool:
    texte = str(fiche.get("pitch") or fiche.get("synopsis") or "").strip().lower()
    if not texte:
        return True
    marqueurs_biographiques = (
        "était un cinéaste",
        "dramaturge et acteur",
        "parfois crédité sous le nom",
        "nouveau cinéma allemand",
        "new german cinema",
        "was a german film director",
    )
    return any(m in texte for m in marqueurs_biographiques)


def formater_duree(secondes: float | int | None) -> str:
    if secondes is None:
        return ""
    try:
        secondes = max(0, int(round(float(secondes))))
    except (TypeError, ValueError):
        return ""
    h, reste = divmod(secondes, 3600)
    m, s = divmod(reste, 60)
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {s:02d} s"
    return f"{s} s"


def resume_mouvements_camera(plans: list[dict]) -> dict:
    """Résumé par film de la couche mouvement caméra déjà écrite dans plans.json."""
    total = len(plans)

    def top_valeurs(cle: str, repli: str | None = None, limite: int = 5) -> list[dict]:
        compte: dict[str, int] = {}
        for plan in plans:
            valeur = plan.get(cle) or (plan.get(repli) if repli else "") or ""
            valeur = str(valeur).strip()
            if not valeur:
                continue
            compte[valeur] = compte.get(valeur, 0) + 1
        return [
            {"label": label, "plans": nb, "pourcentage": round(nb / max(total, 1) * 100)}
            for label, nb in sorted(compte.items(), key=lambda item: (-item[1], item[0]))[:limite]
        ]

    def compter_non_vide(cle: str) -> int:
        return sum(1 for plan in plans if plan.get(cle))

    mesures = sum(1 for plan in plans if plan.get("mouvement_camera") or plan.get("mouvement_camera_final"))
    video = compter_non_vide("mouvement_video_camera")
    modeles_video = sorted({
        str(plan.get("mouvement_video_modele")).strip()
        for plan in plans
        if str(plan.get("mouvement_video_modele") or "").strip()
    })
    confiances: dict[str, int] = {}
    for plan in plans:
        confiance = str(plan.get("mouvement_confiance") or "").strip()
        if confiance:
            confiances[confiance] = confiances.get(confiance, 0) + 1
    return {
        "mouvements_camera_plans_mesures": mesures,
        "mouvements_camera_pourcentage": round(mesures / max(total, 1) * 100),
        "mouvements_camera_principaux": top_valeurs("mouvement_camera_final", "mouvement_camera"),
        "mouvements_camera_confiances": dict(sorted(confiances.items())),
        "mouvements_camera_conflits": sum(1 for plan in plans if plan.get("mouvement_camera_conflit")),
        "mouvements_video_plans_classes": video,
        "mouvements_video_pourcentage": round(video / max(total, 1) * 100),
        "mouvements_video_principaux": top_valeurs("mouvement_video_camera"),
        "mouvements_video_modeles": modeles_video,
    }


def resume_couches_film(data: dict, plans: list[dict]) -> dict:
    """Compte les couches déjà écrites pour guider les relances ciblées."""
    dialogues = sum(1 for plan in plans if plan.get("dialogue") or str(plan.get("dialogue_texte") or "").strip())
    dialogue_sources = sorted({
        str(plan.get("dialogue_source") or "").strip()
        for plan in plans
        if str(plan.get("dialogue_source") or "").strip()
    })
    audio_global = data.get("audio_global") or {}
    sequences = audio_global.get("sequences") if isinstance(audio_global, dict) else []
    sequences = sequences if isinstance(sequences, list) else []
    musique_plans = sum(
        1 for plan in plans
        if plan.get("musique_presente")
        or plan.get("audio_detaille")
        or plan.get("audio_global_ref")
    )
    affinages = sum(1 for plan in plans if plan.get("affinage"))
    return {
        "dialogues_plans": dialogues,
        "dialogues_source": (data.get("dialogues") or {}).get("source") or (" · ".join(dialogue_sources[:2]) if dialogue_sources else ""),
        "musique_plans": musique_plans,
        "musique_sequences": len(sequences),
        "affinages_plans": affinages,
    }


def videos_sans_analyse() -> list[str]:
    """Films source présents mais sans analyse/<film-id>/plans.json."""
    return [info["nom"] for info in videos_source_details() if not info.get("analyse_presente")]


def videos_sans_analyse_details() -> list[dict]:
    return [info for info in videos_source_details() if not info.get("analyse_presente")]


def plan_analyse_complet(plan: dict) -> bool:
    analyse = plan.get("analyse") or {}
    return all(analyse.get(cle) not in (None, "", []) for cle in ("echelle", "lieu", "description", "mots_cles"))


def plan_analyse_tentee(plan: dict) -> bool:
    if plan_analyse_complet(plan):
        return True
    analyse = plan.get("analyse") or {}
    for candidat in (
        plan.get("analyse_modele"),
        analyse.get("modele_analyse"),
        plan.get("analyse_mesuree_le"),
        plan.get("temps_analyse_secondes"),
    ):
        if isinstance(candidat, (int, float)):
            return True
        if str(candidat or "").strip():
            return True
    return False


def film_analyse_terminee(total: int, prepares: int, tentes: int, complets: int) -> bool:
    if total <= 0:
        return False
    if complets >= total:
        return True
    if prepares < total or tentes < total:
        return False
    return (complets / max(total, 1)) >= 0.995


def plans_visibles_pour_controle(data: dict) -> list[dict]:
    rnd = data.get("redecoupage_non_destructif") or {}
    strategie = str(rnd.get("strategie") or "")
    plans_proposes = rnd.get("plans_proposes") or []
    if strategie.startswith("non-destructive-add-only") and plans_proposes:
        return plans_proposes
    return data.get("plans", []) or []


def films_a_analyser_details() -> list[dict]:
    """Films source absents ou incomplets, sans inclure les films déjà terminés."""
    a_traiter = []
    for info in videos_source_details():
        plans_json = ANALYSE / info["id"] / "plans.json"
        if not plans_json.exists():
            a_traiter.append({**info, "etat_analyse": "absent", "plans": 0, "plans_complets": 0})
            continue
        try:
            data = json.loads(plans_json.read_text(encoding="utf-8"))
            plans = plans_visibles_pour_controle(data)
            total = len(plans)
            prepares = sum(
                1 for p in plans
                if p.get("vignette")
                and isinstance(p.get("vignettes"), list)
                and len(p.get("vignettes") or []) >= 3
                and all(str(v or "").strip() for v in (p.get("vignettes") or [])[:3])
            )
            tentes = sum(1 for plan in plans if plan_analyse_tentee(plan))
            complets = sum(1 for plan in plans if plan_analyse_complet(plan))
        except Exception:
            a_traiter.append({**info, "etat_analyse": "illisible", "plans": 0, "plans_complets": 0})
            continue
        if not film_analyse_terminee(total, prepares, tentes, complets):
            a_traiter.append({
                **info,
                "etat_analyse": "incomplet",
                "plans": total,
                "plans_prepares": prepares,
                "plans_tentes": tentes,
                "plans_complets": complets,
            })
    return a_traiter


def photos_sans_analyse() -> list[str]:
    """Photos source présentes mais absentes du catalogue photo analysé."""
    try:
        dossier = dossier_photos()
    except Exception:
        return []
    index = PHOTO_ANALYSE / "index.json"
    analysees = set()
    if index.exists():
        try:
            data = json.loads(index.read_text(encoding="utf-8"))
            analysees = {p.get("chemin_relatif") for p in data.get("photos", []) if p.get("description")}
        except Exception:
            analysees = set()
    manquantes = []
    for fichier in sorted(dossier.rglob("*")) if dossier.exists() else []:
        if not fichier.is_file() or fichier.suffix.lower() not in PHOTO_EXTENSIONS or fichier.name.startswith("."):
            continue
        rel = str(fichier.relative_to(dossier))
        if rel not in analysees:
            manquantes.append(rel)
    return manquantes


def charger_plans_film(film_id: str) -> tuple[dict, Path]:
    fichier = ANALYSE / film_id / "plans.json"
    if not fichier.exists():
        raise FileNotFoundError(f"Film inconnu dans analyse/: {film_id}")
    return json.loads(fichier.read_text(encoding="utf-8")), fichier


def plan_par_numero(film_id: str, numero: int) -> tuple[dict, dict, Path]:
    data, fichier = charger_plans_film(film_id)
    for plan in data.get("plans", []):
        try:
            if int(plan.get("n", 0)) == int(numero):
                return data, plan, fichier
        except (TypeError, ValueError):
            continue
    raise FileNotFoundError(f"Plan introuvable : {film_id} #{numero}")


def fenetre_apercu_plan(plan: dict) -> tuple[float, float]:
    debut = float(plan.get("debut", 0) or 0)
    fin = float(plan.get("fin", debut) or debut)
    if fin <= debut:
        fin = debut + PREVIEW_DUREE_MIN
    return round(debut, 3), round(fin, 3)


def profil_apercu(mode: str | None = None) -> dict[str, object]:
    mode_normalise = (mode or "preview").strip().lower()
    if mode_normalise in {"zoom", "grand", "lecture", "modal"}:
        return {
            "suffixe": "lecteur_v3",
            "largeur": PREVIEW_ZOOM_LARGEUR,
            "fps": PREVIEW_ZOOM_FPS,
            "max_images": PREVIEW_ZOOM_MAX_IMAGES,
            "audio": True,
            "coupe_fin_images": PREVIEW_ZOOM_COUPE_FIN_IMAGES,
        }
    return {
        "suffixe": "preview_v2",
        "largeur": PREVIEW_LARGEUR,
        "fps": PREVIEW_FPS,
        "max_images": PREVIEW_MAX_IMAGES,
        "audio": False,
        "coupe_fin_images": 0,
    }


def chemin_apercu_video(film_id: str, numero: int, mode: str | None = None) -> Path:
    profil = profil_apercu(mode)
    return ANALYSE / film_id / PREVIEW_DIR / f"{int(numero):05d}_{profil['suffixe']}.mp4"


def generer_apercu_video_plan(film_id: str, numero: int, mode: str | None = None) -> Path:
    data, plan, fichier_plans = plan_par_numero(film_id, numero)
    source = Path(str(data.get("source") or "")).expanduser()
    if not source.exists():
        raise FileNotFoundError(f"Source vidéo absente : {source}")
    profil = profil_apercu(mode)
    sortie = chemin_apercu_video(film_id, numero, mode)
    sortie.parent.mkdir(parents=True, exist_ok=True)
    try:
        reference_mtime = max(source.stat().st_mtime, fichier_plans.stat().st_mtime)
        if sortie.exists() and sortie.stat().st_size > 0 and sortie.stat().st_mtime >= reference_mtime:
            return sortie
    except OSError:
        pass

    debut, fin = fenetre_apercu_plan(plan)
    duree = max(PREVIEW_DUREE_MIN, fin - debut)
    cadence = min(
        float(profil["fps"]),
        max(2.0, float(profil["max_images"]) / max(duree, PREVIEW_DUREE_MIN)),
    )
    coupe_fin = max(0.0, float(profil.get("coupe_fin_images") or 0) / max(cadence, 1.0))
    duree_min_sortie = max(0.08, 2.0 / max(cadence, 1.0))
    if coupe_fin > 0 and duree > duree_min_sortie:
        duree = max(duree_min_sortie, duree - coupe_fin)
    tmp = sortie.with_suffix(".tmp.mp4")
    commande = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{debut:.3f}", "-t", f"{duree:.3f}", "-i", str(source),
        "-map", "0:v:0",
    ]
    if bool(profil["audio"]):
        commande += ["-map", "0:a?"]
    commande += [
        "-sn", "-dn",
        "-vf", f"fps={cadence:.3f},scale={int(profil['largeur'])}:-2:flags=lanczos",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "24" if bool(profil["audio"]) else "28",
        "-pix_fmt", "yuv420p",
    ]
    if bool(profil["audio"]):
        commande += ["-c:a", "aac", "-b:a", "128k"]
    else:
        commande += ["-an"]
    commande += ["-movflags", "+faststart", str(tmp)]
    r = subprocess.run(
        commande,
        text=True,
        capture_output=True,
        check=False,
    )
    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size <= 0:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        message = (r.stderr or r.stdout or "ffmpeg n’a pas produit d’aperçu vidéo").strip()
        raise RuntimeError(message[-400:])
    tmp.replace(sortie)
    return sortie


def chemin_media_export(valeur: str | None) -> Path | None:
    texte = str(valeur or "").strip()
    if not texte:
        return None
    chemin = Path(texte)
    if not chemin.is_absolute():
        chemin = ANALYSE / chemin
    try:
        return chemin if chemin.exists() and chemin.is_file() else None
    except OSError:
        return None


def decoder_identifiant_plan_selection(identifiant: str) -> tuple[str, int]:
    film_id, sep, numero_txt = str(identifiant or "").partition("#")
    film_id = film_id.strip()
    if not sep or not film_id or not numero_txt.strip():
        raise ValueError(f"Identifiant de plan invalide : {identifiant}")
    return film_id, int(numero_txt)


def exporter_selection_zip(identifiants: list[str]) -> tuple[bytes, str]:
    if not identifiants:
        raise ValueError("Aucun plan sélectionné.")

    manifestes: list[dict] = []
    deja_vus: set[str] = set()
    cache_films: dict[str, tuple[dict, dict[int, dict]]] = {}
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for identifiant in identifiants:
            if identifiant in deja_vus:
                continue
            deja_vus.add(identifiant)

            film_id, numero = decoder_identifiant_plan_selection(identifiant)
            if film_id not in cache_films:
                data, _ = charger_plans_film(film_id)
                plans_par_numero: dict[int, dict] = {}
                for plan in data.get("plans", []):
                    try:
                        plans_par_numero[int(plan.get("n", 0))] = plan
                    except (TypeError, ValueError):
                        continue
                cache_films[film_id] = (data, plans_par_numero)

            data, plans_par_numero = cache_films[film_id]
            plan = plans_par_numero.get(numero)
            if not plan:
                raise FileNotFoundError(f"Plan introuvable pour export : {identifiant}")

            fiche = data.get("fiche") or {}
            titre_film = str(fiche.get("titre") or data.get("film") or film_id).strip() or film_id
            dossier_zip = f"{slug(titre_film)}_{film_id}/plan_{numero:05d}"
            medias: list[dict[str, str]] = []
            medias_deja_ajoutes: set[str] = set()

            def ajouter_media(role: str, valeur: str | None) -> None:
                chemin = chemin_media_export(valeur)
                if not chemin:
                    return
                arcname = f"{dossier_zip}/{chemin.name}"
                if arcname not in medias_deja_ajoutes:
                    archive.write(chemin, arcname)
                    medias_deja_ajoutes.add(arcname)
                medias.append({"role": role, "fichier": arcname})

            ajouter_media("vignette", plan.get("vignette"))
            for i, vignette in enumerate(plan.get("vignettes") or []):
                ajouter_media(f"vignette_{i}", vignette)
            ajouter_media("apercu", plan.get("apercu"))

            meta = {
                "id": identifiant,
                "film_id": film_id,
                "film": titre_film,
                "plan": numero,
                "tc": plan.get("tc"),
                "debut": plan.get("debut"),
                "fin": plan.get("fin"),
                "duree": plan.get("duree"),
                "scene": plan.get("scene_titre"),
                "description": plan.get("description"),
                "mots_cles": plan.get("mots_cles") or [],
                "fichiers": medias,
            }
            archive.writestr(
                f"{dossier_zip}/metadata.json",
                json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
            )
            manifestes.append(meta)

        archive.writestr(
            "selection.json",
            json.dumps(
                {
                    "genere": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "plans": manifestes,
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
        )

    nom = f"selection-plans-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    return buffer.getvalue(), nom


def films_progression() -> list[dict]:
    """Progression par film depuis les plans.json existants."""
    maintenant = time.time()
    if cache_films_progression.get("data") is not None and maintenant - float(cache_films_progression.get("lu") or 0) < 10:
        return [dict(item) for item in cache_films_progression["data"]]
    lignes = []
    for fichier in sorted(ANALYSE.glob("*/plans.json")):
        try:
            data = json.loads(fichier.read_text(encoding="utf-8"))
            plans = plans_visibles_pour_controle(data)
            total = len(plans)
            temps_analyse = data.get("temps_analyse_secondes")
            film_id = fichier.parent.name
            prepares = sum(
                1 for p in plans
                if p.get("vignette")
                and isinstance(p.get("vignettes"), list)
                and len(p.get("vignettes") or []) >= 3
                and all(str(v or "").strip() for v in (p.get("vignettes") or [])[:3])
            )
            tentes = sum(1 for p in plans if plan_analyse_tentee(p))
            complets = sum(
                1 for p in plans
                if (p.get("analyse") or {}).get("description")
                and (p.get("analyse") or {}).get("echelle")
                and (p.get("analyse") or {}).get("lieu")
            )
            compte_modeles: dict[str, int] = {}
            for plan in plans:
                candidats = [
                    plan.get("analyse_modele"),
                    (plan.get("analyse") or {}).get("modele_analyse"),
                ]
                for candidat in candidats:
                    texte = str(candidat or "").strip()
                    if texte:
                        compte_modeles[texte] = compte_modeles.get(texte, 0) + 1
                        break
            modele_utilise = ""
            if compte_modeles:
                modele_utilise = sorted(compte_modeles.items(), key=lambda item: (-item[1], item[0]))[0][0]
            if not modele_utilise:
                modele_utilise = str(data.get("modele") or "").strip() or modele_analyse_film(film_id)
            mouvements = resume_mouvements_camera(plans)
            lignes.append({
                "id": film_id,
                "titre": data.get("titre") or film_id,
                "source": Path(data.get("source", "")).name,
                "modele_analyse": modele_utilise,
                "scenes": len(data.get("scenes") or []),
                "personnages_recurrents": len(data.get("personnages_recurrents") or []),
                "plans": total,
                "plans_prepares": prepares,
                "plans_tentes": tentes,
                "pourcentage_preparation": round(prepares / max(total, 1) * 100),
                "plans_complets": complets,
                "pourcentage": round(complets / max(total, 1) * 100),
                "termine": film_analyse_terminee(total, prepares, tentes, complets),
                "temps_analyse_secondes": temps_analyse,
                "temps_analyse": data.get("temps_analyse_humain") or formater_duree(temps_analyse),
                "temps_analyse_mesure": temps_analyse is not None,
                "analyse_derniere_mesure": data.get("analyse_derniere_mesure"),
                **resume_couches_film(data, plans),
                **mouvements,
            })
        except Exception:
            continue
    cache_films_progression["lu"] = maintenant
    cache_films_progression["data"] = [dict(item) for item in lignes]
    return lignes


def lire_index() -> dict:
    index = ANALYSE / "index.json"
    if not index.exists():
        return {"films": 0, "plans": 0, "machines": 0, "genere": None}
    try:
        mtime_ns = index.stat().st_mtime_ns
        if cache_index_resume.get("data") is not None and cache_index_resume.get("mtime_ns") == mtime_ns:
            return dict(cache_index_resume["data"])
        data = json.loads(index.read_text(encoding="utf-8"))
        plans = data.get("plans", [])
        films = data.get("films", [])
        resume = {
            "films": len(films),
            "plans": len(plans),
            "machines": sum(1 for p in plans if p.get("machine")),
            "contextes_films": sum(1 for f in films if (f.get("pitch") or f.get("synopsis")) and f.get("scenario")),
            "scenes": sum(int(f.get("scenes") or 0) for f in films),
            "personnages_recurrents": sum(len(f.get("personnages_recurrents") or []) for f in films),
            "dialogues": sum(1 for p in plans if p.get("dialogue")),
            "affinages": sum(1 for p in plans if p.get("affinage")),
            "mouvements_camera": sum(1 for p in plans if p.get("mouvement_camera")),
            "mouvements_camera_video": sum(1 for p in plans if p.get("mouvement_video_modele")),
            "mouvements_camera_final": sum(1 for p in plans if p.get("mouvement_camera_final")),
            "mouvements_camera_conflits": sum(1 for p in plans if p.get("mouvement_camera_conflit")),
            "genere": data.get("genere"),
        }
        cache_index_resume["mtime_ns"] = mtime_ns
        cache_index_resume["data"] = dict(resume)
        return resume
    except Exception as exc:
        return {"films": 0, "plans": 0, "machines": 0, "genere": None, "erreur": str(exc)}


def lire_index_photos() -> dict:
    index = PHOTO_ANALYSE / "index.json"
    if not index.exists():
        return {"photos": 0, "a_verifier": 0, "genere": None}
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
        photos = data.get("photos", [])
        return {
            "photos": len(photos),
            "a_verifier": sum(1 for p in photos if p.get("a_verifier")),
            "avec_description": sum(1 for p in photos if p.get("description")),
            "avec_vignette": sum(1 for p in photos if p.get("vignette") and (ANALYSE / str(p.get("vignette"))).exists()),
            "types": len({p.get("type_image") for p in photos if p.get("type_image")}),
            "genere": data.get("genere"),
        }
    except Exception as exc:
        return {"photos": 0, "a_verifier": 0, "genere": None, "erreur": str(exc)}


def progression_photos() -> dict:
    actif = photos_deja_actives()
    info = {
        "actif": actif,
        "phase": "analyse photo active" if actif else "photo arrêtée",
        "courant": 0,
        "total": compter_photos_source(),
        "pourcentage": 0,
        "photo": "",
        "photo_id": "",
        "modele": modele_photos(),
        "termine": False,
    }
    fichier = PHOTO_ANALYSE / "progression.json"
    if fichier.exists():
        try:
            data = json.loads(fichier.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                info.update({k: data.get(k, info.get(k)) for k in [
                    "phase", "courant", "total", "pourcentage", "photo", "photo_id", "modele"
                ]})
        except Exception as exc:
            info["erreur"] = str(exc)
    total = int(info.get("total") or 0)
    courant = int(info.get("courant") or 0)
    if total and not info.get("pourcentage"):
        info["pourcentage"] = round(courant / max(total, 1) * 100)
    if not actif and total and courant >= total:
        info["termine"] = True
    info["actif"] = actif
    return info


def progression_audio() -> dict:
    log = ROOT / "controle_audio.log"
    actif = audio_actif_detail()
    info = {
        "actif": audio_deja_active(),
        "courant": 0,
        "total": 0,
        "pourcentage": 0,
        "film": "",
        "source": "",
        "plans_dialogues": 0,
        "termine": False,
        "pid": actif.get("pid"),
    }
    if not log.exists():
        return info
    try:
        lignes = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return info
    motif = re.compile(r"^\[(\d+)/(\d+)\]\s+(.+?)\s+—\s+(.+?)\s+—\s+(\d+)\s+plans avec dialogue")
    for ligne in reversed(lignes):
        m = motif.search(ligne.strip())
        if not m:
            continue
        courant = int(m.group(1))
        total = int(m.group(2))
        info.update({
            "courant": courant,
            "total": total,
            "pourcentage": round(courant / max(total, 1) * 100),
            "film": m.group(3),
            "source": m.group(4),
            "plans_dialogues": int(m.group(5)),
            "termine": (courant >= total) and (not info["actif"]),
        })
        if actif.get("film") and actif.get("courant"):
            info["courant"] = actif["courant"]
            info["total"] = actif.get("total") or info["total"]
            info["pourcentage"] = round(info["courant"] / max(info["total"], 1) * 100)
            info["film"] = actif["film"]
            info["source"] = actif.get("source") or info["source"]
        return info
    for ligne in reversed(lignes):
        if "Dialogues indexés" in ligne:
            info["termine"] = not info["actif"]
            if not info["actif"]:
                resume = lire_index()
                total = int(resume.get("films") or 0)
                info.update({
                    "courant": total,
                    "total": total,
                    "pourcentage": 100 if total else 0,
                })
            break
    if actif.get("film") and actif.get("courant"):
        info["courant"] = actif["courant"]
        info["total"] = actif.get("total") or info["total"]
        info["pourcentage"] = round(info["courant"] / max(info["total"], 1) * 100)
        info["film"] = actif["film"]
        info["source"] = actif.get("source") or info["source"]
    return info


def musique_deja_active() -> bool:
    motif = re.compile(r"python[\w./-]*\s+.*analyse_musique\.py\s+analyse")
    exclus = {str(os.getpid())}
    for pid, commande in instantane_processus():
        if pid in exclus or "grep" in commande or "hermes-verify-" in commande or "python3 - <<" in commande:
            continue
        if commande.startswith("/bin/bash -lc "):
            continue
        if motif.search(commande):
            return True
    return False


def scenes_deja_actives() -> bool:
    motif = re.compile(r"python[\w./-]*\s+.*analyse_scenes\.py\s+analyse")
    exclus = {str(os.getpid())}
    for pid, commande in instantane_processus():
        if pid in exclus or "grep" in commande or "hermes-verify-" in commande or "python3 - <<" in commande:
            continue
        if commande.startswith("/bin/bash -lc "):
            continue
        if motif.search(commande):
            return True
    return False


def analyse_active_detail() -> dict:
    info = {"pid": None, "film": "", "source": "", "modele": "", "etape": "", "commande": ""}
    exclus = {str(os.getpid())}
    for pid, commande in instantane_processus():
        if pid in exclus or "grep" in commande or "hermes-verify-" in commande or "python3 - <<" in commande:
            continue
        if commande.startswith("/bin/bash -lc "):
            continue
        if "analyse_plans.py" not in commande or " --mode complet" not in commande and " --mode triage" not in commande:
            continue
        info["commande"] = commande
        try:
            info["pid"] = int(pid)
        except ValueError:
            info["pid"] = None
        m_modele = re.search(r"--modele\s+([^\s]+)", commande)
        if m_modele:
            info["modele"] = m_modele.group(1)
        try:
            morceaux = shlex.split(commande)
        except ValueError:
            morceaux = []
        if morceaux and "analyse_plans.py" in " ".join(morceaux):
            try:
                idx_script = next(i for i, part in enumerate(morceaux) if part.endswith("analyse_plans.py"))
                idx_sortie = morceaux.index("--sortie", idx_script + 1)
                sources = [part for part in morceaux[idx_script + 1:idx_sortie] if part and not part.startswith("--")]
            except (StopIteration, ValueError):
                sources = []
            if len(sources) == 1:
                brut = sources[0].strip()
                info["source"] = brut
                nom = Path(brut).stem if brut else ""
                if nom:
                    info["film"] = slug(nom)
        info["etape"] = "plans"
        return info
    return info


def progression_musique() -> dict:
    log = ROOT / "controle_musique.log"
    info = {
        "actif": musique_deja_active(),
        "courant": 0,
        "total": 0,
        "pourcentage": 0,
        "film": "",
        "sequence": "",
        "plans": 0,
        "termine": False,
    }
    if not log.exists():
        return info
    try:
        lignes = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return info
    motif = re.compile(r"^\[(\d+)/(\d+)\]\s+(.+?)\s+(seq_\d+_\d+)\s+—\s+(.+?)\s+→\s+(.+?)\s+—\s+(\d+)\s+plans")
    for ligne in reversed(lignes):
        m = motif.search(ligne.strip())
        if not m:
            continue
        courant = int(m.group(1))
        total = int(m.group(2))
        info.update({
            "courant": courant,
            "total": total,
            "pourcentage": round(courant / max(total, 1) * 100),
            "film": m.group(3),
            "sequence": m.group(4),
            "plans": int(m.group(7)),
            "termine": (courant >= total) and (not info["actif"]),
        })
        return info
    for ligne in reversed(lignes):
        if "Passe musique globale terminée" in ligne:
            info["termine"] = not info["actif"]
            break
    return info


def audio_ou_musique_active() -> bool:
    return audio_deja_active() or musique_deja_active()


def progression_mouvements() -> dict:
    log = ROOT / "controle_mouvements.log"
    info = {
        "actif": mouvements_deja_actifs(),
        "courant": 0,
        "total": 0,
        "film": "",
        "plans_mesures": 0,
        "plans_total": 0,
        "pourcentage": 0,
        "termine": False,
    }
    if not log.exists():
        return info
    try:
        texte = log.read_text(encoding="utf-8", errors="replace").replace("\r", "\n")
    except Exception:
        return info
    lignes = [ligne.strip() for ligne in texte.splitlines() if ligne.strip()]
    motif_film = re.compile(r"^\[(\d+)/(\d+)\]\s+(.+)$")
    motif_plans = re.compile(r"^(\d+)\s+plans mesurés")
    courant_film = total_films = 0
    film = ""
    plans_mesures = 0
    for ligne in lignes:
        m = motif_film.search(ligne)
        if m:
            courant_film = int(m.group(1))
            total_films = int(m.group(2))
            film = m.group(3).strip()
            plans_mesures = 0
            continue
        p = motif_plans.search(ligne)
        if p:
            plans_mesures = int(p.group(1))
    info.update({
        "courant": courant_film,
        "total": total_films,
        "film": film,
        "plans_mesures": plans_mesures,
        "termine": (courant_film >= total_films > 0) and (not info["actif"]),
    })
    if film:
        plans_json = ANALYSE / film / "plans.json"
        if plans_json.exists():
            try:
                data = json.loads(plans_json.read_text(encoding="utf-8"))
                total_plans = len(data.get("plans", []))
                info["plans_total"] = total_plans
                if total_plans:
                    base = plans_mesures or sum(1 for p in data.get("plans", []) if p.get("mouvement_camera"))
                    info["plans_mesures"] = base
                    info["pourcentage"] = round(base / max(total_plans, 1) * 100)
            except Exception:
                pass
    return info


def processus_mouvements_video() -> list[dict]:
    lignes = []
    exclus = {str(os.getpid())}
    motif = re.compile(r"python[\w./-]*\s+.*analyse_mouvements_videomae\.py\s+analyse")
    for pid, commande in instantane_processus():
        if pid in exclus or "grep" in commande or "hermes-verify-" in commande or "python3 - <<" in commande:
            continue
        if motif.search(commande):
            try:
                lignes.append({"pid": int(pid), "commande": commande})
            except ValueError:
                continue
    return lignes


def progression_mouvements_video() -> dict:
    log = ROOT / "controle_mouvements_video.log"
    info = {
        "actif": mouvements_video_deja_actifs(),
        "pid": None,
        "courant": 0,
        "total": 0,
        "film": "",
        "plans_classes": 0,
        "plans_total": 0,
        "pourcentage": 0,
        "modele": MODELE_MOUVEMENTS_VIDEO,
        "termine": False,
    }
    processus = processus_mouvements_video()
    if processus:
        principal = processus[0]
        info["pid"] = principal.get("pid")
        commande = str(principal.get("commande") or "")
        m_film = re.search(r"(?:^|\s)--film\s+([^\s]+)", commande)
        if m_film:
            info["film"] = m_film.group(1).strip()
        m_modele = re.search(r"(?:^|\s)--modele\s+([^\s]+)", commande)
        if m_modele:
            info["modele"] = m_modele.group(1).strip()
    if log.exists():
        try:
            texte = log.read_text(encoding="utf-8", errors="replace").replace("\r", "\n")
        except Exception:
            texte = ""
        if texte:
            lignes = [ligne.strip() for ligne in texte.splitlines() if ligne.strip()]
            motif_film = re.compile(r"^\[(\d+)/(\d+)\]\s+(.+)$")
            motif_plans = re.compile(r"^(\d+)\s+plans classés VideoMAE")
            courant_film = total_films = 0
            film = info["film"]
            plans_classes = 0
            for ligne in lignes:
                m = motif_film.search(ligne)
                if m:
                    courant_film = int(m.group(1))
                    total_films = int(m.group(2))
                    film = m.group(3).strip()
                    plans_classes = 0
                    continue
                p = motif_plans.search(ligne)
                if p:
                    plans_classes = int(p.group(1))
            info.update({
                "courant": courant_film,
                "total": total_films,
                "film": film or info.get("film") or "",
                "plans_classes": plans_classes,
                "termine": (courant_film >= total_films > 0) and (not info["actif"]),
            })
    if info.get("film"):
        plans_json = ANALYSE / str(info["film"]) / "plans.json"
        if plans_json.exists():
            try:
                data = json.loads(plans_json.read_text(encoding="utf-8"))
                total_plans = len(data.get("plans", []))
                info["plans_total"] = total_plans
                if total_plans:
                    base = sum(1 for p in data.get("plans", []) if p.get("mouvement_video_modele"))
                    if base:
                        info["plans_classes"] = max(int(info.get("plans_classes") or 0), base)
                    info["pourcentage"] = round(info["plans_classes"] / max(total_plans, 1) * 100)
            except Exception:
                pass
    return info


def synchroniser_pages_site() -> None:
    for nom in PAGES_SITE:
        source = ROOT / nom
        if source.exists():
            (ANALYSE / nom).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def reconstruire_index(source: str = "automatique") -> dict:
    if not verrou_index.acquire(blocking=False):
        return {"ok": True, "status": "already_running", "message": "Reconstruction de l’index déjà en cours."}
    try:
        with verrou:
            etat["index_en_cours"] = True
            etat["dernier_index_message"] = f"Reconstruction de l’index demandée ({source})…"
        synchroniser_fiches_films_sources()
        r = subprocess.run(
            [str(PY), "analyse_plans.py", "--sortie", "analyse", "--index-seul"],
            cwd=ROOT,
            env=env_nettoye(),
            text=True,
            capture_output=True,
            timeout=300,
            check=False,
        )
        if r.returncode != 0:
            message = (r.stderr or r.stdout or "erreur inconnue").strip()[-500:]
            with verrou:
                etat["dernier_index_message"] = f"Erreur de reconstruction de l’index : {message}"
            return {"ok": False, "status": "error", "message": etat["dernier_index_message"]}
        synchroniser_pages_site()
        resume = lire_index()
        message = f"Index mis à jour : {resume['films']} films, {resume['plans']} plans."
        with verrou:
            etat["derniere_indexation"] = time.strftime("%Y-%m-%d %H:%M:%S")
            etat["dernier_index_message"] = message
        return {"ok": True, "status": "updated", "message": message, "index": resume}
    except Exception as exc:
        with verrou:
            etat["dernier_index_message"] = f"Erreur de reconstruction de l’index : {exc}"
        return {"ok": False, "status": "error", "message": etat["dernier_index_message"]}
    finally:
        with verrou:
            etat["index_en_cours"] = False
        verrou_index.release()


def boucle_index() -> None:
    # Laisser le launcher ouvrir l’accueil et le catalogue sur l’index existant
    # avant toute réécriture. Sinon un démarrage du contrôle peut coïncider avec
    # un fetch navigateur de index.json et produire un état vide/intermittent.
    time.sleep(30)
    reconstruire_index("démarrage")
    while True:
        time.sleep(INDEX_INTERVALLE)
        reconstruire_index("mise à jour périodique")


def analyse_deja_active() -> bool:
    return bool(processus_analyse())


def instantane_processus(force: bool = False) -> list[tuple[str, str]]:
    maintenant = time.time()
    with verrou_processus:
        if (not force and cache_processus.get("data") is not None
                and (maintenant - float(cache_processus.get("lu") or 0.0)) < PROCESSUS_CACHE_TTL):
            return list(cache_processus.get("data") or [])
    try:
        r = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            text=True,
            capture_output=True,
            check=False,
            timeout=2,
        )
        snapshot = []
        for brute in r.stdout.splitlines():
            ligne = brute.strip()
            if not ligne:
                continue
            pid, _, commande = ligne.partition(" ")
            snapshot.append((pid.strip(), commande.strip()))
        with verrou_processus:
            cache_processus["lu"] = time.time()
            cache_processus["data"] = snapshot
            cache_processus["erreur"] = None
        return list(snapshot)
    except (OSError, subprocess.SubprocessError) as exc:
        with verrou_processus:
            precedent = list(cache_processus.get("data") or [])
            cache_processus["erreur"] = str(exc)
            cache_processus["lu"] = time.time()
        with verrou:
            etat["dernier_message"] = f"Contrôle allégé : réutilisation du cache processus ({type(exc).__name__}: {exc})"
        return precedent


def processus_analyse() -> list[int]:
    pids = []
    exclus = {str(os.getpid())}
    commande_analyse_relative = f"{PY} analyse_plans.py"
    commande_analyse_absolue = f"{PY} {ROOT / 'analyse_plans.py'}"
    for pid, commande in instantane_processus():
        if pid in exclus:
            continue
        if "hermes-verify-" in commande or "pgrep" in commande or "grep" in commande:
            continue
        if " --mode complet" not in commande and " --mode triage" not in commande:
            continue
        if commande_analyse_relative not in commande and commande_analyse_absolue not in commande:
            continue
        try:
            pids.append(int(pid))
        except ValueError:
            pass
    return sorted(set(pids))


def processus_avec_mots(*mots: str) -> list[int]:
    pids = []
    exclus = {str(os.getpid())}
    for pid, commande in instantane_processus():
        if pid in exclus or "hermes-verify-" in commande or "pgrep" in commande or "grep" in commande:
            continue
        if all(mot in commande for mot in mots):
            try:
                pids.append(int(pid))
            except ValueError:
                pass
    return pids


def audio_deja_active() -> bool:
    for _, commande in instantane_processus():
        if "hermes-verify-" in commande or "python3 - <<" in commande:
            continue
        if "analyse_son_dialogues.py" in commande or ("whisper " in commande and "--task transcribe" in commande):
            return True
    return False


def processus_audio() -> list[dict]:
    lignes = []
    exclus = {str(os.getpid())}
    for pid, commande in instantane_processus():
        if pid in exclus or "grep" in commande or "hermes-verify-" in commande or "python3 - <<" in commande:
            continue
        if "analyse_son_dialogues.py" in commande or ("whisper " in commande and "--task transcribe" in commande):
            try:
                lignes.append({"pid": int(pid), "commande": commande})
            except ValueError:
                continue
    return lignes


def position_film_audio(film_id: str) -> tuple[int, int]:
    fichiers = sorted(ANALYSE.glob("*/plans.json"))
    total = len(fichiers)
    for i, fichier in enumerate(fichiers, 1):
        if fichier.parent.name == film_id:
            return i, total
    return 0, total


def audio_actif_detail() -> dict:
    info = {"pid": None, "film": "", "courant": 0, "total": 0, "source": "", "commande": ""}
    processus = processus_audio()
    if not processus:
        return info
    whisper = next((p for p in processus if "whisper " in p["commande"] and "--task transcribe" in p["commande"]), None)
    principal = whisper or processus[0]
    info["pid"] = principal["pid"]
    info["commande"] = principal["commande"]
    if whisper:
        m = re.search(r"analyse/([^/]+)/dialogues/whisper\.wav", whisper["commande"])
        if m:
            film_id = m.group(1)
            courant, total = position_film_audio(film_id)
            info.update({
                "film": film_id,
                "courant": courant,
                "total": total,
                "source": "whisper local (base)",
            })
            return info
    analyse = next((p for p in processus if "analyse_son_dialogues.py" in p["commande"]), None)
    if analyse:
        info["pid"] = analyse["pid"]
    return info


def mouvements_deja_actifs() -> bool:
    return bool(processus_avec_mots("analyse_mouvements.py", "analyse"))


def mouvements_video_deja_actifs() -> bool:
    exclus = {str(os.getpid())}
    motif = re.compile(r"python[\w./-]*\s+.*analyse_mouvements_videomae\.py\s+analyse")
    for pid, commande in instantane_processus():
        if pid in exclus or "grep" in commande or "hermes-verify-" in commande or "python3 - <<" in commande:
            continue
        if motif.search(commande):
            return True
    return False


def affinage_deja_actif() -> bool:
    exclus = {str(os.getpid())}
    motif = re.compile(r"python[\w./-]*\s+.*analyse_affinage\.py\s+analyse")
    for pid, commande in instantane_processus():
        if pid in exclus or "grep" in commande or "hermes-verify-" in commande or "python3 - <<" in commande:
            continue
        if motif.search(commande):
            return True
    return False


def processus_photos() -> list[int]:
    pids = []
    exclus = {str(os.getpid())}
    motif = re.compile(r"python[\w./-]*\s+.*analyse_photos\.py")
    for pid, commande in instantane_processus():
        if pid in exclus or "grep" in commande or "hermes-verify-" in commande or "python3 - <<" in commande:
            continue
        if motif.search(commande) and "--index-seul" not in commande and "--verifier" not in commande:
            try:
                pids.append(int(pid))
            except ValueError:
                pass
    return pids


def photos_deja_actives() -> bool:
    return bool(processus_photos())


def commande_analyse(film_id: str | None = None, modele: str | None = None, refaire: bool = False) -> str:
    infos_cibles = []
    if film_id:
        cible = video_source_par_id(film_id)
        infos_cibles = [{"id": film_id, "chemin": str(cible)}]
    elif refaire:
        cible = dossier_films()
    else:
        infos_cibles = films_a_analyser_details()
        cible = [Path(info["chemin"]) for info in infos_cibles]
    catalogue = ROOT / "films_fiches.json"
    modele = normaliser_modele_analyse(modele or modele_analyse_film(film_id))
    moteur = moteur_pour_modele(modele)
    args = [
        str(PY), "analyse_plans.py",
        *([str(cible)] if isinstance(cible, Path) else [str(c) for c in cible]),
        "--sortie", "analyse",
        "--moteur", moteur,
        "--mode", "complet",
        "--modele", modele,
        "--largeur", str(LARGEUR_ANALYSE),
        "--leger",
    ]
    options = options_analyse_film(film_id)
    try:
        seuil = float(options.get("seuil"))
        if seuil > 0:
            args += ["--seuil", str(seuil)]
    except (TypeError, ValueError):
        pass
    if options.get("apercu"):
        args.append("--apercu")
    contexte_films = films_contexte().strip()
    if contexte_films:
        args += ["--contexte-libre", contexte_films]
    criteres_films = films_criteres()
    if criteres_films:
        args += ["--criteres-libre", " | ".join(criteres_films)]
    if moteur == "mlx":
        args += ["--concurrence", str(concurrence_mlx())]
    if refaire:
        args.append("--refaire")
    if catalogue.exists():
        args += ["--catalogue", "films_fiches.json"]
    commandes = [" ".join(subprocess.list2cmdline([a]) for a in args)]
    films_cibles = [film_id] if film_id else [info["id"] for info in infos_cibles]
    for script in ("analyse_scenes.py", "analyse_presences.py", "analyse_personnages_recurrents.py"):
        if films_cibles:
            for cible_film in films_cibles:
                extra = [str(PY), script, "analyse", "--film", cible_film, "--index"]
                if refaire:
                    extra.append("--refaire")
                commandes.append(" ".join(subprocess.list2cmdline([a]) for a in extra))
        else:
            extra = [str(PY), script, "analyse", "--index"]
            if refaire:
                extra.append("--refaire")
            commandes.append(" ".join(subprocess.list2cmdline([a]) for a in extra))
    return (
        f"cd {subprocess.list2cmdline([str(ROOT)])} && "
        f"unset PYTHONPATH PYTHONHOME && export PYTHONUNBUFFERED=1 && " + " && ".join(commandes) + " && "
        + SYNC_PAGES_COMMANDE
    )


def commande_scenes(film_id: str, refaire: bool = False) -> str:
    args = [
        str(PY), "analyse_scenes.py", "analyse",
        "--film", film_id,
        "--index",
    ]
    if refaire:
        args.append("--refaire")
    quoted = " ".join(subprocess.list2cmdline([a]) for a in args)
    return (
        f"cd {subprocess.list2cmdline([str(ROOT)])} && "
        f"unset PYTHONPATH PYTHONHOME && {quoted} && "
        + SYNC_PAGES_COMMANDE
    )


def commande_audio(film_id: str | None = None, refaire: bool = False) -> str:
    args = [
        str(PY), "analyse_son_dialogues.py", "analyse",
        "--index-seul",
    ]
    if film_id:
        args += ["--film", film_id]
    if refaire:
        args.append("--refaire")
    quoted = " ".join(subprocess.list2cmdline([a]) for a in args)
    return (
        f"cd {subprocess.list2cmdline([str(ROOT)])} && "
        f"unset PYTHONPATH PYTHONHOME && {quoted} && "
        + SYNC_PAGES_COMMANDE
    )


def commande_musique(film_id: str | None = None, modele: str | None = None, refaire: bool = False) -> str:
    modele = normaliser_modele_ollama_analyse(modele or MODELE_AFFINAGE, defaut=MODELE_AFFINAGE)
    args = [
        str(PY), "analyse_musique.py", "analyse",
        "--mode", "sequences",
        "--modele", modele,
        "--film-entier",
        "--empreinte-mode", "auto",
        "--index-seul",
    ]
    if film_id:
        args += ["--film", film_id]
    if refaire:
        args.append("--refaire")
    quoted = " ".join(subprocess.list2cmdline([a]) for a in args)
    return (
        f"cd {subprocess.list2cmdline([str(ROOT)])} && "
        f"unset PYTHONPATH PYTHONHOME && {quoted} && "
        + SYNC_PAGES_COMMANDE
    )


def commande_mouvements(film_id: str | None = None, refaire: bool = False) -> str:
    args = [
        str(PY), "analyse_mouvements.py", "analyse",
        "--index-seul",
    ]
    if film_id:
        args += ["--film", film_id]
    if refaire:
        args.append("--refaire")
    quoted = " ".join(subprocess.list2cmdline([a]) for a in args)
    return (
        f"cd {subprocess.list2cmdline([str(ROOT)])} && "
        f"unset PYTHONPATH PYTHONHOME && {quoted} && "
        + SYNC_PAGES_COMMANDE
    )


def commande_mouvements_video(film_id: str | None = None, modele: str | None = None, refaire: bool = False) -> str:
    modele = normaliser_modele_mouvements_video(modele)
    args = [
        str(PY), "analyse_mouvements_videomae.py", "analyse",
        "--modele", modele,
        "--difficiles",
        "--index-seul",
    ]
    if not film_id:
        args += ["--limite-plans", "40"]
    if film_id:
        args += ["--film", film_id]
    if refaire:
        args.append("--refaire")
    quoted = " ".join(subprocess.list2cmdline([a]) for a in args)
    return (
        f"cd {subprocess.list2cmdline([str(ROOT)])} && "
        f"unset PYTHONPATH PYTHONHOME && {quoted} && "
        + SYNC_PAGES_COMMANDE
    )


def commande_affinage(film_id: str | None = None, modele: str | None = None) -> str:
    modele = normaliser_modele_ollama_analyse(modele or MODELE_AFFINAGE, defaut=MODELE_AFFINAGE)
    args = [
        str(PY), "analyse_affinage.py", "analyse",
        "--modele-affinage", modele,
        "--limite", "20",
        "--index-seul",
    ]
    if film_id:
        args += ["--film", film_id]
    quoted = " ".join(subprocess.list2cmdline([a]) for a in args)
    return (
        f"cd {subprocess.list2cmdline([str(ROOT)])} && "
        f"unset PYTHONPATH PYTHONHOME && {quoted} && "
        + SYNC_PAGES_COMMANDE
    )


def commande_photos(refaire: bool = False, modele: str | None = None, photo_id: str | None = None) -> str:
    dossier = dossier_photos()
    modele = normaliser_modele_ollama_analyse(modele or modele_photos(), defaut=modele_photos())
    args = [
        str(PY), "analyse_photos.py", str(dossier),
        "--sortie", "analyse/photos",
        "--config", "config.json",
        "--modele", modele,
        "--largeur", str(LARGEUR_ANALYSE),
    ]
    if photo_id:
        args += ["--photo-id", str(photo_id)]
    if refaire:
        args.append("--refaire")
    quoted = " ".join(subprocess.list2cmdline([a]) for a in args)
    return (
        f"cd {subprocess.list2cmdline([str(ROOT)])} && "
        f"unset PYTHONPATH PYTHONHOME && {quoted} && "
        + SYNC_PAGES_COMMANDE
    )


def commande_photos_index(modele: str | None = None) -> str:
    modele = normaliser_modele_ollama_analyse(modele or modele_photos(), defaut=modele_photos())
    args = [
        str(PY), "analyse_photos.py", str(dossier_photos()),
        "--sortie", "analyse/photos",
        "--config", "config.json",
        "--modele", modele,
        "--index-seul",
    ]
    quoted = " ".join(subprocess.list2cmdline([a]) for a in args)
    return (
        f"cd {subprocess.list2cmdline([str(ROOT)])} && "
        f"unset PYTHONPATH PYTHONHOME && {quoted} && "
        + SYNC_PAGES_COMMANDE
    )


def lancer_analyse(source: str, modele: str | None = None, film_id: str | None = None, refaire: bool = False, activer_toggle: bool = False) -> dict:
    if activer_toggle:
        memoriser_toggle_analyse(True)
    with verrou:
        if not etat.get("analyse_toggle_on", True):
            etat["dernier_message"] = "Analyse désactivée manuellement. Cliquez sur « Lancer l’analyse » pour reprendre."
            return {"ok": True, "status": "disabled", "message": etat["dernier_message"], "analyse_toggle_on": False}
        modele = enregistrer_modele_analyse(modele or modele_analyse_film(film_id), film_id=film_id if film_id else None)
        if analyse_deja_active():
            etat["dernier_message"] = "Une analyse est déjà en cours."
            return {"ok": True, "status": "already_running", "message": etat["dernier_message"]}
        if photos_deja_actives():
            etat["dernier_message"] = "Analyse film en attente : analyse photo active."
            return {"ok": True, "status": "waiting_photos", "message": etat["dernier_message"]}
        if audio_ou_musique_active():
            etat["dernier_message"] = "Analyse image en attente : couche sonore active."
            return {"ok": True, "status": "waiting_audio", "message": etat["dernier_message"]}
        if scenes_deja_actives():
            etat["dernier_message"] = "Analyse image en attente : génération scènes/séquences active."
            return {"ok": True, "status": "waiting_scenes", "message": etat["dernier_message"]}
        if mouvements_deja_actifs() or mouvements_video_deja_actifs():
            etat["dernier_message"] = "Analyse image en attente : analyse des mouvements caméra active."
            return {"ok": True, "status": "waiting_mouvements", "message": etat["dernier_message"]}
        if affinage_deja_actif():
            etat["dernier_message"] = "Analyse image en attente : analyse fine IA active."
            return {"ok": True, "status": "waiting_affinage", "message": etat["dernier_message"]}
        if not film_id and not refaire and not films_a_analyser_details():
            etat["dernier_message"] = "Aucun film absent ou incomplet à analyser."
            return {"ok": True, "status": "nothing_to_do", "message": etat["dernier_message"]}
        moteur = moteur_pour_modele(modele)
        p = subprocess.Popen(
            ["/bin/bash", "-lc", commande_analyse(film_id=film_id, modele=modele, refaire=refaire)],
            cwd=ROOT,
            env=env_nettoye(),
            stdout=(ROOT / "controle_analyse.log").open("a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            text=True,
        )
        etat["analyse_pid"] = p.pid
        cible = f" pour {film_id}" if film_id else ""
        mode = "relancée" if refaire else "lancée"
        etat["dernier_message"] = f"Analyse {mode}{cible} depuis {source} avec {modele} via {moteur}, processus {p.pid}."
        return {"ok": True, "status": "started", "pid": p.pid, "modele": modele, "moteur": moteur, "film": film_id, "message": etat["dernier_message"]}


def lancer_scenes(source: str, film_id: str, refaire: bool = False) -> dict:
    film_id = str(film_id or "").strip()
    if not film_id:
        return {"ok": False, "status": "missing_film", "message": "Film requis pour générer les scènes/séquences."}
    try:
        charger_plans_film(film_id)
    except Exception as exc:
        return {"ok": False, "status": "missing_plans", "film": film_id, "message": str(exc)}
    with verrou:
        if analyse_deja_active():
            etat["dernier_scenes_message"] = "Scènes/séquences en attente : analyse image active."
            return {"ok": True, "status": "waiting_image", "message": etat["dernier_scenes_message"]}
        if photos_deja_actives():
            etat["dernier_scenes_message"] = "Scènes/séquences en attente : analyse photo active."
            return {"ok": True, "status": "waiting_photos", "message": etat["dernier_scenes_message"]}
        if audio_ou_musique_active():
            etat["dernier_scenes_message"] = "Scènes/séquences en attente : couche sonore active."
            return {"ok": True, "status": "waiting_audio", "message": etat["dernier_scenes_message"]}
        if mouvements_deja_actifs() or mouvements_video_deja_actifs():
            etat["dernier_scenes_message"] = "Scènes/séquences en attente : analyse des mouvements caméra active."
            return {"ok": True, "status": "waiting_mouvements", "message": etat["dernier_scenes_message"]}
        if affinage_deja_actif():
            etat["dernier_scenes_message"] = "Scènes/séquences en attente : analyse fine active."
            return {"ok": True, "status": "waiting_affinage", "message": etat["dernier_scenes_message"]}
        if scenes_deja_actives():
            etat["dernier_scenes_message"] = "Une génération scènes/séquences est déjà en cours."
            return {"ok": True, "status": "already_running", "message": etat["dernier_scenes_message"]}
        p = subprocess.Popen(
            ["/bin/bash", "-lc", commande_scenes(film_id, refaire=refaire)],
            cwd=ROOT,
            env=env_nettoye(),
            stdout=(ROOT / "controle_scenes.log").open("a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            text=True,
        )
        etat["scenes_pid"] = p.pid
        mode = "relancée" if refaire else "lancée"
        etat["dernier_scenes_message"] = f"Génération scènes/séquences {mode} pour {film_id} depuis {source}, processus {p.pid}."
        return {"ok": True, "status": "started", "pid": p.pid, "film": film_id, "message": etat["dernier_scenes_message"]}


def lancer_etape_film(payload: dict | None) -> dict:
    payload = payload or {}
    film_id = str(payload.get("film") or "").strip()
    if not film_id:
        return {"ok": False, "status": "missing_film", "message": "Film requis."}
    etape = normaliser_etape_film(payload.get("etape") or payload.get("analyse") or payload.get("type"))
    refaire = bool(payload.get("refaire"))
    modele = payload.get("modele")
    if etape == "plans":
        return lancer_analyse("bouton film · plans", modele=modele, film_id=film_id, refaire=refaire, activer_toggle=True)
    if etape == "scenes":
        return lancer_scenes("bouton film", film_id=film_id, refaire=refaire)
    if etape == "dialogue":
        return lancer_audio("bouton film", force=False, film_id=film_id, refaire=refaire)
    if etape == "musique":
        return lancer_musique("bouton film", force=False, film_id=film_id, modele=modele, refaire=refaire)
    if etape == "mouvements":
        return lancer_mouvements("bouton film", force=False, film_id=film_id, refaire=refaire)
    if etape == "mouvements-video":
        return lancer_mouvements_video("bouton film", force=False, film_id=film_id, modele=modele, refaire=refaire)
    if etape == "affinage":
        return lancer_affinage("bouton film", force=False, film_id=film_id, modele=modele)
    raise ValueError(f"Analyse film non gérée : {etape}")


def arreter_analyse(source: str) -> dict:
    memoriser_toggle_analyse(False)
    pids = processus_analyse()
    pids += [info["pid"] for info in processus_audio()]
    pids += processus_avec_mots("analyse_musique.py", "analyse")
    pids += processus_avec_mots("analyse_scenes.py", "analyse")
    pids += processus_avec_mots("analyse_presences.py", "analyse")
    pids += processus_avec_mots("analyse_personnages_recurrents.py", "analyse")
    pids += processus_avec_mots("analyse_mouvements.py", "analyse")
    pids += processus_avec_mots("analyse_mouvements_videomae.py", "analyse")
    pids += processus_avec_mots("analyse_affinage.py", "analyse")
    pids = sorted(set(pids))
    if not pids:
        with verrou:
            etat["dernier_message"] = "Analyse désactivée. Aucune analyse active à arrêter."
        return {"ok": True, "status": "none", "message": etat["dernier_message"], "analyse_toggle_on": False}
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    with verrou:
        etat["dernier_message"] = f"Analyse désactivée. Arrêt demandé depuis {source} pour {len(pids)} processus d’analyse."
    return {"ok": True, "status": "stopping", "pids": pids, "message": etat["dernier_message"], "analyse_toggle_on": False}


def lancer_audio(source: str, force: bool = False, film_id: str | None = None, refaire: bool = False) -> dict:
    if film_id:
        try:
            charger_plans_film(film_id)
        except Exception as exc:
            return {"ok": False, "status": "missing_plans", "film": film_id, "message": str(exc)}
    with verrou:
        if analyse_deja_active() and not force:
            etat["dernier_audio_message"] = "Son/dialogues en attente : analyse image active."
            return {"ok": True, "status": "waiting_image", "message": etat["dernier_audio_message"]}
        if photos_deja_actives() and not force:
            etat["dernier_audio_message"] = "Son/dialogues en attente : analyse photo active."
            return {"ok": True, "status": "waiting_photos", "message": etat["dernier_audio_message"]}
        if scenes_deja_actives() and not force:
            etat["dernier_audio_message"] = "Son/dialogues en attente : génération scènes/séquences active."
            return {"ok": True, "status": "waiting_scenes", "message": etat["dernier_audio_message"]}
        if mouvements_deja_actifs() and not force:
            etat["dernier_audio_message"] = "Son/dialogues en attente : mesure des mouvements caméra active."
            return {"ok": True, "status": "waiting_mouvements", "message": etat["dernier_audio_message"]}
        if mouvements_video_deja_actifs() and not force:
            etat["dernier_audio_message"] = "Son/dialogues en attente : classification VideoMAE active."
            return {"ok": True, "status": "waiting_mouvements_video", "message": etat["dernier_audio_message"]}
        if affinage_deja_actif() and not force:
            etat["dernier_audio_message"] = "Son/dialogues en attente : analyse fine IA active."
            return {"ok": True, "status": "waiting_affinage", "message": etat["dernier_audio_message"]}
        if musique_deja_active() and not force:
            etat["dernier_audio_message"] = "Son/dialogues en attente : analyse musique globale active."
            return {"ok": True, "status": "waiting_musique", "message": etat["dernier_audio_message"]}
        if audio_deja_active():
            etat["dernier_audio_message"] = "Une indexation son/dialogues est déjà en cours."
            return {"ok": True, "status": "already_running", "message": etat["dernier_audio_message"]}
        p = subprocess.Popen(
            ["/bin/bash", "-lc", commande_audio(film_id=film_id, refaire=refaire)],
            cwd=ROOT,
            env=env_nettoye(),
            stdout=(ROOT / "controle_audio.log").open("a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            text=True,
        )
        etat["audio_pid"] = p.pid
        etat["dernier_audio_index_genere"] = lire_index().get("genere")
        cible = f" pour {film_id}" if film_id else ""
        mode = "relancée" if refaire else "lancée"
        etat["dernier_audio_message"] = f"Indexation son/dialogues {mode}{cible} depuis {source}, sous-titres d’abord sans Whisper automatique, processus {p.pid}."
        return {"ok": True, "status": "started", "pid": p.pid, "film": film_id, "message": etat["dernier_audio_message"]}


def lancer_musique(source: str, force: bool = False, film_id: str | None = None, modele: str | None = None, refaire: bool = False) -> dict:
    if film_id:
        try:
            charger_plans_film(film_id)
        except Exception as exc:
            return {"ok": False, "status": "missing_plans", "film": film_id, "message": str(exc)}
    modele = normaliser_modele_ollama_analyse(modele or MODELE_AFFINAGE, defaut=MODELE_AFFINAGE)
    with verrou:
        if analyse_deja_active() and not force:
            etat["dernier_musique_message"] = "Musique globale en attente : analyse image active."
            return {"ok": True, "status": "waiting_image", "message": etat["dernier_musique_message"]}
        if photos_deja_actives() and not force:
            etat["dernier_musique_message"] = "Musique globale en attente : analyse photo active."
            return {"ok": True, "status": "waiting_photos", "message": etat["dernier_musique_message"]}
        if audio_deja_active() and not force:
            etat["dernier_musique_message"] = "Musique globale en attente : indexation son/dialogues active."
            return {"ok": True, "status": "waiting_audio", "message": etat["dernier_musique_message"]}
        if scenes_deja_actives() and not force:
            etat["dernier_musique_message"] = "Musique globale en attente : génération scènes/séquences active."
            return {"ok": True, "status": "waiting_scenes", "message": etat["dernier_musique_message"]}
        if mouvements_deja_actifs() and not force:
            etat["dernier_musique_message"] = "Musique globale en attente : mesure des mouvements caméra active."
            return {"ok": True, "status": "waiting_mouvements", "message": etat["dernier_musique_message"]}
        if mouvements_video_deja_actifs() and not force:
            etat["dernier_musique_message"] = "Musique globale en attente : classification VideoMAE active."
            return {"ok": True, "status": "waiting_mouvements_video", "message": etat["dernier_musique_message"]}
        if affinage_deja_actif() and not force:
            etat["dernier_musique_message"] = "Musique globale en attente : analyse fine IA active."
            return {"ok": True, "status": "waiting_affinage", "message": etat["dernier_musique_message"]}
        if musique_deja_active():
            etat["dernier_musique_message"] = "Une analyse musique globale est déjà en cours."
            return {"ok": True, "status": "already_running", "message": etat["dernier_musique_message"]}
        p = subprocess.Popen(
            ["/bin/bash", "-lc", commande_musique(film_id=film_id, modele=modele, refaire=refaire)],
            cwd=ROOT,
            env=env_nettoye(),
            stdout=(ROOT / "controle_musique.log").open("a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            text=True,
        )
        etat["musique_pid"] = p.pid
        cible = f" pour {film_id}" if film_id else ""
        mode = "relancée" if refaire else "lancée"
        etat["dernier_musique_message"] = f"Analyse musique globale {mode}{cible} depuis {source} avec {modele}, processus {p.pid}."
        return {"ok": True, "status": "started", "pid": p.pid, "film": film_id, "modele": modele, "message": etat["dernier_musique_message"]}


def mouvement_film_a_jour(film_id: str, video: bool = False) -> bool:
    data, _fichier = charger_plans_film(film_id)
    plans = data.get("plans", [])
    if not plans:
        return False
    if video:
        return sum(1 for plan in plans if plan.get("mouvement_video_modele")) >= len(plans)
    return sum(1 for plan in plans if plan.get("mouvement_camera")) >= len(plans)


def lancer_mouvements(source: str, force: bool = False, film_id: str | None = None, refaire: bool = False) -> dict:
    if film_id:
        try:
            charger_plans_film(film_id)
        except Exception as exc:
            return {"ok": False, "status": "missing_plans", "film": film_id, "message": str(exc)}
    with verrou:
        if analyse_deja_active() and not force:
            etat["dernier_mouvements_message"] = "Mouvements caméra en attente : analyse image active."
            return {"ok": True, "status": "waiting_image", "message": etat["dernier_mouvements_message"]}
        if photos_deja_actives() and not force:
            etat["dernier_mouvements_message"] = "Mouvements caméra en attente : analyse photo active."
            return {"ok": True, "status": "waiting_photos", "message": etat["dernier_mouvements_message"]}
        if audio_ou_musique_active() and not force:
            etat["dernier_mouvements_message"] = "Mouvements caméra en attente : couche sonore active."
            return {"ok": True, "status": "waiting_audio", "message": etat["dernier_mouvements_message"]}
        if scenes_deja_actives() and not force:
            etat["dernier_mouvements_message"] = "Mouvements caméra en attente : génération scènes/séquences active."
            return {"ok": True, "status": "waiting_scenes", "message": etat["dernier_mouvements_message"]}
        if mouvements_video_deja_actifs() and not force:
            etat["dernier_mouvements_message"] = "Mouvements caméra en attente : classification VideoMAE active."
            return {"ok": True, "status": "waiting_mouvements_video", "message": etat["dernier_mouvements_message"]}
        if affinage_deja_actif() and not force:
            etat["dernier_mouvements_message"] = "Mouvements caméra en attente : analyse fine IA active."
            return {"ok": True, "status": "waiting_affinage", "message": etat["dernier_mouvements_message"]}
        if mouvements_deja_actifs():
            etat["dernier_mouvements_message"] = "Une mesure des mouvements caméra est déjà en cours."
            return {"ok": True, "status": "already_running", "message": etat["dernier_mouvements_message"]}
        if not refaire:
            if film_id and mouvement_film_a_jour(film_id):
                etat["dernier_mouvements_message"] = f"Mouvements caméra à jour pour {film_id}."
                return {"ok": True, "status": "up_to_date", "film": film_id, "message": etat["dernier_mouvements_message"]}
            resume = lire_index()
            if not film_id and resume.get("plans", 0) > 0 and resume.get("mouvements_camera", 0) >= resume.get("plans", 0):
                etat["dernier_mouvements_message"] = "Mouvements caméra à jour pour les plans indexés."
                return {"ok": True, "status": "up_to_date", "message": etat["dernier_mouvements_message"]}
        p = subprocess.Popen(
            ["/bin/bash", "-lc", commande_mouvements(film_id=film_id, refaire=refaire)],
            cwd=ROOT,
            env=env_nettoye(),
            stdout=(ROOT / "controle_mouvements.log").open("a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            text=True,
        )
        etat["mouvements_pid"] = p.pid
        cible = f" pour {film_id}" if film_id else ""
        mode = "relancée" if refaire else "lancée"
        etat["dernier_mouvements_message"] = f"Mesure des mouvements caméra {mode}{cible} depuis {source}, processus {p.pid}."
        return {"ok": True, "status": "started", "pid": p.pid, "film": film_id, "message": etat["dernier_mouvements_message"]}


def lancer_mouvements_video(source: str, force: bool = False, film_id: str | None = None, modele: str | None = None, refaire: bool = False) -> dict:
    if film_id:
        try:
            charger_plans_film(film_id)
        except Exception as exc:
            return {"ok": False, "status": "missing_plans", "film": film_id, "message": str(exc)}
    modele = normaliser_modele_mouvements_video(modele)
    with verrou:
        if analyse_deja_active() and not force:
            etat["dernier_mouvements_video_message"] = "VideoMAE en attente : analyse image active."
            return {"ok": True, "status": "waiting_image", "message": etat["dernier_mouvements_video_message"]}
        if photos_deja_actives() and not force:
            etat["dernier_mouvements_video_message"] = "VideoMAE en attente : analyse photo active."
            return {"ok": True, "status": "waiting_photos", "message": etat["dernier_mouvements_video_message"]}
        if audio_ou_musique_active() and not force:
            etat["dernier_mouvements_video_message"] = "VideoMAE en attente : couche sonore active."
            return {"ok": True, "status": "waiting_audio", "message": etat["dernier_mouvements_video_message"]}
        if scenes_deja_actives() and not force:
            etat["dernier_mouvements_video_message"] = "VideoMAE en attente : génération scènes/séquences active."
            return {"ok": True, "status": "waiting_scenes", "message": etat["dernier_mouvements_video_message"]}
        if mouvements_deja_actifs() and not force:
            etat["dernier_mouvements_video_message"] = "VideoMAE en attente : mesure mécanique des mouvements active."
            return {"ok": True, "status": "waiting_mouvements", "message": etat["dernier_mouvements_video_message"]}
        if affinage_deja_actif() and not force:
            etat["dernier_mouvements_video_message"] = "VideoMAE en attente : analyse fine IA active."
            return {"ok": True, "status": "waiting_affinage", "message": etat["dernier_mouvements_video_message"]}
        if mouvements_video_deja_actifs():
            etat["dernier_mouvements_video_message"] = "Une classification VideoMAE des mouvements est déjà en cours."
            return {"ok": True, "status": "already_running", "message": etat["dernier_mouvements_video_message"]}
        if not refaire:
            if film_id and mouvement_film_a_jour(film_id, video=True):
                etat["dernier_mouvements_video_message"] = f"Classification VideoMAE à jour pour {film_id}."
                return {"ok": True, "status": "up_to_date", "film": film_id, "message": etat["dernier_mouvements_video_message"]}
            resume = lire_index()
            if not film_id and resume.get("plans", 0) > 0 and resume.get("mouvements_camera_video", 0) >= resume.get("plans", 0):
                etat["dernier_mouvements_video_message"] = "Classification VideoMAE à jour pour les plans indexés."
                return {"ok": True, "status": "up_to_date", "message": etat["dernier_mouvements_video_message"]}
        p = subprocess.Popen(
            ["/bin/bash", "-lc", commande_mouvements_video(film_id=film_id, modele=modele, refaire=refaire)],
            cwd=ROOT,
            env=env_nettoye(),
            stdout=(ROOT / "controle_mouvements_video.log").open("a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            text=True,
        )
        etat["mouvements_video_pid"] = p.pid
        cible = f" pour {film_id}" if film_id else ""
        mode = "relancée" if refaire else "lancée"
        etat["dernier_mouvements_video_message"] = f"Classification VideoMAE {mode}{cible} depuis {source} avec {modele}, processus {p.pid}."
        return {"ok": True, "status": "started", "pid": p.pid, "film": film_id, "modele": modele, "message": etat["dernier_mouvements_video_message"]}


def lancer_affinage(source: str, force: bool = False, film_id: str | None = None, modele: str | None = None) -> dict:
    modele = normaliser_modele_ollama_analyse(modele or MODELE_AFFINAGE, defaut=MODELE_AFFINAGE)
    with verrou:
        if analyse_deja_active() and not force:
            etat["dernier_affinage_message"] = "Analyse fine IA en attente : analyse image active."
            return {"ok": True, "status": "waiting_image", "message": etat["dernier_affinage_message"]}
        if photos_deja_actives() and not force:
            etat["dernier_affinage_message"] = "Analyse fine IA en attente : analyse photo active."
            return {"ok": True, "status": "waiting_photos", "message": etat["dernier_affinage_message"]}
        if audio_ou_musique_active() and not force:
            etat["dernier_affinage_message"] = "Analyse fine IA en attente : couche sonore active."
            return {"ok": True, "status": "waiting_audio", "message": etat["dernier_affinage_message"]}
        if scenes_deja_actives() and not force:
            etat["dernier_affinage_message"] = "Analyse fine en attente : génération scènes/séquences active."
            return {"ok": True, "status": "waiting_scenes", "message": etat["dernier_affinage_message"]}
        if mouvements_deja_actifs() and not force:
            etat["dernier_affinage_message"] = "Analyse fine IA en attente : mesure des mouvements caméra active."
            return {"ok": True, "status": "waiting_mouvements", "message": etat["dernier_affinage_message"]}
        if mouvements_video_deja_actifs() and not force:
            etat["dernier_affinage_message"] = "Analyse fine IA en attente : classification VideoMAE active."
            return {"ok": True, "status": "waiting_mouvements_video", "message": etat["dernier_affinage_message"]}
        if affinage_deja_actif():
            etat["dernier_affinage_message"] = "Une analyse fine IA est déjà en cours."
            return {"ok": True, "status": "already_running", "message": etat["dernier_affinage_message"]}
        p = subprocess.Popen(
            ["/bin/bash", "-lc", commande_affinage(film_id=film_id, modele=modele)],
            cwd=ROOT,
            env=env_nettoye(),
            stdout=(ROOT / "controle_affinage.log").open("a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            text=True,
        )
        etat["affinage_pid"] = p.pid
        cible = f" pour {film_id}" if film_id else ""
        etat["dernier_affinage_message"] = f"Analyse fine lancée{cible} depuis {source} avec {modele}, processus {p.pid}."
        return {"ok": True, "status": "started", "pid": p.pid, "film": film_id, "modele": modele, "message": etat["dernier_affinage_message"]}


def enregistrer_config_films(payload: dict | None) -> dict:
    payload = payload or {}
    config = lire_config()
    dossier = payload.get("dossier_films")
    if dossier is not None and str(dossier).strip():
        config["dossier_films"] = str(dossier).strip()
    elif "dossier_films" not in config:
        config["dossier_films"] = str(Path.home() / "Movies")
    if "films_contexte" in payload:
        config["films_contexte"] = str(payload.get("films_contexte") or "")
    if isinstance(payload.get("films_criteres"), list):
        criteres = [str(c).strip() for c in payload.get("films_criteres") if str(c).strip()]
        if criteres:
            config["films_criteres"] = criteres
    ecrire_config(config)
    synchroniser_pages_site()
    with verrou:
        etat["dernier_message"] = "Réglages films enregistrés."
    return {
        "ok": True,
        "status": "saved",
        "message": "Réglages films enregistrés.",
        "dossier_films": str(Path(config.get("dossier_films") or "").expanduser()),
        "films_contexte": config.get("films_contexte", ""),
        "films_criteres": config.get("films_criteres") or films_criteres(),
    }


def choisir_dossier_films_mac() -> dict:
    script = (
        'set chosenFolder to choose folder with prompt "Choisissez le dossier des films à analyser" default location POSIX file "' + str(dossier_films()).replace('"', '\\"') + '"\n'
        'POSIX path of chosenFolder'
    )
    r = subprocess.run(
        ["osascript", "-e", script],
        cwd=ROOT,
        env=env_nettoye(),
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
    )
    sortie = (r.stdout or "").strip()
    erreur = (r.stderr or "").strip()
    if r.returncode == 0 and sortie:
        return {
            "ok": True,
            "status": "chosen",
            "dossier_films": sortie,
            "message": f"Dossier films choisi : {sortie}",
        }
    message = erreur or sortie or "Choix du dossier films annulé."
    if "User canceled" in message or "cancel" in message.lower() or str(r.returncode) == "-128":
        return {"ok": False, "status": "cancelled", "message": "Choix du dossier films annulé."}
    return {"ok": False, "status": "error", "message": f"Impossible d’ouvrir le sélecteur de dossier films : {message}"}


def enregistrer_config_photos(payload: dict | None) -> dict:
    payload = payload or {}
    config = lire_config()
    dossier = payload.get("dossier_photos")
    if dossier is not None and str(dossier).strip():
        config["dossier_photos"] = str(dossier).strip()
    elif "dossier_photos" not in config:
        config["dossier_photos"] = str(Path.home() / "Pictures")
    if "photos_contexte" in payload:
        config["photos_contexte"] = str(payload.get("photos_contexte") or "")
    if isinstance(payload.get("photos_criteres"), list):
        criteres = [str(c).strip() for c in payload.get("photos_criteres") if str(c).strip()]
        if criteres:
            config["photos_criteres"] = criteres
    if payload.get("photos_modele_analyse") is not None or payload.get("modele") is not None:
        config["photos_modele_analyse"] = normaliser_modele_ollama_analyse(
            payload.get("photos_modele_analyse") or payload.get("modele"),
            defaut=modele_photos(),
        )
    elif "photos_modele_analyse" not in config:
        config["photos_modele_analyse"] = modele_photos()
    ecrire_config(config)
    synchroniser_pages_site()
    with verrou:
        etat["dernier_message"] = "Réglages photo enregistrés."
    return {
        "ok": True,
        "status": "saved",
        "message": "Réglages photo enregistrés.",
        "dossier_photos": config.get("dossier_photos"),
        "photos_criteres": config.get("photos_criteres") or photos_criteres(),
        "photos_contexte": config.get("photos_contexte", ""),
        "photos_modele_analyse": config.get("photos_modele_analyse") or modele_photos(),
        "modeles_photos": modeles_photos_disponibles(),
    }


def choisir_dossier_photos_mac() -> dict:
    script = (
        'set chosenFolder to choose folder with prompt "Choisissez le dossier d’images à analyser" default location POSIX file "' + str(dossier_photos()).replace('"', '\\"') + '"\n'
        'POSIX path of chosenFolder'
    )
    r = subprocess.run(
        ["osascript", "-e", script],
        cwd=ROOT,
        env=env_nettoye(),
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
    )
    sortie = (r.stdout or "").strip()
    erreur = (r.stderr or "").strip()
    if r.returncode == 0 and sortie:
        return {
            "ok": True,
            "status": "chosen",
            "dossier_photos": sortie,
            "message": f"Dossier choisi : {sortie}",
        }
    message = erreur or sortie or "Choix du dossier annulé."
    if "User canceled" in message or "cancel" in message.lower() or str(r.returncode) == "-128":
        return {"ok": False, "status": "cancelled", "message": "Choix du dossier annulé."}
    return {"ok": False, "status": "error", "message": f"Impossible d’ouvrir le sélecteur de dossier : {message}"}


def reindexer_photos(source: str) -> dict:
    PHOTO_ANALYSE.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["/bin/bash", "-lc", commande_photos_index()],
        cwd=ROOT,
        env=env_nettoye(),
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    if r.returncode != 0:
        message = (r.stderr or r.stdout or "erreur inconnue").strip()[-500:]
        with verrou:
            etat["dernier_photos_message"] = f"Erreur index photo : {message}"
        return {"ok": False, "status": "error", "message": etat["dernier_photos_message"]}
    resume = lire_index_photos()
    with verrou:
        etat["dernier_photos_message"] = f"Catalogue photo mis à jour depuis {source} : {resume.get('photos', 0)} photos."
    return {"ok": True, "status": "updated", "message": etat["dernier_photos_message"], "photos_index": resume}


def traitements_films_actifs() -> bool:
    return (
        analyse_deja_active()
        or audio_ou_musique_active()
        or scenes_deja_actives()
        or mouvements_deja_actifs()
        or mouvements_video_deja_actifs()
        or affinage_deja_actif()
    )


def attendre_arret_traitements_films(timeout: float = 25.0) -> bool:
    fin = time.time() + timeout
    while time.time() < fin:
        if not traitements_films_actifs():
            return True
        time.sleep(0.5)
    return not traitements_films_actifs()


def lancer_photos(source: str, refaire: bool = False, modele: str | None = None, photo_id: str | None = None) -> dict:
    modele = normaliser_modele_ollama_analyse(modele or modele_photos(), defaut=modele_photos())
    photo_id = str(photo_id or "").strip()
    arret_films = None
    if traitements_films_actifs():
        arret_films = arreter_analyse("lancement analyse photo")
        if not attendre_arret_traitements_films():
            with verrou:
                etat["dernier_photos_message"] = (
                    "Analyse photo en attente : arrêt de l’analyse film demandé, "
                    "démarrage photo dès que les processus film sont arrêtés."
                )
            return {
                "ok": True,
                "status": "stopping_films",
                "arret_films": arret_films,
                "message": etat["dernier_photos_message"],
                "photos_modele_analyse": modele,
            }
    with verrou:
        if photos_deja_actives():
            etat["dernier_photos_message"] = "Une analyse photo est déjà en cours."
            return {"ok": True, "status": "already_running", "message": etat["dernier_photos_message"]}
        dossier = dossier_photos()
        if not dossier.exists():
            etat["dernier_photos_message"] = f"Dossier photo introuvable : {dossier}"
            return {"ok": False, "status": "missing_folder", "message": etat["dernier_photos_message"]}
        if compter_photos_source() == 0:
            etat["dernier_photos_message"] = f"Aucune photo trouvée dans {dossier}."
            return {"ok": False, "status": "empty_folder", "message": etat["dernier_photos_message"]}
        p = subprocess.Popen(
            ["/bin/bash", "-lc", commande_photos(refaire=refaire, modele=modele, photo_id=photo_id or None)],
            cwd=ROOT,
            env=env_nettoye(),
            stdout=(ROOT / "controle_photos.log").open("a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            text=True,
        )
        etat["photos_pid"] = p.pid
        suffixe_arret = " Analyse film arrêtée avant lancement photo." if arret_films else ""
        cible = f" · photo {photo_id}" if photo_id else ""
        etat["dernier_photos_message"] = f"Analyse photo lancée depuis {source}{cible} avec {modele}, processus {p.pid}.{suffixe_arret}"
        return {
            "ok": True,
            "status": "started",
            "pid": p.pid,
            "photo_id": photo_id or None,
            "message": etat["dernier_photos_message"],
            "photos_modele_analyse": modele,
            "arret_films": arret_films,
        }


def arreter_photos(source: str) -> dict:
    pids = processus_photos()
    if not pids:
        with verrou:
            etat["dernier_photos_message"] = "Aucune analyse photo active à arrêter."
        return {"ok": True, "status": "none", "message": etat["dernier_photos_message"]}
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    with verrou:
        etat["dernier_photos_message"] = f"Arrêt photo demandé depuis {source} pour {len(pids)} processus."
    return {"ok": True, "status": "stopping", "pids": pids, "message": etat["dernier_photos_message"]}


def boucle_audio() -> None:
    time.sleep(20)
    while True:
        try:
            if etat.get("audio_auto") and analyse_toggle_active():
                if analyse_deja_active():
                    with verrou:
                        etat["dernier_audio_message"] = "Son/dialogues en attente : analyse image active."
                elif photos_deja_actives():
                    with verrou:
                        etat["dernier_audio_message"] = "Son/dialogues en attente : analyse photo active."
                elif scenes_deja_actives():
                    with verrou:
                        etat["dernier_audio_message"] = "Son/dialogues en attente : génération scènes/séquences active."
                elif musique_deja_active():
                    with verrou:
                        etat["dernier_audio_message"] = "Son/dialogues en attente : analyse musique globale active."
                elif not audio_deja_active():
                    index_genere = lire_index().get("genere")
                    if index_genere and index_genere == etat.get("dernier_audio_index_genere"):
                        with verrou:
                            etat["dernier_audio_message"] = "Son/dialogues à jour pour le dernier index."
                    else:
                        lancer_audio("automatisation")
                else:
                    with verrou:
                        etat["dernier_audio_message"] = "Indexation son/dialogues active (sous-titres d’abord, sans Whisper automatique)."
        except Exception as exc:
            with verrou:
                etat["dernier_audio_message"] = f"Erreur audio/dialogues : {exc}"
        time.sleep(AUDIO_INTERVALLE)


def boucle_mouvements() -> None:
    time.sleep(35)
    while True:
        try:
            if etat.get("mouvements_auto") and analyse_toggle_active():
                if analyse_deja_active():
                    with verrou:
                        etat["dernier_mouvements_message"] = "Mouvements caméra en attente : analyse image active."
                elif photos_deja_actives():
                    with verrou:
                        etat["dernier_mouvements_message"] = "Mouvements caméra en attente : analyse photo active."
                elif audio_ou_musique_active():
                    with verrou:
                        etat["dernier_mouvements_message"] = "Mouvements caméra en attente : couche sonore active."
                elif scenes_deja_actives():
                    with verrou:
                        etat["dernier_mouvements_message"] = "Mouvements caméra en attente : génération scènes/séquences active."
                elif films_a_analyser_details():
                    with verrou:
                        etat["dernier_mouvements_message"] = "Mouvements caméra en attente : films image absents ou incomplets."
                elif mouvements_deja_actifs():
                    with verrou:
                        etat["dernier_mouvements_message"] = "Mesure des mouvements caméra active."
                else:
                    resume = lire_index()
                    if resume.get("plans", 0) and resume.get("mouvements_camera", 0) < resume.get("plans", 0):
                        lancer_mouvements("automatisation")
                    else:
                        with verrou:
                            etat["dernier_mouvements_message"] = "Mouvements caméra à jour pour les plans indexés."
        except Exception as exc:
            with verrou:
                etat["dernier_mouvements_message"] = f"Erreur mouvements caméra : {exc}"
        time.sleep(MOUVEMENTS_INTERVALLE)


def boucle_surveillance() -> None:
    try:
        dossier = dossier_films()
        precedent = signature_videos(dossier)
        with verrou:
            etat["dernier_message"] = f"Surveillance active sur {dossier}."
        if analyse_toggle_active():
            lancer_analyse("surveillance")
        else:
            with verrou:
                etat["dernier_message"] = "Surveillance active : analyse désactivée manuellement. Cliquez sur « Lancer l’analyse » pour reprendre."
        while True:
            time.sleep(60)
            courant = signature_videos(dossier)
            if courant == precedent:
                manquants = films_a_analyser_details()
                if manquants and not analyse_deja_active():
                    if not analyse_toggle_active():
                        with verrou:
                            etat["dernier_message"] = (
                                f"Surveillance active : analyse désactivée manuellement ({len(manquants)} film(s) en attente). Cliquez sur « Lancer l’analyse » pour reprendre."
                            )
                        continue
                    with verrou:
                        etat["dernier_message"] = (
                            f"Surveillance : {len(manquants)} film(s) absent(s) ou incomplet(s), lancement."
                        )
                    lancer_analyse("films absents ou incomplets")
                    continue
                with verrou:
                    etat["dernier_message"] = f"Surveillance active : aucun nouveau film détecté ({time.strftime('%H:%M')})."
                continue
            time.sleep(20)
            confirme = signature_videos(dossier)
            if confirme != courant:
                precedent = confirme
                with verrou:
                    etat["dernier_message"] = "Copie en cours détectée, nouvelle vérification au prochain passage."
                continue
            precedent = confirme
            reconstruire_index("nouveau film détecté")
            lancer_analyse("nouveau film détecté")
    except Exception as exc:
        with verrou:
            etat["surveillance"] = False
            etat["dernier_message"] = f"Surveillance arrêtée : {exc}"


def activer_surveillance() -> dict:
    with verrou:
        if etat["surveillance"]:
            return {"ok": True, "status": "already_running", "message": etat["dernier_message"]}
        etat["surveillance"] = True
        etat["dernier_message"] = "Démarrage de la surveillance…"
    t = threading.Thread(target=boucle_surveillance, daemon=True)
    t.start()
    return {"ok": True, "status": "started", "message": "Surveillance activée."}


class ControleHandler(BaseHTTPRequestHandler):
    def _headers(self, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _json(self, data: dict, code: int = 200) -> None:
        try:
            self._headers(code)
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            return

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            taille = path.stat().st_size
            debut = 0
            fin = taille - 1
            code = 200
            plage = self.headers.get("Range") or ""
            if plage.startswith("bytes="):
                m = re.match(r"bytes=(\d*)-(\d*)", plage)
                if m:
                    if m.group(1):
                        debut = max(0, int(m.group(1)))
                    if m.group(2):
                        fin = min(taille - 1, int(m.group(2)))
                    if not m.group(1) and m.group(2):
                        longueur = min(taille, int(m.group(2)))
                        debut = max(0, taille - longueur)
                        fin = taille - 1
                    if debut > fin or debut >= taille:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{taille}")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.end_headers()
                        return
                    code = 206
            longueur = fin - debut + 1
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(longueur))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Range")
            self.send_header("Cache-Control", "public, max-age=86400")
            if code == 206:
                self.send_header("Content-Range", f"bytes {debut}-{fin}/{taille}")
            self.end_headers()
            with path.open("rb") as fh:
                fh.seek(debut)
                restant = longueur
                while restant > 0:
                    bloc = fh.read(min(1024 * 1024, restant))
                    if not bloc:
                        break
                    self.wfile.write(bloc)
                    restant -= len(bloc)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _send_bytes(self, contenu: bytes, content_type: str, filename: str | None = None) -> None:
        try:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(contenu)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(contenu)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_OPTIONS(self) -> None:
        self._headers(204)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        chemin = parsed.path
        if chemin == "/etat":
            query = parse_qs(parsed.query)
            with verrou:
                data = dict(etat)
            data["modele_analyse"] = modele_analyse_defaut()
            data["modeles_analyse"] = modeles_analyse_disponibles()
            data["modeles_mouvements_video"] = MODELES_MOUVEMENTS_VIDEO
            data["analyses_film"] = analyses_film_disponibles()
            data["mlx_vlm_concurrence"] = concurrence_mlx()
            data["modele_affinage"] = MODELE_AFFINAGE
            data["modele_mouvements_video"] = MODELE_MOUVEMENTS_VIDEO
            data["largeur_analyse"] = LARGEUR_ANALYSE
            data["analyse_active"] = analyse_deja_active()
            data["analyse_progression"] = analyse_active_detail()
            data["audio_active"] = audio_deja_active()
            data["musique_active"] = musique_deja_active()
            data["scenes_active"] = scenes_deja_actives()
            data["mouvements_active"] = mouvements_deja_actifs()
            data["mouvements_progression"] = progression_mouvements()
            data["mouvements_video_active"] = mouvements_video_deja_actifs()
            data["mouvements_video_progression"] = progression_mouvements_video()
            data["affinage_active"] = affinage_deja_actif()
            data["photo_analyse_active"] = photos_deja_actives()
            if query.get("rapide") or query.get("health"):
                films_progression_rapide = films_progression()
                index_resume = resume_index_rapide()
                photos_index_resume = resume_index_photos_rapide()
                self._json({
                    "ok": True,
                    "controle": "prêt",
                    "analyse_active": data["analyse_active"],
                    "analyse_progression": data["analyse_progression"],
                    "analyse_toggle_on": data.get("analyse_toggle_on", True),
                    "analyse_pid": data.get("analyse_pid"),
                    "modele_analyse": data["modele_analyse"],
                    "modeles_analyse": data["modeles_analyse"],
                    "analyses_film": data["analyses_film"],
                    "modeles_mouvements_video": data["modeles_mouvements_video"],
                    "dossier_films": str(dossier_films()),
                    "films_contexte": films_contexte(),
                    "films_criteres": films_criteres(),
                    "films_source": compter_videos_source(),
                    "films_progression": films_progression_rapide,
                    "films_sans_analyse": videos_sans_analyse(),
                    "films_a_analyser": [f.get("id") for f in films_progression_rapide if not f.get("termine")] or None,
                    "index_resume": index_resume,
                    "photos_source": compter_photos_source(),
                    "photos_index": photos_index_resume,
                    "photos_sans_analyse": photos_sans_analyse(),
                    "photos_progression": progression_photos(),
                    "photos_modele_analyse": modele_photos(),
                    "modeles_photos": modeles_photos_disponibles(),
                    "etat_rapide": True,
                    "dernier_message": data.get("dernier_message", ""),
                    "dernier_audio_message": data.get("dernier_audio_message", ""),
                    "dernier_musique_message": data.get("dernier_musique_message", ""),
                    "dernier_scenes_message": data.get("dernier_scenes_message", ""),
                    "dernier_affinage_message": data.get("dernier_affinage_message", ""),
                    "dernier_mouvements_message": data.get("dernier_mouvements_message", ""),
                    "dernier_mouvements_video_message": data.get("dernier_mouvements_video_message", ""),
                    "mouvements_active": data["mouvements_active"],
                    "mouvements_progression": data["mouvements_progression"],
                    "mouvements_video_active": data["mouvements_video_active"],
                    "mouvements_video_progression": data["mouvements_video_progression"],
                    "audio_active": data["audio_active"],
                    "musique_active": data["musique_active"],
                    "scenes_active": data["scenes_active"],
                    "affinage_active": data["affinage_active"],
                    "photo_analyse_active": data["photo_analyse_active"],
                })
                return
            films_a_traiter = films_a_analyser_details()
            data["mlx_vlm"] = etat_mlx_vlm(timeout=0.8)
            data["audio_progression"] = progression_audio()
            data["audio_pid"] = data["audio_progression"].get("pid")
            data["musique_progression"] = progression_musique()
            data["musique_pid"] = data["musique_progression"].get("pid") or data.get("musique_pid")
            data["mouvements_video_progression"] = data["mouvements_video_progression"]
            data["films_source"] = compter_videos_source()
            data["films_source_details"] = videos_source_details()
            data["films_sans_analyse"] = videos_sans_analyse()
            data["films_sans_analyse_details"] = videos_sans_analyse_details()
            data["dossier_films"] = str(dossier_films())
            data["films_contexte"] = films_contexte()
            data["films_criteres"] = films_criteres()
            data["films_a_analyser"] = [info["id"] for info in films_a_traiter]
            data["films_a_analyser_details"] = films_a_traiter
            data["films_progression"] = films_progression()
            data["index"] = lire_index()
            data["dossier_photos"] = str(dossier_photos())
            data["photos_source"] = compter_photos_source()
            data["photos_sans_analyse"] = photos_sans_analyse()
            data["photos_index"] = lire_index_photos()
            data["photos_progression"] = progression_photos()
            data["photos_criteres"] = photos_criteres()
            data["photos_contexte"] = photos_contexte()
            data["photos_modele_analyse"] = modele_photos()
            data["modeles_photos"] = modeles_photos_disponibles()
            self._json({"ok": True, **data})
            return
        if chemin == "/apercu-video":
            query = parse_qs(parsed.query)
            film_id = (query.get("film") or [""])[0].strip()
            plan_txt = (query.get("plan") or [""])[0].strip()
            mode = (query.get("mode") or query.get("qualite") or ["preview"])[0].strip()
            if not film_id or not plan_txt:
                self._json({"ok": False, "message": "Paramètres film et plan requis."}, 400)
                return
            try:
                fichier = generer_apercu_video_plan(film_id, int(plan_txt), mode=mode)
            except FileNotFoundError as exc:
                self._json({"ok": False, "message": str(exc)}, 404)
                return
            except Exception as exc:
                self._json({"ok": False, "message": str(exc)}, 500)
                return
            self._send_file(fichier, "video/mp4")
            return
        if chemin == "/film-video":
            query = parse_qs(parsed.query)
            film_id = (query.get("film") or [""])[0].strip()
            if not film_id:
                self._json({"ok": False, "message": "Paramètre film requis."}, 400)
                return
            try:
                fichier, content_type = video_lecteur_par_id(film_id)
            except FileNotFoundError as exc:
                self._json({"ok": False, "message": str(exc)}, 404)
                return
            except Exception as exc:
                self._json({"ok": False, "message": str(exc)}, 500)
                return
            self._send_file(fichier, content_type)
            return
        self._json({"ok": False, "message": "Endpoint inconnu."}, 404)

    def _body_json(self) -> dict:
        try:
            taille = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            taille = 0
        if taille <= 0:
            return {}
        brut = self.rfile.read(taille).decode("utf-8", errors="replace")
        try:
            data = json.loads(brut)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def do_POST(self) -> None:
        chemin = urlparse(self.path).path
        try:
            payload = self._body_json()
            if chemin == "/analyser":
                enregistrer_config_films(payload)
                self._json(lancer_analyse(
                    "bouton",
                    modele=payload.get("modele"),
                    film_id=(str(payload.get("film") or "").strip() or None),
                    refaire=bool(payload.get("refaire")),
                    activer_toggle=True,
                ))
            elif chemin == "/analyser-film":
                self._json(lancer_analyse(
                    "bouton film",
                    modele=payload.get("modele"),
                    film_id=(str(payload.get("film") or "").strip() or None),
                    refaire=bool(payload.get("refaire")),
                    activer_toggle=True,
                ))
            elif chemin == "/analyse-film-etape":
                self._json(lancer_etape_film(payload))
            elif chemin == "/arreter":
                self._json(arreter_analyse("bouton"))
            elif chemin == "/surveiller":
                enregistrer_config_films(payload)
                self._json(activer_surveillance())
            elif chemin == "/reindexer":
                enregistrer_config_films(payload)
                self._json(reconstruire_index("bouton"))
            elif chemin == "/audio":
                self._json(lancer_audio(
                    "bouton",
                    force=False,
                    film_id=(str(payload.get("film") or "").strip() or None),
                    refaire=bool(payload.get("refaire")),
                ))
            elif chemin == "/musique":
                self._json(lancer_musique(
                    "bouton",
                    force=False,
                    film_id=(str(payload.get("film") or "").strip() or None),
                    modele=payload.get("modele"),
                    refaire=bool(payload.get("refaire")),
                ))
            elif chemin == "/scenes":
                self._json(lancer_scenes(
                    "bouton",
                    film_id=(str(payload.get("film") or "").strip() or ""),
                    refaire=bool(payload.get("refaire")),
                ))
            elif chemin == "/mouvements":
                self._json(lancer_mouvements(
                    "bouton",
                    force=False,
                    film_id=(str(payload.get("film") or "").strip() or None),
                    refaire=bool(payload.get("refaire")),
                ))
            elif chemin == "/mouvements-video":
                self._json(lancer_mouvements_video(
                    "bouton",
                    force=False,
                    film_id=(str(payload.get("film") or "").strip() or None),
                    modele=payload.get("modele"),
                    refaire=bool(payload.get("refaire")),
                ))
            elif chemin == "/affinage":
                self._json(lancer_affinage(
                    "bouton",
                    force=False,
                    film_id=(str(payload.get("film") or "").strip() or None),
                    modele=payload.get("modele"),
                ))
            elif chemin == "/films-config":
                self._json(enregistrer_config_films(payload))
            elif chemin == "/films-choisir-dossier":
                self._json(choisir_dossier_films_mac())
            elif chemin == "/photos-config":
                self._json(enregistrer_config_photos(payload))
            elif chemin == "/photos-choisir-dossier":
                self._json(choisir_dossier_photos_mac())
            elif chemin == "/photos-analyser":
                enregistrer_config_photos(payload)
                self._json(lancer_photos(
                    "bouton",
                    refaire=bool(payload.get("refaire")),
                    modele=payload.get("photos_modele_analyse") or payload.get("modele"),
                ))
            elif chemin == "/photos-analyser-photo":
                enregistrer_config_photos(payload)
                self._json(lancer_photos(
                    "bouton photo",
                    refaire=True,
                    modele=payload.get("photos_modele_analyse") or payload.get("modele"),
                    photo_id=payload.get("photo_id") or payload.get("id"),
                ))
            elif chemin == "/photos-reindexer":
                enregistrer_config_photos(payload)
                self._json(reindexer_photos("bouton"))
            elif chemin == "/photos-arreter":
                self._json(arreter_photos("bouton"))
            elif chemin == "/export-selection-zip":
                contenu, nom = exporter_selection_zip(list(payload.get("plans") or []))
                self._send_bytes(contenu, "application/zip", filename=nom)
            else:
                self._json({"ok": False, "message": "Endpoint inconnu."}, 404)
        except Exception as exc:
            self._json({"ok": False, "message": str(exc)}, 500)

    def log_message(self, format: str, *args) -> None:
        return


class ControleHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def demarrer_services_apres_http() -> None:
    """Lance les tâches lourdes seulement après ouverture du port HTTP.

    Auparavant la surveillance démarrait avant ThreadingHTTPServer. Au double-clic,
    la page d’accueil pouvait donc s’ouvrir pendant que le serveur n’écoutait pas
    encore sur /etat, d’où l’erreur intermittente « serveur absent ».
    """
    threading.Thread(target=boucle_index, daemon=True).start()
    threading.Thread(target=boucle_audio, daemon=True).start()
    threading.Thread(target=boucle_mouvements, daemon=True).start()
    activer_surveillance()


def resume_index_rapide() -> dict:
    try:
        index = lire_index() or {}
    except Exception:
        return {}
    films = index.get("films") or []
    plans = index.get("plans") or []
    scenes = index.get("scenes") or []
    return {
        "genere": index.get("genere"),
        "films": len(films) if isinstance(films, list) else (films or 0),
        "plans": len(plans) if isinstance(plans, list) else (plans or 0),
        "scenes": len(scenes) if isinstance(scenes, list) else (scenes or 0),
        "contextes_films": len(index.get("contexte") or {}),
        "personnages_recurrents": index.get("personnages_recurrents_count") or 0,
        "dialogues": index.get("dialogues_count") or 0,
        "mouvements_camera": index.get("mouvements_camera_count") or 0,
        "mouvements_camera_video": index.get("mouvements_camera_video_count") or 0,
    }


def resume_index_photos_rapide() -> dict:
    try:
        return lire_index_photos() or {}
    except Exception:
        return {}


def main() -> None:
    server = ControleHTTPServer(("127.0.0.1", PORT), ControleHandler)
    print(f"Contrôle de l’analyse disponible sur http://127.0.0.1:{PORT}/etat", flush=True)
    threading.Thread(target=demarrer_services_apres_http, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
