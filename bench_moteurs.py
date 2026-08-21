#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bench_moteurs.py — comparer MLX et Ollama sur les mêmes plans, avant bascule.

Ce script ne modifie aucun plans.json. Il rejoue N plans déjà analysés sur un
ou deux moteurs, et répond aux trois seules questions qui comptent :

1. Le JSON sort-il valide ?
2. Les valeurs restent-elles dans le vocabulaire contrôlé ?
3. Les deux moteurs disent-ils la même chose du même plan ?

… puis mesure le débit réel en fonction de la concurrence, ce qui donne la
valeur à mettre dans MLX_VLM_CONCURRENCE.

Usage :
    .venv/bin/python bench_moteurs.py --film the-omega-man-1971 --plans 30
    .venv/bin/python bench_moteurs.py --film tron-1982 --plans 20 --moteurs mlx
    .venv/bin/python bench_moteurs.py --film tron-1982 --debit 1 2 4 8
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import analyse_plans as ap_mod
from moteur_vision import MoteurMLX, creer_moteur

CHAMPS_COMPARES = [
    "echelle", "angle", "mouvement", "lieu", "personnages",
    "machine", "machine_role", "texte_visible", "generique",
]


# ─────────────────────────────────────────────────────────────────────────────
#  Conformité au vocabulaire contrôlé
# ─────────────────────────────────────────────────────────────────────────────

def ecarts_vocabulaire(schema: Any, valeur: Any, chemin: str = "") -> list[str]:
    """Liste les valeurs produites hors des listes fermées du schéma."""
    if not isinstance(schema, dict):
        return []

    variantes = schema.get("oneOf") or schema.get("anyOf")
    if isinstance(variantes, list) and variantes:
        for v in variantes:
            if not ecarts_vocabulaire(v, valeur, chemin):
                return []
        return [f"{chemin or '(racine)'} = {valeur!r} (aucune variante ne convient)"]

    enum = schema.get("enum")
    if enum is not None:
        return [] if valeur in enum else [f"{chemin or '(racine)'} = {valeur!r}"]

    if schema.get("type") == "object" and isinstance(valeur, dict):
        ecarts = []
        for cle, sous in (schema.get("properties") or {}).items():
            if cle in valeur:
                ecarts += ecarts_vocabulaire(sous, valeur[cle], f"{chemin}.{cle}" if chemin else cle)
        return ecarts

    if schema.get("type") == "array" and isinstance(valeur, list):
        ecarts = []
        for i, item in enumerate(valeur):
            ecarts += ecarts_vocabulaire(schema.get("items") or {}, item, f"{chemin}[{i}]")
        return ecarts

    return []


def cles_manquantes(schema: dict, valeur: Any) -> list[str]:
    if not isinstance(valeur, dict):
        return list(schema.get("required") or [])
    return [c for c in (schema.get("required") or []) if c not in valeur]


# ─────────────────────────────────────────────────────────────────────────────
#  Préparation des tâches
# ─────────────────────────────────────────────────────────────────────────────

def images_du_plan(racine: Path, fid: str, plan: dict) -> list[Path]:
    """Frames pleine résolution si elles existent, vignettes en repli."""
    n = int(plan.get("n") or 0)
    frames = sorted((racine / fid / "frames").glob(f"{n:05d}_*.jpg"))
    if frames:
        return frames
    rels = plan.get("vignettes") or ([plan["vignette"]] if plan.get("vignette") else [])
    return [racine / r for r in rels if (racine / r).exists()]


def preparer(racine: Path, fid: str, nb: int, mode: str) -> tuple[list[dict], dict, bool]:
    fichier = racine / fid / "plans.json"
    if not fichier.exists():
        raise SystemExit(f"plans.json introuvable : {fichier}")
    donnees = json.loads(fichier.read_text(encoding="utf-8"))
    fiche = donnees.get("fiche") or {}
    schema = ap_mod.schema_triage() if mode == "triage" else ap_mod.schema_complet()

    taches, pleine_resolution = [], True
    for plan in donnees.get("plans") or []:
        if len(taches) >= nb:
            break
        images = images_du_plan(racine, fid, plan)
        if not images:
            continue
        if "frames" not in str(images[0]):
            pleine_resolution = False
        scene = ap_mod.scene_du_plan(donnees, plan)
        prompt = (ap_mod.prompt_triage(fiche, scene) if mode == "triage"
                  else ap_mod.prompt_complet(fiche, scene))
        taches.append({
            "n": int(plan.get("n") or 0),
            "prompt": prompt,
            "images": images,
            "schema": schema,
            "tenant": fid,
        })

    if not taches:
        raise SystemExit(f"Aucune image disponible pour {fid} "
                         "(relancez sans --leger, ou gardez les vignettes).")
    return taches, schema, pleine_resolution


# ─────────────────────────────────────────────────────────────────────────────
#  Exécution
# ─────────────────────────────────────────────────────────────────────────────

def executer(moteur, taches: list[dict], schema: dict, concurrence: int = 1) -> dict:
    resultats, durees = [], []

    if concurrence > 1 and isinstance(moteur, MoteurMLX):
        depart = time.time()
        resultats = moteur.decrire_lot(
            [{k: t[k] for k in ("prompt", "images", "schema", "tenant")} for t in taches],
            concurrence=concurrence,
        )
        mur = time.time() - depart
        durees = [mur / max(1, len(taches))] * len(taches)
    else:
        depart = time.time()
        for t in taches:
            t0 = time.time()
            resultats.append(moteur.decrire(t["prompt"], t["images"], t["schema"],
                                            tenant=t["tenant"]))
            durees.append(time.time() - t0)
        mur = time.time() - depart

    valides = [r for r in resultats if isinstance(r, dict) and r]
    ecarts, manquants = [], []
    for t, r in zip(taches, resultats):
        if not r:
            continue
        for e in ecarts_vocabulaire(schema, r):
            ecarts.append(f"plan {t['n']} · {e}")
        for c in cles_manquantes(schema, r):
            manquants.append(f"plan {t['n']} · {c}")

    return {
        "concurrence": concurrence,
        "plans": len(taches),
        "valides": len(valides),
        "taux_json": round(100 * len(valides) / max(1, len(taches)), 1),
        "ecarts_vocabulaire": ecarts,
        "cles_manquantes": manquants,
        "mur_secondes": round(mur, 1),
        "par_plan_median": round(statistics.median(durees), 2) if durees else 0.0,
        "par_plan_p90": round(sorted(durees)[int(0.9 * (len(durees) - 1))], 2) if durees else 0.0,
        "plans_par_heure": round(3600 * len(taches) / mur) if mur > 0 else 0,
        "resultats": resultats,
    }


def accord(a: list[dict], b: list[dict]) -> dict:
    scores = {}
    for champ in CHAMPS_COMPARES:
        comparables = identiques = 0
        for ra, rb in zip(a, b):
            if not ra or not rb or champ not in ra or champ not in rb:
                continue
            comparables += 1
            if ra[champ] == rb[champ]:
                identiques += 1
        if comparables:
            scores[champ] = {
                "comparables": comparables,
                "accord_pct": round(100 * identiques / comparables, 1),
            }
    return scores


# ─────────────────────────────────────────────────────────────────────────────
#  Rapport
# ─────────────────────────────────────────────────────────────────────────────

def afficher(nom: str, r: dict) -> None:
    print(f"\n▶ {nom}  (concurrence {r['concurrence']})")
    print(f"  JSON valides       : {r['valides']}/{r['plans']}  ({r['taux_json']} %)")
    print(f"  Hors vocabulaire   : {len(r['ecarts_vocabulaire'])}")
    for e in r["ecarts_vocabulaire"][:6]:
        print(f"      · {e}")
    if len(r["ecarts_vocabulaire"]) > 6:
        print(f"      … et {len(r['ecarts_vocabulaire']) - 6} autres")
    print(f"  Clés requises abs. : {len(r['cles_manquantes'])}")
    print(f"  Temps par plan     : médiane {r['par_plan_median']} s · p90 {r['par_plan_p90']} s")
    print(f"  Débit              : {r['plans_par_heure']} plans/heure "
          f"({r['mur_secondes']} s pour {r['plans']} plans)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("racine", nargs="?", type=Path, default=Path("analyse"))
    p.add_argument("--film", required=True, help="slug du film, ex. the-omega-man-1971")
    p.add_argument("--plans", type=int, default=30)
    p.add_argument("--mode", choices=["triage", "complet"], default="complet")
    p.add_argument("--moteurs", nargs="*", default=["mlx", "ollama"])
    p.add_argument("--modele-mlx", default=None)
    p.add_argument("--modele-ollama", default=None)
    p.add_argument("--debit", nargs="*", type=int, default=[1, 4, 8],
                   help="niveaux de concurrence à mesurer côté MLX")
    p.add_argument("--rapport", type=Path, default=Path("bench_moteurs.json"))
    args = p.parse_args()

    taches, schema, pleine_res = preparer(args.racine, args.film, args.plans, args.mode)
    print(f"Film   : {args.film}")
    print(f"Plans  : {len(taches)} · mode {args.mode} · "
          f"{'frames pleine résolution' if pleine_res else 'VIGNETTES 480 px — temps non représentatifs'}")
    print(f"Prompt : {len(taches[0]['prompt'])} caractères")

    rapport: dict[str, Any] = {"film": args.film, "plans": len(taches), "mode": args.mode}
    sorties: dict[str, list[dict]] = {}

    if "ollama" in args.moteurs:
        modele = args.modele_ollama or ap_mod.__dict__.get("MODELE_DEFAUT", "")
        moteur = creer_moteur("ollama", modele=modele)
        r = executer(moteur, taches, schema, concurrence=1)
        sorties["ollama"] = r.pop("resultats")
        rapport["ollama"] = r
        afficher(f"Ollama · {modele}", r)

    if "mlx" in args.moteurs:
        kw = {"modele": args.modele_mlx} if args.modele_mlx else {}
        moteur = creer_moteur("mlx", **kw)
        try:
            sante = moteur.sante()
            print(f"\nServeur MLX : {json.dumps(sante, ensure_ascii=False)}")
        except Exception as exc:
            raise SystemExit(f"Serveur mlx-vlm injoignable ({type(exc).__name__}: {exc}). "
                             "Lancez-le avant le bench.")
        rapport["mlx"] = {}
        for c in args.debit:
            r = executer(moteur, taches, schema, concurrence=c)
            if c == args.debit[0]:
                sorties["mlx"] = r["resultats"]
            r.pop("resultats")
            rapport["mlx"][f"concurrence_{c}"] = r
            afficher(f"MLX · {moteur.modele}", r)
        rapport["mlx"]["cache"] = moteur.cache_stats()

    if "ollama" in sorties and "mlx" in sorties:
        scores = accord(sorties["ollama"], sorties["mlx"])
        rapport["accord"] = scores
        print("\n▶ Accord entre moteurs, champ par champ")
        for champ, s in sorted(scores.items(), key=lambda kv: kv[1]["accord_pct"]):
            print(f"  {champ:<18} {s['accord_pct']:>5} %   ({s['comparables']} plans comparables)")
        moyenne = statistics.mean(s["accord_pct"] for s in scores.values()) if scores else 0
        print(f"  {'moyenne':<18} {moyenne:>5.1f} %")
        print("\n  Un accord bas sur « mouvement » ou « angle » est normal : ce sont les")
        print("  champs les plus subjectifs. Un accord bas sur « lieu » ou « machine »")
        print("  signale une vraie régression de perception — inspectez avant de basculer.")

    args.rapport.write_text(json.dumps(rapport, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nRapport écrit : {args.rapport}")


if __name__ == "__main__":
    main()
