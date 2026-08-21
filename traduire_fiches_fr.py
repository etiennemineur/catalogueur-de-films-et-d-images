#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Francise pitch, synopsis, résumé narratif et légende d’affiche des fiches films locales.

- garde les titres de films, noms propres et noms de personnages
- met à jour `films_fiches.json`
- met à jour aussi `analyse/<film>/fiche.json` quand le fichier local existe
- utilise un modèle local Ollama
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import unicodedata
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
CATALOGUE = ROOT / "films_fiches.json"
ANALYSE = ROOT / "analyse"
CHAMPS = ("pitch", "synopsis", "scenario", "poster_legende")
STOP_EN = {
    "the", "and", "with", "from", "into", "after", "before", "while", "about",
    "their", "there", "where", "which", "that", "this", "these", "those", "his",
    "her", "its", "film", "story", "crew", "mission", "planet", "space", "american",
    "directed", "written", "starring", "returns", "discovers", "becomes",
}
STOP_FR = {
    "le", "la", "les", "des", "une", "un", "dans", "avec", "pour", "par", "sur",
    "film", "histoire", "mission", "équipage", "planète", "espace", "américain",
    "réalisé", "écrit", "mettant", "retourne", "découvre", "devient", "alors",
    "après", "avant", "pendant", "qui", "que", "dont", "son", "sa", "ses",
}
NOTE_FR = "Champs narratifs francisés localement pour l’affichage des fiches, titres originaux conservés."


def slug(texte: str) -> str:
    t = unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "-", t).strip("-").lower() or "film"


def lire_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def ecrire_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def mots(texte: str) -> list[str]:
    brut = unicodedata.normalize("NFKD", str(texte or "").lower())
    brut = "".join(ch for ch in brut if unicodedata.category(ch) != "Mn")
    return re.findall(r"[a-z']+", brut)


def semble_anglais(texte: str) -> bool:
    ws = mots(texte)
    if not ws:
        return False
    en = sum(w in STOP_EN for w in ws)
    fr = sum(w in STOP_FR for w in ws)
    if en >= 2 and en >= fr + 1:
        return True
    if not fr and en >= 1 and len(ws) > 12:
        return True
    return False


def ollama_generate(model: str, prompt: str) -> str:
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
    }).encode("utf-8")
    req = Request("http://127.0.0.1:11434/api/generate", data=body, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=300) as r:
        payload = json.loads(r.read().decode("utf-8"))
    return payload["response"]


def traduire_champs(model: str, titre_original: str, champs: dict[str, str]) -> dict[str, str]:
    prompt = (
        "Tu réécris une fiche de film entièrement en français.\n"
        "Obligation absolue : tous les champs de sortie doivent être en français.\n"
        "Conserve les titres de films, noms propres et noms de personnages.\n"
        "Ne résume pas. Ne coupe pas. Ne commente pas.\n"
        "Si un champ d'entrée est déjà en français, recopie-le en français naturel.\n"
        "Réponds uniquement en JSON valide avec exactement les mêmes clés que l'entrée.\n"
        f"Titre original du film : {titre_original or ''}\n"
        f"Entrée : {json.dumps(champs, ensure_ascii=False)}"
    )
    raw = ollama_generate(model, prompt)
    data = json.loads(raw)
    return {k: str(data.get(k) or "").strip() for k in champs}


def maj_fiche_locale(fid: str, valeurs: dict[str, str]) -> None:
    path = ANALYSE / fid / "fiche.json"
    if not path.exists():
        return
    fiche = lire_json(path)
    fiche.update(valeurs)
    note = str(fiche.get("notes") or "").strip()
    if NOTE_FR not in note:
        fiche["notes"] = (note + " " + NOTE_FR).strip()
    path.write_text(json.dumps(fiche, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--modele", default=os.environ.get("BANC_MODELE_TRADUCTION", os.environ.get("BANC_MODELE_ANALYSE", "")))
    ap.add_argument("--limite", type=int)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--pause", type=float, default=0.15)
    args = ap.parse_args()

    catalogue = lire_json(CATALOGUE)
    items = list(catalogue.items())
    if args.limite:
        items = items[:args.limite]

    modifies = 0
    for i, (cle, fiche) in enumerate(items, 1):
        a_traduire = {}
        for champ in CHAMPS:
            valeur = str(fiche.get(champ) or "").strip()
            if not valeur:
                continue
            if args.force or semble_anglais(valeur):
                a_traduire[champ] = valeur
        if not a_traduire:
            print(f"[{i}/{len(items)}] déjà en français ou vide : {cle}")
            continue

        titre_original = str(fiche.get("titre_original") or fiche.get("titre") or "")
        try:
            traduits = traduire_champs(args.modele, titre_original, a_traduire)
        except Exception as exc:
            print(f"[{i}/{len(items)}] erreur : {cle} — {type(exc).__name__}: {exc}")
            continue

        fiche.update({k: v for k, v in traduits.items() if v})
        note = str(fiche.get("notes") or "").strip()
        if NOTE_FR not in note:
            fiche["notes"] = (note + " " + NOTE_FR).strip()
        catalogue[cle] = fiche
        fid = slug(Path(cle).stem)
        maj_fiche_locale(fid, traduits)
        modifies += 1
        print(f"[{i}/{len(items)}] traduit : {cle} · champs: {', '.join(sorted(traduits))}")
        ecrire_json(CATALOGUE, catalogue)
        time.sleep(args.pause)

    ecrire_json(CATALOGUE, catalogue)
    print(json.dumps({"films_modifies": modifies, "modele": args.modele}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
