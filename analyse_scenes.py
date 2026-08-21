#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyse_scenes.py — ajoute une couche scènes au-dessus des plans déjà analysés.

Une scène regroupe plusieurs plans contigus sans changer le principe de base :
un plan reste la plus petite unité entre deux coupes. Cette passe lit les
analyses existantes, crée un tableau film-level `scenes`, puis inscrit sur
chaque plan son `scene_id` et un contexte compact utile à l’index et aux
futures reprises d’analyse.

Usage ciblé :
    python3 analyse_scenes.py analyse --film the-omega-man-1971 --refaire --index

Cette passe est légère : elle ne relance pas le modèle de vision.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path

STOP = {
    "avec", "dans", "pour", "une", "des", "les", "sur", "sous", "par", "vers",
    "entre", "comme", "sans", "plus", "moins", "leur", "leurs", "dont", "cette",
    "celle", "celui", "scene", "scène", "plan", "image", "visible", "visibles",
    "semble", "probablement", "probable", "suggere", "suggère", "montrant",
    "montre", "personnage", "personnages", "homme", "femme", "personne",
    "groupe", "fond", "arriere", "arrière", "premier", "second", "autre",
    "film", "post", "apocalyptique", "ambiance", "atmosphere", "atmosphère",
    "eclairage", "éclairage", "lumiere", "lumière", "interieur", "intérieur",
    "exterieur", "extérieur", "claire", "sombre", "grand", "petit", "gros",
    "tension", "solitude", "survie", "annees", "années", "année", "annee",
    "the", "and", "for", "with", "from", "that", "this", "into", "over", "under",
}

MOMENTS = [
    "nuit", "nocturne", "jour", "journée", "matin", "aube", "soir",
    "crépuscule", "soleil couchant", "midi", "sombre", "plein jour",
]

GENERIQUES = {
    "post-apocalyptique", "tension", "survie", "urgence", "solitude",
    "technologie", "militaire", "intérieur", "extérieur", "sombre", "clair",
}

TAGS_PRIORITE = [
    "générique",
    "projection/cinéma",
    "rituel/procès",
    "Famille/feu",
    "action/conflit",
    "laboratoire/technique",
    "refuge/intérieur",
    "survivants",
    "téléphone/rue",
    "voiture/ville",
    "dialogue/voix",
    "continuité",
]

LIBELLES_TYPES = {
    "générique": "Générique",
    "projection/cinéma": "Cinéma et projection",
    "rituel/procès": "Rituel ou procès de la Famille",
    "Famille/feu": "Affrontement nocturne avec la Famille",
    "action/conflit": "Scène d’action ou de conflit",
    "laboratoire/technique": "Laboratoire et dispositifs techniques",
    "refuge/intérieur": "Intérieur du refuge",
    "survivants": "Rencontre avec les survivants",
    "téléphone/rue": "Téléphone et rue nocturne",
    "voiture/ville": "Traversée de la ville en voiture",
    "dialogue/voix": "Dialogue",
    "continuité": "Continuité narrative",
}

ENJEUX_TYPES = {
    "générique": "Présentation graphique du film et de ses crédits.",
    "projection/cinéma": "Moment de projection ou de spectacle qui dialogue avec l’état mental de Neville.",
    "rituel/procès": "Moment collectif de jugement, menace ou rituel mené par la Famille.",
    "Famille/feu": "Affrontement nocturne ou intimidation autour de la Famille, des flammes et des torches.",
    "action/conflit": "Progression d’une menace, d’une confrontation ou d’une action physique.",
    "laboratoire/technique": "Manipulation ou observation d’appareils scientifiques et techniques.",
    "refuge/intérieur": "Temps intérieur dans le refuge, la maison ou les espaces privés de Neville.",
    "survivants": "Rencontre, déplacement ou organisation d’un groupe de survivants.",
    "téléphone/rue": "Déambulation urbaine autour du téléphone, de la rue et de l’isolement.",
    "voiture/ville": "Déplacement dans Los Angeles déserté, avec alternance de vues de la voiture et de la ville.",
    "dialogue/voix": "Échange verbal ou présence de voix structurant la scène.",
    "continuité": "Continuité d’action à partir de plans contigus.",
}


def normaliser(texte: str) -> str:
    texte = unicodedata.normalize("NFKD", str(texte or "")).encode("ascii", "ignore").decode()
    texte = texte.lower()
    texte = re.sub(r"[^a-z0-9]+", " ", texte)
    return re.sub(r"\s+", " ", texte).strip()


def texte_court(valeur, limite: int = 520) -> str:
    if isinstance(valeur, list):
        valeur = ", ".join(str(v) for v in valeur if v)
    texte = re.sub(r"\s+", " ", str(valeur or "")).strip()
    if len(texte) <= limite:
        return texte
    return texte[:limite].rsplit(" ", 1)[0].rstrip(" .,;:") + "…"


def timecode(secondes: float) -> str:
    total = max(0, float(secondes or 0))
    h = int(total // 3600)
    m = int((total % 3600) // 60)
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def detail_diegetique(plan: dict) -> dict:
    analyse = plan.get("analyse") or {}
    detail = analyse.get("analyse_detaillee") or {}
    return detail.get("description_diegetique") or {}


def valeurs(plan: dict, cle: str) -> list[str]:
    analyse = plan.get("analyse") or {}
    diegetique = detail_diegetique(plan)
    if cle in analyse:
        v = analyse.get(cle)
    elif cle in diegetique:
        v = diegetique.get(cle)
    else:
        v = plan.get(cle)
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if v in (None, ""):
        return []
    return [str(v).strip()]


def mots_texte(texte: str) -> set[str]:
    brut = normaliser(texte)
    mots = set()
    for mot in re.findall(r"\b[a-z0-9]{4,}\b", brut):
        if mot not in STOP and len(mot) >= 4:
            mots.add(mot)
    return mots


def signature_plan(plan: dict) -> set[str]:
    analyse = plan.get("analyse") or {}
    sig: set[str] = set()
    champs = [
        "mots_cles", "machine_types", "lieu_decors", "objets_cles",
        "personnages_sujets", "attitudes_expressions",
    ]
    for champ in champs:
        for v in valeurs(plan, champ):
            n = normaliser(v)
            if not n:
                continue
            sig.update(m for m in n.split() if len(m) >= 4 and m not in STOP)
            if len(n) >= 4 and n not in STOP:
                sig.add(n)
    sig.update(mots_texte(analyse.get("description") or ""))
    sig.update(mots_texte(texte_plan(plan)))
    return sig


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def lieu_simple(plan: dict) -> str:
    return str((plan.get("analyse") or {}).get("lieu") or "").strip()


def texte_plan(plan: dict) -> str:
    analyse = plan.get("analyse") or {}
    detail = analyse.get("analyse_detaillee") or {}
    diegetique = detail.get("description_diegetique") or {}

    morceaux = []
    for champ in [
        "description", "lieu", "echelle", "angle", "mouvement",
        "texte_role", "typographie_description",
    ]:
        morceaux.extend(valeurs(plan, champ))
    for champ in ["mots_cles", "machine_types", "lumiere", "typographie_styles"]:
        morceaux.extend(valeurs(plan, champ))
    for champ in ["lieu_decors", "objets_cles", "personnages_sujets", "attitudes_expressions"]:
        v = diegetique.get(champ)
        if isinstance(v, list):
            morceaux.extend(str(x) for x in v if x)
        elif v:
            morceaux.append(str(v))
    for champ in ["dialogue_texte", "dialogue_types", "musique_types", "ambiance_types"]:
        v = plan.get(champ)
        if isinstance(v, list):
            morceaux.extend(str(x) for x in v if x)
        elif v:
            morceaux.append(str(v))
    return " ".join(morceaux)


def contient(plan: dict, termes: list[str]) -> bool:
    texte = normaliser(texte_plan(plan))
    return any(normaliser(terme) in texte for terme in termes)


def tags_plan(plan: dict) -> set[str]:
    analyse = plan.get("analyse") or {}
    tags: set[str] = set()
    if analyse.get("generique"):
        tags.add("générique")
    if contient(plan, [
        "cinéma", "cinema", "projection", "écran de cinéma", "salle obscure",
        "concert", "musicien", "basse électrique", "interview", "festival",
        "public Woodstock", "Woodstock",
    ]):
        tags.add("projection/cinéma")
    if contient(plan, [
        "tribunal", "procès", "jugement", "rituel", "bâtiment monumental",
        "autel", "colonnes",
    ]):
        tags.add("rituel/procès")
    if contient(plan, [
        "torche", "torchère", "flamme", "feu", "incendie", "masque",
        "mutant", "famille", "cagoule", "bûcher",
    ]):
        tags.add("Famille/feu")
    if contient(plan, [
        "arme", "fusil", "pistolet", "tir", "combat", "lutte", "attaque",
        "confrontation", "menace", "sang", "blessé", "blessure",
    ]):
        tags.add("action/conflit")
    if contient(plan, [
        "laboratoire", "scientifique", "médical", "medical", "vaccin",
        "seringue", "appareil", "équipement scientifique", "terminal",
        "ordinateur", "console de contrôle", "écran cathodique", "moniteur",
    ]):
        tags.add("laboratoire/technique")
    if contient(plan, [
        "salon", "appartement", "chambre", "lit", "cuisine", "bar", "bougies",
        "échecs", "rideaux", "meubles", "intérieur luxueux", "refuge",
        "garage", "entrepôt", "entrepot",
    ]):
        tags.add("refuge/intérieur")
    if contient(plan, [
        "enfants", "adolescents", "femme", "survivants", "groupe de personnes",
        "colline", "camp", "communauté", "jeunes",
    ]):
        tags.add("survivants")
    if contient(plan, ["téléphone public", "téléphone", "cabine téléphonique"]):
        tags.add("téléphone/rue")
    if contient(plan, [
        "voiture", "convertible", "rue déserte", "route", "tableau de bord",
        "showroom", "concession", "véhicule",
    ]):
        tags.add("voiture/ville")
    if plan.get("dialogue") or plan.get("dialogue_texte"):
        tags.add("dialogue/voix")
    if not tags:
        tags.add("continuité")
    return tags


def compter_tags(groupe: list[dict]) -> Counter:
    compteur = Counter()
    for plan in groupe:
        compteur.update(tags_plan(plan))
    return compteur


def tag_prioritaire(tags: set[str]) -> str:
    for tag in TAGS_PRIORITE:
        if tag in tags:
            return tag
    return sorted(tags)[0] if tags else "continuité"


def type_scene(groupe: list[dict]) -> str:
    tags = compter_tags(groupe)
    if tags.get("générique") == len(groupe):
        return "générique"
    candidats = [
        (nb, TAGS_PRIORITE.index(tag) if tag in TAGS_PRIORITE else 999, tag)
        for tag, nb in tags.items()
        if tag not in {"dialogue/voix", "continuité"}
    ]
    candidats.sort(key=lambda item: (-item[0], item[1]))
    if candidats and candidats[0][0] >= max(1, len(groupe) // 3):
        return candidats[0][2]
    if tags.get("dialogue/voix", 0) >= max(1, len(groupe) // 2):
        return "dialogue/voix"
    return tags.most_common(1)[0][0] if tags else "continuité"


def nouvelle_scene(groupe: list[dict], plan: dict, sig: set[str], sig_groupe: set[str], sig_precedent: set[str], max_duree: float, max_plans: int) -> bool:
    if not groupe:
        return False
    precedent = groupe[-1]
    tags_actuels = tags_plan(plan)
    tags_precedent = tags_plan(precedent)
    tags_groupe = compter_tags(groupe)

    # Un générique doit former sa propre scène contiguë : pas de mélange avec
    # l’action, même si les plans de fond continuent visuellement.
    if ("générique" in tags_actuels) != ("générique" in tags_precedent):
        return True
    if tags_groupe.get("générique"):
        return False

    if len(groupe) < 2:
        return False
    debut = float(groupe[0].get("debut") or 0)
    duree_groupe = float(precedent.get("fin") or plan.get("debut") or debut) - debut
    sim_groupe = jaccard(sig, sig_groupe)
    sim_precedent = jaccard(sig, sig_precedent)

    tag_actuel = tag_prioritaire(tags_actuels)
    tag_precedent = tag_prioritaire(tags_precedent)
    tag_dominant = tags_groupe.most_common(1)[0][0] if tags_groupe else "continuité"
    partage_theme = bool(tags_actuels & set(tags_groupe)) or bool(tags_actuels & tags_precedent)

    if len(groupe) >= max_plans:
        return True
    if duree_groupe >= max_duree and len(groupe) >= 2 and sim_groupe < 0.14:
        return True
    if len(groupe) >= 2 and duree_groupe >= 18 and tag_actuel != tag_precedent and sim_precedent < 0.05 and sim_groupe < 0.05:
        return True
    if len(groupe) >= 6 and duree_groupe >= 45 and sim_groupe < 0.045 and not partage_theme:
        return True
    if len(groupe) >= 10 and duree_groupe >= 70 and tag_actuel != tag_dominant and sim_groupe < 0.08:
        return True
    return False


def grouper_plans(plans: list[dict], max_duree: float = 135.0, max_plans: int = 12) -> list[list[dict]]:
    groupes: list[list[dict]] = []
    courant: list[dict] = []
    sig_courante: set[str] = set()
    sig_precedent: set[str] = set()

    for plan in plans:
        sig = signature_plan(plan)
        if courant and nouvelle_scene(courant, plan, sig, sig_courante, sig_precedent, max_duree, max_plans):
            groupes.append(courant)
            courant = []
            sig_courante = set()
        courant.append(plan)
        sig_courante.update(sig)
        sig_precedent = sig
    if courant:
        groupes.append(courant)
    return fusionner_groupes_courts(groupes, max_duree=max_duree, max_plans=max_plans)


def fusionner_groupes_courts(groupes: list[list[dict]], max_duree: float, max_plans: int) -> list[list[dict]]:
    changes = True
    while changes:
        changes = False
        fusionnes: list[list[dict]] = []
        i = 0
        while i < len(groupes):
            groupe = groupes[i]
            if i + 1 < len(groupes):
                suivant = groupes[i + 1]
                tags_groupe = set(compter_tags(groupe))
                tags_suivant = set(compter_tags(suivant))
                debut = float(groupe[0].get("debut") or 0)
                fin = float(suivant[-1].get("fin") or debut)
                peut_fusionner = (
                    "générique" not in tags_groupe
                    and "générique" not in tags_suivant
                    and (len(groupe) <= 1 or fin - float(groupe[-1].get("fin") or debut) < 10)
                    and bool(tags_groupe & tags_suivant)
                    and len(groupe) + len(suivant) <= max_plans
                    and fin - debut <= max_duree + 15
                )
                if peut_fusionner:
                    fusionnes.append(groupe + suivant)
                    i += 2
                    changes = True
                    continue
            fusionnes.append(groupe)
            i += 1
        groupes = fusionnes
    return groupes


def compter_champ(groupe: list[dict], champ: str) -> Counter:
    c = Counter()
    for plan in groupe:
        for v in valeurs(plan, champ):
            t = texte_court(v, 90)
            if not t:
                continue
            n = normaliser(t)
            if n in {"aucun", "aucune", "indetermine", "indeterminee"}:
                continue
            c[t] += 1
    return c


def plus_frequents(compteur: Counter, limite: int = 8, exclure_generiques: bool = False) -> list[str]:
    sorties = []
    vus = set()
    for texte, _ in compteur.most_common():
        cle = normaliser(texte)
        if not cle or cle in vus:
            continue
        if exclure_generiques and texte.lower() in GENERIQUES:
            continue
        vus.add(cle)
        sorties.append(texte)
        if len(sorties) >= limite:
            break
    return sorties


def inferer_temporalite(groupe: list[dict]) -> str:
    texte = normaliser(" ".join(json.dumps(p.get("analyse") or {}, ensure_ascii=False) for p in groupe))
    for moment in MOMENTS:
        if normaliser(moment) in texte:
            return moment
    return "indéterminée"


def inferer_lieu(groupe: list[dict]) -> str:
    lieux = Counter(lieu_simple(p) for p in groupe if lieu_simple(p))
    base = lieux.most_common(1)[0][0] if lieux else "indéterminé"
    decors = plus_frequents(compter_champ(groupe, "lieu_decors"), 3)
    if decors:
        return base + " — " + ", ".join(decors)
    return base


def contexte_specialise(objets: list[str], decors: list[str], mots: list[str]) -> str:
    norm = " ".join(normaliser(x) for x in objets + decors + mots)
    if "helicoptere" in norm and "cockpit" in norm:
        return "Les gros plans intérieurs de cette scène se situent dans ou autour d’un hélicoptère : casques, micros, commandes et tenues doivent être interprétés comme équipement de vol, non comme capsule ou combinaison spatiale sauf preuve visuelle contraire."
    if "automobile" in norm or "voiture" in norm or "vehicule" in norm:
        if "cockpit" in norm or "tableau de bord" in norm:
            return "Les gros plans de commandes et de tableau de bord appartiennent au contexte d’un véhicule terrestre, sauf élément visuel contraire."
    if "hopital" in norm or "laboratoire" in norm or "medical" in norm:
        return "Les appareils, vêtements blancs et gestes techniques doivent être lus dans un contexte médical ou scientifique, sans en déduire une identité précise des personnes."
    return ""


def libelle_type_generique(groupe: list[dict]) -> str:
    debut = int(groupe[0].get("n") or 0)
    role = normaliser(" ".join(str((p.get("analyse") or {}).get("texte_role") or "") for p in groupe))
    if debut <= 25:
        return "Générique d’ouverture"
    if debut > 500:
        return "Générique de fin"
    if "fin" in role and debut > 400:
        return "Générique de fin"
    return "Générique"


def evaluer_confiance_scene(groupe: list[dict], scene_type: str) -> tuple[str, bool]:
    tags = compter_tags(groupe)
    duree = float(groupe[-1].get("fin") or 0) - float(groupe[0].get("debut") or 0)
    melange_generique = 0 < tags.get("générique", 0) < len(groupe)
    dominant = tags.get(scene_type, 0)
    a_verifier = melange_generique or (len(groupe) > 10 and dominant < max(2, len(groupe) // 3))
    if len(groupe) > 1 and duree > 180:
        a_verifier = True
    confiance = "bonne" if not a_verifier else "moyenne"
    return confiance, a_verifier


def construire_scene(numero: int, groupe: list[dict]) -> dict:
    debut = float(groupe[0].get("debut") or 0)
    fin = float(groupe[-1].get("fin") or debut)
    plan_debut = int(groupe[0].get("n") or 0)
    plan_fin = int(groupe[-1].get("n") or plan_debut)

    objets = plus_frequents(compter_champ(groupe, "objets_cles"), 10, exclure_generiques=True)
    decors = plus_frequents(compter_champ(groupe, "lieu_decors"), 8, exclure_generiques=True)
    mots = plus_frequents(compter_champ(groupe, "mots_cles"), 10, exclure_generiques=True)
    machines = plus_frequents(compter_champ(groupe, "machine_types"), 8)
    personnages = plus_frequents(compter_champ(groupe, "personnages_sujets"), 8)
    motifs = plus_frequents(Counter(objets + decors + mots + machines), 10, exclure_generiques=True)

    scene_type = type_scene(groupe)
    libelle_type = libelle_type_generique(groupe) if scene_type == "générique" else LIBELLES_TYPES.get(scene_type, "Continuité narrative")
    confiance, a_verifier = evaluer_confiance_scene(groupe, scene_type)

    index_milieu = len(groupe) // 2
    descriptions = [
        texte_court((groupe[0].get("analyse") or {}).get("description"), 230),
        texte_court((groupe[index_milieu].get("analyse") or {}).get("description"), 230),
        texte_court((groupe[-1].get("analyse") or {}).get("description"), 220),
    ]
    resume_sources = []
    for description in descriptions:
        if description and all(normaliser(description) != normaliser(deja) for deja in resume_sources):
            resume_sources.append(description)
    resume = " ".join(resume_sources)
    if not resume:
        resume = "Scène constituée de plans contigus encore peu décrits."

    titre_motifs = [x for x in motifs if x][:3]
    titre_detail = ", ".join(titre_motifs[:2]) if titre_motifs else f"plans {plan_debut}-{plan_fin}"
    if scene_type == "générique":
        titre = libelle_type
    elif titre_detail:
        titre = f"{libelle_type} — {titre_detail}"
    else:
        titre = libelle_type
    lieu = inferer_lieu(groupe)
    temporalite = inferer_temporalite(groupe)
    action = ENJEUX_TYPES.get(scene_type, "Continuité d’action à partir de plans contigus.")
    if titre_motifs and scene_type not in {"générique", "dialogue/voix"}:
        action += " Motifs dominants : " + ", ".join(titre_motifs[:4]) + "."

    contexte = contexte_specialise(objets, decors, mots)
    if not contexte:
        contexte = (
            "Cette scène regroupe des plans contigus autour de " +
            (", ".join(motifs[:6]) if motifs else "la même continuité d’action") +
            ". Utiliser ce contexte pour désambiguïser les gros plans, sans ajouter d’éléments invisibles."
        )

    return {
        "scene_id": f"scene_{numero:03d}",
        "numero_scene": numero,
        "titre": f"Scène {numero:03d} — {titre}",
        "type_scene": scene_type,
        "plan_debut": plan_debut,
        "plan_fin": plan_fin,
        "plans": [int(p.get("n") or 0) for p in groupe],
        "debut": round(debut, 3),
        "fin": round(fin, 3),
        "duree": round(max(0.0, fin - debut), 3),
        "tc_debut": timecode(debut),
        "tc_fin": timecode(fin),
        "resume_scene": texte_court(resume, 620),
        "lieu": texte_court(lieu, 220),
        "temporalite": temporalite,
        "action_principale": texte_court(action, 260),
        "ambiance": texte_court(", ".join(mots[:5]), 220),
        "personnages_visibles": personnages,
        "objets_significatifs": objets[:10],
        "motifs_structurants": motifs,
        "enjeu_narratif": texte_court(action, 320),
        "contexte_pour_plans": texte_court(contexte, 620),
        "confiance": confiance,
        "a_verifier": a_verifier,
        "methode": "regroupement cohérent v2 : plans contigus triés, génériques isolés, coupures par type narratif, lieu, action, dialogue et similarité descriptive",
    }


def appliquer_scenes(donnees: dict, max_duree: float, max_plans: int) -> list[dict]:
    plans = sorted(donnees.get("plans") or [], key=lambda p: (int(p.get("n") or 0), float(p.get("debut") or 0)))
    donnees["plans"] = plans
    groupes = grouper_plans(plans, max_duree=max_duree, max_plans=max_plans)
    scenes = [construire_scene(i + 1, groupe) for i, groupe in enumerate(groupes)]

    attendus = [int(p.get("n") or 0) for p in plans]
    couverts = []
    for scene in scenes:
        nums = [int(n) for n in scene.get("plans") or []]
        plage = list(range(int(scene["plan_debut"]), int(scene["plan_fin"]) + 1))
        if nums != plage:
            raise RuntimeError(f"Scène non contiguë : {scene['scene_id']} plans {nums[:3]}…")
        couverts.extend(nums)
    if couverts != attendus:
        raise RuntimeError("Les scènes ne couvrent pas exactement les plans du film dans l’ordre")

    par_plan = {}
    for scene in scenes:
        for n in scene["plans"]:
            par_plan[n] = scene

    for plan in plans:
        scene = par_plan.get(int(plan.get("n") or 0))
        if not scene:
            continue
        plan["scene_id"] = scene["scene_id"]
        plan["scene_numero"] = scene["numero_scene"]
        plan["scene_type"] = scene["type_scene"]
        plan["scene_titre"] = scene["titre"]
        plan["scene_resume"] = scene["resume_scene"]
        plan["scene_lieu"] = scene["lieu"]
        plan["scene_temporalite"] = scene["temporalite"]
        plan["scene_action_principale"] = scene["action_principale"]
        plan["scene_ambiance"] = scene["ambiance"]
        plan["scene_personnages_visibles"] = scene["personnages_visibles"]
        plan["scene_objets_significatifs"] = scene["objets_significatifs"]
        plan["scene_motifs_structurants"] = scene["motifs_structurants"]
        plan["scene_enjeu_narratif"] = scene["enjeu_narratif"]
        plan["scene_contexte"] = scene["contexte_pour_plans"]
        plan["scene_confiance"] = scene["confiance"]
        plan["scene_a_verifier"] = scene["a_verifier"]
    donnees["scenes"] = scenes
    donnees["scenes_generees_le"] = time.strftime("%Y-%m-%d %H:%M:%S")
    donnees["scenes_methode"] = "regroupement cohérent v2"
    return scenes


def fichiers_cibles(racine: Path, film: str | None) -> list[Path]:
    if film:
        cible = racine / film / "plans.json"
        if not cible.exists():
            raise SystemExit(f"plans.json introuvable pour le film : {film}")
        return [cible]
    return sorted(racine.glob("*/plans.json"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("racine", nargs="?", type=Path, default=Path("analyse"))
    ap.add_argument("--film", help="slug du film à traiter, par exemple the-omega-man-1971")
    ap.add_argument("--refaire", action="store_true", help="regénérer les scènes même si elles existent déjà")
    ap.add_argument("--dry-run", action="store_true", help="calculer et afficher sans écrire")
    ap.add_argument("--index", action="store_true", help="reconstruire analyse/index.json après écriture")
    ap.add_argument("--max-duree", type=float, default=135.0, help="durée cible maximum d’une scène en secondes")
    ap.add_argument("--max-plans", type=int, default=12, help="nombre cible maximum de plans par scène")
    args = ap.parse_args()

    total_scenes = 0
    total_plans = 0
    for fichier in fichiers_cibles(args.racine, args.film):
        donnees = json.loads(fichier.read_text("utf-8"))
        if donnees.get("scenes") and not args.refaire:
            print(f"déjà scènes : {fichier.parent.name} — {len(donnees.get('scenes') or [])} scènes")
            continue
        scenes = appliquer_scenes(donnees, args.max_duree, args.max_plans)
        total_scenes += len(scenes)
        total_plans += len(donnees.get("plans") or [])
        print(f"{fichier.parent.name} : {len(scenes)} scènes · {len(donnees.get('plans') or [])} plans")
        if args.dry_run:
            for scene in scenes[:8]:
                print(f"  {scene['scene_id']} plans {scene['plan_debut']}-{scene['plan_fin']} · {scene['titre']}")
            continue
        fichier.write_text(json.dumps(donnees, ensure_ascii=False, indent=1), "utf-8")

    if args.index and not args.dry_run:
        try:
            from analyse_plans import construire_index
            construire_index(args.racine)
        except Exception as exc:
            print(f"index non reconstruit ({type(exc).__name__}: {exc})", file=sys.stderr)
            raise
    print(f"Scènes générées : {total_scenes} · plans couverts : {total_plans}")


if __name__ == "__main__":
    main()
