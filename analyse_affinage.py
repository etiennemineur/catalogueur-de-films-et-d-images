#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyse_affinage.py — second module IA pour relire seulement les plans douteux.

Architecture :

1. Module global
   analyse_plans.py analyse tous les plans avec un modèle de vision local choisi par l’utilisateur.

2. Module d’affinage
   ce script relit uniquement les plans douteux, incomplets ou discordants,
   avec un prompt plus exigeant. Il ne redécoupe pas le film et ne refait pas
   toute l’analyse.

Le modèle d’affinage se choisit via `--modele-affinage`, `BANC_MODELE_AFFINAGE`
ou `config.json`.

Exemples :

    .venv/bin/python analyse_affinage.py analyse --dry-run
    .venv/bin/python analyse_affinage.py analyse --limite 20
    .venv/bin/python analyse_affinage.py analyse --film phase-iv-1974 --limite 10
    .venv/bin/python analyse_affinage.py analyse --modele-affinage <modele-ollama-local>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from catalogueur_utils import (  # noqa: E402
    charger_fiche_film,
    chemins_images_plan,
)
from analyse_plans import (  # noqa: E402
    INTERFACES,
    MACHINES,
    contexte_film_prompt,
    interroger,
    schema_complet,
    verifier_modele,
)

CERTITUDE_POIDS = {"douteux": 5, "probable": 3, "": 2, None: 2}


def score_doute(plan: dict) -> tuple[int, list[str]]:
    """Score de priorité pour la relecture."""
    a = plan.get("analyse") or {}
    if not a:
        # Le module 2 ne remplace pas le module global : il attend qu’une
        # première analyse existe. Sinon un plan en cours d’analyse serait
        # confondu avec un plan réellement douteux.
        return 0, []
    score = 0
    raisons = []

    certitude = a.get("certitude")
    if certitude in CERTITUDE_POIDS:
        score += CERTITUDE_POIDS[certitude]
        raisons.append(f"certitude {certitude or 'absente'}")

    if a.get("machine") and not (a.get("machine_types") or a.get("types")):
        score += 4
        raisons.append("machine visible sans type d’appareil")

    if a.get("machine") and not a.get("interface"):
        score += 2
        raisons.append("machine visible sans typologie d’affichage")

    for cle, libelle in [
        ("description", "description absente"),
        ("mots_cles", "mots-clés absents"),
        ("echelle", "échelle absente"),
        ("lieu", "lieu absent"),
        ("lumiere", "lumière absente"),
    ]:
        if a.get(cle) in (None, "", []):
            score += 2
            raisons.append(libelle)

    if plan.get("affinage") and not plan.get("affinage", {}).get("erreur"):
        score = 0
        raisons = []

    return score, raisons


def prompt_affinage(plan: dict, raisons: list[str], fiche: dict | None = None) -> str:
    a = plan.get("analyse") or {}
    ancienne = json.dumps(a, ensure_ascii=False, indent=1)
    return f"""Tu es le module d’affinage d’un catalogue de plans de films.
{contexte_film_prompt(fiche)}

Le module global a déjà produit cette analyse :

{ancienne}

Raisons de la relecture : {', '.join(raisons) or 'relecture demandée'}.

Tu vois les mêmes images du plan. Ton rôle n’est pas de tout réinventer,
mais de corriger et préciser avec prudence :

- confirme ou corrige la présence d’une machine, d’un écran, d’un appareil ou
  d’une interface technique ;
- si une machine est visible, classe-la dans cette liste fermée : {MACHINES} ;
- si un affichage est visible, classe sa typologie dans cette liste : {INTERFACES} ;
- enrichis la description avec plus de précision visuelle et contextuelle, mais
  uniquement quand le détail est visible dans les images ;
- indique "douteux" si l’image est trop petite, floue ou ambiguë ;
- n’identifie pas une personne par son nom ;
- n’invente aucun élément hors champ.

Réponds uniquement avec le même JSON structuré que le module global."""


def chemins_images(racine: Path, plan: dict, images: int) -> list[Path]:
    return chemins_images_plan(racine, plan, images)


def fusionner_analyse(plan: dict, reponse: dict) -> None:
    """Applique prudemment l’affinage à l’analyse visible par le catalogue."""
    globale = plan.get("analyse") or {}
    if "analyse_globale" not in plan:
        plan["analyse_globale"] = globale
    fusion = dict(globale)
    for cle, valeur in reponse.items():
        if valeur not in (None, "", []):
            fusion[cle] = valeur
    plan["analyse"] = fusion


def candidats(racine: Path, film: str | None, seuil: int) -> list[tuple[Path, dict, int, list[str]]]:
    selection = []
    for fichier in sorted(racine.glob("*/plans.json")):
        if film and fichier.parent.name != film:
            continue
        data = json.loads(fichier.read_text(encoding="utf-8"))
        for plan in data.get("plans", []):
            score, raisons = score_doute(plan)
            if score >= seuil:
                selection.append((fichier, plan, score, raisons))
    selection.sort(key=lambda item: (-item[2], item[0].parent.name, item[1].get("n", 0)))
    return selection


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("racine", nargs="?", type=Path, default=Path("analyse"))
    ap.add_argument("--modele-affinage", default=os.environ.get("BANC_MODELE_AFFINAGE", ""))
    ap.add_argument("--film", help="identifiant du film à affiner")
    ap.add_argument("--seuil", type=int, default=3, help="score minimal de doute")
    ap.add_argument("--limite", type=int, default=20, help="nombre maximal de plans à relire")
    ap.add_argument("--images", type=int, default=3, help="nombre d’images envoyées au module")
    ap.add_argument("--dry-run", action="store_true", help="liste les candidats sans appeler le modèle")
    ap.add_argument("--index-seul", action="store_true", help="reconstruit index.json après l’affinage")
    args = ap.parse_args()

    selection = candidats(args.racine, args.film, args.seuil)
    if args.limite:
        selection = selection[:args.limite]

    print(f"{len(selection)} plan(s) à relire par le module d’affinage.")
    for fichier, plan, score, raisons in selection[: min(len(selection), 30)]:
        print(f"- {fichier.parent.name} #{plan.get('n')} {plan.get('tc')} — score {score} — {', '.join(raisons)}")

    if args.dry_run or not selection:
        return

    import ollama
    client = ollama.Client()
    verifier_modele(client, args.modele_affinage)
    schema = schema_complet()

    par_fichier: dict[Path, dict] = {}
    traites = 0
    for fichier, plan, score, raisons in selection:
        data = par_fichier.get(fichier)
        if data is None:
            data = json.loads(fichier.read_text(encoding="utf-8"))
            par_fichier[fichier] = data
        # Toujours remplacer la référence du plan par celle du JSON vivant,
        # sinon le 2e plan d’un même fichier peut être modifié hors structure
        # puis jamais écrit sur disque.
        plan = next((p for p in data.get("plans", []) if p.get("n") == plan.get("n")), plan)

        images = chemins_images(args.racine, plan, args.images)
        if not images:
            plan["affinage"] = {
                "modele": args.modele_affinage,
                "erreur": "images absentes",
                "genere": time.strftime("%Y-%m-%d %H:%M"),
            }
            continue

        fiche = charger_fiche_film(fichier, data)
        reponse = interroger(client, args.modele_affinage, prompt_affinage(plan, raisons, fiche), images, schema)
        plan["affinage"] = {
            "modele": args.modele_affinage,
            "score_initial": score,
            "raisons": raisons,
            "analyse": reponse,
            "contexte_film_utilise": {
                "titre": fiche.get("titre") or "",
                "annee": fiche.get("annee"),
                "pitch": bool(fiche.get("pitch") or fiche.get("synopsis")),
                "scenario": bool(fiche.get("scenario")),
            },
            "genere": time.strftime("%Y-%m-%d %H:%M"),
        }
        if reponse:
            fusionner_analyse(plan, reponse)
            traites += 1
        fichier.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"✓ {fichier.parent.name} #{plan.get('n')} affiné")

    if args.index_seul:
        import subprocess
        subprocess.run([sys.executable, "analyse_plans.py", "--sortie", str(args.racine), "--index-seul"], check=False)

    print(f"\nAffinage terminé : {traites} plan(s) enrichi(s).")


if __name__ == "__main__":
    main()
