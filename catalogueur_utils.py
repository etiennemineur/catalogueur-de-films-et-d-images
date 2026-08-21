#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Petits utilitaires partagés du catalogueur de films et d‘images.

Ce module ne lance aucun traitement lourd et ne dépend d’aucun service externe.
Il regroupe seulement des helpers purs ou des lectures JSON tolérantes qui
étaient dupliqués entre plusieurs scripts.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any


def slugify(texte: Any, default: str = "item") -> str:
    """Retourne un identifiant ASCII stable à partir d’un texte libre."""
    normalise = unicodedata.normalize("NFKD", str(texte or ""))
    ascii_text = normalise.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    return slug or default


def lire_json_config(path: Path, default: dict | None = None) -> dict:
    """Lit un JSON de configuration, avec retour sûr si le fichier manque."""
    fallback = {} if default is None else dict(default)
    if not path.exists():
        return fallback
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback
    return data if isinstance(data, dict) else fallback


def texte_court(valeur: Any, limite: int = 1000) -> str:
    """Nettoie et tronque un texte sans couper brutalement le dernier mot."""
    texte = re.sub(r"\s+", " ", str(valeur or "")).strip()
    if len(texte) <= limite:
        return texte
    coupe = texte[:limite].rsplit(" ", 1)[0].rstrip(" .,;:")
    return (coupe or texte[:limite].rstrip(" .,;:")) + "…"


def charger_fiche_film(fichier_plans: Path, donnees: dict) -> dict:
    """Fusionne la fiche embarquée et le fichier fiche.json voisin si présent."""
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
    """Retourne les images disponibles d’un plan, vignettes puis vignette principale."""
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
