#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Enrichit les fiches films locales avec des crédits Wikidata.

La passe est locale côté fichiers et prudente côté écriture : elle ne remplace pas
les champs déjà renseignés, elle complète seulement les champs vides.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
ANALYSE = ROOT / "analyse"
CATALOGUE = ROOT / "films_fiches.json"
CONFIG = ROOT / "config.json"
EXTENSIONS = {".mkv", ".mp4", ".mov", ".avi", ".m4v", ".webm", ".mpg", ".ts"}
WIKIDATA_API = "https://www.wikidata.org/w/api.php"

FICHE_MODELE = {
    "titre": "",
    "titre_original": "",
    "realisateur": "",
    "annee": None,
    "date_sortie": "",
    "pays": "",
    "langue": "",
    "synopsis": "",
    "acteurs": [],
    "scenaristes": [],
    "producteurs": [],
    "directeur_photo": "",
    "chef_decorateur": "",
    "monteur": "",
    "musique": "",
    "costumes": "",
    "effets_speciaux": [],
    "societes_production": [],
    "distributeurs": [],
    "format": "",
    "ratio": "",
    "couleur": "",
    "genres": [],
    "sources": [],
    "notes": "",
}

PROPS = {
    "realisateur": "P57",
    "scenaristes": "P58",
    "producteurs": "P162",
    "directeur_photo": "P344",
    "monteur": "P1040",
    "musique": "P86",
    "acteurs": "P161",
    "genres": "P136",
    "pays": "P495",
    "langue": "P364",
    "societes_production": "P272",
    "distributeurs": "P750",
    "date_sortie": "P577",
}
SINGLE = {"realisateur", "directeur_photo", "monteur", "musique", "pays", "langue", "date_sortie"}


def api(params: dict) -> dict:
    params = {**params, "format": "json"}
    url = WIKIDATA_API + "?" + urlencode(params)
    req = Request(url, headers={"User-Agent": "BancDePlansLocal/1.0"})
    for tentative in range(5):
        try:
            with urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code != 429 or tentative == 4:
                raise
            time.sleep(2 + tentative * 2)
    return {}


def slug(texte: str) -> str:
    t = unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-zA-Z0-9]+", "-", t).strip("-").lower()
    return t or "film"


def titre_annee_depuis_nom(nom: str) -> tuple[str, int | None]:
    stem = Path(nom).stem
    m = re.search(r"\((18[9]\d|19\d{2}|20\d{2})\)", stem)
    annee = int(m.group(1)) if m else None
    titre = re.sub(r"\s*\((18[9]\d|19\d{2}|20\d{2})\)\s*$", "", stem).strip()
    return titre, annee


def dossier_films() -> Path | None:
    if CONFIG.exists():
        try:
            data = json.loads(CONFIG.read_text(encoding="utf-8"))
            if data.get("dossier_films"):
                return Path(data["dossier_films"])
        except Exception:
            return None
    return None


def fichiers_source() -> list[Path]:
    dossier = dossier_films()
    if not dossier or not dossier.exists():
        return []
    return [p for p in sorted(dossier.rglob("*")) if p.is_file() and p.suffix.lower() in EXTENSIONS and not p.name.startswith(".")]


def qids_depuis_claims(entity: dict, prop: str, limite: int | None = None) -> list[str]:
    claims = entity.get("claims", {}).get(prop, [])
    qids = []
    for c in claims:
        value = (((c.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {})
        if isinstance(value, dict) and value.get("id"):
            qids.append(value["id"])
        if limite and len(qids) >= limite:
            break
    return qids


def date_depuis_claims(entity: dict, prop: str) -> str:
    claims = entity.get("claims", {}).get(prop, [])
    dates = []
    for c in claims:
        value = (((c.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {})
        if isinstance(value, dict) and value.get("time"):
            dates.append(value["time"].lstrip("+").split("T")[0])
    return sorted(dates)[0] if dates else ""


def labels(qids: list[str], cache: dict[str, str]) -> list[str]:
    manquants = [q for q in qids if q not in cache]
    for i in range(0, len(manquants), 50):
        bloc = manquants[i:i + 50]
        if not bloc:
            continue
        data = api({
            "action": "wbgetentities",
            "ids": "|".join(bloc),
            "props": "labels",
            "languages": "fr|en",
            "languagefallback": 1,
        })
        for qid, ent in data.get("entities", {}).items():
            lab = ent.get("labels", {}).get("fr") or ent.get("labels", {}).get("en") or {}
            cache[qid] = lab.get("value", qid)
        time.sleep(0.35)
    return [cache.get(q, q) for q in qids]


def annee_entity(entity: dict) -> int | None:
    d = date_depuis_claims(entity, "P577")
    if d[:4].isdigit():
        return int(d[:4])
    return None


def chercher_film(titre: str, annee: int | None) -> tuple[str | None, dict | None]:
    essais = [titre]
    if annee:
        essais.insert(0, f"{titre} {annee} film")
    vus = set()
    for terme in essais:
        data = api({
            "action": "wbsearchentities",
            "search": terme,
            "language": "en",
            "uselang": "en",
            "limit": 8,
        })
        for item in data.get("search", []):
            qid = item.get("id")
            if not qid or qid in vus:
                continue
            vus.add(qid)
            ent = api({
                "action": "wbgetentities",
                "ids": qid,
                "props": "claims|labels|descriptions|sitelinks",
                "languages": "fr|en",
                "languagefallback": 1,
            }).get("entities", {}).get(qid)
            if not ent:
                continue
            types = qids_depuis_claims(ent, "P31")
            if "Q11424" not in types:
                descs = ent.get("descriptions", {})
                desc = " ".join((descs.get(k, {}) or {}).get("value", "") for k in ("fr", "en")).lower()
                if "film" not in desc:
                    continue
            ea = annee_entity(ent)
            if annee and ea and abs(ea - annee) > 1:
                continue
            return qid, ent

    return None, None


def fiche_depuis_entity(qid: str, ent: dict, label_cache: dict[str, str]) -> dict:
    fiche = dict(FICHE_MODELE)
    lab = ent.get("labels", {}).get("fr") or ent.get("labels", {}).get("en") or {}
    fiche["titre"] = lab.get("value", "")
    fiche["titre_original"] = (ent.get("labels", {}).get("en") or lab).get("value", "")
    date = date_depuis_claims(ent, PROPS["date_sortie"])
    fiche["date_sortie"] = date
    if date[:4].isdigit():
        fiche["annee"] = int(date[:4])
    for champ, prop in PROPS.items():
        if champ == "date_sortie":
            continue
        limite = 10 if champ == "acteurs" else None
        noms = labels(qids_depuis_claims(ent, prop, limite), label_cache)
        if not noms:
            continue
        if champ in SINGLE:
            fiche[champ] = noms[0]
        else:
            fiche[champ] = noms
    sources = [f"https://www.wikidata.org/wiki/{qid}"]
    sitelinks = ent.get("sitelinks", {})
    if "frwiki" in sitelinks:
        sources.append("https://fr.wikipedia.org/wiki/" + sitelinks["frwiki"]["title"].replace(" ", "_"))
    elif "enwiki" in sitelinks:
        sources.append("https://en.wikipedia.org/wiki/" + sitelinks["enwiki"]["title"].replace(" ", "_"))
    fiche["sources"] = sources
    fiche["notes"] = "Crédits complétés automatiquement depuis Wikidata, à vérifier si nécessaire."
    return fiche


def merge_prudent(existant: dict, nouveau: dict) -> dict:
    out = {**FICHE_MODELE, **(existant or {})}
    for k, v in nouveau.items():
        if k == "sources":
            out[k] = sorted(set((out.get(k) or []) + (v or [])))
            continue
        if k == "notes":
            if v and v not in (out.get(k) or ""):
                out[k] = ((out.get(k) or "") + " " + v).strip()
            continue
        if out.get(k) in (None, "", []):
            out[k] = v
    return out


def lire_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limite", type=int)
    args = ap.parse_args()

    catalogue = lire_json(CATALOGUE)
    label_cache: dict[str, str] = {}
    sources = fichiers_source()
    if args.limite:
        sources = sources[:args.limite]
    touches = 0
    echecs = []

    for i, video in enumerate(sources, 1):
        titre, annee = titre_annee_depuis_nom(video.name)
        fid = slug(video.stem)
        fiche_path = ANALYSE / fid / "fiche.json"
        existant = lire_json(fiche_path) or catalogue.get(video.name) or catalogue.get(fid) or {}
        if existant.get("realisateur") and existant.get("scenaristes"):
            catalogue[video.name] = merge_prudent(catalogue.get(video.name, {}), existant)
            print(f"[{i}/{len(sources)}] déjà renseigné : {video.name}")
            continue
        qid, ent = chercher_film(titre, annee)
        if not qid or not ent:
            echecs.append(video.name)
            print(f"[{i}/{len(sources)}] introuvable : {video.name}")
            continue
        nouveau = fiche_depuis_entity(qid, ent, label_cache)
        if not nouveau.get("titre"):
            nouveau["titre"] = titre
        if annee and not nouveau.get("annee"):
            nouveau["annee"] = annee
        fusion = merge_prudent(existant, nouveau)
        catalogue[video.name] = merge_prudent(catalogue.get(video.name, {}), fusion)
        if fiche_path.parent.exists():
            print(f"[{i}/{len(sources)}] fiche locale : {video.name} ← {qid} · {fusion.get('realisateur','?')}")
            if not args.dry_run:
                fiche_path.write_text(json.dumps(fusion, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        else:
            print(f"[{i}/{len(sources)}] catalogue futur : {video.name} ← {qid} · {fusion.get('realisateur','?')}")
        touches += 1
        time.sleep(0.5)

    if not args.dry_run:
        CATALOGUE.write_text(json.dumps(catalogue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nFiches complétées : {touches}")
    if echecs:
        print("Introuvables à vérifier manuellement :")
        for nom in echecs:
            print("-", nom)


if __name__ == "__main__":
    main()
