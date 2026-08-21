#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyse_typographie.py — seconde passe IA dédiée aux plans contenant
une typographie visible, un générique, un titre ou un texte dans l’image.

Le but n’est pas de refaire toute l’analyse, mais de qualifier plus finement :
- présence de texte
- lien éventuel avec le générique
- rôle du texte dans le plan
- famille et sous-genres typographiques

Exemples :

    .venv/bin/python analyse_typographie.py analyse --dry-run
    .venv/bin/python analyse_typographie.py analyse --film alien-1979 --limite 20
    .venv/bin/python analyse_typographie.py analyse --tous-les-plans --limite 40
    .venv/bin/python analyse_typographie.py analyse --modele <modele-ollama-local> --index-seul
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from catalogueur_utils import (  # noqa: E402
    charger_fiche_film,
    chemins_images_plan,
)
from analyse_plans import (  # noqa: E402
    TEXTE_ROLES,
    TYPOGRAPHIES_CATEGORIES,
    TYPOGRAPHIES_STYLES,
    contexte_film_prompt,
    interroger,
    verifier_modele,
)

TYPOGRAPHIE_CLES = [
    "texte_visible",
    "texte_lisible",
    "generique",
    "texte_role",
    "typographie_categorie",
    "typographie_styles",
    "typographie_description",
]

INDICES_TEXTE = [
    "texte", "typographie", "titre", "générique", "generique", "carton",
    "intertitre", "sous-titre", "soustitre", "credit", "crédit",
    "enseigne", "signalétique", "signaletique", "affiche", "journal",
    "lettrage", "lettre", "logo", "panneau",
]


def normaliser(texte: str) -> str:
    texte = unicodedata.normalize("NFKD", str(texte or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", texte).strip().lower()


def chemins_images(racine: Path, plan: dict, images: int) -> list[Path]:
    return chemins_images_plan(racine, plan, images)


def texte_indice(plan: dict) -> tuple[int, list[str]]:
    a = plan.get("analyse") or {}
    score = 0
    raisons: list[str] = []

    def bloc(valeur) -> str:
        if isinstance(valeur, list):
            return " ".join(str(v) for v in valeur if v)
        return str(valeur or "")

    if a.get("texte_visible"):
        score += 8
        raisons.append("texte visible déjà détecté")
    if a.get("generique"):
        score += 8
        raisons.append("plan lié au générique déjà détecté")
    if a.get("interface") == "typographie surdimensionnée":
        score += 5
        raisons.append("interface typographique déjà signalée")

    corpus = " ".join([
        bloc(a.get("description") or a.get("note") or ""),
        bloc(a.get("mots_cles") or []),
        bloc(a.get("interface") or ""),
        bloc(plan.get("tc") or ""),
    ])
    brut = normaliser(corpus)
    indices = [mot for mot in INDICES_TEXTE if mot in brut]
    if indices:
        score += min(6, len(indices) + 2)
        raisons.append("indices textuels : " + ", ".join(sorted(set(indices))[:4]))

    return score, raisons


def score_typographie(plan: dict, tous_les_plans: bool = False, refaire: bool = False) -> tuple[int, list[str]]:
    a = plan.get("analyse") or {}
    if not a:
        return 0, []
    if plan.get("typographie") and not (plan.get("typographie") or {}).get("erreur") and not refaire:
        return 0, []
    if tous_les_plans:
        return 1, ["relecture typographique forcée sur tout le corpus"]
    return texte_indice(plan)


def schema_typographie() -> dict:
    return {
        "type": "object",
        "properties": {
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
        },
        "required": TYPOGRAPHIE_CLES,
    }


def prompt_typographie(plan: dict, raisons: list[str], fiche: dict | None = None) -> str:
    a = plan.get("analyse") or {}
    ancienne = json.dumps({k: a.get(k) for k in [
        "description", "mots_cles", "certitude", "texte_visible", "generique",
        "texte_role", "typographie_categorie", "typographie_styles",
        "typographie_description",
    ]}, ensure_ascii=False, indent=1)
    return f"""Tu observes 3 images extraites d’un même plan de film.
{contexte_film_prompt(fiche)}

Analyse existante du plan :
{ancienne}

Raisons de cette seconde passe typographique : {', '.join(raisons) or 'relecture typographique demandée'}.

Ta mission est uniquement typographique.

Détermine avec prudence :
- s’il y a du texte visible dans le plan ;
- si le plan relève du générique, d’un titre, d’un carton, d’un intertitre ou d’un crédit ;
- le rôle principal du texte dans l’image ;
- la famille typographique dominante ;
- jusqu’à 3 sous-genres typographiques visibles, parmi la liste fermée ;
- une courte description visuelle de la typographie.

Contraintes fortes :
- n’invente jamais un nom exact de police commerciale ;
- ne prétends pas reconnaître une fonte précise si l’image ne le permet pas ;
- si du texte est visible mais trop petit ou flou, réponds texte_visible=true, texte_lisible=false,
  typographie_categorie="indéterminée", typographie_styles=["indéterminée"] ;
- si aucun texte n’est visible, réponds texte_visible=false, texte_lisible=false,
  generique=false, texte_role="", typographie_categorie="", typographie_styles=[],
  typographie_description="".

Réponds uniquement avec un objet JSON respectant exactement ce schéma implicite :
- texte_visible: bool
- texte_lisible: bool
- generique: bool
- texte_role: une valeur parmi {TEXTE_ROLES} ou ""
- typographie_categorie: une valeur parmi {TYPOGRAPHIES_CATEGORIES} ou ""
- typographie_styles: 0 à 3 valeurs parmi {TYPOGRAPHIES_STYLES}
- typographie_description: courte phrase factuelle en français
"""


def fusionner_typographie(plan: dict, reponse: dict) -> None:
    analyse = dict(plan.get("analyse") or {})
    for cle in TYPOGRAPHIE_CLES:
        valeur = reponse.get(cle)
        if cle in ("texte_visible", "texte_lisible", "generique"):
            analyse[cle] = bool(valeur)
        elif cle == "typographie_styles":
            analyse[cle] = valeur if isinstance(valeur, list) else []
        else:
            analyse[cle] = valeur if valeur not in (None,) else ""
    plan["analyse"] = analyse



def candidats(racine: Path, film: str | None, seuil: int, tous_les_plans: bool = False, refaire: bool = False) -> list[tuple[Path, dict, int, list[str]]]:
    selection = []
    for fichier in sorted(racine.glob("*/plans.json")):
        if film and fichier.parent.name != film:
            continue
        data = json.loads(fichier.read_text(encoding="utf-8"))
        for plan in data.get("plans", []):
            score, raisons = score_typographie(plan, tous_les_plans=tous_les_plans, refaire=refaire)
            if score >= seuil:
                selection.append((fichier, plan, score, raisons))
    selection.sort(key=lambda item: (-item[2], item[0].parent.name, item[1].get("n", 0)))
    return selection



def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("racine", nargs="?", type=Path, default=Path("analyse"))
    ap.add_argument("--modele", default=os.environ.get("BANC_MODELE_AFFINAGE", os.environ.get("BANC_MODELE_ANALYSE", "")))
    ap.add_argument("--film", help="identifiant du film à relire")
    ap.add_argument("--seuil", type=int, default=3, help="score minimal pour sélectionner un plan")
    ap.add_argument("--limite", type=int, default=30, help="nombre maximal de plans à relire")
    ap.add_argument("--images", type=int, default=3, help="nombre d’images envoyées au modèle")
    ap.add_argument("--dry-run", action="store_true", help="liste les candidats sans appeler le modèle")
    ap.add_argument("--index-seul", action="store_true", help="reconstruit index.json après la passe")
    ap.add_argument("--refaire", action="store_true", help="refait la passe même si elle existe déjà")
    ap.add_argument("--tous-les-plans", action="store_true", help="force la relecture typographique sur tous les plans déjà analysés")
    args = ap.parse_args()

    selection = candidats(args.racine, args.film, args.seuil, tous_les_plans=args.tous_les_plans, refaire=args.refaire)
    if args.limite:
        selection = selection[:args.limite]

    print(f"{len(selection)} plan(s) candidat(s) pour la passe typographique.")
    for fichier, plan, score, raisons in selection[: min(len(selection), 40)]:
        print(f"- {fichier.parent.name} #{plan.get('n')} {plan.get('tc')} — score {score} — {', '.join(raisons)}")

    if args.dry_run or not selection:
        return

    import ollama
    client = ollama.Client()
    verifier_modele(client, args.modele)
    schema = schema_typographie()

    par_fichier: dict[Path, dict] = {}
    traites = 0
    for fichier, plan, score, raisons in selection:
        data = par_fichier.get(fichier)
        if data is None:
            data = json.loads(fichier.read_text(encoding="utf-8"))
            par_fichier[fichier] = data
        # Toujours réattacher le plan à la structure JSON vivante, sinon un
        # second plan du même fichier peut être modifié hors structure.
        plan = next((p for p in data.get("plans", []) if p.get("n") == plan.get("n")), plan)

        images = chemins_images(args.racine, plan, args.images)
        if not images:
            plan["typographie"] = {
                "modele": args.modele,
                "erreur": "images absentes",
                "genere": time.strftime("%Y-%m-%d %H:%M"),
            }
            continue

        fiche = charger_fiche_film(fichier, data)
        reponse = interroger(client, args.modele, prompt_typographie(plan, raisons, fiche), images, schema)
        plan["typographie"] = {
            "modele": args.modele,
            "score_initial": score,
            "raisons": raisons,
            "analyse": reponse,
            "genere": time.strftime("%Y-%m-%d %H:%M"),
        }
        if reponse:
            fusionner_typographie(plan, reponse)
            traites += 1
        fichier.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"✓ {fichier.parent.name} #{plan.get('n')} typographie relue")

    if args.index_seul:
        subprocess.run([sys.executable, "analyse_plans.py", "--sortie", str(args.racine), "--index-seul"], check=False)

    print(f"\nPasse typographique terminée : {traites} plan(s) enrichi(s).")


if __name__ == "__main__":
    main()
