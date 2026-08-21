#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyse_photos.py — catalogage local de photos par modèle de vision Ollama.

Le module est volontairement séparé de l’analyse des films : pas de plans, pas de
mouvements caméra, pas de son. Il crée un catalogue photo dans analyse/photos/ et
il est reprenable : les photos déjà analysées sont ignorées tant que le fichier,
les critères et le contexte n’ont pas changé, sauf option --refaire.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from hashlib import sha1
from pathlib import Path

from PIL import ExifTags, Image, ImageOps

from catalogueur_utils import lire_json_config, slugify, texte_court

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
MODELE = os.environ.get("BANC_MODELE_PHOTOS", os.environ.get("BANC_MODELE_ANALYSE", "")).strip()
LARGEUR_ANALYSE = 896
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".heic", ".heif"}
PROGRESSION_FICHIER = ROOT / "analyse" / "photos" / "progression.json"
CRITERES_DEFAUT = [
    "sujets et objets visibles",
    "composition et cadrage",
    "lumière et couleur",
    "lieu ou décor",
    "personnes sans identification nominative",
    "texte visible dans l’image",
    "qualité technique",
    "usage possible dans un catalogue",
]
TYPES_IMAGE = [
    "portrait", "groupe", "paysage", "architecture", "intérieur", "objet",
    "document", "œuvre graphique", "capture d’écran", "scène urbaine",
    "nature", "événement", "détail", "autre",
]
LUMIERES = [
    "naturelle", "artificielle", "diffuse", "directe", "contre-jour",
    "clair-obscur", "lumière d’écran", "nuit", "surexposée", "sous-exposée",
]
COMPOSITIONS = [
    "centrée", "symétrique", "asymétrique", "plan large", "plan moyen",
    "gros plan", "détail", "vue plongeante", "contre-plongée", "frontale",
    "diagonale", "minimaliste", "chargée",
]
CERTITUDES = ["élevée", "moyenne", "faible"]


def slug(texte: str) -> str:
    return slugify(texte, default="photo")


def lire_config(path: Path = CONFIG) -> dict:
    return lire_json_config(path)


def criteres_depuis_config(config: dict, args) -> list[str]:
    criteres = []
    if args.criteres:
        for item in args.criteres:
            criteres.extend(x.strip() for x in item.split("|") if x.strip())
    elif isinstance(config.get("photos_criteres"), list):
        criteres = [str(x).strip() for x in config.get("photos_criteres") if str(x).strip()]
    return criteres or CRITERES_DEFAUT


def contexte_depuis_config(config: dict, args) -> str:
    if args.contexte:
        return texte_court(args.contexte, 3000)
    return texte_court(config.get("photos_contexte", ""), 3000)


def dossier_depuis_config(config: dict, args) -> Path:
    if args.dossier:
        return args.dossier.expanduser()
    dossier = config.get("dossier_photos") or str(Path.home() / "Pictures")
    return Path(dossier).expanduser()


def images_source(dossier: Path) -> list[Path]:
    if not dossier.exists():
        return []
    return [
        p for p in sorted(dossier.rglob("*"))
        if p.is_file() and p.suffix.lower() in PHOTO_EXTENSIONS and not p.name.startswith(".")
    ]


def id_photo(dossier: Path, photo: Path) -> str:
    try:
        rel = photo.relative_to(dossier)
    except ValueError:
        rel = photo.name
    empreinte = sha1(str(rel).encode("utf-8", errors="ignore")).hexdigest()[:8]
    return f"{slug(Path(rel).stem)}-{empreinte}"


def nettoyer_exif_valeur(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        return ""
    if isinstance(v, tuple):
        return "/".join(str(x) for x in v)
    return str(v)


def lire_image_meta(photo: Path) -> dict:
    with Image.open(photo) as im0:
        im = ImageOps.exif_transpose(im0)
        largeur, hauteur = im.size
        exif = {}
        try:
            raw = im0.getexif()
            tags = {v: k for k, v in ExifTags.TAGS.items()}
            for nom in ("DateTimeOriginal", "DateTime", "Make", "Model", "LensModel", "ISOSpeedRatings", "FNumber", "ExposureTime", "FocalLength"):
                tag = tags.get(nom)
                if tag and raw.get(tag) is not None:
                    exif[nom] = nettoyer_exif_valeur(raw.get(tag))
        except Exception:
            pass
    st = photo.stat()
    orientation = "paysage" if largeur > hauteur else "portrait" if hauteur > largeur else "carré"
    return {
        "largeur": largeur,
        "hauteur": hauteur,
        "orientation": orientation,
        "taille_octets": st.st_size,
        "mtime": int(st.st_mtime),
        "date_photo": exif.get("DateTimeOriginal") or exif.get("DateTime") or "",
        "appareil": " ".join(x for x in [exif.get("Make", ""), exif.get("Model", "")] if x).strip(),
        "objectif": exif.get("LensModel", ""),
        "iso": exif.get("ISOSpeedRatings", ""),
        "ouverture": exif.get("FNumber", ""),
        "vitesse": exif.get("ExposureTime", ""),
        "focale": exif.get("FocalLength", ""),
    }


def sauver_image(source: Path, cible: Path, largeur: int, qualite: int = 84) -> None:
    cible.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as im0:
        im = ImageOps.exif_transpose(im0).convert("RGB")
        if im.width > largeur:
            ratio = largeur / im.width
            im = im.resize((largeur, max(1, round(im.height * ratio))), Image.Resampling.LANCZOS)
        im.save(cible, "JPEG", quality=qualite, optimize=True)


def encoder_image(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def schema_photo() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "description": {"type": "string"},
            "type_image": {"type": "string", "enum": TYPES_IMAGE},
            "sujets": {"type": "array", "items": {"type": "string"}},
            "personnes": {"type": "array", "items": {"type": "string"}},
            "objets": {"type": "array", "items": {"type": "string"}},
            "lieux": {"type": "array", "items": {"type": "string"}},
            "texte_visible": {"type": "string"},
            "lumiere": {"type": "string", "enum": LUMIERES},
            "composition": {"type": "string", "enum": COMPOSITIONS},
            "couleurs": {"type": "array", "items": {"type": "string"}},
            "ambiance": {"type": "array", "items": {"type": "string"}},
            "qualite_technique": {"type": "string"},
            "usage_catalogue": {"type": "array", "items": {"type": "string"}},
            "mots_cles": {"type": "array", "items": {"type": "string"}},
            "criteres_repondus": {"type": "array", "items": {"type": "string"}},
            "certitude": {"type": "string", "enum": CERTITUDES},
            "a_verifier": {"type": "boolean"},
            "note": {"type": "string"},
        },
        "required": ["description", "type_image", "sujets", "mots_cles", "certitude", "a_verifier"],
    }


def prompt_photo(photo: Path, meta: dict, criteres: list[str], contexte: str) -> str:
    lignes = [
        "Tu analyses une photographie pour un catalogue local.",
        "Réponds uniquement en JSON conforme au schéma.",
        "L’image est la source principale : n’invente pas ce qui n’est pas visible.",
        "Ne donne pas de noms de personnes sauf si un texte visible dans l’image les donne explicitement.",
        "Décris la photo comme une image fixe : pas de mouvements caméra, pas de timecode, pas de son.",
        f"Fichier : {photo.name}",
        f"Dimensions : {meta.get('largeur')}×{meta.get('hauteur')} · orientation {meta.get('orientation')}",
    ]
    if contexte:
        lignes.append("Contexte fourni par l’utilisateur : " + contexte)
    if criteres:
        lignes.append("Critères demandés pour ce catalogue :")
        lignes.extend(f"- {c}" for c in criteres)
    lignes.append(
        "Privilégie des mots-clés courts et filtrables en français. "
        "Si un critère ne peut pas être déduit de l’image, indique-le dans note et mets a_verifier à true."
    )
    return "\n".join(lignes)


def interroger_ollama(client, modele: str, prompt: str, image: Path, essais: int = 2) -> dict:
    schema = schema_photo()
    image_b64 = encoder_image(image)
    for tentative in range(essais):
        try:
            kwargs = dict(
                model=modele,
                format=schema,
                options={"temperature": 0.2, "num_ctx": 8192},
                messages=[{"role": "user", "content": prompt, "images": [image_b64]}],
            )
            try:
                reponse = client.chat(think=False, **kwargs)
            except TypeError:
                reponse = client.chat(**kwargs)
            return json.loads(reponse["message"]["content"])
        except json.JSONDecodeError:
            if tentative == essais - 1:
                return {}
        except Exception as exc:
            print(f"    ⚠ {exc}", file=sys.stderr)
            time.sleep(2)
    return {}


def charger_json(path: Path, defaut):
    if not path.exists():
        return defaut
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return defaut


def signature(meta: dict, criteres: list[str], contexte: str, modele: str = "") -> dict:
    return {
        "taille_octets": meta.get("taille_octets"),
        "mtime": meta.get("mtime"),
        "criteres": criteres,
        "contexte": contexte,
        "modele": modele,
    }


def complet_et_a_jour(existant: dict, sig: dict) -> bool:
    analyse = existant.get("analyse") or {}
    return bool(analyse.get("description") and existant.get("signature") == sig)


def chemin_relatif_photo(dossier: Path, photo: Path) -> str:
    try:
        return str(photo.relative_to(dossier))
    except ValueError:
        return photo.name


def record_photo_source(dossier: Path, photo: Path, sortie: Path, criteres: list[str], contexte: str, modele: str, existant: dict | None = None, creer_images: bool = True) -> dict:
    """Crée ou complète une entrée photo, avec vignette même sans analyse IA."""
    pid = id_photo(dossier, photo)
    meta = lire_image_meta(photo)
    sig = signature(meta, criteres, contexte, modele)
    record = dict(existant or {})
    if creer_images:
        vignette = sortie / "vignettes" / f"{pid}.jpg"
        image_analyse = sortie / "images_analyse" / f"{pid}.jpg"
        if not vignette.exists():
            sauver_image(photo, vignette, 640, qualite=82)
        if not image_analyse.exists():
            sauver_image(photo, image_analyse, LARGEUR_ANALYSE, qualite=88)
    record.update({
        "id": pid,
        "titre": record.get("titre") or photo.stem,
        "fichier": photo.name,
        "chemin_relatif": chemin_relatif_photo(dossier, photo),
        "source": str(photo),
        "extension": photo.suffix.lower(),
        "vignette": f"photos/vignettes/{pid}.jpg",
        "largeur": meta.get("largeur"),
        "hauteur": meta.get("hauteur"),
        "orientation": meta.get("orientation"),
        "date_photo": meta.get("date_photo", ""),
        "appareil": meta.get("appareil", ""),
        "objectif": meta.get("objectif", ""),
        "iso": meta.get("iso", ""),
        "ouverture": meta.get("ouverture", ""),
        "vitesse": meta.get("vitesse", ""),
        "focale": meta.get("focale", ""),
        "taille_octets": meta.get("taille_octets"),
        "mtime": meta.get("mtime"),
        "modele": record.get("modele") or modele,
        "largeur_analyse": record.get("largeur_analyse") or LARGEUR_ANALYSE,
        "criteres_utilises": record.get("criteres_utilises") or criteres,
        "contexte_utilise": record.get("contexte_utilise") if record.get("contexte_utilise") is not None else contexte,
        "signature": record.get("signature") or sig,
        "analyse": record.get("analyse") or {},
    })
    return record


def ecrire_progression(sortie: Path, payload: dict) -> None:
    progression = sortie / "progression.json"
    progression.parent.mkdir(parents=True, exist_ok=True)
    progression.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def synchroniser_sources(dossier: Path, sortie: Path, criteres: list[str], contexte: str, modele: str, limite: int | None = None) -> dict:
    """Met à jour photos.json/index.json et les vignettes sans appeler le modèle IA."""
    images = images_source(dossier)
    if limite:
        images = images[:limite]
    sortie.mkdir(parents=True, exist_ok=True)
    (sortie / "vignettes").mkdir(parents=True, exist_ok=True)
    (sortie / "images_analyse").mkdir(parents=True, exist_ok=True)
    data = charger_json(sortie / "photos.json", {"photos": []})
    existants = {p.get("id"): p for p in data.get("photos", []) if p.get("id")}
    photos = []
    erreurs = []
    total = len(images)
    for i, photo in enumerate(images, 1):
        pid = id_photo(dossier, photo)
        try:
            ecrire_progression(sortie, {
                "actif": False,
                "phase": "mise à jour des aperçus",
                "courant": i,
                "total": total,
                "pourcentage": round(i / total * 100) if total else 100,
                "photo": photo.name,
                "photo_id": pid,
                "modele": modele,
            })
            photos.append(record_photo_source(dossier, photo, sortie, criteres, contexte, modele, existants.get(pid), creer_images=True))
        except Exception as exc:
            erreurs.append(f"{photo}: {type(exc).__name__}: {exc}")
    sortie.joinpath("photos.json").write_text(json.dumps({
        "dossier_photos": str(dossier),
        "contexte": contexte,
        "criteres": criteres,
        "modele": modele,
        "photos": photos,
        "erreurs": erreurs[:20],
    }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    index = construire_index(sortie)
    ecrire_progression(sortie, {
        "actif": False,
        "phase": "aperçus à jour",
        "courant": total,
        "total": total,
        "pourcentage": 100 if total else 0,
        "photo": "",
        "photo_id": "",
        "modele": modele,
    })
    return index


def construire_index(sortie: Path) -> dict:
    photos_json = sortie / "photos.json"
    data = charger_json(photos_json, {"photos": []})
    photos = []
    for p in data.get("photos", []):
        analyse = p.get("analyse") or {}
        flat = {**p}
        for cle in [
            "description", "type_image", "sujets", "personnes", "objets", "lieux",
            "texte_visible", "lumiere", "composition", "couleurs", "ambiance",
            "qualite_technique", "usage_catalogue", "mots_cles", "criteres_repondus",
            "certitude", "a_verifier", "note",
        ]:
            flat[cle] = analyse.get(cle, [] if cle in {"sujets", "personnes", "objets", "lieux", "couleurs", "ambiance", "usage_catalogue", "mots_cles", "criteres_repondus"} else "")
        flat["analyse"] = analyse
        photos.append(flat)
    index = {
        "genere": time.strftime("%Y-%m-%d %H:%M"),
        "dossier_photos": data.get("dossier_photos", ""),
        "contexte": data.get("contexte", ""),
        "criteres": data.get("criteres", CRITERES_DEFAUT),
        "photos": photos,
        "vocabulaire": {
            "types_image": TYPES_IMAGE,
            "lumieres": LUMIERES,
            "compositions": COMPOSITIONS,
            "certitudes": CERTITUDES,
        },
    }
    sortie.mkdir(parents=True, exist_ok=True)
    (sortie / "index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    print(f"Index photos écrit : {len(photos)} photos")
    return index


def verifier_modele(client, modele: str) -> None:
    try:
        data = client.list()
        noms = []
        for m in data.get("models", []) if isinstance(data, dict) else getattr(data, "models", []):
            nom = m.get("name") if isinstance(m, dict) else getattr(m, "model", None) or getattr(m, "name", None)
            if nom:
                noms.append(nom)
        if noms and modele not in noms:
            print(f"⚠ modèle {modele} non listé par Ollama, tentative d’appel quand même.", file=sys.stderr)
    except Exception:
        pass


def analyser(args) -> dict:
    config = lire_config(args.config)
    dossier = dossier_depuis_config(config, args)
    sortie = args.sortie
    criteres = criteres_depuis_config(config, args)
    contexte = contexte_depuis_config(config, args)
    images = images_source(dossier)
    if args.limite:
        images = images[:args.limite]

    sortie.mkdir(parents=True, exist_ok=True)
    (sortie / "vignettes").mkdir(parents=True, exist_ok=True)
    (sortie / "images_analyse").mkdir(parents=True, exist_ok=True)

    data = charger_json(sortie / "photos.json", {"photos": []})
    existants = {p.get("id"): p for p in data.get("photos", []) if p.get("id")}
    photo_id_cible = str(getattr(args, "photo_id", "") or "").strip()
    if photo_id_cible:
        images = [photo for photo in images if id_photo(dossier, photo) == photo_id_cible]
    nouveaux = [p for p in data.get("photos", []) if photo_id_cible and p.get("id") != photo_id_cible]
    erreurs = []

    if args.verifier:
        payload = {
            "dossier_photos": str(dossier),
            "dossier_existe": dossier.exists(),
            "photos_source": len(images_source(dossier)),
            "photos_limite": len(images),
            "sortie": str(sortie),
            "modele": args.modele,
            "largeur": args.largeur,
            "criteres": criteres,
            "contexte_present": bool(contexte),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return payload

    import ollama
    client = ollama.Client()
    verifier_modele(client, args.modele)
    total = len(images)
    ecrire_progression(sortie, {
        "actif": True,
        "phase": "analyse IA photo",
        "courant": 0,
        "total": total,
        "pourcentage": 0,
        "photo": "",
        "photo_id": "",
        "modele": args.modele,
    })

    for i, photo in enumerate(images, 1):
        pid = id_photo(dossier, photo)
        try:
            meta = lire_image_meta(photo)
            sig = signature(meta, criteres, contexte, args.modele)
            deja = existants.get(pid)
            base_record = record_photo_source(dossier, photo, sortie, criteres, contexte, args.modele, deja, creer_images=True)
            ecrire_progression(sortie, {
                "actif": True,
                "phase": "analyse IA photo",
                "courant": i,
                "total": total,
                "pourcentage": round((i - 1) / total * 100) if total else 0,
                "photo": photo.name,
                "photo_id": pid,
                "modele": args.modele,
            })
            if deja and not args.refaire and complet_et_a_jour(deja, sig):
                print(f"[{i}/{len(images)}] déjà analysée : {photo.name}", flush=True)
                nouveaux.append(base_record)
                continue

            image_analyse = sortie / "images_analyse" / f"{pid}.jpg"
            sauver_image(photo, image_analyse, args.largeur, qualite=88)
            prompt = prompt_photo(photo, meta, criteres, contexte)
            debut = time.time()
            analyse = interroger_ollama(client, args.modele, prompt, image_analyse)
            record = {
                **base_record,
                "modele": args.modele,
                "largeur_analyse": args.largeur,
                "criteres_utilises": criteres,
                "contexte_utilise": contexte,
                "signature": sig,
                "analyse": analyse,
                "temps_analyse_secondes": round(time.time() - debut, 1),
                "analyse_le": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            nouveaux.append(record)
            data = {"dossier_photos": str(dossier), "contexte": contexte, "criteres": criteres, "modele": args.modele, "photos": nouveaux}
            (sortie / "photos.json").write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            drapeau = "▣" if analyse.get("description") else " "
            print(f"[{i}/{len(images)}] {drapeau} {photo.name}", flush=True)
        except Exception as exc:
            erreurs.append(f"{photo}: {type(exc).__name__}: {exc}")
            print(f"[{i}/{len(images)}] erreur : {photo.name} — {exc}", file=sys.stderr, flush=True)

    data = {"dossier_photos": str(dossier), "contexte": contexte, "criteres": criteres, "modele": args.modele, "photos": nouveaux, "erreurs": erreurs[:20]}
    (sortie / "photos.json").write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    index = construire_index(sortie)
    ecrire_progression(sortie, {
        "actif": False,
        "phase": "analyse photo terminée",
        "courant": len(images),
        "total": len(images),
        "pourcentage": 100 if images else 0,
        "photo": "",
        "photo_id": "",
        "modele": args.modele,
    })
    print(json.dumps({"photos_traitées": len(nouveaux), "erreurs": erreurs[:10], "index": len(index.get("photos", []))}, ensure_ascii=False, indent=2))
    return index


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dossier", nargs="?", type=Path)
    ap.add_argument("--sortie", type=Path, default=Path("analyse/photos"))
    ap.add_argument("--config", type=Path, default=CONFIG)
    ap.add_argument("--modele", default=MODELE)
    ap.add_argument("--largeur", type=int, default=LARGEUR_ANALYSE)
    ap.add_argument("--contexte", default="")
    ap.add_argument("--criteres", action="append", help="critères séparés par | ; sinon config.json")
    ap.add_argument("--refaire", action="store_true")
    ap.add_argument("--limite", type=int)
    ap.add_argument("--photo-id", default="", help="relance uniquement la photo dont l’identifiant correspond")
    ap.add_argument("--index-seul", action="store_true")
    ap.add_argument("--verifier", action="store_true")
    args = ap.parse_args()

    os.environ.pop("PYTHONPATH", None)
    os.environ.pop("PYTHONHOME", None)

    if args.index_seul:
        config = lire_config(args.config)
        dossier = dossier_depuis_config(config, args)
        criteres = criteres_depuis_config(config, args)
        contexte = contexte_depuis_config(config, args)
        synchroniser_sources(dossier, args.sortie, criteres, contexte, args.modele, limite=args.limite)
        return
    analyser(args)


if __name__ == "__main__":
    main()
