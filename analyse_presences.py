#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyse_presences.py — ajoute une couche structurée personnes / animaux.

Cette passe ne relance pas le modèle de vision. Elle extrait prudemment des
catégories filtrables depuis les descriptions visuelles déjà écrites dans
chaque plan : personnes visibles, genre perçu, âge perçu, carnation apparente,
apparence ethnique éventuellement mentionnée à vérifier, et animaux visibles.

Règle de prudence : une origine ethnique certaine n’est pas déduite d’une image.
Le champ `origines_ethniques_documentees` reste réservé à une source fiable ou à
une fiche validée. Les champs d’apparence sont des aides de recherche, à vérifier
humainement.

Usage :
    .venv/bin/python analyse_presences.py analyse --film the-omega-man-1971 --refaire --index
    .venv/bin/python analyse_presences.py analyse --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

GENRES_PERSONNES = ["homme", "femme", "genre indéterminé"]
AGES_PERSONNES = ["bébé", "enfant", "adolescent", "adulte", "personne âgée", "âge indéterminé"]
CARNATIONS_APPARENTES = [
    "très claire / albinos", "claire", "médiane", "foncée", "très foncée",
    "non visible", "indéterminée",
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

NOMBRES = {
    "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5,
    "six": 6, "sept": 7, "huit": 8, "neuf": 9, "dix": 10,
}

ANIMAL_SYNONYMES = {
    "chien": ["chien", "chiens"],
    "chat": ["chat", "chats"],
    "cheval": ["cheval", "chevaux"],
    "oiseau": ["oiseau", "oiseaux"],
    "rat": ["rat", "rats"],
    "souris": ["souris"],
    "serpent": ["serpent", "serpents"],
    "insecte": ["insecte", "insectes"],
    "poisson": ["poisson", "poissons"],
    "animal indéterminé": ["animal", "animaux"],
}


def normaliser(texte: Any) -> str:
    txt = str(texte or "").lower().replace("’", "'")
    txt = unicodedata.normalize("NFD", txt)
    txt = "".join(c for c in txt if unicodedata.category(c) != "Mn")
    return txt


def tokens(texte: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", normaliser(texte)))


def ajouter_unique(liste: list[str], valeur: str) -> None:
    if valeur and valeur not in liste:
        liste.append(valeur)


def liste_texte(valeur: Any) -> list[str]:
    if isinstance(valeur, list):
        return [str(v).strip() for v in valeur if str(v).strip()]
    if valeur in (None, ""):
        return []
    return [str(valeur).strip()]


def texte_visuel_plan(plan: dict[str, Any]) -> str:
    a = plan.get("analyse") or {}
    detail = a.get("analyse_detaillee") or {}
    diegetique = detail.get("description_diegetique") or {}
    morceaux = [
        a.get("description", ""),
        " ".join(liste_texte(a.get("mots_cles"))),
        " ".join(liste_texte(diegetique.get("personnages_sujets"))),
        " ".join(liste_texte(diegetique.get("attitudes_expressions"))),
        " ".join(liste_texte(diegetique.get("objets_cles"))),
        " ".join(liste_texte(diegetique.get("lieu_decors"))),
    ]
    return " ".join(m for m in morceaux if m)


def nombre_personnes(plan: dict[str, Any], texte: str) -> int | None:
    a = plan.get("analyse") or {}
    n = a.get("personnages")
    if isinstance(n, int) and n >= 0:
        return n
    t = normaliser(texte)
    motifs = [
        r"\b(\d{1,2})\s+(?:personnes?|personnages?|hommes?|femmes?|enfants?)\b",
        r"\b(un|une|deux|trois|quatre|cinq|six|sept|huit|neuf|dix)\s+(?:personnes?|personnages?|hommes?|femmes?|enfants?)\b",
    ]
    for motif in motifs:
        m = re.search(motif, t)
        if not m:
            continue
        brut = m.group(1)
        if brut.isdigit():
            return int(brut)
        return NOMBRES.get(brut)
    return None


def detecter_humains(plan: dict[str, Any], texte: str, n: int | None) -> bool:
    if isinstance(n, int) and n > 0:
        return True
    t = tokens(texte)
    indices = {
        "personne", "personnes", "personnage", "personnages", "homme", "hommes",
        "femme", "femmes", "enfant", "enfants", "fille", "filles", "garcon",
        "garcons", "visage", "silhouette", "silhouettes", "groupe", "foule",
        "adolescent", "adolescente", "adolescents", "adolescentes",
    }
    if t & indices:
        return True
    return bool(liste_texte(((plan.get("analyse") or {}).get("analyse_detaillee") or {}).get("description_diegetique", {}).get("personnages_sujets")))


def detecter_genres(texte: str, humain_visible: bool) -> list[str]:
    t = normaliser(texte)
    genres: list[str] = []
    if re.search(r"\b(hommes?\s+et\s+femmes?|femmes?\s+et\s+hommes?|genres?\s+mixtes?|feminin\s+et\s+masculin|masculin\s+et\s+feminin)\b", t):
        genres.extend(["homme", "femme"])
    if re.search(r"\b(femmes?|feminin(?:e|es|s)?|filles?|jeunes?\s+filles?|fillette?s?|mere|meres)\b", t):
        ajouter_unique(genres, "femme")
    if re.search(r"\b(hommes?|masculin(?:e|s)?|garcons?|jeunes?\s+hommes?|pere|peres)\b", t):
        ajouter_unique(genres, "homme")
    if not genres and humain_visible:
        genres.append("genre indéterminé")
    return genres


def detecter_ages(texte: str, humain_visible: bool) -> list[str]:
    t = normaliser(texte)
    ages: list[str] = []
    if re.search(r"\b(bebe|bebes|nourrisson|nourrissons)\b", t):
        ajouter_unique(ages, "bébé")
    if re.search(r"\b(enfants?|fillettes?|garcons?|jeunes?\s+filles?)\b", t):
        ajouter_unique(ages, "enfant")
    if re.search(r"\b(adolescent(?:e|es|s)?|adolescence|teenager|teenagers)\b", t):
        ajouter_unique(ages, "adolescent")
    if re.search(r"\b(adultes?|jeunes?\s+adultes?|hommes?|femmes?)\b", t):
        ajouter_unique(ages, "adulte")
    if re.search(r"\b(personnes?\s+agees?|vieill?es?|age\s+avance)\b", t):
        ajouter_unique(ages, "personne âgée")
    if not ages and humain_visible:
        ages.append("âge indéterminé")
    return ages


def detecter_carnations_et_apparences(texte: str, humain_visible: bool) -> tuple[list[str], list[str], bool, str]:
    if not humain_visible:
        return ["non visible"], [], False, "aucune personne visible"
    t = normaliser(texte)
    carnations: list[str] = []
    apparences: list[str] = []
    notes: list[str] = []

    def contexte_peau(motif: str) -> bool:
        return bool(re.search(r"\b(?:peau|teint|carnation|visage)\b[^.;,]{0,50}" + motif, t) or
                    re.search(motif + r"[^.;,]{0,50}\b(?:peau|teint|carnation|visage)\b", t))

    if re.search(r"\balbinos?\b", t) or contexte_peau(r"\b(pale|tres\s+claire|blafard(?:e|es|s)?)\b"):
        ajouter_unique(carnations, "très claire / albinos")
        ajouter_unique(apparences, "albinos / très pâle")
    if contexte_peau(r"\b(claire?s?|blanche?s?)\b"):
        ajouter_unique(carnations, "claire")
    if contexte_peau(r"\b(median(?:e|es|s)?|mate?s?|olive)\b"):
        ajouter_unique(carnations, "médiane")
    if contexte_peau(r"\b(foncee?s?|sombre?s?|brune?s?)\b"):
        ajouter_unique(carnations, "foncée")
    if contexte_peau(r"\b(tres\s+foncee?s?|noire?s?)\b"):
        ajouter_unique(carnations, "très foncée")

    # Mentions déjà présentes dans les descriptions IA. Elles restent des aides
    # de recherche visuelle, jamais des identifications ethniques certaines.
    if re.search(r"\b(afro[-\s]?americaine?s?|afro[-\s]?americain(?:e|es|s)?|afro[-\s]?descendant(?:e|es|s)?)\b", t):
        ajouter_unique(apparences, "afro-descendante apparente")
        if not carnations:
            ajouter_unique(carnations, "foncée")
        notes.append("apparence afro-descendante mentionnée par l’analyse IA, à vérifier")
    if re.search(r"\b(caucasien(?:ne|nes|s)?|europeen(?:ne|nes|s)?|blanche?s?)\b", t) and contexte_peau(r"\b(caucasien(?:ne|nes|s)?|europeen(?:ne|nes|s)?|blanche?s?)\b"):
        ajouter_unique(apparences, "européenne / blanche apparente")
    if re.search(r"\b(asiatique?s?|chinois(?:e|es)?|japonais(?:e|es)?|coreen(?:ne|nes|s)?)\b", t):
        ajouter_unique(apparences, "asiatique apparente")
        notes.append("apparence asiatique mentionnée par l’analyse IA, à vérifier")
    if re.search(r"\b(latino[-\s]?americain(?:e|es|s)?|latina?s?|latino?s?|hispanique?s?)\b", t):
        ajouter_unique(apparences, "latino-américaine apparente")
        notes.append("apparence latino-américaine mentionnée par l’analyse IA, à vérifier")
    if re.search(r"\b(moyen[-\s]?oriental(?:e|es|s)?|arabe?s?)\b", t):
        ajouter_unique(apparences, "moyen-orientale apparente")
        notes.append("apparence moyen-orientale mentionnée par l’analyse IA, à vérifier")
    if re.search(r"\b(autochtone?s?|amerindien(?:ne|nes|s)?)\b", t):
        ajouter_unique(apparences, "autochtone apparente")
        notes.append("apparence autochtone mentionnée par l’analyse IA, à vérifier")

    if not carnations:
        carnations.append("indéterminée")
    a_verifier = bool(apparences)
    note = "; ".join(notes) if notes else "carnation apparente déduite seulement si mention visuelle explicite"
    return carnations, apparences, a_verifier, note


def detecter_origines_documentees(plan: dict[str, Any]) -> list[str]:
    valeurs: list[str] = []
    for personne in plan.get("personnes_reconnues") or []:
        if not isinstance(personne, dict):
            continue
        for cle in ("origine_ethnique_documentee", "origine_ethnique", "ethnie_documentee"):
            v = personne.get(cle)
            if isinstance(v, list):
                for item in v:
                    ajouter_unique(valeurs, str(item).strip())
            elif v:
                ajouter_unique(valeurs, str(v).strip())
    return valeurs


def detecter_animaux(texte: str) -> tuple[bool, list[str], str]:
    t = tokens(texte)
    animaux: list[str] = []
    for canon, synonymes in ANIMAL_SYNONYMES.items():
        if any(normaliser(s) in t for s in synonymes):
            ajouter_unique(animaux, canon)
    if animaux:
        return True, animaux, "probable"
    return False, [], "sûr"


def analyser_plan(plan: dict[str, Any]) -> dict[str, Any]:
    texte = texte_visuel_plan(plan)
    n = nombre_personnes(plan, texte)
    humain = detecter_humains(plan, texte, n)
    genres = detecter_genres(texte, humain)
    ages = detecter_ages(texte, humain)
    carnations, apparences, apparences_a_verifier, note_apparence = detecter_carnations_et_apparences(texte, humain)
    animal_visible, animaux, animal_confiance = detecter_animaux(texte)

    categories: list[str] = []
    ajouter_unique(categories, "personne visible" if humain else "aucune personne visible")
    if "homme" in genres:
        ajouter_unique(categories, "homme visible")
    if "femme" in genres:
        ajouter_unique(categories, "femme visible")
    if "enfant" in ages:
        ajouter_unique(categories, "enfant visible")
    if "adolescent" in ages:
        ajouter_unique(categories, "adolescent visible")
    if "adulte" in ages:
        ajouter_unique(categories, "adulte visible")
    if "personne âgée" in ages:
        ajouter_unique(categories, "personne âgée visible")
    if isinstance(n, int) and n >= 3:
        ajouter_unique(categories, "groupe")
    if re.search(r"\bsilhouettes?\b", normaliser(texte)):
        ajouter_unique(categories, "silhouette")
    ajouter_unique(categories, "animal visible" if animal_visible else "aucun animal visible")

    return {
        "personnes_visibles": bool(humain),
        "nombre_personnes": n if isinstance(n, int) and n >= 0 else 0,
        "genres_personnes": genres,
        "ages_personnes": ages,
        "carnations_apparentes": carnations,
        "apparences_ethniques": apparences,
        "apparences_ethniques_a_verifier": bool(apparences_a_verifier),
        "origines_ethniques_documentees": detecter_origines_documentees(plan),
        "animaux_visibles": animaux,
        "animal_visible": bool(animal_visible),
        "animal_confiance": animal_confiance,
        "categories_presence": categories,
        "methode": "extraction_textuelle_conservative_depuis_analyse_visuelle",
        "note": note_apparence,
    }


def traiter_fichier(plans_path: Path, refaire: bool, dry_run: bool) -> tuple[int, int, Counter]:
    data = json.loads(plans_path.read_text(encoding="utf-8"))
    faits = 0
    modifies = 0
    stats: Counter = Counter()
    for plan in data.get("plans", []):
        if plan.get("presences") and not refaire:
            continue
        presences = analyser_plan(plan)
        faits += 1
        if presences.get("personnes_visibles"):
            stats["plans_avec_personne"] += 1
        if "homme" in presences.get("genres_personnes", []):
            stats["plans_avec_homme"] += 1
        if "femme" in presences.get("genres_personnes", []):
            stats["plans_avec_femme"] += 1
        if "enfant" in presences.get("ages_personnes", []):
            stats["plans_avec_enfant"] += 1
        if presences.get("animal_visible"):
            stats["plans_avec_animal"] += 1
        if presences.get("apparences_ethniques"):
            stats["plans_avec_apparence_ethnique_a_verifier"] += 1
        if presences.get("carnations_apparentes") and presences["carnations_apparentes"] not in (["non visible"], ["indéterminée"]):
            stats["plans_avec_carnation_qualifiee"] += 1
        if plan.get("presences") != presences:
            modifies += 1
            if not dry_run:
                plan["presences"] = presences
    if modifies and not dry_run:
        plans_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return faits, modifies, stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("racine", nargs="?", type=Path, default=Path("analyse"))
    ap.add_argument("--film", help="identifiant de film précis")
    ap.add_argument("--refaire", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--index", action="store_true", help="reconstruire l’index après écriture")
    args = ap.parse_args()

    fichiers = sorted(args.racine.glob("*/plans.json"))
    if args.film:
        fichiers = [p for p in fichiers if p.parent.name == args.film]
    if not fichiers:
        raise SystemExit("Aucun plans.json trouvé.")

    total_faits = total_modifies = 0
    total_stats: Counter = Counter()
    for plans_path in fichiers:
        faits, modifies, stats = traiter_fichier(plans_path, args.refaire, args.dry_run)
        total_faits += faits
        total_modifies += modifies
        total_stats.update(stats)
        print(f"{plans_path.parent.name} : {faits} plans lus · {modifies} {'seraient modifiés' if args.dry_run else 'modifiés'}")

    if args.index and not args.dry_run:
        import analyse_plans
        analyse_plans.construire_index(args.racine)

    sortie = {"plans_lus": total_faits, "plans_modifies": total_modifies, **dict(total_stats)}
    print(json.dumps(sortie, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
