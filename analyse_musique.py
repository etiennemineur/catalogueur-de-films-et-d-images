#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyse_musique.py — seconde passe audio détaillée pour la musique, les sous-genres
et le design sonore, sans relancer l’analyse image.

Deux modes sont disponibles :
1. `plans` : relit un plan à la fois (mode historique) ;
2. `sequences` : relit une séquence sonore couvrant plusieurs plans, afin de mieux
   suivre une musique qui continue au-delà d’un seul cut.

Principe :
1. extraire le vrai son du plan ou de la séquence depuis le fichier source ;
2. générer localement une forme d’onde et un spectrogramme ;
3. envoyer ces supports au modèle IA local choisi avec les indices déjà connus
   (dialogues, sous-titres, mesures ffmpeg, images de plans) ;
4. écrire une couche audio détaillée prudente dans plans.json ;
5. si disponible, calculer une empreinte locale Chromaprint/AcoustID et, si une clé
   AcoustID est fournie, faire une recherche distante légère.

Important :
- l’identification exacte d’une chanson connue reste conservatrice ;
- on ne remplit titre / artiste / auteur que si des indices locaux explicites ou une
  empreinte crédible le permettent ;
- sans clé AcoustID (ou sans miroir local), l’empreinte reste strictement locale et
  ne peut pas résoudre automatiquement un titre distant.

Exemples :

    .venv/bin/python analyse_musique.py analyse --dry-run
    .venv/bin/python analyse_musique.py analyse --film 2001-a-space-odyssey-1968 --plan 1 --limite 1 --index-seul
    .venv/bin/python analyse_musique.py analyse --mode sequences --film barbarella-1968 --film-entier --index-seul
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyse_plans import contexte_film_prompt, interroger, verifier_modele  # noqa: E402

try:
    import acoustid  # type: ignore
except Exception:  # pragma: no cover - dépendance optionnelle
    acoustid = None

try:
    import musicbrainzngs  # type: ignore
except Exception:  # pragma: no cover - dépendance optionnelle
    musicbrainzngs = None

MUSIQUE_FAMILLES = [
    "électronique",
    "orchestrale / classique",
    "jazz / blues",
    "rock / pop",
    "funk / soul / disco",
    "folk / acoustique",
    "ambient / drone",
    "percussive / rythmique",
    "indéterminée",
]

MUSIQUE_SOUS_GENRES = [
    "synthétique spatiale",
    "synthwave / kosmische",
    "ambient électronique",
    "électronique pulsée",
    "orchestrale dramatique",
    "orchestrale solennelle",
    "musique de chambre / intimiste",
    "chorale / liturgique",
    "jazz orchestral",
    "jazz modal",
    "blues",
    "rock psychédélique",
    "rock énergique",
    "pop / chanson",
    "funk / groove",
    "soul / rhythm and blues",
    "folk acoustique",
    "drone / texture continue",
    "expérimental / musique concrète",
    "percussion tribale / rituelle",
    "indéterminée",
]

DESIGN_SONORE_TYPES = [
    "silence / quasi-silence",
    "ambiance naturelle",
    "ambiance urbaine",
    "machine / moteur",
    "interface électronique",
    "radio / interphone / voix filtrée",
    "impact / explosion / arme",
    "foule / applaudissements",
    "souffle / respiration / corps",
    "bruit abstrait / texture",
    "réverbération / espace",
    "indéterminé",
]

INTENSITES_SONORES = ["très faible", "faible", "moyenne", "forte", "très forte", ""]
IDENTIFICATIONS_METHODE = [
    "",
    "indices textuels / sous-titres",
    "métadonnées locales",
    "analyse spectrale locale",
    "empreinte acoustid locale",
    "empreinte acoustid + musicbrainz",
]
IDENTIFICATIONS_CONFIANCE = ["", "faible", "moyenne", "élevée"]
AUDIO_CLES = [
    "musique_presente",
    "parole_chantee",
    "musique_familles",
    "musique_sous_genres",
    "design_sonore_types",
    "intensite_sonore",
    "titre_morceau",
    "artiste_morceau",
    "auteur_morceau",
    "identification_methode",
    "identification_confiance",
    "a_verifier",
    "notes_audio",
]

SEQUENCE_DUREE_CIBLE = 45.0
SEQUENCE_DUREE_MAX = 90.0
SEQUENCE_RAYON_PLANS = 3
SEQUENCE_GAP_MAX = 1.2
ACOUSTID_TIMEOUT = 12
MUSICBRAINZ_USER_AGENT = ("BancDePlansLocal", "1.0", "http://127.0.0.1")


def normaliser(texte: str) -> str:
    texte = unicodedata.normalize("NFKD", str(texte or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", texte).strip().lower()


def lire_config_locale() -> dict:
    config = Path(__file__).resolve().with_name("config.json")
    if not config.exists():
        return {}
    try:
        return json.loads(config.read_text(encoding="utf-8"))
    except Exception:
        return {}


def ressemble_a_un_codec_ou_nom_technique(texte: str) -> bool:
    brut = normaliser(texte)
    if not brut:
        return False
    motifs = [
        r"e[- ]?ac[- ]?3", r"ac[- ]?3", r"\bdolby\b", r"\bdts\b", r"\bpcm\b",
        r"[57][\., ]1", r"\bstereo\b", r"\bmono\b",
        r"\baudio\b", r"\btrack\b", r"\bversion\b",
        r"\b(?:480p|576p|720p|1080p|2160p|4k|uhd)\b",
        r"\b(?:blu[- ]?ray|brrip|dvdrip|hdrip|webrip|web[- ]?dl|remux)\b",
        r"\b(?:x264|x265|h264|h265|hevc|aac|rarbg|yify|proper|repack)\b",
    ]
    return any(re.search(m, brut) for m in motifs)


def charger_fiche_film(fichier_plans: Path, donnees: dict) -> dict:
    fiche = dict(donnees.get("fiche") or {})
    fichier_fiche = fichier_plans.with_name("fiche.json")
    if fichier_fiche.exists():
        try:
            fiche.update(json.loads(fichier_fiche.read_text(encoding="utf-8")))
        except Exception:
            pass
    return fiche


def extraire_tags_locaux(video: Path, titre_film: str = "") -> dict:
    r = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_entries",
            "format_tags=title,artist,composer,album:stream=index,codec_type:stream_tags=title,artist,composer",
            str(video),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}

    titre_film_norm = normaliser(titre_film)

    def propre(valeur: str) -> str:
        texte = str(valeur or "").strip()
        if not texte:
            return ""
        brut = normaliser(texte)
        if titre_film_norm and brut == titre_film_norm:
            return ""
        if ressemble_a_un_codec_ou_nom_technique(texte):
            return ""
        if len(texte) > 180:
            return ""
        return texte

    tags = dict(data.get("format", {}).get("tags") or {})
    for flux in data.get("streams", []):
        if flux.get("codec_type") == "audio":
            tags_audio = flux.get("tags") or {}
            for cle in ("title", "artist", "composer"):
                if tags_audio.get(cle) and not tags.get(cle):
                    tags[cle] = tags_audio.get(cle)
            break

    return {
        "title": propre(tags.get("title")),
        "artist": propre(tags.get("artist")),
        "composer": propre(tags.get("composer")),
        "album": propre(tags.get("album")),
    }


def identifier_depuis_texte(texte: str) -> dict:
    texte = str(texte or "")
    motifs = [
        r"[«\"]([^\"]+)[»\"]\s+(?:by|de)\s+([^\n]+)",
        r"(?:song|chanson|musique)\s*[:\-]\s*[«\"]?([^\"\n]+?)[»\"]?\s+(?:by|de)\s+([^\n]+)",
    ]
    for motif in motifs:
        m = re.search(motif, texte, re.I)
        if m:
            titre = re.sub(r"\s+", " ", m.group(1)).strip(" .,:;–-")
            artiste = re.sub(r"\s+", " ", m.group(2)).strip(" .,:;–-")
            if titre and artiste:
                return {
                    "titre_morceau": titre,
                    "artiste_morceau": artiste,
                    "auteur_morceau": artiste,
                    "identification_methode": "indices textuels / sous-titres",
                    "identification_confiance": "moyenne",
                    "a_verifier": True,
                }
    return {}


def extraire_audio_intervalle(source: Path, debut: float, fin: float, wav: Path) -> bool:
    wav.parent.mkdir(parents=True, exist_ok=True)
    duree = max(0.25, float(fin) - float(debut))
    r = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{float(debut):.3f}", "-t", f"{duree:.3f}", "-i", str(source),
            "-map", "0:a:0?", "-vn", "-sn", "-dn", "-ac", "1", "-ar", "22050",
            str(wav),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    return r.returncode == 0 and wav.exists() and wav.stat().st_size > 0


def generer_spectrogramme(wav: Path, png: Path) -> bool:
    png.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav),
            "-lavfi",
            "showspectrumpic=s=1280x720:legend=disabled:scale=log:color=intensity:mode=combined",
            "-frames:v", "1", str(png),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    return r.returncode == 0 and png.exists() and png.stat().st_size > 0


def generer_forme_onde(wav: Path, png: Path) -> bool:
    png.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav),
            "-lavfi", "showwavespic=s=1280x240:colors=white",
            "-frames:v", "1", str(png),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    return r.returncode == 0 and png.exists() and png.stat().st_size > 0


def mesurer_audio(wav: Path) -> dict:
    info = {"mean_volume_db": None, "max_volume_db": None, "silence": False, "duree": 0.0}
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_entries", "format=duration", str(wav)],
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        info["duree"] = float(json.loads(r.stdout).get("format", {}).get("duration") or 0)
    except Exception:
        info["duree"] = 0.0

    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(wav), "-af", "volumedetect", "-f", "null", "-"],
        text=True,
        capture_output=True,
        check=False,
    )
    texte = (r.stderr or "") + "\n" + (r.stdout or "")
    m_mean = re.search(r"mean_volume:\s*(-?[0-9]+(?:\.[0-9]+)?)\s*dB", texte)
    m_max = re.search(r"max_volume:\s*(-?[0-9]+(?:\.[0-9]+)?)\s*dB", texte)
    if m_mean:
        info["mean_volume_db"] = float(m_mean.group(1))
    if m_max:
        info["max_volume_db"] = float(m_max.group(1))
    mean_db = info["mean_volume_db"]
    max_db = info["max_volume_db"]
    info["silence"] = (
        (mean_db is not None and mean_db <= -52.0) or
        (max_db is not None and max_db <= -34.0)
    )
    return info


def schema_audio_detaille() -> dict:
    return {
        "type": "object",
        "properties": {
            "musique_presente": {"type": "boolean"},
            "parole_chantee": {"type": "boolean"},
            "musique_familles": {
                "type": "array",
                "items": {"type": "string", "enum": MUSIQUE_FAMILLES},
                "maxItems": 3,
            },
            "musique_sous_genres": {
                "type": "array",
                "items": {"type": "string", "enum": MUSIQUE_SOUS_GENRES},
                "maxItems": 4,
            },
            "design_sonore_types": {
                "type": "array",
                "items": {"type": "string", "enum": DESIGN_SONORE_TYPES},
                "maxItems": 4,
            },
            "intensite_sonore": {"type": "string", "enum": INTENSITES_SONORES},
            "titre_morceau": {"type": "string"},
            "artiste_morceau": {"type": "string"},
            "auteur_morceau": {"type": "string"},
            "identification_methode": {"type": "string", "enum": IDENTIFICATIONS_METHODE},
            "identification_confiance": {"type": "string", "enum": IDENTIFICATIONS_CONFIANCE},
            "a_verifier": {"type": "boolean"},
            "notes_audio": {"type": "string"},
        },
        "required": AUDIO_CLES,
    }


def prompt_audio(plan: dict, raisons: list[str], mesures: dict, tags: dict, fiche: dict | None = None) -> str:
    texte_dialogue = str(plan.get("dialogue_texte") or "").strip()
    indices = json.dumps({
        "dialogue_source": plan.get("dialogue_source", ""),
        "dialogue_types": plan.get("dialogue_types", []),
        "musique_types": plan.get("musique_types", []),
        "ambiance_types": plan.get("ambiance_types", []),
        "mesures_audio": mesures,
        "metadonnees_locales": tags,
    }, ensure_ascii=False, indent=1)
    return f"""Tu observes trois supports issus du son exact d’un même plan de film :
1. une image fixe du plan (contexte visuel) ;
2. un spectrogramme du son du plan ;
3. une forme d’onde du son du plan.

{contexte_film_prompt(fiche)}

Timecode du plan : {plan.get('tc', '')}
Durée du plan : {plan.get('duree', '')} seconde(s)
Texte de dialogue / sous-titres déjà indexé : {texte_dialogue or '(aucun texte disponible)'}

Indices déjà disponibles :
{indices}

Raisons de cette seconde passe audio : {', '.join(raisons) or 'analyse audio détaillée demandée'}.

Ta mission est strictement sonore et doit rester prudente.

Détermine :
- si de la musique est réellement présente dans ce plan ;
- s’il y a de la parole chantée ;
- jusqu’à 3 grandes familles musicales parmi la liste fermée ;
- jusqu’à 4 sous-genres plausibles parmi la liste fermée ;
- jusqu’à 4 types de design sonore / ambiance parmi la liste fermée ;
- l’intensité sonore globale ;
- une courte note audio factuelle en français.

Pour titre_morceau, artiste_morceau et auteur_morceau :
- ne remplis ces champs que si un indice local explicite le permet vraiment
  (paroles / sous-titres nommant le morceau, métadonnées locales claires,
  ou autre indice direct) ;
- n’invente jamais un titre connu à partir d’une simple impression ;
- si tu hésites, laisse ces champs vides et mets a_verifier=true.

Règles fortes :
- si le plan est silencieux ou quasi silencieux, réponds musique_presente=false,
  musique_familles=["indéterminée"] ou [], musique_sous_genres=[],
  design_sonore_types=["silence / quasi-silence"], titre_morceau="",
  artiste_morceau="", auteur_morceau="" ;
- si la musique est perceptible mais impossible à qualifier proprement,
  utilise la famille "indéterminée" et le sous-genre "indéterminée" ;
- ne confonds pas musique et simple bruitage ;
- ne fais aucune promesse de reconnaissance certaine d’un morceau commercial sans preuve.

Réponds uniquement avec un objet JSON respectant exactement ce schéma implicite :
- musique_presente: bool
- parole_chantee: bool
- musique_familles: 0 à 3 valeurs parmi {MUSIQUE_FAMILLES}
- musique_sous_genres: 0 à 4 valeurs parmi {MUSIQUE_SOUS_GENRES}
- design_sonore_types: 0 à 4 valeurs parmi {DESIGN_SONORE_TYPES}
- intensite_sonore: une valeur parmi {INTENSITES_SONORES}
- titre_morceau: string
- artiste_morceau: string
- auteur_morceau: string
- identification_methode: une valeur parmi {IDENTIFICATIONS_METHODE}
- identification_confiance: une valeur parmi {IDENTIFICATIONS_CONFIANCE}
- a_verifier: bool
- notes_audio: courte phrase factuelle en français

Important : une chaîne technique comme "E-AC-3 5.1", "Dolby", "stereo" ou un nom de piste codec
ne doit jamais être prise pour un titre de morceau.
"""


def resume_dialogues(plans: list[dict], limite: int = 900) -> str:
    morceaux = []
    vus = set()
    for plan in plans:
        texte = re.sub(r"\s+", " ", str(plan.get("dialogue_texte") or "")).strip()
        if not texte:
            continue
        cle = texte[:160]
        if cle in vus:
            continue
        vus.add(cle)
        morceaux.append(f"plan {plan.get('n')} : {texte}")
    texte = " | ".join(morceaux)
    if len(texte) > limite:
        return texte[: limite - 1].rstrip() + "…"
    return texte


def images_sequence(racine: Path, plans: list[dict]) -> list[Path]:
    if not plans:
        return []
    indices = sorted({0, len(plans) // 2, len(plans) - 1})
    supports = []
    for idx in indices:
        support = support_visuel_plan(racine, plans[idx])
        if support and support.exists() and support not in supports:
            supports.append(support)
    return supports


def prompt_sequence_audio(sequence: dict, raisons: list[str], mesures: dict, tags: dict, fiche: dict | None = None) -> str:
    plans = sequence.get("plans_obj") or []
    texte_dialogue = resume_dialogues(plans)
    indices = json.dumps({
        "plans": [int(p.get("n") or 0) for p in plans],
        "dialogues_detectes": sum(1 for p in plans if p.get("dialogue")),
        "musique_types_existants": sorted({x for p in plans for x in (p.get("musique_types") or []) if x}),
        "ambiances_existantes": sorted({x for p in plans for x in (p.get("ambiance_types") or []) if x}),
        "mesures_audio": mesures,
        "metadonnees_locales": tags,
        "empreinte_locale": sequence.get("empreinte_locale") or {},
    }, ensure_ascii=False, indent=1)
    return f"""Tu observes plusieurs supports issus d’une même séquence sonore de film, couvrant plusieurs plans consécutifs :
1. jusqu’à trois images fixes (début, milieu, fin) pour situer le contexte visuel ;
2. un spectrogramme de l’extrait sonore complet ;
3. une forme d’onde du même extrait.

{contexte_film_prompt(fiche)}

Séquence : {sequence.get('id')}
Plans concernés : {sequence.get('plans_txt')}
Fenêtre sonore : {sequence.get('tc_debut')} → {sequence.get('tc_fin')}
Durée totale : {sequence.get('duree', 0):.1f} secondes
Dialogue déjà indexé sur cette séquence : {texte_dialogue or '(aucun texte utile)'}

Indices déjà disponibles :
{indices}

Raisons de cette passe globale : {', '.join(raisons) or 'analyse sonore à l’échelle de la séquence'}.

Ta mission reste strictement sonore et prudente, mais cette fois à l’échelle de la séquence entière, pas d’un seul plan.
La musique peut donc traverser plusieurs cuts. Si un même morceau ou une même texture continue sur plusieurs plans,
raisonne à cette échelle globale.

Détermine :
- si une musique est réellement présente dans cette séquence ;
- s’il y a de la parole chantée ;
- jusqu’à 3 grandes familles musicales ;
- jusqu’à 4 sous-genres plausibles ;
- jusqu’à 4 types de design sonore / ambiance ;
- l’intensité sonore globale ;
- une note audio factuelle courte en français.

Pour titre_morceau, artiste_morceau et auteur_morceau :
- privilégie les indices explicites et l’empreinte si elle est fournie ;
- n’invente jamais un morceau connu par simple ressemblance stylistique ;
- si l’empreinte locale seule existe mais sans résolution distante, laisse les noms vides ;
- si tu hésites, laisse vide et mets a_verifier=true.

Règles fortes :
- si l’extrait est silencieux ou quasi silencieux, réponds musique_presente=false,
  design_sonore_types=["silence / quasi-silence"], titre_morceau="", artiste_morceau="", auteur_morceau="" ;
- si la musique est perceptible mais floue, utilise la famille "indéterminée" ;
- ne confonds pas musique et bruitage ;
- ne prétends pas qu’un titre est reconnu sans preuve concrète.

Réponds uniquement avec un objet JSON respectant exactement ce schéma implicite :
- musique_presente: bool
- parole_chantee: bool
- musique_familles: 0 à 3 valeurs parmi {MUSIQUE_FAMILLES}
- musique_sous_genres: 0 à 4 valeurs parmi {MUSIQUE_SOUS_GENRES}
- design_sonore_types: 0 à 4 valeurs parmi {DESIGN_SONORE_TYPES}
- intensite_sonore: une valeur parmi {INTENSITES_SONORES}
- titre_morceau: string
- artiste_morceau: string
- auteur_morceau: string
- identification_methode: une valeur parmi {IDENTIFICATIONS_METHODE}
- identification_confiance: une valeur parmi {IDENTIFICATIONS_CONFIANCE}
- a_verifier: bool
- notes_audio: courte phrase factuelle en français

Important : une chaîne technique comme "E-AC-3 5.1", "Dolby", "stereo" ou un nom de piste codec
ne doit jamais être prise pour un titre de morceau.
"""


def fusionner_audio(plan: dict, reponse: dict) -> None:
    plan["musique_presente"] = bool(reponse.get("musique_presente"))
    plan["parole_chantee"] = bool(reponse.get("parole_chantee"))
    plan["musique_familles"] = reponse.get("musique_familles") if isinstance(reponse.get("musique_familles"), list) else []
    plan["musique_sous_genres"] = reponse.get("musique_sous_genres") if isinstance(reponse.get("musique_sous_genres"), list) else []
    plan["design_sonore_types"] = reponse.get("design_sonore_types") if isinstance(reponse.get("design_sonore_types"), list) else []
    plan["intensite_sonore"] = str(reponse.get("intensite_sonore") or "")
    plan["musique_titre"] = str(reponse.get("titre_morceau") or "")
    plan["musique_artiste"] = str(reponse.get("artiste_morceau") or "")
    plan["musique_auteur"] = str(reponse.get("auteur_morceau") or "")
    plan["musique_methode"] = str(reponse.get("identification_methode") or "")
    plan["musique_confiance"] = str(reponse.get("identification_confiance") or "")
    plan["musique_a_verifier"] = bool(reponse.get("a_verifier"))
    plan["audio_notes"] = str(reponse.get("notes_audio") or "")


def neutraliser_identification_trompeuse(reponse: dict) -> dict:
    titre = str(reponse.get("titre_morceau") or "")
    methode = str(reponse.get("identification_methode") or "")
    if (
        ressemble_a_un_codec_ou_nom_technique(titre)
        or (titre and methode == "analyse spectrale locale")
        or (titre and not reponse.get("musique_presente") and methode == "métadonnées locales")
    ):
        reponse["titre_morceau"] = ""
        reponse["artiste_morceau"] = ""
        reponse["auteur_morceau"] = ""
        reponse["identification_methode"] = ""
        reponse["identification_confiance"] = ""
        reponse["a_verifier"] = False
    return reponse


def score_audio(plan: dict, tous_les_plans: bool = False, refaire: bool = False, numero_force: int | None = None) -> tuple[int, list[str]]:
    if numero_force is not None and int(plan.get("n") or 0) != int(numero_force):
        return 0, []
    if plan.get("audio_detaille") and not (plan.get("audio_detaille") or {}).get("erreur") and not refaire:
        return 0, []
    if tous_les_plans:
        return 1, ["analyse audio détaillée forcée sur tous les plans"]

    score = 0
    raisons = []
    if plan.get("dialogue") or str(plan.get("dialogue_texte") or "").strip():
        score += 4
        raisons.append("dialogue déjà indexé")
    if any(x for x in plan.get("musique_types", []) if x != "aucune musique signalée"):
        score += 5
        raisons.append("indices de musique déjà signalés")
    if any(x for x in plan.get("ambiance_types", []) if x != "ambiance non qualifiée"):
        score += 3
        raisons.append("indices d’ambiance sonore déjà signalés")
    duree = float(plan.get("duree") or 0)
    if duree >= 8:
        score += 1
        raisons.append("plan assez long pour une lecture sonore")
    if duree >= 20:
        score += 1
    return score, raisons


def candidats_plans(racine: Path, film: str | None, seuil: int, tous_les_plans: bool = False,
                    refaire: bool = False, numero_force: int | None = None) -> list[tuple[Path, dict, int, list[str]]]:
    selection = []
    for fichier in sorted(racine.glob("*/plans.json")):
        if film and fichier.parent.name != film:
            continue
        data = json.loads(fichier.read_text(encoding="utf-8"))
        for plan in data.get("plans", []):
            score, raisons = score_audio(plan, tous_les_plans=tous_les_plans, refaire=refaire, numero_force=numero_force)
            if score >= seuil:
                selection.append((fichier, plan, score, raisons))
    selection.sort(key=lambda item: (-item[2], item[0].parent.name, item[1].get("n", 0)))
    return selection


def support_visuel_plan(racine: Path, plan: dict) -> Path | None:
    for rel in (plan.get("vignettes") or []):
        p = racine / rel
        if p.exists():
            return p
    vignette = plan.get("vignette")
    if vignette:
        p = racine / vignette
        if p.exists():
            return p
    return None


def plan_debut(plan: dict) -> float:
    return float(plan.get("debut") or 0)


def plan_fin(plan: dict) -> float:
    fin = float(plan.get("fin") or plan_debut(plan))
    return fin if fin > plan_debut(plan) else plan_debut(plan)


def plans_contigus(plan_a: dict, plan_b: dict, gap_max: float) -> bool:
    return plan_debut(plan_b) - plan_fin(plan_a) <= gap_max


def duree_fenetre(plans: list[dict], debut_idx: int, fin_idx: int) -> float:
    return max(0.25, plan_fin(plans[fin_idx]) - plan_debut(plans[debut_idx]))


def timecode_humain(secondes: float) -> str:
    total_ms = int(round(max(0.0, float(secondes)) * 1000))
    h, reste = divmod(total_ms, 3_600_000)
    m, reste = divmod(reste, 60_000)
    s, ms = divmod(reste, 1_000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def construire_sequence_autour(plans: list[dict], idx: int, duree_cible: float,
                               duree_max: float, rayon_plans: int, gap_max: float) -> tuple[int, int]:
    debut_idx = fin_idx = idx
    for _ in range(max(0, rayon_plans)):
        change = False
        if debut_idx > 0 and plans_contigus(plans[debut_idx - 1], plans[debut_idx], gap_max):
            if duree_fenetre(plans, debut_idx - 1, fin_idx) <= duree_max:
                debut_idx -= 1
                change = True
        if fin_idx < len(plans) - 1 and plans_contigus(plans[fin_idx], plans[fin_idx + 1], gap_max):
            if duree_fenetre(plans, debut_idx, fin_idx + 1) <= duree_max:
                fin_idx += 1
                change = True
        if not change:
            break

    while duree_fenetre(plans, debut_idx, fin_idx) < duree_cible:
        options = []
        if debut_idx > 0 and plans_contigus(plans[debut_idx - 1], plans[debut_idx], gap_max):
            nouvelle_duree = duree_fenetre(plans, debut_idx - 1, fin_idx)
            if nouvelle_duree <= duree_max:
                gap = max(0.0, plan_debut(plans[debut_idx]) - plan_fin(plans[debut_idx - 1]))
                options.append((gap, nouvelle_duree, "left"))
        if fin_idx < len(plans) - 1 and plans_contigus(plans[fin_idx], plans[fin_idx + 1], gap_max):
            nouvelle_duree = duree_fenetre(plans, debut_idx, fin_idx + 1)
            if nouvelle_duree <= duree_max:
                gap = max(0.0, plan_debut(plans[fin_idx + 1]) - plan_fin(plans[fin_idx]))
                options.append((gap, nouvelle_duree, "right"))
        if not options:
            break
        options.sort(key=lambda item: (item[0], item[1], item[2]))
        direction = options[0][2]
        if direction == "left":
            debut_idx -= 1
        else:
            fin_idx += 1
    return debut_idx, fin_idx


def creer_sequence(fichier: Path, plans: list[dict], debut_idx: int, fin_idx: int, score: int,
                   raisons: list[str], mode_sequence: str) -> dict:
    subset = plans[debut_idx:fin_idx + 1]
    numeros = [int(p.get("n") or 0) for p in subset]
    debut = plan_debut(subset[0])
    fin = plan_fin(subset[-1])
    return {
        "fichier": fichier,
        "film": fichier.parent.name,
        "debut_idx": debut_idx,
        "fin_idx": fin_idx,
        "plans_obj": subset,
        "plans_n": numeros,
        "plans_txt": f"{numeros[0]} → {numeros[-1]}" if len(numeros) > 1 else str(numeros[0]),
        "debut": debut,
        "fin": fin,
        "duree": round(fin - debut, 3),
        "tc_debut": timecode_humain(debut),
        "tc_fin": timecode_humain(fin),
        "score": score,
        "raisons": list(raisons),
        "id": f"seq_{numeros[0]:05d}_{numeros[-1]:05d}",
        "mode_sequence": mode_sequence,
    }


def fusionner_sequences(candidates: list[dict], duree_max: float) -> list[dict]:
    if not candidates:
        return []
    candidates = sorted(candidates, key=lambda s: (s["film"], s["debut_idx"], s["fin_idx"]))
    fusion = []
    for courant in candidates:
        if not fusion or courant["film"] != fusion[-1]["film"]:
            fusion.append(courant)
            continue
        precedent = fusion[-1]
        if courant["debut_idx"] <= precedent["fin_idx"] + 1:
            nouvelle_duree = max(precedent["fin"], courant["fin"]) - min(precedent["debut"], courant["debut"])
            if nouvelle_duree <= duree_max:
                precedent["debut_idx"] = min(precedent["debut_idx"], courant["debut_idx"])
                precedent["fin_idx"] = max(precedent["fin_idx"], courant["fin_idx"])
                plans = precedent["plans_obj"] + courant["plans_obj"]
                uniques = []
                deja = set()
                for plan in plans:
                    numero = int(plan.get("n") or 0)
                    if numero in deja:
                        continue
                    deja.add(numero)
                    uniques.append(plan)
                precedent["plans_obj"] = uniques
                precedent["plans_n"] = [int(p.get("n") or 0) for p in uniques]
                precedent["plans_txt"] = f"{precedent['plans_n'][0]} → {precedent['plans_n'][-1]}" if len(precedent["plans_n"]) > 1 else str(precedent["plans_n"][0])
                precedent["debut"] = min(precedent["debut"], courant["debut"])
                precedent["fin"] = max(precedent["fin"], courant["fin"])
                precedent["duree"] = round(precedent["fin"] - precedent["debut"], 3)
                precedent["tc_debut"] = timecode_humain(precedent["debut"])
                precedent["tc_fin"] = timecode_humain(precedent["fin"])
                precedent["score"] = max(precedent["score"], courant["score"])
                precedent["raisons"] = sorted(set(precedent["raisons"] + courant["raisons"]))
                precedent["id"] = f"seq_{precedent['plans_n'][0]:05d}_{precedent['plans_n'][-1]:05d}"
                continue
        fusion.append(courant)
    return fusion


def sequences_candidates(racine: Path, film: str | None, seuil: int, refaire: bool,
                         numero_force: int | None, duree_cible: float, duree_max: float,
                         rayon_plans: int, gap_max: float) -> list[dict]:
    resultats = []
    for fichier in sorted(racine.glob("*/plans.json")):
        if film and fichier.parent.name != film:
            continue
        data = json.loads(fichier.read_text(encoding="utf-8"))
        plans = data.get("plans", [])
        candidats = []
        for idx, plan in enumerate(plans):
            score, raisons = score_audio(plan, tous_les_plans=False, refaire=refaire, numero_force=numero_force)
            if score < seuil:
                continue
            debut_idx, fin_idx = construire_sequence_autour(plans, idx, duree_cible, duree_max, rayon_plans, gap_max)
            candidats.append(creer_sequence(fichier, plans, debut_idx, fin_idx, score, raisons, "autour-plan"))
        resultats.extend(fusionner_sequences(candidats, duree_max=duree_max))
    resultats.sort(key=lambda s: (-s["score"], s["film"], s["debut"]))
    return resultats


def sequences_film_entier(racine: Path, film: str | None, duree_cible: float, duree_max: float,
                          gap_max: float) -> list[dict]:
    resultats = []
    for fichier in sorted(racine.glob("*/plans.json")):
        if film and fichier.parent.name != film:
            continue
        data = json.loads(fichier.read_text(encoding="utf-8"))
        plans = data.get("plans", [])
        if not plans:
            continue
        debut_idx = 0
        while debut_idx < len(plans):
            fin_idx = debut_idx
            while fin_idx < len(plans) - 1:
                if not plans_contigus(plans[fin_idx], plans[fin_idx + 1], gap_max):
                    break
                nouvelle_duree = duree_fenetre(plans, debut_idx, fin_idx + 1)
                if nouvelle_duree > duree_max:
                    break
                fin_idx += 1
                if nouvelle_duree >= duree_cible:
                    break
            subset = plans[debut_idx:fin_idx + 1]
            raisons = ["analyse sonore globale sur séquence multi-plans"]
            if any(p.get("dialogue") for p in subset):
                raisons.append("présence de dialogues ou de voix sur la séquence")
            resultats.append(creer_sequence(fichier, plans, debut_idx, fin_idx, 1, raisons, "film-entier"))
            debut_idx = fin_idx + 1
    resultats.sort(key=lambda s: (s["film"], s["debut"]))
    return resultats


def confiance_depuis_score(score: float) -> str:
    if score >= 0.90:
        return "élevée"
    if score >= 0.70:
        return "moyenne"
    return "faible"


def config_acoustid_api_key() -> str:
    cfg = lire_config_locale()
    return str(cfg.get("acoustid_api_key") or "").strip()


def acoustid_api_key(args) -> str:
    return (
        str(getattr(args, "acoustid_api_key", "") or "").strip()
        or str(os.environ.get("ACOUSTID_API_KEY") or "").strip()
        or config_acoustid_api_key()
    )


def empreinte_disponible() -> bool:
    return acoustid is not None and (shutil.which("fpcalc") is not None)


def extraire_auteurs_musicbrainz(recording_id: str) -> str:
    if not musicbrainzngs or not recording_id:
        return ""
    try:
        musicbrainzngs.set_useragent(*MUSICBRAINZ_USER_AGENT)
        data = musicbrainzngs.get_recording_by_id(recording_id, includes=["work-rels", "artist-credits"])
        recording = data.get("recording") or {}
        relations = recording.get("work-relation-list") or []
        auteurs = []
        for rel in relations:
            work = rel.get("work") or {}
            for sous_rel in work.get("artist-relation-list") or []:
                rel_type = normaliser(sous_rel.get("type") or "")
                if rel_type in {"composer", "writer", "lyricist"}:
                    artiste = sous_rel.get("artist") or {}
                    nom = str(artiste.get("name") or "").strip()
                    if nom and nom not in auteurs:
                        auteurs.append(nom)
        return ", ".join(auteurs[:4])
    except Exception:
        return ""


def empreinte_audio(wav: Path, args) -> dict:
    mode = getattr(args, "empreinte_mode", "auto")
    info = {
        "mode": mode,
        "active": mode != "off",
        "disponible": False,
        "api_key_presente": False,
        "fingerprint": "",
        "duration": 0,
        "candidats": [],
        "titre_morceau": "",
        "artiste_morceau": "",
        "auteur_morceau": "",
        "identification_methode": "",
        "identification_confiance": "",
        "a_verifier": False,
        "erreur": "",
    }
    if mode == "off":
        return info
    if acoustid is None:
        info["erreur"] = "pyacoustid absent"
        return info
    try:
        duree, empreinte = acoustid.fingerprint_file(str(wav), force_fpcalc=True)
    except Exception as exc:
        info["erreur"] = str(exc)
        return info
    info["disponible"] = True
    info["duration"] = int(round(float(duree or 0)))
    info["fingerprint"] = str(empreinte or "")
    info["identification_methode"] = "empreinte acoustid locale"

    cle = acoustid_api_key(args)
    info["api_key_presente"] = bool(cle)
    if mode == "local" or not cle:
        return info

    try:
        resultats = []
        for score, recording_id, title, artist in acoustid.match(cle, str(wav), timeout=ACOUSTID_TIMEOUT, force_fpcalc=True):
            resultats.append({
                "score": round(float(score or 0), 4),
                "recording_id": str(recording_id or ""),
                "title": str(title or "").strip(),
                "artist": str(artist or "").strip(),
            })
            if len(resultats) >= 5:
                break
        info["candidats"] = resultats
        if resultats:
            meilleur = resultats[0]
            if meilleur["title"] and meilleur["artist"]:
                info["titre_morceau"] = meilleur["title"]
                info["artiste_morceau"] = meilleur["artist"]
                info["auteur_morceau"] = extraire_auteurs_musicbrainz(meilleur["recording_id"]) or meilleur["artist"]
                info["identification_methode"] = "empreinte acoustid + musicbrainz"
                info["identification_confiance"] = confiance_depuis_score(float(meilleur["score"]))
                info["a_verifier"] = float(meilleur["score"]) < 0.95
    except Exception as exc:
        info["erreur"] = str(exc)
    return info


def appliquer_identification_externe_si_absente(reponse: dict, empreinte: dict) -> dict:
    if reponse.get("titre_morceau") and reponse.get("artiste_morceau"):
        return reponse
    titre = str(empreinte.get("titre_morceau") or "").strip()
    artiste = str(empreinte.get("artiste_morceau") or "").strip()
    if not titre or not artiste:
        return reponse
    reponse["titre_morceau"] = titre
    reponse["artiste_morceau"] = artiste
    reponse["auteur_morceau"] = str(empreinte.get("auteur_morceau") or artiste)
    reponse["identification_methode"] = str(empreinte.get("identification_methode") or "empreinte acoustid + musicbrainz")
    reponse["identification_confiance"] = str(empreinte.get("identification_confiance") or "moyenne")
    reponse["a_verifier"] = bool(empreinte.get("a_verifier", True))
    return reponse


def reponse_silence() -> dict:
    return {
        "musique_presente": False,
        "parole_chantee": False,
        "musique_familles": [],
        "musique_sous_genres": [],
        "design_sonore_types": ["silence / quasi-silence"],
        "intensite_sonore": "très faible",
        "titre_morceau": "",
        "artiste_morceau": "",
        "auteur_morceau": "",
        "identification_methode": "",
        "identification_confiance": "",
        "a_verifier": False,
        "notes_audio": "Extrait très faible ou quasi silencieux.",
    }


def reponse_non_concluante() -> dict:
    return {
        "musique_presente": False,
        "parole_chantee": False,
        "musique_familles": [],
        "musique_sous_genres": [],
        "design_sonore_types": ["indéterminé"],
        "intensite_sonore": "",
        "titre_morceau": "",
        "artiste_morceau": "",
        "auteur_morceau": "",
        "identification_methode": "",
        "identification_confiance": "",
        "a_verifier": True,
        "notes_audio": "Analyse locale non concluante.",
    }


def traiter_plan_mode(client, schema: dict, fichier: Path, plan: dict, score: int, raisons: list[str], args) -> bool:
    data = json.loads(fichier.read_text(encoding="utf-8"))
    plan_live = next((p for p in data.get("plans", []) if p.get("n") == plan.get("n")), plan)
    source = Path(data.get("source", ""))
    if not source.exists():
        plan_live["audio_detaille"] = {
            "modele": args.modele,
            "mode": "plan",
            "erreur": "source vidéo absente",
            "genere": time.strftime("%Y-%m-%d %H:%M"),
        }
        fichier.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return False

    audio_dir = fichier.parent / "audio_detaille"
    numero = int(plan_live.get("n") or 0)
    wav = audio_dir / f"{numero:05d}.wav"
    spectro = audio_dir / f"{numero:05d}_spectre.png"
    onde = audio_dir / f"{numero:05d}_onde.png"

    if not extraire_audio_intervalle(source, float(plan_live.get("debut") or 0), float(plan_live.get("fin") or 0), wav):
        plan_live["audio_detaille"] = {
            "modele": args.modele,
            "mode": "plan",
            "erreur": "audio absent ou inexploitable",
            "genere": time.strftime("%Y-%m-%d %H:%M"),
        }
        fichier.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return False

    mesures = mesurer_audio(wav)
    tags = extraire_tags_locaux(source, data.get("titre") or data.get("film") or source.stem)
    empreinte = empreinte_audio(wav, args)

    if mesures.get("silence"):
        reponse = reponse_silence()
    else:
        supports = []
        image_plan = support_visuel_plan(args.racine, plan_live)
        if image_plan and image_plan.exists():
            supports.append(image_plan)
        if generer_spectrogramme(wav, spectro):
            supports.append(spectro)
        if generer_forme_onde(wav, onde):
            supports.append(onde)
        if not supports:
            plan_live["audio_detaille"] = {
                "modele": args.modele,
                "mode": "plan",
                "erreur": "supports audio visuels absents",
                "genere": time.strftime("%Y-%m-%d %H:%M"),
            }
            fichier.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            return False
        fiche = charger_fiche_film(fichier, data)
        reponse = interroger(client, args.modele, prompt_audio(plan_live, raisons, mesures, tags, fiche), supports, schema) or reponse_non_concluante()

    indices_texte = identifier_depuis_texte(plan_live.get("dialogue_texte") or "")
    if indices_texte and not (reponse.get("titre_morceau") and reponse.get("artiste_morceau")):
        reponse.update(indices_texte)
    if tags.get("title") and not reponse.get("titre_morceau"):
        reponse["titre_morceau"] = tags.get("title", "")
        reponse["artiste_morceau"] = reponse.get("artiste_morceau") or tags.get("artist", "")
        reponse["auteur_morceau"] = reponse.get("auteur_morceau") or tags.get("composer", "") or tags.get("artist", "")
        reponse["identification_methode"] = reponse.get("identification_methode") or "métadonnées locales"
        reponse["identification_confiance"] = reponse.get("identification_confiance") or "faible"
        reponse["a_verifier"] = True if reponse.get("a_verifier") is None else bool(reponse.get("a_verifier"))

    reponse = appliquer_identification_externe_si_absente(reponse, empreinte)
    reponse = neutraliser_identification_trompeuse(reponse)

    plan_live["audio_detaille"] = {
        "modele": args.modele,
        "mode": "plan",
        "score_initial": score,
        "raisons": raisons,
        "mesures": mesures,
        "metadonnees_locales": tags,
        "empreinte": empreinte,
        "analyse": reponse,
        "genere": time.strftime("%Y-%m-%d %H:%M"),
    }
    fusionner_audio(plan_live, reponse)
    fichier.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✓ {fichier.parent.name} #{plan_live.get('n')} audio détaillé")
    return True


def appliquer_sequence_aux_plans(plans_subset: list[dict], reponse: dict, meta: dict, mesures: dict, empreinte: dict, modele: str) -> None:
    for plan in plans_subset:
        fusionner_audio(plan, reponse)
        plan["audio_global_ref"] = meta["id"]
        plan["audio_global_debut"] = meta["debut"]
        plan["audio_global_fin"] = meta["fin"]
        plan["audio_global_plans"] = meta["plans_n"]
        plan["audio_global_mode"] = meta["mode_sequence"]
        plan["audio_detaille"] = {
            "modele": modele,
            "mode": f"sequence:{meta['mode_sequence']}",
            "sequence_id": meta["id"],
            "score_initial": meta.get("score", 0),
            "raisons": meta.get("raisons", []),
            "mesures": mesures,
            "empreinte": empreinte,
            "analyse": reponse,
            "genere": time.strftime("%Y-%m-%d %H:%M"),
        }


def enregistrer_sequence_globale(data: dict, meta: dict, mesures: dict, tags: dict, empreinte: dict, reponse: dict, modele: str) -> None:
    bloc = data.setdefault("audio_global", {})
    bloc["mode"] = "sequences"
    bloc["modele"] = modele
    bloc["genere"] = time.strftime("%Y-%m-%d %H:%M")
    bloc["empreinte_mode"] = empreinte.get("mode", "off")
    sequences = [s for s in list(bloc.get("sequences") or []) if s.get("id") != meta["id"]]
    sequences.append({
        "id": meta["id"],
        "mode_sequence": meta["mode_sequence"],
        "plans": meta["plans_n"],
        "debut": meta["debut"],
        "fin": meta["fin"],
        "tc_debut": meta["tc_debut"],
        "tc_fin": meta["tc_fin"],
        "duree": meta["duree"],
        "raisons": meta.get("raisons", []),
        "mesures": mesures,
        "metadonnees_locales": tags,
        "empreinte": empreinte,
        "analyse": reponse,
        "genere": time.strftime("%Y-%m-%d %H:%M"),
    })
    sequences.sort(key=lambda s: (float(s.get("debut") or 0), float(s.get("fin") or 0)))
    bloc["sequences"] = sequences


def traiter_sequence_mode(client, schema: dict, sequence: dict, args) -> bool:
    fichier = sequence["fichier"]
    data = json.loads(fichier.read_text(encoding="utf-8"))
    plans = data.get("plans", [])
    subset = [p for p in plans if int(p.get("n") or 0) in set(sequence["plans_n"])]
    if not subset:
        return False
    source = Path(data.get("source", ""))
    if not source.exists():
        return False

    audio_dir = fichier.parent / "audio_global"
    wav = audio_dir / f"{sequence['id']}.wav"
    spectro = audio_dir / f"{sequence['id']}_spectre.png"
    onde = audio_dir / f"{sequence['id']}_onde.png"

    if not extraire_audio_intervalle(source, sequence["debut"], sequence["fin"], wav):
        return False

    mesures = mesurer_audio(wav)
    tags = extraire_tags_locaux(source, data.get("titre") or data.get("film") or source.stem)
    empreinte = empreinte_audio(wav, args)
    sequence["empreinte_locale"] = {
        "disponible": empreinte.get("disponible"),
        "api_key_presente": empreinte.get("api_key_presente"),
        "candidats": empreinte.get("candidats", [])[:3],
        "erreur": empreinte.get("erreur", ""),
    }

    if mesures.get("silence"):
        reponse = reponse_silence()
    else:
        supports = images_sequence(args.racine, subset)
        if generer_spectrogramme(wav, spectro):
            supports.append(spectro)
        if generer_forme_onde(wav, onde):
            supports.append(onde)
        fiche = charger_fiche_film(fichier, data)
        reponse = interroger(client, args.modele, prompt_sequence_audio({**sequence, "plans_obj": subset}, sequence.get("raisons", []), mesures, tags, fiche), supports, schema) or reponse_non_concluante()

    texte_dialogue = resume_dialogues(subset)
    indices_texte = identifier_depuis_texte(texte_dialogue)
    if indices_texte and not (reponse.get("titre_morceau") and reponse.get("artiste_morceau")):
        reponse.update(indices_texte)
    if tags.get("title") and not reponse.get("titre_morceau"):
        reponse["titre_morceau"] = tags.get("title", "")
        reponse["artiste_morceau"] = reponse.get("artiste_morceau") or tags.get("artist", "")
        reponse["auteur_morceau"] = reponse.get("auteur_morceau") or tags.get("composer", "") or tags.get("artist", "")
        reponse["identification_methode"] = reponse.get("identification_methode") or "métadonnées locales"
        reponse["identification_confiance"] = reponse.get("identification_confiance") or "faible"
        reponse["a_verifier"] = True if reponse.get("a_verifier") is None else bool(reponse.get("a_verifier"))

    reponse = appliquer_identification_externe_si_absente(reponse, empreinte)
    reponse = neutraliser_identification_trompeuse(reponse)

    appliquer_sequence_aux_plans(subset, reponse, sequence, mesures, empreinte, args.modele)
    enregistrer_sequence_globale(data, sequence, mesures, tags, empreinte, reponse, args.modele)
    fichier.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        f"[{sequence.get('courant', '?')}/{sequence.get('total', '?')}] {sequence['film']} {sequence['id']} "
        f"— {sequence['tc_debut']} → {sequence['tc_fin']} — {len(subset)} plans"
    )
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("racine", nargs="?", type=Path, default=Path("analyse"))
    ap.add_argument("--modele", default=os.environ.get("BANC_MODELE_AFFINAGE", os.environ.get("BANC_MODELE_ANALYSE", "")))
    ap.add_argument("--film", help="identifiant du film à relire")
    ap.add_argument("--plan", type=int, help="numéro exact du plan à analyser")
    ap.add_argument("--mode", choices=["plans", "sequences"], default="plans")
    ap.add_argument("--film-entier", action="store_true", help="en mode sequences, couvre tout le film en fenêtres multi-plans")
    ap.add_argument("--seuil", type=int, default=3, help="score minimal pour sélectionner un plan candidat")
    ap.add_argument("--limite", type=int, default=20, help="nombre maximal de plans ou séquences à relire")
    ap.add_argument("--dry-run", action="store_true", help="liste les candidats sans appeler le modèle")
    ap.add_argument("--index-seul", action="store_true", help="reconstruit index.json après la passe")
    ap.add_argument("--refaire", action="store_true", help="refait la passe même si elle existe déjà")
    ap.add_argument("--tous-les-plans", action="store_true", help="force la passe historique sur tous les plans sélectionnés")
    ap.add_argument("--duree-sequence-cible", type=float, default=SEQUENCE_DUREE_CIBLE)
    ap.add_argument("--duree-sequence-max", type=float, default=SEQUENCE_DUREE_MAX)
    ap.add_argument("--rayon-plans", type=int, default=SEQUENCE_RAYON_PLANS)
    ap.add_argument("--gap-max", type=float, default=SEQUENCE_GAP_MAX)
    ap.add_argument("--empreinte-mode", choices=["off", "local", "ping", "auto"], default="auto")
    ap.add_argument("--acoustid-api-key", default="", help="clé API AcoustID optionnelle pour résoudre un titre à distance")
    args = ap.parse_args()

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        sys.exit("ffmpeg/ffprobe sont nécessaires.")

    seuil = 1 if args.plan or args.tous_les_plans or args.film_entier else args.seuil
    if args.mode == "sequences":
        if args.film_entier:
            selection = sequences_film_entier(
                args.racine,
                args.film,
                duree_cible=args.duree_sequence_cible,
                duree_max=args.duree_sequence_max,
                gap_max=args.gap_max,
            )
        else:
            selection = sequences_candidates(
                args.racine,
                args.film,
                seuil,
                refaire=args.refaire,
                numero_force=args.plan,
                duree_cible=args.duree_sequence_cible,
                duree_max=args.duree_sequence_max,
                rayon_plans=args.rayon_plans,
                gap_max=args.gap_max,
            )
        if args.limite:
            selection = selection[:args.limite]
        print(f"{len(selection)} séquence(s) candidate(s) pour la passe audio globale.")
        for seq in selection[: min(len(selection), 60)]:
            print(
                f"- {seq['film']} {seq['id']} {seq['tc_debut']} → {seq['tc_fin']} "
                f"({len(seq['plans_obj'])} plans, {seq['duree']:.1f} s) — {', '.join(seq['raisons'])}"
            )
    else:
        selection = candidats_plans(
            args.racine,
            args.film,
            seuil,
            tous_les_plans=args.tous_les_plans or bool(args.plan),
            refaire=args.refaire,
            numero_force=args.plan,
        )
        if args.limite:
            selection = selection[:args.limite]
        print(f"{len(selection)} plan(s) candidat(s) pour la passe audio détaillée.")
        for fichier, plan, score, raisons in selection[: min(len(selection), 40)]:
            print(f"- {fichier.parent.name} #{plan.get('n')} {plan.get('tc')} — score {score} — {', '.join(raisons)}")

    if args.dry_run or not selection:
        return

    import ollama
    client = ollama.Client()
    verifier_modele(client, args.modele)
    schema = schema_audio_detaille()
    traites = 0

    if args.mode == "sequences":
        total = len(selection)
        for i, sequence in enumerate(selection, 1):
            sequence["courant"] = i
            sequence["total"] = total
            if traiter_sequence_mode(client, schema, sequence, args):
                traites += 1
        print(f"\nPasse musique globale terminée : {traites} séquence(s) enrichie(s).")
    else:
        for fichier, plan, score, raisons in selection:
            if traiter_plan_mode(client, schema, fichier, plan, score, raisons, args):
                traites += 1
        print(f"\nPasse audio détaillée terminée : {traites} plan(s) enrichi(s).")

    if args.index_seul:
        subprocess.run([sys.executable, "analyse_plans.py", "--sortie", str(args.racine), "--index-seul"], check=False)


if __name__ == "__main__":
    main()
