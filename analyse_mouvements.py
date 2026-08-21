#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyse_mouvements.py — mesure le mouvement de caméra plan par plan.

Cette passe est locale, mécanique et reprenable. Elle ne remplace pas l’analyse
IA : elle ajoute une couche mesurée à partir de plusieurs images du plan.

Principe :
1. extraire un vrai extrait vidéo court dans chaque fenêtre du plan ;
2. en tirer une série dense d’images basse définition ;
3. suivre des points visuels stables entre images successives ;
4. estimer une transformation globale de l’image ;
5. classer le mouvement : fixe, zoom in/out, panoramique, tilt, rotation,
   caméra portée, mouvement complexe ou indéterminé ;
6. écrire les champs `mouvement_camera`, `mouvement_direction`,
   `mouvement_intensite`, `mouvement_confiance`, `mouvement_mesures`.

Exemples :

    .venv/bin/python analyse_mouvements.py analyse --dry-run --limite 20
    .venv/bin/python analyse_mouvements.py analyse --limite 50 --index-seul
    .venv/bin/python analyse_mouvements.py analyse --film la-jetee-1962 --index-seul
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

MOUVEMENTS_CAMERA = [
    "fixe",
    "zoom in",
    "zoom out",
    "panoramique gauche",
    "panoramique droite",
    "tilt haut",
    "tilt bas",
    "rotation horaire",
    "rotation antihoraire",
    "caméra portée",
    "mouvement complexe",
    "indéterminé",
]


def extraire_images(video: Path, debut: float, fin: float, dossier: Path, nb: int, largeur: int, prefixe: str, fps: float) -> list[Path]:
    duree = max(fin - debut, 0.20)
    cadence = max(1.5, float(fps or 0))
    cible_frames = max(4, int(nb or 8))
    cadence = max(cadence, cible_frames / max(duree, 0.20))
    motif = dossier / f"{prefixe}_%03d.jpg"
    r = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{debut:.3f}", "-to", f"{fin:.3f}", "-i", str(video), "-map", "0:v:0",
            "-an", "-sn", "-dn",
            "-vf", f"fps={cadence:.3f},scale={largeur}:-2",
            "-q:v", "5", str(motif),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if r.returncode != 0:
        return []
    sorties = sorted(dossier.glob(f"{prefixe}_*.jpg"))
    if len(sorties) <= cible_frames:
        return sorties
    positions = np.linspace(0, len(sorties) - 1, cible_frames)
    gardees = []
    vus = set()
    for pos in positions:
        idx = int(round(float(pos)))
        idx = min(max(idx, 0), len(sorties) - 1)
        if idx in vus:
            continue
        vus.add(idx)
        gardees.append(sorties[idx])
    return gardees or sorties


def segments_plan(plan: dict, args) -> list[tuple[float, float]]:
    """Découpe un plan long en quelques fenêtres temporelles.

    La mesure mécanique garde ainsi une petite timeline interne au plan, sans
    multiplier exagérément le coût sur tout le corpus.
    """
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


def gris(path: Path):
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    return cv2.GaussianBlur(img, (3, 3), 0)


def mesure_paire(a_path: Path, b_path: Path) -> dict | None:
    a = gris(a_path)
    b = gris(b_path)
    if a is None or b is None:
        return None
    pts = cv2.goodFeaturesToTrack(a, maxCorners=420, qualityLevel=0.01, minDistance=8, blockSize=7)
    if pts is None or len(pts) < 12:
        return None
    nxt, status, _err = cv2.calcOpticalFlowPyrLK(a, b, pts, None, winSize=(21, 21), maxLevel=3)
    if nxt is None or status is None:
        return None
    p0 = pts[status.ravel() == 1].reshape(-1, 2)
    p1 = nxt[status.ravel() == 1].reshape(-1, 2)
    if len(p0) < 12:
        return None
    mat, inliers = cv2.estimateAffinePartial2D(p0, p1, method=cv2.RANSAC, ransacReprojThreshold=3.0)
    if mat is None:
        return None
    inlier_ratio = float(inliers.mean()) if inliers is not None else 0.0
    dx, dy = float(mat[0, 2]), float(mat[1, 2])
    sx = math.hypot(float(mat[0, 0]), float(mat[1, 0]))
    sy = math.hypot(float(mat[0, 1]), float(mat[1, 1]))
    scale = (sx + sy) / 2.0
    angle = math.degrees(math.atan2(float(mat[1, 0]), float(mat[0, 0])))
    residus = p1 - cv2.transform(p0.reshape(-1, 1, 2), mat).reshape(-1, 2)
    jitter = float(np.median(np.linalg.norm(residus, axis=1))) if len(residus) else 0.0
    h, w = a.shape[:2]
    return {
        "dx": dx / max(w, 1),
        "dy": dy / max(h, 1),
        "scale_delta": scale - 1.0,
        "rotation": angle,
        "jitter": jitter / max(w, h, 1),
        "points": int(len(p0)),
        "inliers": round(inlier_ratio, 3),
    }


def mediane(valeurs: list[float]) -> float:
    if not valeurs:
        return 0.0
    return float(np.median(np.array(valeurs, dtype=float)))


def classifier(mesures: list[dict]) -> dict:
    if len(mesures) < 2:
        return resultat("indéterminé", "", "faible", "douteux", mesures)
    dx = mediane([m["dx"] for m in mesures])
    dy = mediane([m["dy"] for m in mesures])
    scale = mediane([m["scale_delta"] for m in mesures])
    rot = mediane([m["rotation"] for m in mesures])
    jitter = mediane([m["jitter"] for m in mesures])
    inliers = mediane([m["inliers"] for m in mesures])
    amp = math.hypot(dx, dy)

    intensite_score = max(abs(scale) * 18, abs(rot) / 2.5, amp * 7, jitter * 12)
    if intensite_score < 0.08:
        intensite = "faible"
    elif intensite_score < 0.22:
        intensite = "moyenne"
    else:
        intensite = "forte"

    confiance = "sûr" if inliers > 0.55 and len(mesures) >= 4 else "probable" if inliers > 0.35 else "douteux"

    if jitter > 0.012 and amp > 0.006:
        return resultat("caméra portée", "instable", intensite, confiance, mesures)
    if abs(scale) > 0.010 and abs(scale) > amp * 0.8:
        return resultat("zoom in" if scale > 0 else "zoom out", "avant" if scale > 0 else "arrière", intensite, confiance, mesures)
    if abs(rot) > 0.55 and abs(rot) > amp * 20:
        return resultat("rotation horaire" if rot > 0 else "rotation antihoraire", "rotation", intensite, confiance, mesures)
    if amp < 0.004 and abs(scale) < 0.006 and abs(rot) < 0.35:
        return resultat("fixe", "", "faible", confiance, mesures)
    if abs(dx) > abs(dy) * 1.45:
        # Si l’image globale glisse vers la gauche, la caméra panote souvent vers la droite.
        return resultat("panoramique droite" if dx < 0 else "panoramique gauche", "horizontal", intensite, confiance, mesures)
    if abs(dy) > abs(dx) * 1.45:
        return resultat("tilt bas" if dy < 0 else "tilt haut", "vertical", intensite, confiance, mesures)
    return resultat("mouvement complexe", "mixte", intensite, confiance, mesures)


def resultat(camera: str, direction: str, intensite: str, confiance: str, mesures: list[dict]) -> dict:
    return {
        "mouvement_camera": camera,
        "mouvement_direction": direction,
        "mouvement_intensite": intensite,
        "mouvement_confiance": confiance,
        "mouvement_mesures": {
            "paires": len(mesures),
            "dx_mediane": round(mediane([m["dx"] for m in mesures]), 5),
            "dy_mediane": round(mediane([m["dy"] for m in mesures]), 5),
            "scale_delta_mediane": round(mediane([m["scale_delta"] for m in mesures]), 5),
            "rotation_mediane": round(mediane([m["rotation"] for m in mesures]), 4),
            "jitter_mediane": round(mediane([m["jitter"] for m in mesures]), 5),
            "inliers_mediane": round(mediane([m["inliers"] for m in mesures]), 3),
        },
    }


def analyser_segment(video: Path, plan: dict, debut: float, fin: float, dossier: Path, args, index: int) -> dict:
    prefixe = f"{int(plan.get('n', 0)):05d}_{index:02d}"
    images = extraire_images(video, debut, fin, dossier, args.images, args.largeur, prefixe, args.fps)
    mesures = []
    for a, b in zip(images, images[1:]):
        m = mesure_paire(a, b)
        if m:
            mesures.append(m)
    res = classifier(mesures)
    res.setdefault("mouvement_mesures", {}).update({
        "source": "extrait_video",
        "fps_echantillonnage": float(args.fps),
        "images_echantillonnees": len(images),
    })
    return {
        "debut": round(debut, 3),
        "fin": round(fin, 3),
        "mouvement_camera": res.get("mouvement_camera", "indéterminé"),
        "mouvement_direction": res.get("mouvement_direction", ""),
        "mouvement_intensite": res.get("mouvement_intensite", ""),
        "mouvement_confiance": res.get("mouvement_confiance", ""),
        "mouvement_mesures": res.get("mouvement_mesures", {}),
        "_mesures_brutes": mesures,
    }


def fusionner_segments(segments: list[dict]) -> dict:
    mesures = []
    images_echantillonnees = 0
    fps_echantillonnage = None
    source_mesure = ""
    for seg in segments:
        mesures.extend(seg.get("_mesures_brutes") or [])
        meta = seg.get("mouvement_mesures") or {}
        images_echantillonnees += int(meta.get("images_echantillonnees") or 0)
        if fps_echantillonnage is None and meta.get("fps_echantillonnage") is not None:
            fps_echantillonnage = meta.get("fps_echantillonnage")
        if not source_mesure and meta.get("source"):
            source_mesure = str(meta.get("source"))
    base = classifier(mesures)
    mouvements = [s.get("mouvement_camera") or "indéterminé" for s in segments]
    utiles = [m for m in mouvements if m not in {"", "indéterminé"}]
    non_fixes = [m for m in utiles if m != "fixe"]
    distincts = sorted(set(non_fixes or utiles))
    if len(distincts) > 1:
        base.update({
            "mouvement_camera": "mouvement complexe",
            "mouvement_direction": "mixte",
            "mouvement_confiance": "probable" if len(mesures) >= 3 else "douteux",
        })
    timeline = []
    for seg in segments:
        timeline.append({
            "debut": seg.get("debut"),
            "fin": seg.get("fin"),
            "mouvement_camera": seg.get("mouvement_camera"),
            "mouvement_direction": seg.get("mouvement_direction"),
            "mouvement_intensite": seg.get("mouvement_intensite"),
            "mouvement_confiance": seg.get("mouvement_confiance"),
            "mouvement_mesures": seg.get("mouvement_mesures", {}),
        })
    base["mouvement_mecanique_timeline"] = timeline
    base.setdefault("mouvement_mesures", {})["segments"] = len(segments)
    base.setdefault("mouvement_mesures", {})["source"] = source_mesure or "extrait_video"
    base.setdefault("mouvement_mesures", {})["fps_echantillonnage"] = float(fps_echantillonnage or 0)
    base.setdefault("mouvement_mesures", {})["images_echantillonnees"] = images_echantillonnees
    base["mouvement_camera_final"] = base.get("mouvement_camera", "")
    base["mouvement_camera_sources"] = ["mécanique"]
    base["mouvement_camera_conflit"] = False
    base["mouvement_camera_notes"] = "Première fusion mécanique par suivi de points et estimation affine."
    return base


def analyser_plan(video: Path, plan: dict, args) -> dict:
    with tempfile.TemporaryDirectory(prefix="mvt-plan-") as td:
        dossier = Path(td)
        segments = [
            analyser_segment(video, plan, debut, fin, dossier, args, i)
            for i, (debut, fin) in enumerate(segments_plan(plan, args), 1)
        ]
        return fusionner_segments(segments)


def traiter_film(plans_json: Path, args) -> dict:
    data = json.loads(plans_json.read_text(encoding="utf-8"))
    video = Path(data.get("source", ""))
    if not video.exists():
        return {"film": plans_json.parent.name, "ok": False, "message": "source vidéo absente"}
    plans = data.get("plans", [])
    traites = 0
    for plan in plans:
        if args.limite_plans and traites >= args.limite_plans:
            break
        if plan.get("mouvement_camera") and not args.refaire:
            continue
        if args.dry_run:
            print(f"  candidat #{plan.get('n')} {plan.get('tc')}")
            traites += 1
            continue
        res = analyser_plan(video, plan, args)
        plan.update(res)
        traites += 1
        if traites % 10 == 0:
            plans_json.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"  {traites} plans mesurés…", end="\r", flush=True)
    if not args.dry_run:
        data["mouvements_camera"] = {"plans_mesures": sum(1 for p in plans if p.get("mouvement_camera"))}
        plans_json.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"film": plans_json.parent.name, "ok": True, "plans_mesures": traites}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("racine", nargs="?", type=Path, default=Path("analyse"))
    ap.add_argument("--film", help="identifiant de film à traiter")
    ap.add_argument("--limite", type=int, help="nombre maximal de films à traiter")
    ap.add_argument("--limite-plans", type=int, help="nombre maximal de plans par film")
    ap.add_argument("--images", type=int, default=8, help="images mesurées par plan")
    ap.add_argument("--segment-secondes", type=float, default=2.0,
                    help="durée visée des fenêtres internes pour les plans longs")
    ap.add_argument("--max-segments", type=int, default=3,
                    help="nombre maximal de fenêtres mécaniques par plan")
    ap.add_argument("--largeur", type=int, default=360, help="largeur des images de mesure")
    ap.add_argument("--fps", type=float, default=6.0,
                    help="cadence d’images extraite depuis le vrai extrait vidéo de chaque fenêtre")
    ap.add_argument("--refaire", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--index-seul", action="store_true", help="reconstruire index.json après la passe")
    args = ap.parse_args()

    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg est nécessaire.")

    fichiers = sorted(args.racine.glob("*/plans.json"))
    if args.film:
        fichiers = [p for p in fichiers if p.parent.name == args.film]
    if args.limite:
        fichiers = fichiers[:args.limite]
    if not fichiers:
        sys.exit("Aucun plans.json à traiter.")

    total = 0
    for i, fichier in enumerate(fichiers, 1):
        print(f"[{i}/{len(fichiers)}] {fichier.parent.name}")
        r = traiter_film(fichier, args)
        total += int(r.get("plans_mesures") or 0)
        print(f"  {r.get('plans_mesures', 0)} plan(s) traités")

    if args.index_seul and not args.dry_run:
        subprocess.run([sys.executable, "analyse_plans.py", "--sortie", str(args.racine), "--index-seul"], check=False)

    print(f"\nMouvements caméra mesurés : {total} plan(s).")


if __name__ == "__main__":
    main()
