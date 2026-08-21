#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyse_plans.py — découpe un film en plans, extrait des images, et fait
décrire chaque plan par un modèle de vision local choisi par l’utilisateur.

Tout est local. Le script est reprenable : relancé, il saute les plans
déjà traités et écrit dans le JSON après chaque plan.

    python3 analyse_plans.py films/*.mkv --sortie analyse --mode complet

Dépendances :
    pip install ollama pillow
    brew install ffmpeg
    ollama pull <modele-vision-local>
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from moteur_vision import creer_moteur, interroger

SCRIPT_ROOT = Path(__file__).resolve().parent

# ─────────────────────────────────────────────────────────────────────────────
#  Vocabulaire contrôlé
#  C'est la pièce maîtresse : sans listes fermées, rien n'est filtrable.
# ─────────────────────────────────────────────────────────────────────────────

ECHELLES = [
    "très gros plan", "gros plan", "plan poitrine", "plan taille",
    "plan américain", "plan moyen", "plan large", "plan d'ensemble",
    "plan général",
]

ANGLES = [
    "frontal", "trois-quarts", "profil", "plongée", "contre-plongée",
    "plongée verticale", "vue subjective", "de dos",
]

MOUVEMENTS = [
    "fixe", "panoramique", "travelling", "travelling optique", "grue",
    "caméra portée", "steadicam", "zoom",
]

LUMIERES = [
    "diffuse", "clair-obscur", "contre-jour", "lumière naturelle",
    "lumière artificielle", "nuit américaine", "surexposée", "silhouette",
    "lumière d'écran",
]

# Le vocabulaire qui sert votre recherche finale.
MACHINES = [
    # écrans et affichages
    "écran cathodique", "écran plat", "moniteur de contrôle", "téléviseur",
    "mur d\u2019écrans", "affichage tête haute", "hologramme", "projection",
    "écran transparent", "interface graphique", "oscilloscope", "radar",
    # machines à calculer
    "terminal informatique", "ordinateur", "ordinateur central",
    "bandes magnétiques", "cartes perforées", "clavier", "imprimante",
    # commandes et instruments
    "pupitre de commande", "tableau de bord", "console de pilotage",
    "voyants lumineux", "cadrans analogiques", "manette", "commutateurs",
    "instrument de mesure", "appareillage scientifique",
    # science-fiction
    "cockpit de vaisseau", "sas", "capsule", "caisson cryogénique",
    "réacteur", "salle des machines", "antenne", "satellite",
    "combinaison spatiale", "casque", "exosquelette", "implant",
    "robot", "androïde", "drone", "bras robotisé", "machine industrielle",
    "arme", "dispositif médical", "téléphone", "caméra", "haut-parleur",
]

# Comment l'image de l'écran est faite : l'axe le plus utile pour comparer
# une décennie à l'autre, et souvent invisible dans une simple description.
INTERFACES = [
    "texte monochrome", "vectoriel", "pixels apparents", "cadrans analogiques",
    "voyants et diodes", "hologramme", "image photographique", "schéma technique",
    "carte ou plan", "typographie surdimensionnée", "interface tactile",
    "signal parasité", "écran éteint",
]

TEXTE_ROLES = [
    "générique d'ouverture", "générique de fin", "titre / intertitre",
    "crédit sur image", "enseigne / signalétique", "interface / écran",
    "texte imprimé", "sous-titre / légende", "autre typographie",
]

TYPOGRAPHIES_CATEGORIES = [
    "linéale", "sérif", "égyptienne", "monospace", "script",
    "gothique", "ornementale", "indéterminée",
]

TYPOGRAPHIES_STYLES = [
    "linéale grotesque", "linéale néo-grotesque", "linéale géométrique",
    "linéale humaniste", "linéale carrée", "futuriste spatiale",
    "futuriste techno", "sérif classique", "sérif moderne", "égyptienne",
    "monospace", "script", "gothique / blackletter", "art déco",
    "ornementale", "indéterminée",
]

PROFONDEURS_CHAMP = [
    "faible", "moyenne", "nette", "hyperfocale", "indéterminée",
]

COMPOSITIONS_CADRE = [
    "règle des tiers", "symétrie", "centrée", "lignes de fuite",
    "cadre dans le cadre", "amorce", "diagonale", "superposition de plans",
    "horizon haut", "horizon bas", "désaxée", "indéterminée",
]

PRESENCE_TEXTE_TYPES = [
    "aucun", "générique", "sous-titres intra-diégétiques", "enseigne",
    "affiche dans le décor", "hud / interface", "texte imprimé",
    "crédit sur image", "titre / intertitre",
]

CLASSIFICATIONS_TYPOGRAPHIQUES = [
    "sérif", "sans-serif", "manuscrite", "monospace", "modulaire",
    "cinétique", "indéterminée",
]

LUMIERE_ETALONNAGES = [
    "high-key", "low-key", "clair-obscur", "contraste fort",
    "contraste doux", "température chaude", "température froide",
    "contre-jour", "silhouette", "lumière diffuse", "lumière d'écran",
    "indéterminé",
]

DIRECTIONS_LUMIERE = [
    "frontale", "latérale gauche", "latérale droite", "zénithale",
    "contre-jour", "arrière-plan", "mixte", "indéterminée",
]

MATIERES_TEXTURES = [
    "grain de pellicule marqué", "grain léger", "bruit numérique", "lisse",
    "aspect argentique", "aspect vidéo", "vhs", "glitch", "flares",
    "halo", "fumée", "reflets", "indéterminée",
]

GENRES_PERSONNES = ["homme", "femme", "genre indéterminé"]

AGES_PERSONNES = [
    "bébé", "enfant", "adolescent", "adulte", "personne âgée",
    "âge indéterminé",
]

CARNATIONS_APPARENTES = [
    "très claire / albinos", "claire", "médiane", "foncée",
    "très foncée", "non visible", "indéterminée",
]

APPARENCES_ETHNIQUES = [
    "afro-descendante apparente", "européenne / blanche apparente",
    "asiatique apparente", "latino-américaine apparente",
    "moyen-orientale apparente", "autochtone apparente",
    "albinos / très pâle", "indéterminée",
]

ANIMAUX_VISIBLES = [
    "chien", "chat", "cheval", "oiseau", "rat", "souris", "serpent",
    "insecte", "poisson", "animal indéterminé",
]

CATEGORIES_PRESENCE = [
    "personne visible", "aucune personne visible", "homme visible",
    "femme visible", "enfant visible", "adolescent visible", "adulte visible",
    "personne âgée visible", "groupe", "silhouette", "animal visible",
    "aucun animal visible",
]

# ─────────────────────────────────────────────────────────────────────────────
#  Fiche film
#  Ces informations ne se déduisent pas de l'image : on les saisit une fois.
#  Au premier passage, le script écrit un gabarit vide dans
#  analyse/<film>/fiche.json — il suffit de le compléter, puis de relancer
#  avec --index-seul pour que le site en tienne compte.
# ─────────────────────────────────────────────────────────────────────────────

FICHE_MODELE = {
    "titre": "",
    "titre_original": "",
    "realisateur": "",
    "annee": None,              # entier : 1968
    "date_sortie": "",          # "1968-04-02" si vous voulez la date exacte
    "pays": "",
    "langue": "",
    "pitch": "",                # résumé très court, utile au prompt IA
    "synopsis": "",
    "scenario": "",             # résumé narratif public, pas le scénario dialogué
    "poster_url": "",           # image du poster ou de l’affiche
    "poster_fichier": "",
    "poster_legende": "",
    "acteurs": [],              # 10 noms principaux, dans l’ordre de crédit
    "scenaristes": [],
    "producteurs": [],
    "directeur_photo": "",
    "chef_decorateur": "",
    "monteur": "",
    "musique": "",
    "costumes": "",
    "effets_speciaux": [],
    "societes_production": [],
    "distributeurs": [],
    "format": "",               # 35 mm, 70 mm, 16 mm, numérique…
    "ratio": "",                # 1.37, 1.85, 2.20, 2.35…
    "couleur": "",              # couleur, noir et blanc, mixte
    "genres": [],
    "sources": [],
    "notes": "",
}


def charger_fiche(base: Path, video: Path, catalogue: dict) -> dict:
    """Lit analyse/<film>/fiche.json, le crée si absent.

    Un catalogue global (--catalogue films.json) permet de pré-remplir
    plusieurs films d'un coup : les clés sont des noms de fichier ou des
    identifiants de film.
    """
    fichier = base / "fiche.json"
    fiche = dict(FICHE_MODELE)

    if fichier.exists():
        fiche.update(json.loads(fichier.read_text("utf-8")))

    pre = catalogue.get(video.name) or catalogue.get(video.stem) \
        or catalogue.get(slug(video.stem))
    if pre:
        fiche.update({k: v for k, v in pre.items() if v not in ("", None, [])})

    if not fiche["titre"]:
        fiche["titre"] = video.stem
    if not fiche["annee"]:
        # une année entre 1890 et 2099 dans le nom du fichier, si elle y est
        trouve = re.search(r"(?<!\d)(18[9]\d|19\d{2}|20\d{2})(?!\d)", video.stem)
        if trouve:
            fiche["annee"] = int(trouve.group(1))
    if fiche["annee"] and not fiche["date_sortie"]:
        fiche["date_sortie"] = str(fiche["annee"])

    fichier.write_text(json.dumps(fiche, ensure_ascii=False, indent=1), "utf-8")
    return fiche


def decennie(annee) -> str:
    try:
        return f"{int(annee) // 10 * 10}s"
    except (TypeError, ValueError):
        return ""


def credits_par_fonction(fiche: dict) -> dict:
    """Regroupe les crédits d’un film par fonction pour l’affichage."""
    groupes = {
        "Réalisation": fiche.get("realisateur"),
        "Scénario": fiche.get("scenaristes", []),
        "Acteurs": fiche.get("acteurs", []),
        "Production": fiche.get("producteurs", []),
        "Photographie": fiche.get("directeur_photo"),
        "Décors": fiche.get("chef_decorateur"),
        "Montage": fiche.get("monteur"),
        "Musique": fiche.get("musique"),
        "Costumes": fiche.get("costumes"),
        "Effets spéciaux": fiche.get("effets_speciaux", []),
        "Sociétés de production": fiche.get("societes_production", []),
        "Distribution": fiche.get("distributeurs", []),
    }
    return {k: v for k, v in groupes.items()
            if v and (not isinstance(v, list) or len(v))}


def normaliser_texte_audio(texte: str) -> str:
    texte = unicodedata.normalize("NFKD", str(texte or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", texte).strip().lower()


def classer_source_audio(source: str) -> str:
    brut = normaliser_texte_audio(source)
    if not brut:
        return "aucune source"
    if "sous-titres" in brut or "sous titres" in brut:
        return "sous-titres intégrés"
    if "whisper" in brut:
        return "transcription whisper locale"
    return source.strip()


MUSIQUE_FAMILLES_DETAILLEES = [
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

MUSIQUE_SOUS_GENRES_DETAILLES = [
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


def analyser_couche_sonore(plan: dict) -> dict:
    texte = str(plan.get("dialogue_texte") or "")
    dialogues = plan.get("dialogues") or []
    source = classer_source_audio(plan.get("dialogue_source") or "")
    brut = normaliser_texte_audio(texte)
    mots = re.findall(r"\b[\w']+\b", brut)
    nb_mots = len(mots)
    nb_segments = len(dialogues)
    nb_questions = texte.count("?")
    nb_exclamations = texte.count("!")
    nb_tirets = len(re.findall(r"(?:^|\s)-\s*\w", texte))
    crochets = re.findall(r"\[[^\]]{0,60}\]", texte)
    crochets_norm = [normaliser_texte_audio(x) for x in crochets]

    dialogue_types = []
    musique_types = []
    ambiance_types = []

    def ajouter(cible: list, valeur: str) -> None:
        if valeur and valeur not in cible:
            cible.append(valeur)

    if plan.get("dialogue") and texte.strip():
        if nb_segments >= 3 or nb_tirets >= 2:
            ajouter(dialogue_types, "conversation")
        if nb_segments <= 2 and nb_mots >= 18:
            ajouter(dialogue_types, "monologue")
        if nb_questions >= 1:
            ajouter(dialogue_types, "questions / réponses")
        if nb_mots <= 8:
            ajouter(dialogue_types, "réplique brève")
        if nb_exclamations >= 2:
            ajouter(dialogue_types, "dialogue expressif")

        if any(x in brut for x in [
            "welcome", "attention", "please state", "this is", "announcement",
            "voiceprint", "recording", "message", "captain speaking",
            "ordinateur", "systeme", "système", "transmission"
        ]):
            ajouter(dialogue_types, "annonce / message")

        if any(x in brut for x in [" sing ", " singing", "song", "chanson", "musique", "orchestra", "theme"]):
            ajouter(musique_types, "chant / chanson")

    if any(any(m in c for m in ["music", "musique", "song", "theme", "♪", "orchestra", "band", "radio"]) for c in crochets_norm) or re.search(r"\[(?:[^\]]*♪[^\]]*|[^\]]*music[^\]]*|[^\]]*musique[^\]]*)\]", texte, re.I):
        ajouter(musique_types, "musique signalée")

    if any(any(m in c for m in ["singing", "chant", "song", "lyrics", "chorus", "choir", "♪"]) for c in crochets_norm):
        ajouter(musique_types, "chant / chanson")

    if any(any(m in c for m in [
        "wind", "thunder", "rain", "applause", "laugh", "laughing", "scream",
        "screaming", "crying", "gun", "gunshot", "explosion", "door", "alarm",
        "sirens", "engine", "static", "noise", "howling", "breathing", "panting"
    ]) for c in crochets_norm):
        ajouter(ambiance_types, "effets / ambiance")

    if any(any(m in c for m in ["speaking in", "radio", "over pa", "intercom", "offscreen"]) for c in crochets_norm):
        ajouter(ambiance_types, "voix hors-champ / médiatisée")

    if plan.get("dialogue") and not dialogue_types:
        ajouter(dialogue_types, "dialogue continu")

    if not musique_types:
        ajouter(musique_types, "aucune musique signalée")
    if plan.get("dialogue") and not ambiance_types and not crochets_norm:
        ajouter(ambiance_types, "ambiance non qualifiée")

    return {
        "audio_source": source,
        "dialogue_types": dialogue_types,
        "musique_types": musique_types,
        "ambiance_types": ambiance_types,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Contexte film et prompts
# ─────────────────────────────────────────────────────────────────────────────

def texte_court(valeur, limite: int = 900) -> str:
    """Texte compact pour ne pas saturer le contexte Ollama."""
    if isinstance(valeur, list):
        valeur = ", ".join(str(v) for v in valeur if v)
    texte = re.sub(r"\s+", " ", str(valeur or "")).strip()
    if len(texte) <= limite:
        return texte
    return texte[:limite].rsplit(" ", 1)[0].rstrip(" .,;:") + "…"


def fiche_contexte_suffisant(fiche: dict) -> bool:
    """Le contexte est utile s’il contient au moins un pitch/synopsis et un récit."""
    accroche = fiche.get("pitch") or fiche.get("synopsis")
    return bool(texte_court(accroche, 20) and texte_court(fiche.get("scenario"), 20))


def fusionner_contextes_fiche(existant: dict, nouveau: dict) -> dict:
    """Complète seulement les champs vides, en conservant les sources."""
    fusion = {**FICHE_MODELE, **existant}
    for cle, valeur in (nouveau or {}).items():
        if cle == "sources":
            sources = list(fusion.get("sources") or [])
            for src in valeur or []:
                if src and src not in sources:
                    sources.append(src)
            fusion["sources"] = sources
        elif cle == "notes":
            note = texte_court(valeur, 2000)
            if note and note not in (fusion.get("notes") or ""):
                fusion["notes"] = ((fusion.get("notes") or "") + " " + note).strip()
        elif valeur not in (None, "", []) and fusion.get(cle) in (None, "", []):
            fusion[cle] = valeur
    return fusion


def completer_contexte_film(base: Path, video: Path, fiche: dict, actif: bool = True) -> dict:
    """Tente de récupérer pitch + résumé narratif une fois par film.

    On ne fait jamais cette recherche à chaque plan : elle serait lente et
    fragile. Si Wikipédia échoue ou limite les requêtes, l’analyse continue avec
    la fiche déjà présente.
    """
    if not actif or fiche_contexte_suffisant(fiche):
        return fiche
    try:
        from enrichir_fiches_wikipedia import fiche_depuis_wikipedia  # import local tardif
        nouveau = fiche_depuis_wikipedia(video)
    except Exception as exc:
        print(f"  contexte film non enrichi ({type(exc).__name__}: {exc})", file=sys.stderr)
        return fiche
    fusion = fusionner_contextes_fiche(fiche, nouveau)
    (base / "fiche.json").write_text(json.dumps(fusion, ensure_ascii=False, indent=1), "utf-8")
    if fiche_contexte_suffisant(fusion):
        film = libelle_film_contexte(fusion)
        if film:
            print(f"  contexte film chargé : {film} · pitch + résumé de scénario")
        else:
            print("  contexte film chargé : pitch + résumé de scénario")
    return fusion


def libelle_film_contexte(fiche: dict | None) -> str:
    """Nom de film compact pour les prompts et traces d’audit."""
    if not fiche:
        return ""
    titre = str(fiche.get("titre") or fiche.get("titre_original") or "").strip()
    if not titre:
        return ""
    annee = fiche.get("annee")
    return f"{titre} ({annee})" if annee else titre


def contexte_film_prompt(fiche: dict | None) -> str:
    """Bloc de contexte narratif injecté dans les prompts de vision."""
    if not fiche:
        return ""
    lignes = []
    deja_vus = set()

    def ajouter_ligne(libelle: str, valeur, limite: int) -> None:
        texte = texte_court(valeur, limite)
        if not texte:
            return
        cle = re.sub(r"\s+", " ", texte).strip().lower()
        if cle in deja_vus:
            return
        deja_vus.add(cle)
        lignes.append(f"{libelle} : {texte}")

    film = libelle_film_contexte(fiche)
    if film:
        lignes.append(f"Film : {film}")
    if fiche.get("realisateur"):
        lignes.append(f"Réalisation : {texte_court(fiche.get('realisateur'), 160)}")
    if fiche.get("scenaristes"):
        lignes.append(f"Scénario : {texte_court(fiche.get('scenaristes'), 220)}")
    if fiche.get("genres"):
        lignes.append(f"Genres : {texte_court(fiche.get('genres'), 180)}")
    ajouter_ligne("Pitch", fiche.get("pitch"), 420)
    ajouter_ligne("Synopsis", fiche.get("synopsis"), 720)
    ajouter_ligne("Résumé narratif", fiche.get("scenario"), 1200)
    if not lignes:
        return ""
    return """

Contexte du film, à utiliser avec prudence :
{contexte}

Ce contexte aide à désambiguïser plus finement les lieux, les machines, les
interfaces, les objets distinctifs et la situation dramatique visible.
Pour la description, vise le résumé le plus précis possible : sujet principal,
action visible, décor, accessoires distinctifs et enjeu apparent seulement si
ces éléments sont réellement lisibles dans l’image. Utilise d’abord le nom du
film et son pitch pour mieux désambiguïser un plan ambigu, puis le synopsis et
le résumé narratif seulement si cela affine une description déjà visible. Mais
l’image reste prioritaire : n’invente jamais un élément qui n’est pas visible
dans les 3 images du plan, ne déduis pas une action hors champ, et n’identifie
pas les personnes par leur nom.
""".format(contexte="\n".join(f"- {ligne}" for ligne in lignes))


def contexte_libre_prompt(texte: str | None, criteres: str | None = None) -> str:
    lignes = []
    contexte = texte_court(texte, 1400)
    if contexte:
        lignes.append(f"- Contexte libre utilisateur : {contexte}")
    criteres_txt = texte_court(criteres, 1200)
    if criteres_txt:
        lignes.append(f"- Critères prioritaires : {criteres_txt}")
    if not lignes:
        return ""
    return """

Consignes locales de l’utilisateur pour ce lot de films :
{lignes}

Utilise ces indications pour orienter l’attention et le vocabulaire du résumé,
sans jamais inventer un élément invisible. L’image reste prioritaire.
""".format(lignes="\n".join(lignes))


def scene_par_id(donnees: dict) -> dict:
    """Indexe les scènes film-level par identifiant."""
    return {s.get("scene_id"): s for s in (donnees.get("scenes") or [])
            if isinstance(s, dict) and s.get("scene_id")}


def scene_du_plan(donnees: dict, plan: dict) -> dict:
    """Retourne la scène associée à un plan, si la couche scènes existe."""
    sid = plan.get("scene_id")
    if sid:
        trouve = scene_par_id(donnees).get(sid)
        if trouve:
            return trouve
    numero = plan.get("n")
    for scene in donnees.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        plans = scene.get("plans") or []
        if numero in plans:
            return scene
        debut = scene.get("plan_debut")
        fin = scene.get("plan_fin")
        if debut and fin and debut <= numero <= fin:
            return scene
    if plan.get("scene_titre") or plan.get("scene_contexte"):
        return {
            "scene_id": plan.get("scene_id"),
            "numero_scene": plan.get("scene_numero"),
            "titre": plan.get("scene_titre"),
            "resume_scene": plan.get("scene_resume"),
            "lieu": plan.get("scene_lieu"),
            "temporalite": plan.get("scene_temporalite"),
            "action_principale": plan.get("scene_action_principale"),
            "ambiance": plan.get("scene_ambiance"),
            "personnages_visibles": plan.get("scene_personnages_visibles"),
            "objets_significatifs": plan.get("scene_objets_significatifs"),
            "motifs_structurants": plan.get("scene_motifs_structurants"),
            "enjeu_narratif": plan.get("scene_enjeu_narratif"),
            "contexte_pour_plans": plan.get("scene_contexte"),
            "confiance": plan.get("scene_confiance"),
        }
    return {}


def contexte_scene_prompt(scene: dict | None) -> str:
    """Bloc de contexte scène injecté plan par plan dans le prompt de vision."""
    if not scene:
        return ""
    lignes = []

    def ajouter(libelle: str, valeur, limite: int) -> None:
        texte = texte_court(valeur, limite)
        if texte:
            lignes.append(f"{libelle} : {texte}")

    ajouter("Scène", scene.get("titre") or scene.get("scene_id"), 220)
    bornes = ""
    if scene.get("plan_debut") and scene.get("plan_fin"):
        bornes = f"plans {scene.get('plan_debut')} à {scene.get('plan_fin')}"
    if scene.get("tc_debut") and scene.get("tc_fin"):
        bornes = (bornes + " · " if bornes else "") + f"{scene.get('tc_debut')} → {scene.get('tc_fin')}"
    ajouter("Bornes", bornes, 180)
    ajouter("Résumé de scène", scene.get("resume_scene"), 520)
    ajouter("Lieu de scène", scene.get("lieu"), 260)
    ajouter("Temporalité", scene.get("temporalite"), 120)
    ajouter("Action dominante", scene.get("action_principale"), 260)
    ajouter("Présences visibles", scene.get("personnages_visibles"), 280)
    ajouter("Objets ou motifs structurants", scene.get("objets_significatifs") or scene.get("motifs_structurants"), 360)
    ajouter("Contexte à utiliser pour ce plan", scene.get("contexte_pour_plans"), 520)
    if not lignes:
        return ""
    return """

Contexte de scène, à utiliser pour désambiguïser le plan :
{contexte}

Ce contexte vient d’un regroupement de plans contigus. Il aide surtout pour les
gros plans ambigus : cockpit, casque, combinaison, commande, objet isolé,
fragment de décor. L’image du plan reste prioritaire : ne transforme pas le
contexte en preuve visuelle si l’élément n’apparaît pas dans les images.
""".format(contexte="\n".join(f"- {ligne}" for ligne in lignes))


def contexte_film_meta(fiche: dict) -> dict:
    """Trace compacte du contexte transmis au modèle pour reprise/audit."""
    return {
        "film": libelle_film_contexte(fiche),
        "titre": fiche.get("titre") or "",
        "annee": fiche.get("annee"),
        "pitch": bool(fiche.get("pitch")),
        "pitch_extrait": texte_court(fiche.get("pitch"), 240),
        "synopsis": bool(fiche.get("synopsis")),
        "scenario": bool(fiche.get("scenario")),
        "sources": fiche.get("sources", []),
    }


def contexte_scene_meta(scene: dict | None) -> dict:
    """Trace compacte du contexte scène transmis au modèle pour reprise/audit."""
    if not scene:
        return {}
    return {
        "scene_id": scene.get("scene_id") or "",
        "numero_scene": scene.get("numero_scene"),
        "titre": scene.get("titre") or "",
        "resume_scene": bool(scene.get("resume_scene")),
        "contexte_pour_plans": bool(scene.get("contexte_pour_plans")),
        "methode": scene.get("methode") or "",
    }


def prompt_triage(fiche: dict | None = None, scene: dict | None = None,
                  contexte_libre: str | None = None, criteres_libres: str | None = None) -> str:
    return f"""Tu observes 3 images extraites d'un même plan de film.
{contexte_film_prompt(fiche)}
{contexte_scene_prompt(scene)}
{contexte_libre_prompt(contexte_libre, criteres_libres)}

Question unique : une machine, un écran, un ordinateur ou un appareil
technique est-il VISIBLE, même petit, même en arrière-plan, même flou ?

Réponds uniquement par un objet JSON :
{{"machine": true|false,
  "types": [valeurs prises dans cette liste : {", ".join(MACHINES)}],
  "certitude": "sûr"|"probable"|"douteux",
  "note": "une courte phrase, en français"}}

En cas de doute, réponds true avec certitude "douteux". Il vaut mieux un
faux positif qu'un plan manqué."""

def prompt_complet(fiche: dict | None = None, scene: dict | None = None,
                   contexte_libre: str | None = None, criteres_libres: str | None = None) -> str:
    return f"""Tu observes 3 images extraites d'un même plan de film
(début, milieu, fin). Analyse-les comme un directeur de la photographie.
{contexte_film_prompt(fiche)}
{contexte_scene_prompt(scene)}
{contexte_libre_prompt(contexte_libre, criteres_libres)}

Réponds uniquement par un objet JSON, en français, avec ces clés :

"echelle"    : une valeur parmi {ECHELLES}
"angle"      : une valeur parmi {ANGLES}
"mouvement"  : une valeur parmi {MOUVEMENTS}
"lumiere"    : une ou deux valeurs parmi {LUMIERES}
"palette"    : 2 à 4 couleurs dominantes, nommées simplement en français
"lieu"       : "intérieur" ou "extérieur"
"personnages": nombre de personnes visibles (entier)
"presences"  : un objet structuré pour les personnes et animaux visibles :
  {{
    "personnes_visibles": true|false,
    "nombre_personnes": nombre de personnes visibles,
    "genres_personnes": 0 à 3 valeurs parmi {GENRES_PERSONNES},
    "ages_personnes": 0 à 5 valeurs parmi {AGES_PERSONNES},
    "carnations_apparentes": valeurs parmi {CARNATIONS_APPARENTES},
    "apparences_ethniques": valeurs parmi {APPARENCES_ETHNIQUES},
    "apparences_ethniques_a_verifier": true|false,
    "origines_ethniques_documentees": liste vide sauf si une source fiable
       ou une fiche validée documente explicitement l’origine ; ne la déduis
       jamais du visage ou de la couleur de peau,
    "animaux_visibles": valeurs parmi {ANIMAUX_VISIBLES},
    "animal_visible": true|false,
    "animal_confiance": "sûr"|"probable"|"douteux",
    "categories_presence": valeurs parmi {CATEGORIES_PRESENCE},
    "note": courte phrase de prudence si nécessaire
  }}
"machine"    : true si une machine, un écran, un ordinateur ou un appareil
               technique est visible, même petit ou flou ; sinon false
"machine_types" : si machine vaut true, valeurs prises dans {MACHINES}
"machine_role"  : si machine vaut true, "sujet principal" | "élément de décor"
                  | "arrière-plan"
"interface"  : si un écran ou un affichage est lisible, une valeur parmi
               {INTERFACES} ; sinon omets la clé
"texte_visible" : true si une typographie, un mot, un crédit, un sous-titre,
               une enseigne ou un texte imprimé est visible ; sinon false
"texte_lisible" : true si la forme de la typographie est assez nette pour être
               qualifiée ; sinon false
"generique"  : true si le plan relève du générique, d’un titre, d’un carton,
               d’un intertitre ou d’un crédit ; sinon false
"texte_role" : si texte_visible vaut true, une valeur parmi {TEXTE_ROLES} ;
               sinon chaîne vide
"typographie_categorie" : si texte_visible vaut true, une valeur parmi
               {TYPOGRAPHIES_CATEGORIES} ; sinon chaîne vide
"typographie_styles" : 0 à 3 valeurs parmi {TYPOGRAPHIES_STYLES}
"typographie_description" : une courte phrase factuelle sur la typographie
               visible (graisse, dessin, ambiance, usage) ; vide si absent
"description": une seule phrase factuelle, précise et contextuelle décrivant
               exactement ce que l'on voit, en nommant le sujet principal,
               l'action visible, le décor, les objets distinctifs et, seulement
               si l'image le permet, l'enjeu apparent de la scène
"mots_cles"  : 4 à 8 mots-clés en français, au singulier, sans article
"certitude"  : "sûr" | "probable" | "douteux", selon la fiabilité de l’analyse

Ajoute aussi une clé "analyse_detaillee" qui synthétise l’analyse avec ces
quatre catégories :

"analyse_detaillee": {{
  "cadrage_optique": {{
    "valeur_plan": reprend exactement la valeur de "echelle",
    "angle_prise_vue": reprend exactement la valeur de "angle",
    "profondeur_champ": une valeur parmi {PROFONDEURS_CHAMP},
    "details_profondeur_champ": courte phrase factuelle sur le net, le flou,
                                 le bokeh et ce qui reste lisible,
    "composition": 1 à 3 valeurs parmi {COMPOSITIONS_CADRE}
  }},
  "direction_artistique_couleur": {{
    "palette_colorimetrique": 3 à 5 couleurs dominantes, en français ou en hex,
    "lumiere_etalonnage": 1 à 4 valeurs parmi {LUMIERE_ETALONNAGES},
    "direction_lumiere_principale": une valeur parmi {DIRECTIONS_LUMIERE},
    "matiere_texture": 1 à 4 valeurs parmi {MATIERES_TEXTURES}
  }},
  "design_graphique_typographie": {{
    "presence_texte": si aucun texte visible, ["aucun"], sinon 1 à 4 valeurs
                       parmi {PRESENCE_TEXTE_TYPES} sans "aucun",
    "classification_typographique": 0 à 3 valeurs parmi
                       {CLASSIFICATIONS_TYPOGRAPHIQUES},
    "composition_graphique": "Aucun" s'il n'y a pas de texte, sinon une courte
                       phrase factuelle sur le placement, la hiérarchie et le
                       rapport texte / image
  }},
  "description_diegetique": {{
    "lieu_decors": 2 à 6 éléments factuels sur le décor et l’architecture,
    "personnages_sujets": 0 à 5 éléments factuels sur nombre, âge perçu,
                           genre perçu et costumes,
    "attitudes_expressions": 0 à 5 éléments factuels sur posture,
                              expression et direction du regard,
    "objets_cles": 1 à 10 objets ou accessoires visibles, au singulier
  }}
}}

N'invente rien. Si un élément n'est pas visible, ne le mentionne pas.
Pour la clé "description", utilise en priorité le nom du film et son pitch,
puis le synopsis et le résumé narratif, seulement pour désambiguïser ce qui est
visible et rendre le résumé plus précis.
Chaque réponse doit décrire un seul plan isolé. Les images fournies sont
plusieurs instants du même plan, pas une liste de plans successifs.
N'écris jamais « premier plan », « deuxième plan », « troisième plan », ni une
énumération équivalente pour commenter les vignettes d'un même plan.
Si le contenu évolue au fil du plan, synthétise cette continuité comme un seul
mouvement ou une seule action continue.
N'utilise jamais ce contexte pour affirmer une action hors champ, un nom de
personnage ou une identité d'acteur non vérifiable visuellement.
Pour "presences", distingue strictement ce qui est visible dans les images :
ne marque pas "femme", "homme", "enfant" ou "animal" si le plan ne le montre
pas. Ne déduis jamais une origine ethnique certaine à partir de l’image ; utilise
au besoin une apparence à vérifier ou une carnation apparente, et laisse
"origines_ethniques_documentees" vide sans source fiable.
Pour "machine", sois généreux : en cas de doute, réponds true."""


def schema_triage() -> dict:
    """Schéma JSON Ollama pour contraindre le triage au décodage."""
    return {
        "type": "object",
        "properties": {
            "machine": {"type": "boolean"},
            "types": {
                "type": "array",
                "items": {"type": "string", "enum": MACHINES},
            },
            "certitude": {"type": "string", "enum": ["sûr", "probable", "douteux"]},
            "note": {"type": "string"},
        },
        "required": ["machine", "types", "certitude", "note"],
    }


def schema_complet() -> dict:
    """Schéma JSON Ollama pour empêcher les valeurs hors vocabulaire."""
    return {
        "type": "object",
        "properties": {
            "echelle": {"type": "string", "enum": ECHELLES},
            "angle": {"type": "string", "enum": ANGLES},
            "mouvement": {"type": "string", "enum": MOUVEMENTS},
            "lumiere": {
                "oneOf": [
                    {"type": "string", "enum": LUMIERES},
                    {"type": "array", "items": {"type": "string", "enum": LUMIERES}},
                ]
            },
            "palette": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 4,
            },
            "lieu": {"type": "string", "enum": ["intérieur", "extérieur"]},
            "personnages": {"type": "integer", "minimum": 0},
            "presences": {
                "type": "object",
                "properties": {
                    "personnes_visibles": {"type": "boolean"},
                    "nombre_personnes": {"type": "integer", "minimum": 0},
                    "genres_personnes": {
                        "type": "array",
                        "items": {"type": "string", "enum": GENRES_PERSONNES},
                        "maxItems": 3,
                    },
                    "ages_personnes": {
                        "type": "array",
                        "items": {"type": "string", "enum": AGES_PERSONNES},
                        "maxItems": 5,
                    },
                    "carnations_apparentes": {
                        "type": "array",
                        "items": {"type": "string", "enum": CARNATIONS_APPARENTES},
                        "maxItems": 4,
                    },
                    "apparences_ethniques": {
                        "type": "array",
                        "items": {"type": "string", "enum": APPARENCES_ETHNIQUES},
                        "maxItems": 4,
                    },
                    "apparences_ethniques_a_verifier": {"type": "boolean"},
                    "origines_ethniques_documentees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 4,
                    },
                    "animaux_visibles": {
                        "type": "array",
                        "items": {"type": "string", "enum": ANIMAUX_VISIBLES},
                        "maxItems": 5,
                    },
                    "animal_visible": {"type": "boolean"},
                    "animal_confiance": {
                        "type": "string",
                        "enum": ["sûr", "probable", "douteux"],
                    },
                    "categories_presence": {
                        "type": "array",
                        "items": {"type": "string", "enum": CATEGORIES_PRESENCE},
                        "maxItems": 8,
                    },
                    "note": {"type": "string"},
                },
                "required": [
                    "personnes_visibles", "nombre_personnes", "genres_personnes",
                    "ages_personnes", "carnations_apparentes", "apparences_ethniques",
                    "apparences_ethniques_a_verifier", "origines_ethniques_documentees",
                    "animaux_visibles", "animal_visible", "animal_confiance",
                    "categories_presence", "note",
                ],
            },
            "machine": {"type": "boolean"},
            "machine_types": {
                "type": "array",
                "items": {"type": "string", "enum": MACHINES},
            },
            "machine_role": {
                "type": "string",
                "enum": ["sujet principal", "élément de décor", "arrière-plan", ""],
            },
            "interface": {"type": "string", "enum": INTERFACES + [""]},
            "texte_visible": {"type": "boolean"},
            "texte_lisible": {"type": "boolean"},
            "generique": {"type": "boolean"},
            "texte_role": {"type": "string", "enum": TEXTE_ROLES + [""]},
            "typographie_categorie": {"type": "string", "enum": TYPOGRAPHIES_CATEGORIES + [""]},
            "typographie_styles": {
                "type": "array",
                "items": {"type": "string", "enum": TYPOGRAPHIES_STYLES},
                "maxItems": 3,
            },
            "typographie_description": {"type": "string"},
            "description": {"type": "string"},
            "mots_cles": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 4,
                "maxItems": 8,
            },
            "certitude": {"type": "string", "enum": ["sûr", "probable", "douteux"]},
            "analyse_detaillee": {
                "type": "object",
                "properties": {
                    "cadrage_optique": {
                        "type": "object",
                        "properties": {
                            "valeur_plan": {"type": "string", "enum": ECHELLES},
                            "angle_prise_vue": {"type": "string", "enum": ANGLES},
                            "profondeur_champ": {"type": "string", "enum": PROFONDEURS_CHAMP},
                            "details_profondeur_champ": {"type": "string"},
                            "composition": {
                                "type": "array",
                                "items": {"type": "string", "enum": COMPOSITIONS_CADRE},
                                "minItems": 1,
                                "maxItems": 3,
                            },
                        },
                        "required": [
                            "valeur_plan", "angle_prise_vue", "profondeur_champ",
                            "details_profondeur_champ", "composition",
                        ],
                    },
                    "direction_artistique_couleur": {
                        "type": "object",
                        "properties": {
                            "palette_colorimetrique": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 3,
                                "maxItems": 5,
                            },
                            "lumiere_etalonnage": {
                                "type": "array",
                                "items": {"type": "string", "enum": LUMIERE_ETALONNAGES},
                                "minItems": 1,
                                "maxItems": 4,
                            },
                            "direction_lumiere_principale": {
                                "type": "string",
                                "enum": DIRECTIONS_LUMIERE,
                            },
                            "matiere_texture": {
                                "type": "array",
                                "items": {"type": "string", "enum": MATIERES_TEXTURES},
                                "minItems": 1,
                                "maxItems": 4,
                            },
                        },
                        "required": [
                            "palette_colorimetrique", "lumiere_etalonnage",
                            "direction_lumiere_principale", "matiere_texture",
                        ],
                    },
                    "design_graphique_typographie": {
                        "type": "object",
                        "properties": {
                            "presence_texte": {
                                "type": "array",
                                "items": {"type": "string", "enum": PRESENCE_TEXTE_TYPES},
                                "minItems": 1,
                                "maxItems": 4,
                            },
                            "classification_typographique": {
                                "type": "array",
                                "items": {"type": "string", "enum": CLASSIFICATIONS_TYPOGRAPHIQUES},
                                "maxItems": 3,
                            },
                            "composition_graphique": {"type": "string"},
                        },
                        "required": [
                            "presence_texte", "classification_typographique",
                            "composition_graphique",
                        ],
                    },
                    "description_diegetique": {
                        "type": "object",
                        "properties": {
                            "lieu_decors": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 2,
                                "maxItems": 6,
                            },
                            "personnages_sujets": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 5,
                            },
                            "attitudes_expressions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 5,
                            },
                            "objets_cles": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                                "maxItems": 10,
                            },
                        },
                        "required": [
                            "lieu_decors", "personnages_sujets",
                            "attitudes_expressions", "objets_cles",
                        ],
                    },
                },
                "required": [
                    "cadrage_optique", "direction_artistique_couleur",
                    "design_graphique_typographie", "description_diegetique",
                ],
            },
        },
        "required": [
            "echelle", "angle", "mouvement", "lumiere", "lieu",
            "personnages", "presences", "machine", "machine_types", "description", "mots_cles",
            "certitude", "texte_visible", "texte_lisible", "generique",
            "texte_role", "typographie_categorie", "typographie_styles",
            "typographie_description", "analyse_detaillee",
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Utilitaires
# ─────────────────────────────────────────────────────────────────────────────

def slug(texte: str) -> str:
    t = unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-zA-Z0-9]+", "-", t).strip("-").lower()
    return t or "film"


def tc(secondes: float) -> str:
    h = int(secondes // 3600)
    m = int((secondes % 3600) // 60)
    s = secondes % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def verifier_outils() -> None:
    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg est introuvable. Installez-le : brew install ffmpeg")


def verifier_modele(client, modele: str) -> None:
    """S’arrête tôt si le modèle Ollama demandé n’est pas installé."""
    try:
        client.show(modele)
    except Exception as e:
        sys.exit(
            f"Modèle Ollama introuvable : {modele}\n"
            f"Installez-le avec : ollama pull {modele}\n"
            "Ou choisissez un autre modèle dans lancer.command, option 2.\n"
            f"Détail : {e}"
        )


def plan_a_contexte_film_actuel(plan: dict) -> bool:
    """Vrai si le plan a été repassé avec pitch + synopsis + résumé narratif."""
    contexte = plan.get("contexte_film_utilise") or {}
    return bool(contexte.get("pitch") and contexte.get("synopsis") and contexte.get("scenario"))


def plan_deja_analyse(plan: dict, mode: str = "", contexte_actuel: bool = False) -> bool:
    """Indique si le plan possède déjà une analyse.

    En mode complet, une ancienne analyse de triage ne suffit pas : il faut les
    champs qui alimentent les filtres comme lieu, mots-clés, lumière, etc.
    """
    analyse = plan.get("analyse") or {}
    if not analyse:
        return False
    if mode == "triage":
        fait = "machine" in analyse
        return fait and (not contexte_actuel or plan_a_contexte_film_actuel(plan))
    if mode == "complet":
        requis = ("echelle", "lieu", "description", "mots_cles")
        fait = all(analyse.get(cle) not in (None, "", []) for cle in requis)
        return fait and (not contexte_actuel or plan_a_contexte_film_actuel(plan))
    fait = bool(analyse)
    return fait and (not contexte_actuel or plan_a_contexte_film_actuel(plan))


def plans_cibles_pour_analyse(donnees: dict) -> list[dict]:
    """Retourne la liste de plans réellement à reprendre.

    En présence d’un re-découpage non destructif, la reprise doit travailler sur
    `plans_proposes` plutôt que sur l’ancien socle `plans`, sinon les nouveaux
    plans visibles côté catalogue ne sont jamais analysés.
    """
    proposition = donnees.get("redecoupage_non_destructif") or {}
    strategie = str(proposition.get("strategie") or "").strip()
    plans_proposes = proposition.get("plans_proposes") or []
    if strategie.startswith("non-destructive-add-only") and plans_proposes:
        return plans_proposes
    return donnees.get("plans") or []


def score_analyse_mode(mode: str, analyse: dict | None) -> tuple[int, int]:
    analyse = analyse or {}
    if mode == "triage":
        requis = ("machine",)
    elif mode == "complet":
        requis = ("echelle", "lieu", "description", "mots_cles")
    else:
        requis = ()
    score = sum(1 for cle in requis if analyse.get(cle) not in (None, "", []))
    return score, len(analyse)


SUFFIXE_MODELE_DESCRIPTION_RE = re.compile(
    r"\s*\((?:analyse|modèle d['’]analyse)\s*:\s*[^)]+\)\s*$",
    re.IGNORECASE,
)


REMPLACEMENTS_DESCRIPTION_PLAN_UNIQUE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[Ll]a première image affiche\b"), "Au début du plan apparaît"),
    (re.compile(r"\b[Ll]a première image montre\b"), "Au début du plan, on voit"),
    (re.compile(r"\b[Ll]e premier plan affiche\b"), "Au début du plan apparaît"),
    (re.compile(r"\b[Ll]e premier plan montre\b"), "Au début du plan, on voit"),
    (re.compile(r"\b[Ll]a seconde image affiche\b"), "puis apparaît"),
    (re.compile(r"\b[Ll]a seconde image montre\b"), "puis on voit"),
    (re.compile(r"\b[Ll]e deuxième plan affiche\b"), "puis apparaît"),
    (re.compile(r"\b[Ll]e deuxième plan montre\b"), "puis on voit"),
    (re.compile(r"\b[Ll]a troisième image affiche\b"), "enfin apparaît"),
    (re.compile(r"\b[Ll]a troisième image montre\b"), "enfin on voit"),
    (re.compile(r"\b[Ll]e troisième plan affiche\b"), "enfin apparaît"),
    (re.compile(r"\b[Ll]e troisième plan montre\b"), "enfin on voit"),
    (re.compile(r"\b[Ll]a première image\b"), "au début du plan"),
    (re.compile(r"\b[Ll]e premier plan\b"), "au début du plan"),
    (re.compile(r"\b[Ll]a seconde image\b"), "puis"),
    (re.compile(r"\b[Ll]e deuxième plan\b"), "puis"),
    (re.compile(r"\b[Ll]a troisième image\b"), "enfin"),
    (re.compile(r"\b[Ll]e troisième plan\b"), "enfin"),
]


def normaliser_description_plan_unique(description: str) -> str:
    texte = str(description or "").strip()
    if not texte:
        return texte
    base = SUFFIXE_MODELE_DESCRIPTION_RE.sub("", texte).strip()
    remplace = base
    for motif, remplacement in REMPLACEMENTS_DESCRIPTION_PLAN_UNIQUE:
        remplace = motif.sub(remplacement, remplace)
    remplace = re.sub(r"\s+,", ",", remplace)
    remplace = re.sub(r"\s{2,}", " ", remplace)
    remplace = remplace.replace(", puis apparaît", ". Puis apparaît")
    remplace = remplace.replace(", puis on voit", ". Puis on voit")
    remplace = remplace.replace(", enfin apparaît", ". Enfin apparaît")
    remplace = remplace.replace(", enfin on voit", ". Enfin on voit")
    return remplace.strip()


def description_avec_modele(description: str, modele: str) -> str:
    texte = str(description or "").strip()
    modele = str(modele or "").strip()
    if not texte or not modele:
        return texte
    base = SUFFIXE_MODELE_DESCRIPTION_RE.sub("", texte).rstrip()
    return f"{base} (analyse : {modele})"


def normaliser_analyse_modele(analyse: dict | None, modele: str) -> dict:
    analyse_normalisee = dict(analyse or {})
    modele = str(modele or "").strip()
    description_brute = normaliser_description_plan_unique(analyse_normalisee.get("description", ""))
    if description_brute:
        analyse_normalisee["description"] = description_brute
    if modele:
        description = description_avec_modele(analyse_normalisee.get("description", ""), modele)
        if description:
            analyse_normalisee["description"] = description
        analyse_normalisee["modele_analyse"] = modele
    return analyse_normalisee


def retro_annoter_descriptions_modele(donnees: dict, modele_defaut: str = "") -> bool:
    change = False
    fallback_modele = str(modele_defaut or donnees.get("modele") or "").strip()
    for plan in donnees.get("plans") or []:
        analyse = plan.get("analyse")
        if not isinstance(analyse, dict) or not analyse:
            continue
        modele_plan = str(
            plan.get("analyse_modele")
            or analyse.get("modele_analyse")
            or fallback_modele
        ).strip()
        description_initiale = normaliser_description_plan_unique(analyse.get("description", ""))
        description_finale = description_avec_modele(description_initiale, modele_plan)
        if description_finale and description_finale != description_initiale:
            analyse["description"] = description_finale
            change = True
        if modele_plan and analyse.get("modele_analyse") != modele_plan:
            analyse["modele_analyse"] = modele_plan
            change = True
        if modele_plan and plan.get("analyse_modele") != modele_plan:
            plan["analyse_modele"] = modele_plan
            change = True
    return change


EXTENSIONS = {".mkv", ".mp4", ".mov", ".avi", ".m4v", ".webm", ".mpg", ".ts"}


def rassembler(chemins: list) -> list:
    """Accepte des fichiers, des dossiers, ou un mélange des deux."""
    videos = []
    for c in chemins:
        if c.is_dir():
            videos += [f for f in sorted(c.rglob("*"))
                       if f.suffix.lower() in EXTENSIONS and not f.name.startswith(".")]
        elif c.suffix.lower() in EXTENSIONS:
            videos.append(c)
        else:
            print(f"Ignoré (extension inconnue) : {c}", file=sys.stderr)
    return videos


def inspecter(videos: list) -> None:
    """Contrôle avant lancement : codec, cadence, durée, pistes multiples."""
    if shutil.which("ffprobe") is None:
        print("ffprobe introuvable — inspection ignorée.", file=sys.stderr)
        return

    print(f"{'Fichier':<42} {'Codec':<9} {'Définition':<12} {'Cadence':<9} Durée")
    print("─" * 88)
    total = 0.0
    for v in videos:
        sortie = subprocess.run(
            ["ffprobe", "-v", "error", "-of", "json",
             "-show_entries",
             "format=duration:stream=index,codec_type,codec_name,width,height,"
             "avg_frame_rate,r_frame_rate",
             str(v)],
            capture_output=True, text=True)
        try:
            d = json.loads(sortie.stdout)
        except json.JSONDecodeError:
            print(f"{v.name[:41]:<42} ILLISIBLE")
            continue

        flux = [s for s in d.get("streams", []) if s.get("codec_type") == "video"]
        if not flux:
            print(f"{v.name[:41]:<42} aucune piste vidéo")
            continue
        s = flux[0]
        duree = float(d.get("format", {}).get("duration", 0) or 0)
        total += duree

        def fps(expr):
            try:
                a, b = expr.split("/")
                return float(a) / float(b) if float(b) else 0.0
            except Exception:
                return 0.0

        moy, ref = fps(s.get("avg_frame_rate", "0/0")), fps(s.get("r_frame_rate", "0/0"))
        variable = abs(moy - ref) > 0.05 and moy > 0
        alerte = ""
        if len(flux) > 1:
            alerte += "  ⚠ plusieurs pistes vidéo"
        if variable:
            alerte += "  ⚠ cadence variable"

        print(f"{v.name[:41]:<42} {s.get('codec_name',''):<9} "
              f"{s.get('width','?')}×{s.get('height','?'):<6} "
              f"{moy:>5.2f} i/s  {duree/60:>6.1f} min{alerte}")

    print("─" * 88)
    print(f"{len(videos)} fichiers · {total/3600:.1f} h de film\n")


# ─────────────────────────────────────────────────────────────────────────────
#  1. Détection des plans
# ─────────────────────────────────────────────────────────────────────────────

def duree_video(video: Path) -> float:
    """Durée du fichier en secondes, lue par ffprobe."""
    sortie = subprocess.run(
        ["ffprobe", "-v", "error", "-of", "json", "-show_entries",
         "format=duration", str(video)],
        capture_output=True, text=True)
    try:
        return float(json.loads(sortie.stdout).get("format", {}).get("duration") or 0)
    except (json.JSONDecodeError, ValueError, TypeError):
        return 0.0


def normaliser_bornes_plans(bornes: list[tuple[float, float]],
                            ecart_min_coupe: float = 0.08,
                            duree_min_plan: float = 0.04) -> list[tuple[float, float]]:
    """Nettoie une liste de bornes de plans et refusionne les micro-segments."""
    points = []
    for debut, fin in bornes:
        try:
            debut = round(float(debut), 3)
            fin = round(float(fin), 3)
        except (TypeError, ValueError):
            continue
        if fin <= debut:
            continue
        if not points:
            points.extend([debut, fin])
            continue
        if debut - points[-1] >= ecart_min_coupe:
            points.append(debut)
        points.append(fin)
    if len(points) < 2:
        return []
    points = sorted(set(points))
    return [
        (points[i], points[i + 1])
        for i in range(len(points) - 1)
        if points[i + 1] - points[i] >= duree_min_plan
    ]


def detecter_coupes_ffmpeg(video: Path, seuil: float,
                           debut: float = 0.0, fin: float | None = None) -> tuple[list[float], float, float]:
    """Retourne les coupes détectées sur une portion de vidéo.

    Les timecodes retournés sont absolus dans la vidéo source.
    """
    duree = duree_video(video)
    if duree <= 0:
        return [], 0.0, 0.0

    clip_debut = max(0.0, float(debut or 0.0))
    clip_fin = duree if fin is None else min(duree, float(fin))
    if clip_fin <= clip_debut:
        return [], clip_debut, clip_fin

    scene = max(0.01, min(1.0, seuil / 10.0))
    filtre = f"select='gt(scene,{scene:.3f})',showinfo"
    commande = ["ffmpeg", "-hide_banner", "-loglevel", "info"]
    if clip_debut > 0:
        commande.extend(["-ss", f"{clip_debut:.3f}"])
    if clip_fin < duree:
        commande.extend(["-t", f"{clip_fin - clip_debut:.3f}"])
    commande.extend([
        "-i", str(video),
        "-map", "0:v:0", "-vf", filtre, "-an", "-sn", "-dn", "-f", "null", "-"
    ])
    sortie = subprocess.run(commande, capture_output=True, text=True, check=False)

    coupes = []
    for ligne in sortie.stderr.splitlines():
        trouve = re.search(r"pts_time:([0-9]+(?:\.[0-9]+)?)", ligne)
        if not trouve:
            continue
        t = clip_debut + float(trouve.group(1))
        if clip_debut + 0.05 < t < clip_fin - 0.05:
            coupes.append(t)
    return coupes, clip_debut, clip_fin


def detecter_plans_ffmpeg(video: Path, seuil: float,
                          debut: float = 0.0, fin: float | None = None) -> list:
    """Retourne une liste de (debut_s, fin_s) avec le filtre scene de ffmpeg.

    Ce chemin évite de charger OpenCV et PyAV dans le même processus, ce qui
    déclenche sur macOS un conflit entre deux copies des bibliothèques FFmpeg.
    Le seuil historique du script vaut 3.0 ; ffmpeg attend une valeur entre 0
    et 1, donc on divise par 10.
    """
    coupes, clip_debut, clip_fin = detecter_coupes_ffmpeg(video, seuil, debut=debut, fin=fin)
    if clip_fin <= clip_debut:
        return []
    points = [clip_debut]
    points.extend(sorted(set(round(c, 3) for c in coupes)))
    points.append(clip_fin)
    return normaliser_bornes_plans(list(zip(points, points[1:])))


def detecter_plans_deux_passes_ffmpeg(video: Path, seuil: float,
                                      seuil_seconde_passe: float | None = None,
                                      duree_max_plan: float = 30.0,
                                      concurrence_seconde_passe: int = 1) -> tuple[list[tuple[float, float]], dict]:
    """Détection de plans en 2 passes avec re-scan plus sensible des segments longs."""
    bornes_passe1 = detecter_plans_ffmpeg(video, seuil)
    if not bornes_passe1:
        return [], {
            "strategie": "ffmpeg-deux-passes",
            "seuil_passe1": seuil,
            "seuil_passe2": seuil_seconde_passe or max(0.8, seuil * 0.6),
            "duree_max_plan": duree_max_plan,
            "concurrence_seconde_passe": max(1, int(concurrence_seconde_passe or 1)),
            "plans_passe1": 0,
            "plans_passe2": 0,
            "segments_rescannes": 0,
            "segments_affines": 0,
        }

    seuil2 = float(seuil_seconde_passe or max(0.8, seuil * 0.6))
    concurrence2 = max(1, int(concurrence_seconde_passe or 1))
    rescannes = 0
    affines = 0
    segments_a_rescanner: list[tuple[int, float, float]] = []
    for idx, (debut, fin) in enumerate(bornes_passe1):
        duree = fin - debut
        if duree > max(0.0, float(duree_max_plan or 0.0)):
            rescannes += 1
            segments_a_rescanner.append((idx, debut, fin))

    rescans_par_segment: dict[int, list[tuple[float, float]]] = {}
    if segments_a_rescanner:
        def rescanner_segment(item: tuple[int, float, float]) -> tuple[int, float, float, list[tuple[float, float]]]:
            idx, debut, fin = item
            sous_bornes = detecter_plans_ffmpeg(video, seuil2, debut=debut, fin=fin)
            return idx, debut, fin, sous_bornes

        if concurrence2 > 1 and len(segments_a_rescanner) > 1:
            max_workers = min(concurrence2, len(segments_a_rescanner))
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                for idx, debut, fin, sous_bornes in pool.map(rescanner_segment, segments_a_rescanner):
                    rescans_par_segment[idx] = sous_bornes if len(sous_bornes) > 1 else [(debut, fin)]
                    if len(sous_bornes) > 1:
                        affines += 1
        else:
            for idx, debut, fin in segments_a_rescanner:
                sous_bornes = detecter_plans_ffmpeg(video, seuil2, debut=debut, fin=fin)
                rescans_par_segment[idx] = sous_bornes if len(sous_bornes) > 1 else [(debut, fin)]
                if len(sous_bornes) > 1:
                    affines += 1

    bornes_finales: list[tuple[float, float]] = []
    for idx, (debut, fin) in enumerate(bornes_passe1):
        bornes_finales.extend(rescans_par_segment.get(idx, [(debut, fin)]))

    bornes_finales = normaliser_bornes_plans(bornes_finales)
    meta = {
        "strategie": "ffmpeg-deux-passes",
        "seuil_passe1": seuil,
        "seuil_passe2": seuil2,
        "duree_max_plan": duree_max_plan,
        "concurrence_seconde_passe": concurrence2,
        "plans_passe1": len(bornes_passe1),
        "plans_passe2": len(bornes_finales),
        "segments_rescannes": rescannes,
        "segments_affines": affines,
    }
    return bornes_finales, meta


def detecter_plans(video: Path, seuil: float, backend: str = "ffmpeg"):
    """Retourne une liste de (debut_s, fin_s)."""
    if backend == "ffmpeg":
        return detecter_plans_ffmpeg(video, seuil)

    from scenedetect import detect, AdaptiveDetector
    try:
        scenes = detect(str(video), AdaptiveDetector(adaptive_threshold=seuil),
                        backend=backend)
    except Exception as e:
        print(f"    backend {backend} indisponible ({e}) — retour à opencv",
              file=sys.stderr)
        scenes = detect(str(video), AdaptiveDetector(adaptive_threshold=seuil))
    return [(d.get_seconds(), f.get_seconds()) for d, f in scenes]


# ─────────────────────────────────────────────────────────────────────────────
#  2. Extraction des images
# ─────────────────────────────────────────────────────────────────────────────

def extraire_images(video: Path, debut: float, fin: float,
                    dossier: Path, num: int, largeur: int) -> list:
    """Extrait 3 images (25 %, 50 %, 75 % du plan). Retourne les chemins."""
    duree = max(fin - debut, 0.04)
    chemins = []
    replis = {
        0: (0.25, 0.08, 0.02, 0.0),
        1: (0.50, 0.16, 0.04, 0.0),
        2: (0.75, 0.24, 0.06, 0.0),
    }
    for i, position in enumerate((0.25, 0.5, 0.75)):
        sortie = dossier / f"{num:05d}_{i}.jpg"
        if not sortie.exists():
            for fraction in replis.get(i, (position, 0.0)):
                t = debut + duree * fraction
                if fin > debut:
                    t = min(t, max(debut, fin - 0.04))
                subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                     "-ss", f"{t:.3f}", "-i", str(video),
                     "-map", "0:v:0",          # la vraie piste vidéo, pas la jaquette
                     "-an", "-sn", "-dn",      # ni son, ni sous-titres, ni données
                     "-frames:v", "1",
                     "-vf", f"scale={largeur}:-2",
                     "-pix_fmt", "yuvj420p",
                     "-q:v", "3", str(sortie)],
                    check=False,
                )
                if sortie.exists() and sortie.stat().st_size > 0:
                    break
                sortie.unlink(missing_ok=True)
        if sortie.exists() and sortie.stat().st_size > 0:
            chemins.append(sortie)
    return chemins


def faire_vignette(source: Path, cible: Path, largeur: int = 480) -> None:
    if cible.exists():
        return
    from PIL import Image
    with Image.open(source) as im:
        ratio = largeur / im.width
        im = im.resize((largeur, max(1, round(im.height * ratio))), Image.LANCZOS)
        im.convert("RGB").save(cible, "JPEG", quality=78, optimize=True)


def faire_apercu(video: Path, debut: float, duree: float, cible: Path,
                 largeur: int = 320, images_sec: int = 8) -> bool:
    """Boucle WebP couvrant tout le plan, avec cadence adaptée aux plans longs."""
    global _APERCU_WEBP_DISPONIBLE
    if cible.exists():
        return True
    if _APERCU_WEBP_DISPONIBLE is False:
        return False
    duree = max(float(duree or 0), 0.20)
    max_images = 72
    fps = min(float(images_sec), max(2.0, max_images / duree))
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-ss", f"{debut:.3f}", "-t", f"{duree:.3f}",
         "-i", str(video), "-map", "0:v:0", "-an", "-sn", "-dn",
         "-vf", f"fps={fps:.3f},scale={largeur}:-2",
         "-loop", "0", "-compression_level", "4", "-q:v", "55",
         str(cible)], check=False, capture_output=True, text=True)
    ok = r.returncode == 0 and cible.exists()
    if ok:
        _APERCU_WEBP_DISPONIBLE = True
        return True
    cible.unlink(missing_ok=True)
    erreur = "\n".join(x for x in [r.stderr, r.stdout] if x)
    if any(motif in erreur for motif in (
        "Automatic encoder selection failed",
        "Error selecting an encoder",
        "Encoder not found",
    )):
        if _APERCU_WEBP_DISPONIBLE is not False:
            print("    ⚠ aperçu WebP désactivé : encodeur ffmpeg indisponible", file=sys.stderr)
        _APERCU_WEBP_DISPONIBLE = False
    return False


def formater_duree(secondes: float) -> str:
    secondes = max(0, int(round(secondes)))
    h, reste = divmod(secondes, 3600)
    m, s = divmod(reste, 60)
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {s:02d} s"
    return f"{s} s"


_APERCU_WEBP_DISPONIBLE: bool | None = None


def ajouter_temps_analyse(donnees: dict, secondes: float) -> None:
    """Cumule le temps réellement passé à analyser ce film."""
    if secondes <= 0:
        return
    total = float(donnees.get("temps_analyse_secondes") or 0) + secondes
    donnees["temps_analyse_secondes"] = round(total, 1)
    donnees["temps_analyse_humain"] = formater_duree(total)
    donnees["analyse_derniere_mesure"] = time.strftime("%Y-%m-%d %H:%M:%S")


def preparer_redecoupage_plans(base: Path, fid: str, fichier: Path) -> Path | None:
    """Sauvegarde l'ancien plans.json et archive les artefacts dépendants du découpage."""
    sauvegarde = None
    horodatage = time.strftime("%Y%m%d_%H%M%S")
    dossier_sauvegarde = SCRIPT_ROOT / "archives" / "redecoupage-plans" / horodatage
    dossier_sauvegarde.mkdir(parents=True, exist_ok=True)
    if fichier.exists():
        sauvegarde = dossier_sauvegarde / f"{fid}-plans.json"
        shutil.copy2(fichier, sauvegarde)
    for nom in ("frames", "vignettes"):
        dossier = base / nom
        if not dossier.exists():
            continue
        archive = dossier_sauvegarde / f"{fid}-{nom}"
        if archive.exists():
            shutil.rmtree(archive)
        shutil.move(str(dossier), str(archive))
    return sauvegarde


def palette_dominante(source: Path, n: int = 4) -> list:
    """Couleurs dominantes en hexadécimal, calculées sans le modèle."""
    from PIL import Image
    with Image.open(source) as im:
        im = im.convert("RGB").resize((100, 100))
        reduit = im.quantize(colors=n, method=Image.MEDIANCUT).convert("RGB")
        couleurs = sorted(reduit.getcolors(10000) or [], reverse=True)[:n]
    return ["#%02x%02x%02x" % rgb for _, rgb in couleurs]


# ─────────────────────────────────────────────────────────────────────────────
#  3. Description par le modèle
# ─────────────────────────────────────────────────────────────────────────────

def interroger_historique(client, modele: str, prompt: str, images: list,
                          schema: dict, essais: int = 2) -> dict:
    """Ancien chemin Ollama direct, conservé comme référence de comparaison."""
    encodees = [base64.b64encode(p.read_bytes()).decode() for p in images]
    for tentative in range(essais):
        try:
            kwargs = dict(
                model=modele,
                format=schema,
                options={"temperature": 0.2, "num_ctx": 8192},
                messages=[{"role": "user", "content": prompt,
                           "images": encodees}],
            )
            try:
                # Gemma 4 raisonne par défaut ; on coupe pour le débit.
                reponse = client.chat(think=False, **kwargs)
            except TypeError:
                reponse = client.chat(**kwargs)
            brut = reponse["message"]["content"]
            return json.loads(brut)
        except json.JSONDecodeError:
            if tentative == essais - 1:
                return {}
        except Exception as e:
            print(f"    ⚠ {e}", file=sys.stderr)
            time.sleep(2)
    return {}


# ─────────────────────────────────────────────────────────────────────────────
#  Traitement d'un film
# ─────────────────────────────────────────────────────────────────────────────

def charger_plans_existants_ou_vide(fichier: Path) -> dict:
    """Charge un plans.json généré, ou le traite comme absent s’il est corrompu.

    Une interruption brutale ou un disque plein peut laisser un JSON vide. Dans ce
    cas on le sauvegarde non destructivement puis on laisse le pipeline le recréer.
    """
    if not fichier.exists():
        return {}
    try:
        texte = fichier.read_text("utf-8")
        if not texte.strip():
            raise json.JSONDecodeError("empty generated JSON", texte, 0)
        donnees = json.loads(texte)
        return donnees if isinstance(donnees, dict) else {}
    except json.JSONDecodeError as exc:
        suffixe = "empty" if fichier.stat().st_size == 0 else "invalid"
        sauvegarde = fichier.with_name(f"{fichier.name}.{suffixe}-{time.strftime('%Y%m%d-%H%M%S')}.bak")
        fichier.rename(sauvegarde)
        print(f"  ⚠ {fichier.name} {suffixe} ignoré et sauvegardé dans {sauvegarde.name} ({exc})")
        return {}

def traiter(video: Path, racine: Path, args, client) -> dict:
    fid = slug(video.stem)
    base = racine / fid
    fichier = base / "plans.json"
    sauvegarde_redecoupage = None
    if getattr(args, "redecouper_plans", False):
        sauvegarde_redecoupage = preparer_redecoupage_plans(base, fid, fichier)
    frames = base / "frames"
    vignettes = base / "vignettes"
    for d in (frames, vignettes):
        d.mkdir(parents=True, exist_ok=True)

    fiche = charger_fiche(base, video, args.catalogue)
    fiche = completer_contexte_film(base, video, fiche, actif=not getattr(args, "sans_contexte_web", False))

    donnees = {} if getattr(args, "redecouper_plans", False) else charger_plans_existants_ou_vide(fichier)
    donnees["film"] = video.stem
    donnees["id"] = fid
    donnees["source"] = str(video)
    donnees["modele"] = args.modele
    donnees["fiche"] = fiche

    if "plans" not in donnees:
        if getattr(args, "redecouper_plans", False):
            if sauvegarde_redecoupage:
                print(f"  Re-découpage forcé : ancien plans.json sauvegardé dans {sauvegarde_redecoupage}")
            else:
                print("  Re-découpage forcé : aucun ancien plans.json à sauvegarder")
        print(f"  Détection des plans…")
        detection_meta = {
            "strategie": f"{args.backend}-une-passe",
            "seuil_passe1": args.seuil,
        }
        if args.backend == "ffmpeg" and not getattr(args, "sans_deux_passes", False):
            bornes, detection_meta = detecter_plans_deux_passes_ffmpeg(
                video,
                args.seuil,
                seuil_seconde_passe=args.seuil_seconde_passe,
                duree_max_plan=args.duree_max_plan,
                concurrence_seconde_passe=args.concurrence_seconde_passe,
            )
        else:
            bornes = detecter_plans(video, args.seuil, args.backend)
            detection_meta["plans_passe1"] = len(bornes)
            detection_meta["plans_passe2"] = len(bornes)
        donnees = {
            "film": video.stem,
            "id": fid,
            "source": str(video),
            "modele": args.modele,
            "fiche": fiche,
            "detection_plans": detection_meta,
            "plans": [
                {"n": i + 1,
                 "debut": round(d, 3), "fin": round(f, 3),
                 "duree": round(f - d, 3),
                 "tc": tc(d)}
                for i, (d, f) in enumerate(bornes)
            ],
        }
        fichier.write_text(json.dumps(donnees, ensure_ascii=False, indent=1), "utf-8")
        print(f"  {len(donnees['plans'])} plans détectés")

    schema = schema_triage() if args.mode == "triage" else schema_complet()
    plans = plans_cibles_pour_analyse(donnees)
    total = len(plans)
    debut_chrono = time.time()
    temps_session = 0.0

    if retro_annoter_descriptions_modele(donnees, args.modele):
        fichier.write_text(json.dumps(donnees, ensure_ascii=False, indent=1), "utf-8")

    if plans and not args.refaire and all(plan_deja_analyse(plan, args.mode, args.reprendre_contexte_actuel) for plan in plans):
        print(f"  déjà analysé : {total} plans — ignoré")
        return donnees

    if args.limite:
        plans = plans[:args.limite]
        total = len(plans)
        print(f"  calibrage : {total} premiers plans seulement")

    a_faire = [
        (idx, plan) for idx, plan in enumerate(plans)
        if args.refaire or not plan_deja_analyse(plan, args.mode, args.reprendre_contexte_actuel)
    ]
    if not a_faire:
        print("  aucun plan restant à analyser")
        return donnees

    moteur = getattr(args, "moteur", "ollama")
    taille_paquet = max(1, int(getattr(args, "concurrence", 1) or 1)) if moteur == "mlx" else 1

    def preparer_plan(item: tuple[int, dict]) -> dict | None:
        idx, plan = item
        images = extraire_images(video, plan["debut"], plan["fin"],
                                 frames, plan["n"], args.largeur)
        if not images:
            return None

        milieu = images[len(images) // 2]
        plan["vignettes"] = []
        for k, image in enumerate(images):
            v = vignettes / f"{plan['n']:05d}_{k}.jpg"
            faire_vignette(image, v)
            plan["vignettes"].append(f"{fid}/vignettes/{v.name}")
        plan["vignette"] = plan["vignettes"][len(plan["vignettes"]) // 2]
        plan["couleurs"] = palette_dominante(milieu)

        if args.apercu:
            a = vignettes / f"{plan['n']:05d}.webp"
            if faire_apercu(video, plan["debut"], plan["duree"], a):
                plan["apercu"] = f"{fid}/vignettes/{a.name}"

        scene = scene_du_plan(donnees, plan)
        prompt = (
            prompt_triage(fiche, scene, args.contexte_libre, args.criteres_libre)
            if args.mode == "triage"
            else prompt_complet(fiche, scene, args.contexte_libre, args.criteres_libre)
        )
        return {"idx": idx, "plan": plan, "scene": scene, "prompt": prompt, "images": images}

    for depart_paquet in range(0, len(a_faire), taille_paquet):
        paquet = a_faire[depart_paquet:depart_paquet + taille_paquet]
        debut_paquet = time.time()

        if len(paquet) > 1:
            with ThreadPoolExecutor(max_workers=min(4, len(paquet))) as pool:
                prepares = [p for p in pool.map(preparer_plan, paquet) if p]
        else:
            prepare = preparer_plan(paquet[0])
            prepares = [prepare] if prepare else []
        if not prepares:
            continue

        taches = [
            {"prompt": p["prompt"], "images": p["images"], "schema": schema, "tenant": fid}
            for p in prepares
        ]
        if len(taches) > 1 and hasattr(client, "decrire_lot"):
            analyses = client.decrire_lot(taches, concurrence=taille_paquet)
        else:
            analyses = [
                interroger(client, args.modele, t["prompt"], t["images"], t["schema"], tenant=fid)
                for t in taches
            ]

        duree_paquet = time.time() - debut_paquet
        duree_moyenne = duree_paquet / max(1, len(prepares))
        for prepare, analyse in zip(prepares, analyses):
            plan = prepare["plan"]
            analyse_normalisee = normaliser_analyse_modele(analyse, args.modele)
            meilleur_score = score_analyse_mode(args.mode, analyse_normalisee)
            if not plan_deja_analyse({"analyse": analyse_normalisee}, args.mode):
                for tentative in range(2):
                    relance = interroger(
                        client, args.modele,
                        prepare["prompt"], prepare["images"], schema,
                        tenant=fid,
                    )
                    candidate = normaliser_analyse_modele(relance, args.modele)
                    score_candidat = score_analyse_mode(args.mode, candidate)
                    if score_candidat > meilleur_score:
                        analyse_normalisee = candidate
                        meilleur_score = score_candidat
                    if plan_deja_analyse({"analyse": analyse_normalisee}, args.mode):
                        break
            plan["analyse"] = analyse_normalisee
            plan["analyse_modele"] = str(args.modele or "")
            plan["contexte_film_utilise"] = contexte_film_meta(fiche)
            plan["contexte_scene_utilise"] = contexte_scene_meta(prepare["scene"])
            plan["temps_analyse_secondes"] = round(duree_moyenne, 1)
            plan["analyse_mesuree_le"] = time.strftime("%Y-%m-%d %H:%M:%S")
            if args.leger:
                for p in prepare["images"]:
                    p.unlink(missing_ok=True)

        temps_session += duree_paquet
        ajouter_temps_analyse(donnees, duree_paquet)
        fichier.write_text(json.dumps(donnees, ensure_ascii=False, indent=1), "utf-8")

        faits_session = min(depart_paquet + len(paquet), len(a_faire))
        ecoule = time.time() - debut_chrono
        reste = ecoule / max(faits_session, 1) * (len(a_faire) - faits_session)
        dernier_plan = prepares[-1]["plan"]
        drapeau = "▣" if any((a or {}).get("machine") for a in analyses) else " "
        lot = f"lot {len(prepares)}" if len(prepares) > 1 else "plan"
        print(f"  [{faits_session}/{len(a_faire)} à faire · {dernier_plan['tc']}] {drapeau} {lot} "
              f"— analyse {formater_duree(donnees.get('temps_analyse_secondes') or temps_session)} "
              f"— reste ≈ {reste/60:.0f} min", end="\r", flush=True)

    print()
    return donnees


# ─────────────────────────────────────────────────────────────────────────────
#  Index global consommé par le site
# ─────────────────────────────────────────────────────────────────────────────

def audio_global_pour_index(donnees: dict) -> dict:
    """Résume la couche audio globale sans embarquer les empreintes lourdes."""
    bloc = donnees.get("audio_global") or {}
    sequences = []
    familles, sous_genres, design = set(), set(), set()
    for sequence in bloc.get("sequences") or []:
        analyse = sequence.get("analyse") or {}
        familles.update(x for x in analyse.get("musique_familles") or [] if x)
        sous_genres.update(x for x in analyse.get("musique_sous_genres") or [] if x)
        design.update(x for x in analyse.get("design_sonore_types") or [] if x)
        sequences.append({
            "id": sequence.get("id", ""),
            "mode_sequence": sequence.get("mode_sequence", ""),
            "plans": sequence.get("plans") or [],
            "debut": sequence.get("debut"),
            "fin": sequence.get("fin"),
            "tc_debut": sequence.get("tc_debut", ""),
            "tc_fin": sequence.get("tc_fin", ""),
            "duree": sequence.get("duree"),
            "musique_presente": bool(analyse.get("musique_presente")),
            "parole_chantee": bool(analyse.get("parole_chantee")),
            "musique_familles": analyse.get("musique_familles") or [],
            "musique_sous_genres": analyse.get("musique_sous_genres") or [],
            "design_sonore_types": analyse.get("design_sonore_types") or [],
            "intensite_sonore": analyse.get("intensite_sonore", ""),
            "musique_titre": analyse.get("titre_morceau", ""),
            "musique_artiste": analyse.get("artiste_morceau", ""),
            "musique_auteur": analyse.get("auteur_morceau", ""),
            "musique_methode": analyse.get("identification_methode", ""),
            "musique_confiance": analyse.get("identification_confiance", ""),
            "musique_a_verifier": bool(analyse.get("a_verifier")),
            "audio_notes": analyse.get("notes_audio", ""),
        })
    return {
        "mode": bloc.get("mode", ""),
        "modele": bloc.get("modele", ""),
        "genere": bloc.get("genere", ""),
        "empreinte_mode": bloc.get("empreinte_mode", ""),
        "sequences": sequences,
        "sequences_count": len(sequences),
        "sequences_musicales_count": sum(1 for s in sequences if s["musique_presente"]),
        "musique_familles": sorted(familles, key=str.lower),
        "musique_sous_genres": sorted(sous_genres, key=str.lower),
        "design_sonore_types": sorted(design, key=str.lower),
    }


def texte_musiques_connues(fiche: dict) -> str:
    """Version compacte et affichable des musiques/cues documentés publiquement."""
    lignes = []
    for item in fiche.get("musiques_connues") or []:
        if not isinstance(item, dict):
            continue
        titre = item.get("titre") or ""
        auteur = item.get("auteur") or item.get("compositeur") or item.get("artiste") or ""
        usage = item.get("usage") or item.get("type") or ""
        confiance = item.get("confiance") or ""
        ligne = " — ".join(x for x in [titre, auteur] if x)
        suffixe = " · ".join(x for x in [usage, confiance] if x)
        if ligne and suffixe:
            ligne += f" ({suffixe})"
        if ligne:
            lignes.append(ligne)
    return " ; ".join(lignes)


def total_plans_visible_film(donnees: dict) -> int:
    """Nombre de plans à afficher côté catalogue.

    Si une proposition de re-découpage non destructif existe, on affiche ce total
    proposé pour refléter le compteur visuel attendu par film, sans écraser les
    anciens plans annotés.
    """
    plans = donnees.get("plans") or []
    proposition = donnees.get("redecoupage_non_destructif") or {}
    strategie = str(proposition.get("strategie") or "").strip()
    total_propose = proposition.get("plans_proposes_total")
    if strategie.startswith("non-destructive-add-only") and isinstance(total_propose, int) and total_propose > 0:
        return total_propose
    plans_proposes = proposition.get("plans_proposes") or []
    if strategie.startswith("non-destructive-add-only") and plans_proposes:
        return len(plans_proposes)
    return len(plans)


def plans_visibles_film(donnees: dict) -> list:
    """Liste de plans à exposer dans le catalogue web.

    Quand un re-découpage non destructif existe, le banc de plans doit montrer
    ces plans proposés, pas seulement l'ancien socle annoté.
    """
    plans = donnees.get("plans") or []
    proposition = donnees.get("redecoupage_non_destructif") or {}
    strategie = str(proposition.get("strategie") or "").strip()
    plans_proposes = proposition.get("plans_proposes") or []
    if strategie.startswith("non-destructive-add-only") and plans_proposes:
        return plans_proposes
    return plans

def construire_index(racine: Path) -> None:
    films, plans = [], []
    films_vus = set()
    for fichier in sorted(racine.glob("*/plans.json")):
        d = json.loads(fichier.read_text("utf-8"))
        f = d.get("fiche") or {}
        fiche_json = fichier.with_name("fiche.json")
        if fiche_json.exists():
            f = {**f, **json.loads(fiche_json.read_text("utf-8"))}
        films_vus.add(fichier.parent.name)
        titre = f.get("titre") or d["film"]
        annee = f.get("annee")

        credits_groupes = credits_par_fonction(f)
        audio_global_index = audio_global_pour_index(d)

        personnages_recurrents_film = d.get("personnages_recurrents") or []
        plans_visibles = total_plans_visible_film(d)
        films.append({**FICHE_MODELE, **f,
                      "id": d["id"], "titre": titre,
                      "decennie": decennie(annee),
                      "credits_par_fonction": credits_groupes,
                      "scenes": len(d.get("scenes") or []),
                      "musiques_connues_texte": texte_musiques_connues(f),
                      "audio_global": audio_global_index,
                      "audio_sequences_count": audio_global_index["sequences_count"],
                      "audio_sequences_musicales_count": audio_global_index["sequences_musicales_count"],
                      "audio_design_sonore_types": audio_global_index["design_sonore_types"],
                      "personnages_recurrents": personnages_recurrents_film,
                      "personnages_recurrents_count": len(personnages_recurrents_film),
                      "plans": plans_visibles,
                      "plans_sources": len(d.get("plans") or []),
                      "plans_proposes_non_destructif": ((d.get("redecoupage_non_destructif") or {}).get("plans_proposes_total") or len((d.get("redecoupage_non_destructif") or {}).get("plans_proposes") or []))})

        credits = []
        for cle in ("realisateur", "directeur_photo", "scenaristes", "acteurs",
                    "producteurs", "chef_decorateur", "monteur", "musique",
                    "costumes", "effets_speciaux"):
            valeur = f.get(cle)
            if isinstance(valeur, list):
                credits.extend(x for x in valeur if x)
            elif valeur:
                credits.append(valeur)

        plans_film = []

        def ajouter_plan_index(cible: list, p: dict, detail_complet: bool = False) -> None:
            a = p.get("analyse") or {}
            detail = a.get("analyse_detaillee") or {}
            cadrage = detail.get("cadrage_optique") or {}
            direction = detail.get("direction_artistique_couleur") or {}
            design = detail.get("design_graphique_typographie") or {}
            diegetique = detail.get("description_diegetique") or {}
            personnes_reconnues = p.get("personnes_reconnues") or []
            presences = p.get("presences") or a.get("presences") or {}
            personnages_recurrents = p.get("personnages_recurrents") or []
            audio = analyser_couche_sonore(p)
            scene = scene_du_plan(d, p)
            entree = {
                "film": d["id"], "titre": titre,
                "annee": annee, "decennie": decennie(annee),
                "realisateur": f.get("realisateur", ""),
                "directeur_photo": f.get("directeur_photo", ""),
                "scenaristes": f.get("scenaristes", []),
                "acteurs": f.get("acteurs", []),
                "credits": sorted(set(credits), key=str.lower),
                "credits_par_fonction": credits_groupes,
                "synopsis": f.get("synopsis", ""),
                "pitch": f.get("pitch", ""),
                "scenario": f.get("scenario", ""),
                "contexte_film": " ".join(x for x in [
                    f.get("pitch", ""), f.get("synopsis", ""), f.get("scenario", "")
                ] if x),
                "personnes_reconnues": personnes_reconnues,
                "personnes_visibles": [x.get("nom") for x in personnes_reconnues
                                        if isinstance(x, dict) and x.get("nom")],
                "personnages_recurrents": personnages_recurrents,
                "personnages_recurrents_ids": [x.get("personnage_id") for x in personnages_recurrents
                                                if isinstance(x, dict) and x.get("personnage_id")],
                "personnages_recurrents_labels": [
                    " · ".join(x for x in [item.get("label"), item.get("profil_resume")] if x)
                    for item in personnages_recurrents if isinstance(item, dict)
                ],
                "presence_personnes": bool(presences.get("personnes_visibles")),
                "nombre_personnes": presences.get("nombre_personnes", a.get("personnages", 0)),
                "genres_personnes": presences.get("genres_personnes", []),
                "ages_personnes": presences.get("ages_personnes", []),
                "carnations_apparentes": presences.get("carnations_apparentes", []),
                "apparences_ethniques": presences.get("apparences_ethniques", []),
                "apparences_ethniques_a_verifier": bool(presences.get("apparences_ethniques_a_verifier")),
                "origines_ethniques_documentees": presences.get("origines_ethniques_documentees", []),
                "animal_visible": bool(presences.get("animal_visible")),
                "animaux_visibles": presences.get("animaux_visibles", []),
                "animal_confiance": presences.get("animal_confiance", ""),
                "categories_presence": presences.get("categories_presence", []),
                "presences_note": presences.get("note", ""),
                "n": p["n"], "tc": p["tc"],
                "debut": p["debut"], "fin": p["fin"], "duree": p["duree"],
                "scene_id": p.get("scene_id") or scene.get("scene_id", ""),
                "scene_numero": p.get("scene_numero") or scene.get("numero_scene"),
                "scene_type": p.get("scene_type") or scene.get("type_scene", ""),
                "scene_titre": p.get("scene_titre") or scene.get("titre", ""),
                "scene_resume": p.get("scene_resume") or scene.get("resume_scene", ""),
                "scene_lieu": p.get("scene_lieu") or scene.get("lieu", ""),
                "scene_temporalite": p.get("scene_temporalite") or scene.get("temporalite", ""),
                "scene_action_principale": p.get("scene_action_principale") or scene.get("action_principale", ""),
                "scene_ambiance": p.get("scene_ambiance") or scene.get("ambiance", ""),
                "scene_personnages_visibles": p.get("scene_personnages_visibles") or scene.get("personnages_visibles", []),
                "scene_objets_significatifs": p.get("scene_objets_significatifs") or scene.get("objets_significatifs", []),
                "scene_motifs_structurants": p.get("scene_motifs_structurants") or scene.get("motifs_structurants", []),
                "scene_enjeu_narratif": p.get("scene_enjeu_narratif") or scene.get("enjeu_narratif", ""),
                "scene_contexte": p.get("scene_contexte") or scene.get("contexte_pour_plans", ""),
                "scene_confiance": p.get("scene_confiance") or scene.get("confiance", ""),
                "scene_a_verifier": bool(p.get("scene_a_verifier") or scene.get("a_verifier")),
                "vignette": p.get("vignette", ""),
                "vignettes": p.get("vignettes", []),
                "apercu": p.get("apercu", ""),
                "couleurs": p.get("couleurs", []),
                "machine": bool(a.get("machine")),
                "machine_types": a.get("machine_types") or a.get("types") or [],
                "machine_role": a.get("machine_role", ""),
                "interface": a.get("interface", ""),
                "texte_visible": bool(a.get("texte_visible")),
                "texte_lisible": bool(a.get("texte_lisible")),
                "generique": bool(a.get("generique")),
                "texte_role": a.get("texte_role", ""),
                "typographie_categorie": a.get("typographie_categorie", ""),
                "typographie_styles": a.get("typographie_styles", []),
                "typographie_description": a.get("typographie_description", ""),
                "certitude": a.get("certitude", ""),
                "echelle": a.get("echelle", ""),
                "angle": a.get("angle", ""),
                "profondeur_champ": cadrage.get("profondeur_champ", ""),
                "details_profondeur_champ": cadrage.get("details_profondeur_champ", ""),
                "composition_cadre": cadrage.get("composition", []),
                "mouvement": a.get("mouvement", ""),
                "mouvement_camera": p.get("mouvement_camera", ""),
                "mouvement_direction": p.get("mouvement_direction", ""),
                "mouvement_intensite": p.get("mouvement_intensite", ""),
                "mouvement_confiance": p.get("mouvement_confiance", ""),
                "mouvement_mesures": p.get("mouvement_mesures", {}),
                "mouvement_mecanique_timeline": p.get("mouvement_mecanique_timeline", []),
                "mouvement_video_modele": p.get("mouvement_video_modele", ""),
                "mouvement_video_camera": p.get("mouvement_video_camera", ""),
                "mouvement_video_labels": p.get("mouvement_video_labels", []),
                "mouvement_video_scores": p.get("mouvement_video_scores", {}),
                "mouvement_video_segments": p.get("mouvement_video_segments", []),
                "mouvement_video_confiance": p.get("mouvement_video_confiance", ""),
                "mouvement_camera_final": p.get("mouvement_camera_final") or p.get("mouvement_video_camera") or p.get("mouvement_camera", ""),
                "mouvement_camera_sources": p.get("mouvement_camera_sources", []),
                "mouvement_camera_conflit": bool(p.get("mouvement_camera_conflit")),
                "mouvement_camera_notes": p.get("mouvement_camera_notes", ""),
                "lumiere": a.get("lumiere", ""),
                "palette_colorimetrique": direction.get("palette_colorimetrique", []),
                "lumiere_etalonnage": direction.get("lumiere_etalonnage", []),
                "direction_lumiere_principale": direction.get("direction_lumiere_principale", ""),
                "matiere_texture": direction.get("matiere_texture", []),
                "lieu": a.get("lieu", ""),
                "lieu_decors": diegetique.get("lieu_decors", []),
                "analyse_modele": p.get("analyse_modele") or a.get("modele_analyse") or d.get("modele", ""),
                "description": description_avec_modele(
                    a.get("description") or a.get("note", ""),
                    p.get("analyse_modele") or a.get("modele_analyse") or d.get("modele", ""),
                ),
                "mots_cles": a.get("mots_cles", []),
                "personnages_sujets": diegetique.get("personnages_sujets", []),
                "attitudes_expressions": diegetique.get("attitudes_expressions", []),
                "objets_cles": diegetique.get("objets_cles", []),
                "presence_texte_types": design.get("presence_texte", []),
                "classification_typographique": design.get("classification_typographique", []),
                "composition_graphique": design.get("composition_graphique", ""),
                "dialogue": bool(p.get("dialogue")),
                "dialogues": p.get("dialogues", []),
                "dialogue_texte": p.get("dialogue_texte", ""),
                "dialogue_source": p.get("dialogue_source", ""),
                "audio_source": audio.get("audio_source", "aucune source"),
                "dialogue_types": audio.get("dialogue_types", []),
                "musique_types": audio.get("musique_types", []),
                "ambiance_types": audio.get("ambiance_types", []),
                "musique_presente": bool(p.get("musique_presente")),
                "parole_chantee": bool(p.get("parole_chantee")),
                "musique_familles": p.get("musique_familles", []),
                "musique_sous_genres": p.get("musique_sous_genres", []),
                "design_sonore_types": p.get("design_sonore_types", []),
                "intensite_sonore": p.get("intensite_sonore", ""),
                "musique_titre": p.get("musique_titre", ""),
                "musique_artiste": p.get("musique_artiste", ""),
                "musique_auteur": p.get("musique_auteur", ""),
                "musique_methode": p.get("musique_methode", ""),
                "musique_confiance": p.get("musique_confiance", ""),
                "musique_a_verifier": bool(p.get("musique_a_verifier")),
                "audio_global_ref": p.get("audio_global_ref", ""),
                "audio_global_debut": p.get("audio_global_debut"),
                "audio_global_fin": p.get("audio_global_fin"),
                "audio_global_plans": p.get("audio_global_plans", []),
                "audio_global_mode": p.get("audio_global_mode", ""),
                "audio_notes": p.get("audio_notes", ""),
                "audio_detaille": p.get("audio_detaille", {}),
                "affinage": p.get("affinage", {}),
                "affinage_modele": (p.get("affinage") or {}).get("modele", ""),
                "affinage_raisons": (p.get("affinage") or {}).get("raisons", []),
                "typographie": p.get("typographie", {}),
                "typographie_modele": (p.get("typographie") or {}).get("modele", ""),
                "analyse_detaillee": detail,
                "contexte_film_utilise": p.get("contexte_film_utilise", {}),
                "contexte_scene_utilise": p.get("contexte_scene_utilise", {}),
            }
            if detail_complet:
                cible.append(entree)
                return
            cible.append({
                "film": entree["film"],
                "personnages_recurrents_labels": entree["personnages_recurrents_labels"],
                "presence_personnes": entree["presence_personnes"],
                "nombre_personnes": entree["nombre_personnes"],
                "genres_personnes": entree["genres_personnes"],
                "ages_personnes": entree["ages_personnes"],
                "animal_visible": entree["animal_visible"],
                "animaux_visibles": entree["animaux_visibles"],
                "categories_presence": entree["categories_presence"],
                "n": entree["n"],
                "tc": entree["tc"],
                "debut": entree["debut"],
                "fin": entree["fin"],
                "duree": entree["duree"],
                "scene_id": entree["scene_id"],
                "scene_numero": entree["scene_numero"],
                "scene_type": entree["scene_type"],
                "scene_titre": entree["scene_titre"],
                "scene_lieu": entree["scene_lieu"],
                "scene_temporalite": entree["scene_temporalite"],
                "scene_personnages_visibles": entree["scene_personnages_visibles"],
                "scene_confiance": entree["scene_confiance"],
                "scene_a_verifier": entree["scene_a_verifier"],
                "vignette": entree["vignette"],
                "vignettes": entree["vignettes"],
                "couleurs": entree["couleurs"],
                "machine": entree["machine"],
                "machine_types": entree["machine_types"],
                "machine_role": entree["machine_role"],
                "interface": entree["interface"],
                "texte_visible": entree["texte_visible"],
                "generique": entree["generique"],
                "texte_role": entree["texte_role"],
                "typographie_categorie": entree["typographie_categorie"],
                "typographie_styles": entree["typographie_styles"],
                "classification_typographique": entree["classification_typographique"],
                "composition_graphique": entree["composition_graphique"],
                "certitude": entree["certitude"],
                "echelle": entree["echelle"],
                "angle": entree["angle"],
                "profondeur_champ": entree["profondeur_champ"],
                "composition_cadre": entree["composition_cadre"],
                "mouvement": entree["mouvement"],
                "mouvement_camera": entree["mouvement_camera"],
                "mouvement_direction": entree["mouvement_direction"],
                "mouvement_intensite": entree["mouvement_intensite"],
                "mouvement_confiance": entree["mouvement_confiance"],
                "mouvement_video_modele": entree["mouvement_video_modele"],
                "mouvement_video_camera": entree["mouvement_video_camera"],
                "mouvement_video_confiance": entree["mouvement_video_confiance"],
                "mouvement_camera_final": entree["mouvement_camera_final"],
                "mouvement_camera_sources": entree["mouvement_camera_sources"],
                "mouvement_camera_conflit": entree["mouvement_camera_conflit"],
                "mouvement_camera_notes": entree["mouvement_camera_notes"],
                "lumiere": entree["lumiere"],
                "palette_colorimetrique": entree["palette_colorimetrique"],
                "lumiere_etalonnage": entree["lumiere_etalonnage"],
                "direction_lumiere_principale": entree["direction_lumiere_principale"],
                "matiere_texture": entree["matiere_texture"],
                "lieu": entree["lieu"],
                "dialogue": entree["dialogue"],
                "audio_source": entree["audio_source"],
                "dialogue_types": entree["dialogue_types"],
                "musique_types": entree["musique_types"],
                "musique_familles": entree["musique_familles"],
                "musique_sous_genres": entree["musique_sous_genres"],
                "design_sonore_types": entree["design_sonore_types"],
                "ambiance_types": entree["ambiance_types"],
                "musique_titre": entree["musique_titre"],
                "musique_artiste": entree["musique_artiste"],
                "musique_auteur": entree["musique_auteur"],
                "musique_methode": entree["musique_methode"],
                "musique_confiance": entree["musique_confiance"],
                "musique_a_verifier": entree["musique_a_verifier"],
                "audio_global_ref": entree["audio_global_ref"],
                "audio_global_debut": entree["audio_global_debut"],
                "audio_global_fin": entree["audio_global_fin"],
                "audio_global_plans": entree["audio_global_plans"],
                "audio_global_mode": entree["audio_global_mode"],
                "affinage_modele": entree["affinage_modele"],
                "affinage_raisons": entree["affinage_raisons"],
                "typographie_modele": entree["typographie_modele"],
            })

        for p in d.get("plans") or []:
            ajouter_plan_index(plans, p, detail_complet=False)
        for p in plans_visibles_film(d):
            ajouter_plan_index(plans_film, p, detail_complet=True)

        (fichier.parent / "index-plans.json").write_text(
            json.dumps({
                "genere": time.strftime("%Y-%m-%d %H:%M"),
                "film": d["id"],
                "plans": plans_film,
            }, ensure_ascii=False),
            "utf-8",
        )

    for fiche_json in sorted(racine.glob("*/fiche.json")):
        film_id = fiche_json.parent.name
        if film_id in films_vus:
            continue
        f = {**FICHE_MODELE, **json.loads(fiche_json.read_text("utf-8"))}
        titre = f.get("titre") or film_id
        annee = f.get("annee")
        credits_groupes = credits_par_fonction(f)
        films.append({**FICHE_MODELE, **f,
                      "id": film_id,
                      "titre": titre,
                      "decennie": decennie(annee),
                      "credits_par_fonction": credits_groupes,
                      "scenes": 0,
                      "musiques_connues_texte": texte_musiques_connues(f),
                      "audio_global": {
                          "sequences": [],
                          "sequences_count": 0,
                          "sequences_musicales_count": 0,
                          "design_sonore_types": [],
                      },
                      "audio_sequences_count": 0,
                      "audio_sequences_musicales_count": 0,
                      "audio_design_sonore_types": [],
                      "personnages_recurrents": [],
                      "personnages_recurrents_count": 0,
                      "plans": 0})

    index = {
        "genere": time.strftime("%Y-%m-%d %H:%M"),
        "films": films,
        "vocabulaire": {"echelles": ECHELLES, "angles": ANGLES,
                        "mouvements": MOUVEMENTS, "lumieres": LUMIERES,
                        "profondeurs_champ": PROFONDEURS_CHAMP,
                        "compositions_cadre": COMPOSITIONS_CADRE,
                        "machines": MACHINES, "interfaces": INTERFACES,
                        "texte_roles": TEXTE_ROLES,
                        "typographies_categories": TYPOGRAPHIES_CATEGORIES,
                        "typographies_styles": TYPOGRAPHIES_STYLES,
                        "presence_texte_types": PRESENCE_TEXTE_TYPES,
                        "classifications_typographiques": CLASSIFICATIONS_TYPOGRAPHIQUES,
                        "lumiere_etalonnages": LUMIERE_ETALONNAGES,
                        "directions_lumiere": DIRECTIONS_LUMIERE,
                        "matieres_textures": MATIERES_TEXTURES,
                        "genres_personnes": GENRES_PERSONNES,
                        "ages_personnes": AGES_PERSONNES,
                        "carnations_apparentes": CARNATIONS_APPARENTES,
                        "apparences_ethniques": APPARENCES_ETHNIQUES,
                        "animaux_visibles": ANIMAUX_VISIBLES,
                        "categories_presence": CATEGORIES_PRESENCE,
                        "mouvements_camera": [
                            "fixe", "zoom in", "zoom out", "panoramique gauche",
                            "panoramique droite", "tilt haut", "tilt bas",
                            "rotation horaire", "rotation antihoraire",
                            "caméra portée", "mouvement complexe", "indéterminé",
                        ],
                        "mouvements_camera_video": [
                            "fixe", "travelling avant", "travelling arrière",
                            "travelling gauche", "travelling droite",
                            "travelling de suivi", "travelling de suivi latéral",
                            "travelling de suivi frontal", "travelling de suivi aérien",
                            "panoramique gauche", "panoramique droite",
                            "tilt haut", "tilt bas", "pedestal haut", "pedestal bas",
                            "zoom in", "zoom out", "rotation horaire",
                            "rotation antihoraire", "arc gauche", "arc droite",
                            "arc horaire", "arc antihoraire", "caméra portée",
                            "point de vue subjectif", "mouvement complexe", "indéterminé",
                        ],
                        "audio_sources": [
                            "sous-titres intégrés", "transcription whisper locale", "aucune source",
                        ],
                        "dialogue_types": [
                            "conversation", "monologue", "questions / réponses",
                            "réplique brève", "dialogue expressif", "annonce / message",
                            "dialogue continu",
                        ],
                        "musique_types": [
                            "musique signalée", "chant / chanson", "aucune musique signalée",
                        ],
                        "musique_familles": MUSIQUE_FAMILLES_DETAILLEES,
                        "musique_sous_genres": MUSIQUE_SOUS_GENRES_DETAILLES,
                        "design_sonore_types": DESIGN_SONORE_TYPES,
                        "ambiance_types": [
                            "effets / ambiance", "voix hors-champ / médiatisée",
                            "ambiance non qualifiée",
                        ],
                        "mouvements_camera_final": [
                            "fixe", "zoom in", "zoom out", "travelling avant",
                            "travelling arrière", "travelling gauche", "travelling droite",
                            "travelling de suivi", "panoramique gauche", "panoramique droite",
                            "tilt haut", "tilt bas", "pedestal haut", "pedestal bas",
                            "rotation horaire", "rotation antihoraire", "arc gauche",
                            "arc droite", "caméra portée", "point de vue subjectif",
                            "mouvement complexe", "indéterminé",
                        ]},
        "plans": plans,
    }
    (racine / "index.json").write_text(
        json.dumps(index, ensure_ascii=False), "utf-8")

    films_legers = {
        "genere": index["genere"],
        "films": films,
        "totaux": {
            "films": len(films),
            "plans": len(plans),
        },
    }
    (racine / "films.json").write_text(
        json.dumps(films_legers, ensure_ascii=False), "utf-8")

    avec = sum(1 for p in plans if p["machine"])
    print(f"\nIndex écrit : {len(plans)} plans, "
          f"{avec} avec machine ({avec / max(len(plans),1):.0%})")


# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("videos", nargs="*", type=Path)
    ap.add_argument("--sortie", type=Path, default=Path("analyse"))
    ap.add_argument("--moteur", choices=["mlx", "ollama"], default="ollama",
                    help="moteur d'inférence ; mlx active Apple MLX, ollama reste le témoin historique")
    ap.add_argument("--modele", default=os.environ.get("BANC_MODELE_ANALYSE", ""))
    ap.add_argument("--concurrence", type=int, default=6,
                    help="nombre de plans envoyés simultanément au serveur MLX")
    ap.add_argument("--mode", choices=["triage", "complet"], default="complet")
    ap.add_argument("--seuil", type=float, default=3.0,
                    help="sensibilité de détection (bas = plus de plans)")
    ap.add_argument("--seuil-seconde-passe", type=float,
                    help="sensibilité du re-scan des segments trop longs (par défaut : plus sensible que --seuil)")
    ap.add_argument("--duree-max-plan", type=float, default=30.0,
                    help="au-delà de cette durée en secondes, un plan est rescanné avec une sensibilité plus fine")
    ap.add_argument("--sans-deux-passes", action="store_true",
                    help="désactive le re-scan des segments trop longs et revient à la détection en une passe")
    ap.add_argument("--concurrence-seconde-passe", type=int, default=3,
                    help="nombre de rescans ffmpeg parallèles pour les segments trop longs")
    ap.add_argument("--redecouper-plans", action="store_true",
                    help="force la régénération de plans.json, sauvegarde l'ancien fichier et purge frames/vignettes")
    ap.add_argument("--largeur", type=int, default=896,
                    help="largeur des images envoyées au modèle")
    ap.add_argument("--leger", action="store_true",
                    help="supprime les images pleine taille après analyse")
    ap.add_argument("--refaire", action="store_true")
    ap.add_argument("--reprendre-contexte-actuel", action="store_true",
                    help="en reprise, saute seulement les plans déjà repassés avec pitch + synopsis + résumé narratif")
    ap.add_argument("--index-seul", action="store_true")
    ap.add_argument("--backend", default="ffmpeg",
                    choices=["ffmpeg", "pyav", "opencv", "moviepy"])
    ap.add_argument("--limite", type=int,
                    help="n'analyser que les N premiers plans (calibrage)")
    ap.add_argument("--apercu", action="store_true",
                    help="produire une boucle animée WebP par plan (~7 Ko)")
    ap.add_argument("--verifier", action="store_true",
                    help="inspecter les fichiers sans rien analyser")
    ap.add_argument("--catalogue", type=Path,
                    help="JSON de fiches films, indexé par nom de fichier")
    ap.add_argument("--sans-contexte-web", action="store_true",
                    help="ne tente pas de compléter pitch/scénario depuis Wikipédia avant l’analyse")
    ap.add_argument("--contexte-libre", default="",
                    help="contexte libre défini dans l’interface locale pour ce lot de films")
    ap.add_argument("--criteres-libre", default="",
                    help="critères prioritaires définis dans l’interface locale pour ce lot de films")
    args = ap.parse_args()

    args.catalogue = (json.loads(args.catalogue.read_text("utf-8"))
                      if args.catalogue and args.catalogue.exists() else {})
    args.sortie.mkdir(parents=True, exist_ok=True)

    if args.index_seul:
        construire_index(args.sortie)
        return

    verifier_outils()
    videos = rassembler(args.videos)
    if not videos:
        sys.exit("Aucune vidéo trouvée.")

    inspecter(videos)
    if args.verifier:
        return

    client = creer_moteur(args.moteur, modele=args.modele, concurrence=args.concurrence)
    if args.moteur == "ollama":
        verifier_modele(client.client, args.modele)
    else:
        try:
            sante = client.sante()
            modele_charge = sante.get("loaded_model") or sante.get("model") or args.modele
            apc = sante.get("apc_enabled")
            print(f"  moteur MLX : {modele_charge} · APC={apc}")
        except Exception as exc:
            sys.exit(
                "Serveur MLX-VLM injoignable sur http://127.0.0.1:8080.\n"
                "Lancez d'abord le serveur MLX, puis relancez avec --moteur mlx.\n"
                f"Détail : {type(exc).__name__}: {exc}"
            )

    for i, video in enumerate(videos, 1):
        print(f"\n▶ [{i}/{len(videos)}] {video.name}")
        traiter(video, args.sortie, args, client)

    construire_index(args.sortie)


if __name__ == "__main__":
    main()
