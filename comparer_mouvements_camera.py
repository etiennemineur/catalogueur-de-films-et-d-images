#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compare les couches de détection des mouvements caméra dans analyse/index.json.

Usage :
    .venv/bin/python comparer_mouvements_camera.py
    .venv/bin/python comparer_mouvements_camera.py --limite 30
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path


def compter(plans, cle: str):
    return collections.Counter(p.get(cle) or "non renseigné" for p in plans)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("index", nargs="?", type=Path, default=Path("analyse/index.json"))
    ap.add_argument("--limite", type=int, default=20)
    args = ap.parse_args()

    if not args.index.exists():
        raise SystemExit(f"Index absent : {args.index}")
    data = json.loads(args.index.read_text(encoding="utf-8"))
    plans = data.get("plans", [])
    print(f"Plans : {len(plans)}")
    for titre, cle in [
        ("Mesure mécanique", "mouvement_camera"),
        ("Classification VideoMAE", "mouvement_video_camera"),
        ("Fusion finale", "mouvement_camera_final"),
    ]:
        print(f"\n{titre}")
        for valeur, total in compter(plans, cle).most_common(15):
            print(f"  {valeur}: {total}")

    conflits = [p for p in plans if p.get("mouvement_camera_conflit")]
    sans_video = [p for p in plans if p.get("mouvement_camera") and not p.get("mouvement_video_camera")]
    print(f"\nConflits mécanique / VideoMAE : {len(conflits)}")
    for p in conflits[: args.limite]:
        print(
            f"  {p.get('titre')} #{p.get('n')} {p.get('tc')} — "
            f"mécanique={p.get('mouvement_camera') or 'n/a'} · "
            f"VideoMAE={p.get('mouvement_video_camera') or 'n/a'} · "
            f"final={p.get('mouvement_camera_final') or 'n/a'}"
        )
    print(f"\nPlans mécaniques sans classification VideoMAE : {len(sans_video)}")


if __name__ == "__main__":
    main()
