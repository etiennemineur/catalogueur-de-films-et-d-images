#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small shared utilities for the “catalogueur de films et d‘images” project.

This module does not start heavy processing and does not depend on any external
service. It only groups pure helpers and tolerant JSON readers that used to be
duplicated across several scripts.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any


def slugify(texte: Any, default: str = "item") -> str:
    """Return a stable ASCII identifier from free text."""
    normalise = unicodedata.normalize("NFKD", str(texte or ""))
    ascii_text = normalise.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    return slug or default


def lire_json_config(path: Path, default: dict | None = None) -> dict:
    """Read a JSON configuration file, safely falling back if it is missing."""
    fallback = {} if default is None else dict(default)
    if not path.exists():
        return fallback
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback
    return data if isinstance(data, dict) else fallback


def texte_court(valeur: Any, limite: int = 1000) -> str:
    """Clean and shorten text without cutting the last word abruptly."""
    texte = re.sub(r"\s+", " ", str(valeur or "")).strip()
    if len(texte) <= limite:
        return texte
    coupe = texte[:limite].rsplit(" ", 1)[0].rstrip(" .,;:")
    return (coupe or texte[:limite].rstrip(" .,;:")) + "…"


def charger_fiche_film(fichier_plans: Path, donnees: dict) -> dict:
    """Merge the embedded film record with a neighbouring fiche.json if present."""
    fiche = dict(donnees.get("fiche") or {})
    fichier_fiche = fichier_plans.with_name("fiche.json")
    if fichier_fiche.exists():
        try:
            contenu = json.loads(fichier_fiche.read_text(encoding="utf-8"))
            if isinstance(contenu, dict):
                fiche.update(contenu)
        except Exception:
            return fiche
    return fiche


def chemins_images_plan(racine: Path, plan: dict, images: int) -> list[Path]:
    """Return available images for a shot: thumbnails first, then main thumbnail."""
    chemins: list[Path] = []
    for rel in (plan.get("vignettes") or [])[:images]:
        chemin = racine / rel
        if chemin.exists():
            chemins.append(chemin)
    if not chemins and plan.get("vignette"):
        chemin = racine / plan["vignette"]
        if chemin.exists():
            chemins.append(chemin)
    return chemins
