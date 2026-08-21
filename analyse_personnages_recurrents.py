#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyse_personnages_recurrents.py — détecte des personnages récurrents probables.

Cette passe est légère et locale. Elle ne relance pas de modèle de vision IA. Elle lit les
vignettes et les analyses déjà produites, tente une détection de visages avec
OpenCV, regroupe les apparitions proches, puis écrit :

- film-level `personnages_recurrents` : personnage_001, occurrences, plans,
  scènes, vignette représentative, méthode, confiance, à vérifier ;
- plan-level `personnages_recurrents` : références compactes aux personnages vus
  dans ce plan.

Important : ce script ne reconnaît pas l’identité civile ou le nom d’un acteur.
Il produit des grappes visuelles anonymes destinées à compter les personnages
principaux par fréquence visuelle. Les noms d’acteurs restent une passe séparée,
fermée et prudente.

Usage :
    .venv/bin/python analyse_personnages_recurrents.py analyse --film the-omega-man-1971 --refaire --index
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import cv2
import numpy as np

VISAGE_MIN_PLANS = 2
PROFIL_MIN_PLANS = 6
SIMILARITE_VISAGE = 0.82

STOP = {
    "aucun", "aucune", "indéterminé", "indéterminée", "age indetermine",
    "âge indéterminé", "genre indéterminé", "personne visible",
    "aucune personne visible", "adulte", "âge perçu : adulte",
}


def normaliser(texte: Any) -> str:
    txt = str(texte or "").lower().replace("’", "'")
    txt = unicodedata.normalize("NFD", txt)
    txt = "".join(c for c in txt if unicodedata.category(c) != "Mn")
    txt = re.sub(r"[^a-z0-9]+", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


def liste(valeur: Any) -> list[str]:
    if isinstance(valeur, list):
        return [str(x).strip() for x in valeur if str(x).strip()]
    if valeur in (None, ""):
        return []
    return [str(valeur).strip()]


def plan_scene_id(plan: dict[str, Any]) -> str:
    return str(plan.get("scene_id") or "").strip()


def analyse(plan: dict[str, Any]) -> dict[str, Any]:
    return plan.get("analyse") or {}


def diegetique(plan: dict[str, Any]) -> dict[str, Any]:
    return ((analyse(plan).get("analyse_detaillee") or {}).get("description_diegetique") or {})


def presences(plan: dict[str, Any]) -> dict[str, Any]:
    return plan.get("presences") or analyse(plan).get("presences") or {}


def texte_plan(plan: dict[str, Any]) -> str:
    a = analyse(plan)
    d = diegetique(plan)
    p = presences(plan)
    morceaux = [
        a.get("description", ""),
        " ".join(liste(a.get("mots_cles"))),
        " ".join(liste(d.get("personnages_sujets"))),
        " ".join(liste(d.get("attitudes_expressions"))),
        " ".join(liste(p.get("genres_personnes"))),
        " ".join(liste(p.get("ages_personnes"))),
        " ".join(liste(p.get("carnations_apparentes"))),
        " ".join(liste(p.get("apparences_ethniques"))),
    ]
    return " ".join(x for x in morceaux if x)


def libelle_profil(plan: dict[str, Any]) -> str:
    p = presences(plan)
    genres = [x for x in liste(p.get("genres_personnes")) if normaliser(x) not in STOP]
    ages = [x for x in liste(p.get("ages_personnes")) if normaliser(x) not in STOP]
    carnations = [x for x in liste(p.get("carnations_apparentes")) if normaliser(x) not in {"non visible", "indeterminee", "indetermine"}]
    apparences = [x for x in liste(p.get("apparences_ethniques")) if normaliser(x) not in {"indeterminee", "indetermine"}]
    d = diegetique(plan)
    sujets = [x for x in liste(d.get("personnages_sujets")) if normaliser(x) not in STOP]
    attitudes = [x for x in liste(d.get("attitudes_expressions")) if normaliser(x) not in STOP]

    elements = []
    if genres:
        elements.append("/".join(genres[:2]))
    if ages:
        elements.append("/".join(ages[:2]))
    if carnations:
        elements.append("carnation " + "/".join(carnations[:2]))
    if apparences:
        elements.append("apparence à vérifier " + "/".join(apparences[:2]))
    for source in (sujets, attitudes):
        for item in source[:2]:
            n = normaliser(item)
            if n and n not in STOP and len(n) > 4:
                elements.append(item)
                break
    return " · ".join(elements[:5]) or "personnage visible récurrent probable"


def profil_key(plan: dict[str, Any]) -> str:
    p = presences(plan)
    genres = sorted(normaliser(x) for x in liste(p.get("genres_personnes")) if normaliser(x) and normaliser(x) != "genre indetermine")
    ages = sorted(normaliser(x) for x in liste(p.get("ages_personnes")) if normaliser(x) and normaliser(x) != "age indetermine")
    carnations = sorted(normaliser(x) for x in liste(p.get("carnations_apparentes")) if normaliser(x) not in {"", "non visible", "indeterminee", "indetermine"})
    apparences = sorted(normaliser(x) for x in liste(p.get("apparences_ethniques")) if normaliser(x) not in {"", "indeterminee", "indetermine"})
    base = ["+".join(genres[:2]), "+".join(ages[:2]), "+".join(carnations[:2]), "+".join(apparences[:2])]
    return "|".join(base)


def image_path(racine: Path, plan: dict[str, Any]) -> Path | None:
    rels = liste(plan.get("vignettes")) or liste(plan.get("vignette"))
    # privilégie l’image centrale du plan, souvent la moins proche de la coupe
    if len(rels) >= 2:
        rels = [rels[len(rels) // 2], *rels]
    for rel in rels:
        p = racine / rel
        if p.exists():
            return p
    return None


def face_cascade() -> cv2.CascadeClassifier | None:
    path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    if not path.exists():
        return None
    cascade = cv2.CascadeClassifier(str(path))
    return None if cascade.empty() else cascade


def detecter_visages(path: Path, cascade: cv2.CascadeClassifier) -> list[tuple[tuple[int, int, int, int], np.ndarray]]:
    image = cv2.imread(str(path))
    if image is None:
        return []
    h, w = image.shape[:2]
    gris = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gris = cv2.equalizeHist(gris)
    min_size = max(24, min(w, h) // 14)
    faces = cascade.detectMultiScale(gris, scaleFactor=1.08, minNeighbors=4, minSize=(min_size, min_size))
    sorties = []
    for (x, y, fw, fh) in faces[:4]:
        if fw * fh < 900:
            continue
        pad = int(max(fw, fh) * 0.18)
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(w, x + fw + pad), min(h, y + fh + pad)
        crop = image[y0:y1, x0:x1]
        sorties.append(((int(x), int(y), int(fw), int(fh)), embedding_visage(crop)))
    return sorties


def embedding_visage(crop: np.ndarray) -> np.ndarray:
    crop = cv2.resize(crop, (64, 64), interpolation=cv2.INTER_AREA)
    gris = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gris = cv2.equalizeHist(gris).astype("float32") / 255.0
    petit = cv2.resize(gris, (16, 16), interpolation=cv2.INTER_AREA).reshape(-1)
    hist_b = cv2.calcHist([crop], [0], None, [16], [0, 256]).reshape(-1)
    hist_g = cv2.calcHist([crop], [1], None, [16], [0, 256]).reshape(-1)
    hist_r = cv2.calcHist([crop], [2], None, [16], [0, 256]).reshape(-1)
    vec = np.concatenate([petit, hist_b, hist_g, hist_r]).astype("float32")
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def ajouter_occurrence(cluster: dict[str, Any], det: dict[str, Any]) -> None:
    cluster["detections"].append(det)
    vec = det.get("embedding")
    if vec is not None:
        n = len(cluster["detections"])
        cluster["embedding"] = (cluster["embedding"] * (n - 1) + vec) / n
        norm = np.linalg.norm(cluster["embedding"])
        if norm > 0:
            cluster["embedding"] = cluster["embedding"] / norm


def clusters_visages(racine: Path, plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cascade = face_cascade()
    if cascade is None:
        return []
    clusters: list[dict[str, Any]] = []
    for plan in plans:
        if not presences(plan).get("personnes_visibles"):
            continue
        path = image_path(racine, plan)
        if not path:
            continue
        for bbox, emb in detecter_visages(path, cascade):
            det = {
                "plan": int(plan.get("n") or 0),
                "scene_id": plan_scene_id(plan),
                "image": str(path.relative_to(racine)),
                "bbox": bbox,
                "embedding": emb,
                "profil": libelle_profil(plan),
            }
            meilleur = None
            meilleur_score = -1.0
            for cluster in clusters:
                score = cosine(cluster["embedding"], emb)
                if score > meilleur_score:
                    meilleur = cluster
                    meilleur_score = score
            if meilleur is not None and meilleur_score >= SIMILARITE_VISAGE:
                ajouter_occurrence(meilleur, det)
            else:
                clusters.append({"embedding": emb, "detections": [det], "source": "visage_opencv"})
    return clusters


def clusters_profils(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groupes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for plan in plans:
        p = presences(plan)
        if not p.get("personnes_visibles"):
            continue
        key = profil_key(plan)
        if not key.strip("|"):
            continue
        groupes[key].append({
            "plan": int(plan.get("n") or 0),
            "scene_id": plan_scene_id(plan),
            "image": str(plan.get("vignette") or ""),
            "profil": libelle_profil(plan),
        })
    clusters = []
    for key, dets in groupes.items():
        plans_uniques = {d["plan"] for d in dets}
        if len(plans_uniques) >= PROFIL_MIN_PLANS:
            clusters.append({"profil_key": key, "detections": dets, "source": "profil_textuel_structuré"})
    return clusters


def resumer_cluster(numero: int, cluster: dict[str, Any]) -> dict[str, Any]:
    detections = cluster.get("detections") or []
    plans = sorted({int(d.get("plan") or 0) for d in detections if d.get("plan")})
    scenes = sorted({str(d.get("scene_id") or "") for d in detections if d.get("scene_id")})
    profils = Counter(d.get("profil") for d in detections if d.get("profil"))
    images = [d.get("image") for d in detections if d.get("image")]
    label_base = profils.most_common(1)[0][0] if profils else "personnage récurrent probable"
    label = f"Personnage récurrent probable {numero:03d}"
    source = cluster.get("source") or "indéterminée"
    min_plans = VISAGE_MIN_PLANS if source == "visage_opencv" else PROFIL_MIN_PLANS
    confiance = "moyenne" if source == "visage_opencv" and len(plans) >= max(3, min_plans) else "faible"
    return {
        "personnage_id": f"personnage_{numero:03d}",
        "label": label,
        "profil_resume": label_base,
        "occurrences_visuelles": len(detections),
        "occurrences_plans": len(plans),
        "occurrences_scenes": len(scenes),
        "plans": plans,
        "scenes": scenes,
        "vignette": images[0] if images else "",
        "methode": source,
        "confiance": confiance,
        "a_verifier": True,
        "note": "Grappe visuelle anonyme destinée au comptage des personnages principaux ; identité et nom d’acteur non déduits.",
    }


def appliquer(donnees: dict[str, Any], racine: Path) -> list[dict[str, Any]]:
    plans = donnees.get("plans") or []
    visuels = clusters_visages(racine, plans)
    visuels = [c for c in visuels if len({d.get("plan") for d in c.get("detections", [])}) >= VISAGE_MIN_PLANS]
    profils = clusters_profils(plans)

    # Les grappes visage sont plus fiables que les profils textuels. Les profils
    # ne servent que de filet quand la détection de visage manque des personnages.
    clusters = visuels + profils
    clusters.sort(key=lambda c: len({d.get("plan") for d in c.get("detections", [])}), reverse=True)
    resumes = [resumer_cluster(i + 1, c) for i, c in enumerate(clusters[:24])]

    par_plan: dict[int, list[dict[str, Any]]] = defaultdict(list)
    resume_par_id = {r["personnage_id"]: r for r in resumes}
    for resume in resumes:
        for n in resume.get("plans") or []:
            par_plan[int(n)].append({
                "personnage_id": resume["personnage_id"],
                "label": resume["label"],
                "profil_resume": resume.get("profil_resume", ""),
                "methode": resume.get("methode", ""),
                "confiance": resume.get("confiance", ""),
                "a_verifier": True,
            })

    for plan in plans:
        plan["personnages_recurrents"] = par_plan.get(int(plan.get("n") or 0), [])

    donnees["personnages_recurrents"] = resumes
    donnees["personnages_recurrents_genere_le"] = time.strftime("%Y-%m-%d %H:%M:%S")
    donnees["personnages_recurrents_methode"] = "opencv_haar_visage_plus_profils_structures_v1"
    return resumes


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
    ap.add_argument("--film", help="slug du film à traiter")
    ap.add_argument("--refaire", action="store_true", help="regénérer même si les personnages existent déjà")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--index", action="store_true", help="reconstruire analyse/index.json après écriture")
    args = ap.parse_args()

    total = 0
    films = 0
    for fichier in fichiers_cibles(args.racine, args.film):
        donnees = json.loads(fichier.read_text(encoding="utf-8"))
        if donnees.get("personnages_recurrents") and not args.refaire:
            print(f"déjà personnages : {fichier.parent.name} — {len(donnees.get('personnages_recurrents') or [])} personnages")
            continue
        resumes = appliquer(donnees, args.racine)
        films += 1
        total += len(resumes)
        print(f"{fichier.parent.name} : {len(resumes)} personnages récurrents probables")
        for r in resumes[:10]:
            print(f"  {r['personnage_id']} · {r['occurrences_plans']} plans · {r['occurrences_scenes']} scènes · {r['profil_resume']} · {r['methode']}")
        if not args.dry_run:
            fichier.write_text(json.dumps(donnees, ensure_ascii=False, indent=1), encoding="utf-8")

    if args.index and not args.dry_run:
        try:
            from analyse_plans import construire_index
            construire_index(args.racine)
        except Exception as exc:
            print(f"index non reconstruit ({type(exc).__name__}: {exc})", file=sys.stderr)
            raise
    print(json.dumps({"films_traites": films, "personnages_recurrents": total}, ensure_ascii=False))


if __name__ == "__main__":
    main()
