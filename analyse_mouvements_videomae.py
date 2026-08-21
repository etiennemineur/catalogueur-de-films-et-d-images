#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyse_mouvements_videomae.py — classification vidéo des mouvements caméra.

Cette passe complète la mesure mécanique `analyse_mouvements.py` : elle extrait
une courte séquence temporelle par plan, applique un modèle VideoMAE spécialisé
mouvements caméra, puis fusionne prudemment le résultat avec la mesure OpenCV.

Exemples :

    .venv/bin/python analyse_mouvements_videomae.py analyse --dry-run --limite-plans 20
    .venv/bin/python analyse_mouvements_videomae.py analyse --film la-jetee-1962 --limite-plans 10
    .venv/bin/python analyse_mouvements_videomae.py analyse --difficiles --limite-plans 40 --index-seul
    .venv/bin/python analyse_mouvements_videomae.py --verifier-modele
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

MODELE_DEFAUT = os.environ.get("BANC_MODELE_MOUVEMENTS_VIDEO", "").strip()

LABELS_FR = {
    "arc_left": "arc gauche",
    "arc_right": "arc droite",
    "arc_cw": "arc horaire",
    "arc_ccw": "arc antihoraire",
    "dolly_in": "travelling avant",
    "dolly_out": "travelling arrière",
    "pan_left": "panoramique gauche",
    "pan_right": "panoramique droite",
    "pedestal_down": "pedestal bas",
    "pedestal_up": "pedestal haut",
    "roll_left": "rotation antihoraire",
    "roll_right": "rotation horaire",
    "static": "fixe",
    "tilt_down": "tilt bas",
    "tilt_up": "tilt haut",
    "truck_left": "travelling gauche",
    "truck_right": "travelling droite",
    "undefined": "indéterminé",
    "zoom_in": "zoom in",
    "zoom_out": "zoom out",
    "pov": "point de vue subjectif",
    "shake": "caméra portée",
    "track": "travelling de suivi",
    "tracking": "travelling de suivi",
    "side_tracking": "travelling de suivi latéral",
    "lead_tracking": "travelling de suivi frontal",
    "aerial_tracking": "travelling de suivi aérien",
    "complex_motion": "mouvement complexe",
    "no_motion": "fixe",
}

PRIORITE_LABELS = [
    "caméra portée",
    "travelling de suivi",
    "travelling de suivi latéral",
    "travelling de suivi frontal",
    "travelling de suivi aérien",
    "travelling avant",
    "travelling arrière",
    "travelling gauche",
    "travelling droite",
    "arc gauche",
    "arc droite",
    "arc horaire",
    "arc antihoraire",
    "panoramique gauche",
    "panoramique droite",
    "tilt haut",
    "tilt bas",
    "pedestal haut",
    "pedestal bas",
    "zoom in",
    "zoom out",
    "rotation horaire",
    "rotation antihoraire",
    "point de vue subjectif",
    "mouvement complexe",
    "fixe",
    "indéterminé",
]

AMBIGUITES = {
    ("zoom in", "travelling avant"),
    ("zoom out", "travelling arrière"),
    ("panoramique gauche", "travelling gauche"),
    ("panoramique droite", "travelling droite"),
    ("tilt haut", "pedestal haut"),
    ("tilt bas", "pedestal bas"),
}

COMPATIBLES = {
    ("fixe", "fixe"),
    ("zoom in", "zoom in"),
    ("zoom out", "zoom out"),
    ("panoramique gauche", "panoramique gauche"),
    ("panoramique droite", "panoramique droite"),
    ("tilt haut", "tilt haut"),
    ("tilt bas", "tilt bas"),
    ("rotation horaire", "rotation horaire"),
    ("rotation antihoraire", "rotation antihoraire"),
    ("caméra portée", "caméra portée"),
}


def normaliser_label(label: str) -> str:
    brut = str(label or "").strip().lower().replace("-", "_").replace(" ", "_")
    return LABELS_FR.get(brut, LABELS_FR.get(brut.replace("__", "_"), brut.replace("_", " ")))


def confiance(score: float) -> str:
    if score >= 0.70:
        return "sûr"
    if score >= 0.45:
        return "probable"
    return "douteux"


def choisir_device(nom: str):
    import torch

    if nom == "cpu":
        return torch.device("cpu")
    if nom == "mps":
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return torch.device("mps")
        print("MPS indisponible, repli CPU.")
        return torch.device("cpu")
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def charger_modele(args):
    import torch
    from transformers import AutoImageProcessor, AutoModelForVideoClassification

    device = choisir_device(args.device)
    print(f"Chargement VideoMAE : {args.modele} sur {device}…")
    processor = AutoImageProcessor.from_pretrained(args.modele)
    model = AutoModelForVideoClassification.from_pretrained(args.modele)
    model.to(device)
    model.eval()
    return processor, model, device, torch


def segmenter_plan(plan: dict, args) -> list[tuple[float, float]]:
    debut = float(plan.get("debut", 0))
    fin = float(plan.get("fin", debut))
    duree = max(fin - debut, 0.04)
    longueur = max(0.4, float(args.segment_secondes or duree))
    max_segments = max(1, int(args.max_segments or 1))
    if duree <= longueur * 1.35 or max_segments == 1:
        return [(debut, fin)]
    nb = min(max_segments, max(2, math.ceil(duree / longueur)))
    dernier_depart = max(debut, fin - longueur)
    departs = np.linspace(debut, dernier_depart, nb)
    segments: list[tuple[float, float]] = []
    vus = set()
    for depart in departs:
        a = round(float(depart), 3)
        b = round(min(a + longueur, fin), 3)
        if b - a < 0.35:
            continue
        cle = (a, b)
        if cle not in vus:
            vus.add(cle)
            segments.append(cle)
    return segments or [(debut, fin)]


def extraire_images(video: Path, debut: float, fin: float, dossier: Path, nb: int, largeur: int, prefixe: str) -> list[Image.Image]:
    duree = max(fin - debut, 0.04)
    positions = np.linspace(0.08, 0.92, max(2, nb))
    images: list[Image.Image] = []
    for i, pos in enumerate(positions):
        t = debut + duree * float(pos)
        cible = dossier / f"{prefixe}_{i:02d}.jpg"
        r = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{t:.3f}", "-i", str(video), "-map", "0:v:0",
                "-an", "-sn", "-dn", "-frames:v", "1",
                "-vf", f"scale={largeur}:-2", "-q:v", "5", str(cible),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if r.returncode == 0 and cible.exists():
            with Image.open(cible) as im:
                images.append(im.convert("RGB"))
    return images


def preparer_entrees(processor, images: list[Image.Image], torch):
    try:
        inputs = processor(images, return_tensors="pt")
    except TypeError:
        inputs = processor(images=images, return_tensors="pt")
    if "pixel_values" in inputs and inputs["pixel_values"].ndim == 4:
        inputs["pixel_values"] = inputs["pixel_values"].unsqueeze(0)
    return inputs


def labels_depuis_scores(scores, id2label: dict[int, str], seuil: float, top_k: int) -> list[dict[str, Any]]:
    paires = []
    for i, score in enumerate(scores):
        brut = id2label.get(i, str(i))
        paires.append({"label": brut, "label_fr": normaliser_label(brut), "score": round(float(score), 4)})
    paires.sort(key=lambda x: x["score"], reverse=True)
    retenus = [p for p in paires if p["score"] >= seuil]
    if not retenus and paires:
        retenus = [paires[0]]
    return retenus[:top_k]


def choisir_mouvement(labels: list[dict[str, Any]]) -> str:
    presents = [l["label_fr"] for l in labels]
    mouvements = [p for p in presents if p not in {"fixe", "indéterminé"}]
    if len(set(mouvements)) > 1:
        return "mouvement complexe"
    for prioritaire in PRIORITE_LABELS:
        if prioritaire in presents:
            return prioritaire
    return presents[0] if presents else "indéterminé"


def analyser_segment(video: Path, plan: dict, debut: float, fin: float, dossier: Path, args, runtime, index: int) -> dict:
    processor, model, device, torch = runtime
    images = extraire_images(video, debut, fin, dossier, args.images, args.largeur, f"{int(plan.get('n', 0)):05d}_{index:02d}")
    if len(images) < 2:
        return {
            "debut": round(debut, 3), "fin": round(fin, 3),
            "mouvement_video_camera": "indéterminé",
            "mouvement_video_confiance": "douteux",
            "labels": [],
            "erreur": "images insuffisantes",
        }
    inputs = preparer_entrees(processor, images, torch)
    inputs = {k: v.to(device) for k, v in inputs.items()}
    try:
        with torch.no_grad():
            logits = model(**inputs).logits[0]
    except Exception:
        if str(device) != "cpu":
            print("Inférence MPS impossible sur ce plan, repli CPU.")
            model.to("cpu")
            inputs = {k: v.to("cpu") for k, v in inputs.items()}
            with torch.no_grad():
                logits = model(**inputs).logits[0]
            model.to(device)
        else:
            raise
    scores = torch.sigmoid(logits).detach().cpu().numpy()
    labels = labels_depuis_scores(scores, model.config.id2label, args.seuil, args.top_k)
    score_max = max((l["score"] for l in labels), default=0.0)
    return {
        "debut": round(debut, 3),
        "fin": round(fin, 3),
        "mouvement_video_camera": choisir_mouvement(labels),
        "mouvement_video_confiance": confiance(score_max),
        "labels": labels,
    }


def fusionner_segments_video(segments: list[dict], args) -> dict:
    tous_labels: list[dict] = []
    for seg in segments:
        tous_labels.extend(seg.get("labels") or [])
    par_label: dict[str, float] = {}
    for item in tous_labels:
        label = item.get("label_fr") or "indéterminé"
        par_label[label] = max(par_label.get(label, 0.0), float(item.get("score") or 0.0))
    labels_tries = sorted(par_label.items(), key=lambda x: x[1], reverse=True)
    labels = [{"label_fr": label, "score": round(score, 4)} for label, score in labels_tries[: args.top_k]]
    mouvement_segments = [s.get("mouvement_video_camera") for s in segments if s.get("mouvement_video_camera")]
    utiles = [m for m in mouvement_segments if m not in {"", "indéterminé", "fixe"}]
    if len(set(utiles)) > 1:
        mouvement = "mouvement complexe"
    elif utiles:
        mouvement = utiles[0]
    elif mouvement_segments:
        mouvement = mouvement_segments[0]
    else:
        mouvement = "indéterminé"
    score_max = max((x["score"] for x in labels), default=0.0)
    return {
        "mouvement_video_modele": args.modele,
        "mouvement_video_camera": mouvement,
        "mouvement_video_labels": labels,
        "mouvement_video_scores": {x["label_fr"]: x["score"] for x in labels},
        "mouvement_video_segments": [
            {
                "debut": s.get("debut"),
                "fin": s.get("fin"),
                "mouvement_video_camera": s.get("mouvement_video_camera"),
                "mouvement_video_confiance": s.get("mouvement_video_confiance"),
                "labels": s.get("labels", []),
            }
            for s in segments
        ],
        "mouvement_video_confiance": confiance(score_max),
    }


def pair(a: str, b: str) -> tuple[str, str]:
    return (a or "", b or "")


def fusionner_final(plan: dict, video: dict) -> dict:
    mecanique = plan.get("mouvement_camera") or ""
    video_mvt = video.get("mouvement_video_camera") or ""
    video_conf = video.get("mouvement_video_confiance") or "douteux"
    sources = []
    if mecanique:
        sources.append("mécanique")
    if video_mvt:
        sources.append("VideoMAE")

    conflit = False
    note = "Fusion mécanique + classification vidéo."
    final = mecanique or video_mvt or "indéterminé"

    if not video_mvt or video_mvt == "indéterminé":
        final = mecanique or "indéterminé"
        note = "VideoMAE n’a pas donné de mouvement exploitable, conservation de la mesure mécanique."
    elif not mecanique or mecanique == "indéterminé":
        final = video_mvt
        note = "Pas de mesure mécanique exploitable, conservation de la classification VideoMAE."
    elif pair(mecanique, video_mvt) in COMPATIBLES:
        final = video_mvt
        note = "La mesure mécanique et VideoMAE concordent."
    elif pair(mecanique, video_mvt) in AMBIGUITES:
        conflit = True
        final = video_mvt if video_conf in {"sûr", "probable"} else mecanique
        note = f"Ambiguïté cinéma classique : mécanique={mecanique}, VideoMAE={video_mvt}."
    else:
        conflit = True
        final = video_mvt if video_conf in {"sûr", "probable"} else mecanique
        note = f"Désaccord entre mesure mécanique ({mecanique}) et VideoMAE ({video_mvt})."

    return {
        "mouvement_camera_final": final,
        "mouvement_camera_sources": sources or ["VideoMAE"],
        "mouvement_camera_conflit": conflit,
        "mouvement_camera_notes": note,
    }


def candidat(plan: dict, args) -> bool:
    if plan.get("mouvement_video_modele") and not args.refaire:
        return False
    if not args.difficiles:
        return True
    mecanique = plan.get("mouvement_camera") or ""
    confiance_meca = plan.get("mouvement_confiance") or ""
    return (
        not mecanique
        or mecanique in {"indéterminé", "mouvement complexe"}
        or confiance_meca in {"", "douteux", "probable"}
        or bool(plan.get("mouvement_camera_conflit"))
    )


def analyser_plan(video: Path, plan: dict, args, runtime) -> dict:
    with tempfile.TemporaryDirectory(prefix="mvt-videomae-") as td:
        dossier = Path(td)
        segments = [
            analyser_segment(video, plan, debut, fin, dossier, args, runtime, i)
            for i, (debut, fin) in enumerate(segmenter_plan(plan, args), 1)
        ]
    res = fusionner_segments_video(segments, args)
    res.update(fusionner_final(plan, res))
    return res


def traiter_film(plans_json: Path, args, runtime) -> dict:
    data = json.loads(plans_json.read_text(encoding="utf-8"))
    video = Path(data.get("source", ""))
    if not video.exists():
        return {"film": plans_json.parent.name, "ok": False, "message": "source vidéo absente"}
    plans = [p for p in data.get("plans", []) if candidat(p, args)]
    if args.limite_plans:
        plans = plans[: args.limite_plans]
    if args.dry_run:
        for p in plans:
            print(
                f"  candidat #{p.get('n')} {p.get('tc')} — mécanique={p.get('mouvement_camera') or 'non mesuré'} "
                f"({p.get('mouvement_confiance') or 'sans confiance'})"
            )
        return {"film": plans_json.parent.name, "ok": True, "plans_video": len(plans)}

    traites = 0
    for plan in plans:
        res = analyser_plan(video, plan, args, runtime)
        plan.update(res)
        traites += 1
        if traites % 5 == 0:
            plans_json.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"  {traites} plans classés VideoMAE…", end="\r", flush=True)
    data["mouvements_video"] = {
        "modele": args.modele,
        "plans_classes": sum(1 for p in data.get("plans", []) if p.get("mouvement_video_modele")),
    }
    plans_json.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"film": plans_json.parent.name, "ok": True, "plans_video": traites}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("racine", nargs="?", type=Path, default=Path("analyse"))
    ap.add_argument("--film", help="identifiant de film à traiter")
    ap.add_argument("--limite", type=int, help="nombre maximal de films à traiter")
    ap.add_argument("--limite-plans", type=int, help="nombre maximal de plans par film")
    ap.add_argument("--modele", default=MODELE_DEFAUT)
    ap.add_argument("--device", choices=["auto", "cpu", "mps"], default="auto")
    ap.add_argument("--images", type=int, default=16, help="images envoyées au modèle par segment")
    ap.add_argument("--largeur", type=int, default=224, help="largeur des images extraites avant processor")
    ap.add_argument("--segment-secondes", type=float, default=2.0)
    ap.add_argument("--max-segments", type=int, default=3)
    ap.add_argument("--seuil", type=float, default=0.35, help="seuil multi-label VideoMAE")
    ap.add_argument("--top-k", type=int, default=6)
    ap.add_argument("--difficiles", action="store_true", help="traiter seulement les plans mécaniques douteux, complexes ou conflictuels")
    ap.add_argument("--refaire", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--index-seul", action="store_true")
    ap.add_argument("--verifier-modele", action="store_true", help="charger le modèle puis quitter")
    args = ap.parse_args()

    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg est nécessaire.")

    if args.verifier_modele:
        charger_modele(args)
        print("Modèle VideoMAE disponible.")
        return

    fichiers = sorted(args.racine.glob("*/plans.json"))
    if args.film:
        fichiers = [p for p in fichiers if p.parent.name == args.film]
    if args.limite:
        fichiers = fichiers[: args.limite]
    if not fichiers:
        sys.exit("Aucun plans.json à traiter.")

    runtime = None if args.dry_run else charger_modele(args)
    total = 0
    for i, fichier in enumerate(fichiers, 1):
        print(f"[{i}/{len(fichiers)}] {fichier.parent.name}")
        r = traiter_film(fichier, args, runtime)
        total += int(r.get("plans_video") or 0)
        print(f"  {r.get('plans_video', 0)} plan(s) candidat(s)/classé(s)")

    if args.index_seul and not args.dry_run:
        subprocess.run([sys.executable, "analyse_plans.py", "--sortie", str(args.racine), "--index-seul"], check=False)

    print(f"\nMouvements caméra classifiés par VideoMAE : {total} plan(s).")


if __name__ == "__main__":
    main()
