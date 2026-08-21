#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyse_son_dialogues.py — ajoute une couche son/dialogues aux plans déjà détectés.

Le script est local et reprenable. Il ne refait pas l’analyse image.

Ordre de travail :
1. chercher des sous-titres intégrés au film et les extraire en SRT ;
2. si demandé avec --whisper, transcrire l’audio avec la commande locale whisper ;
3. rattacher les segments de dialogue aux plans selon les timecodes ;
4. écrire les champs sonores dans analyse/<film>/plans.json.

Exemples :

    .venv/bin/python analyse_son_dialogues.py analyse --index-seul
    .venv/bin/python analyse_son_dialogues.py analyse --film la-jetee-1962
    .venv/bin/python analyse_son_dialogues.py analyse --film phase-iv-1974 --whisper --modele-whisper base
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

EXTENSIONS_SOUS_TITRES = {"subrip", "ass", "ssa", "webvtt", "mov_text"}
AUDIO_STREAM_OVERRIDES = {
    "alphaville-1965": 2,
}
LANGUE_AUDIO_OVERRIDES = {
    "alphaville-1965": "fr",
}


def slug(texte: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(texte or "").lower()).strip("-") or "film"


def secondes_depuis_srt(tc: str) -> float:
    h, m, reste = tc.strip().replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(reste)


def nettoyer_dialogue(texte: str) -> str:
    texte = re.sub(r"<[^>]+>", " ", texte)
    texte = re.sub(r"\{\\[^}]+\}", " ", texte)
    texte = re.sub(r"\s+", " ", texte)
    return texte.strip()


def lire_srt(path: Path) -> list[dict]:
    if not path.exists():
        return []
    brut = path.read_text(encoding="utf-8", errors="ignore").replace("\r\n", "\n")
    blocs = re.split(r"\n\s*\n", brut.strip())
    segments = []
    for bloc in blocs:
        lignes = [x.strip() for x in bloc.splitlines() if x.strip()]
        if not lignes:
            continue
        ligne_temps = next((x for x in lignes if "-->" in x), "")
        if not ligne_temps:
            continue
        try:
            debut_txt, fin_txt = [x.strip().split()[0] for x in ligne_temps.split("-->", 1)]
            debut, fin = secondes_depuis_srt(debut_txt), secondes_depuis_srt(fin_txt)
        except Exception:
            continue
        idx = lignes.index(ligne_temps)
        texte = nettoyer_dialogue(" ".join(lignes[idx + 1:]))
        if texte:
            segments.append({"debut": round(debut, 3), "fin": round(fin, 3), "texte": texte})
    return segments


def ffprobe_json(video: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(video)],
        text=True,
        capture_output=True,
        check=False,
    )
    if r.returncode != 0:
        return {}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}


def identifiant_video(video: Path) -> str:
    return slug(video.stem)


def premier_sous_titre(video: Path) -> int | None:
    data = ffprobe_json(video)
    candidats = []
    for s in data.get("streams", []):
        if s.get("codec_type") != "subtitle":
            continue
        codec = s.get("codec_name") or ""
        if codec in EXTENSIONS_SOUS_TITRES or codec:
            langue = (s.get("tags") or {}).get("language", "").lower()
            candidats.append((int(s.get("index", 0)), codec, langue))
    if not candidats:
        return None
    priorites = ["fre", "fra", "fr", "eng", "en"]
    for pref in priorites:
        for idx, _codec, langue in candidats:
            if langue == pref:
                return idx
    return candidats[0][0]


def piste_audio_whisper(video: Path) -> int:
    identifiant = identifiant_video(video)
    if identifiant in AUDIO_STREAM_OVERRIDES:
        return int(AUDIO_STREAM_OVERRIDES[identifiant])
    data = ffprobe_json(video)
    candidats = []
    for s in data.get("streams", []):
        if s.get("codec_type") != "audio":
            continue
        idx = int(s.get("index", 0))
        langue = (s.get("tags") or {}).get("language", "").lower()
        candidats.append((idx, langue))
    if not candidats:
        return 0
    for pref in ["fre", "fra", "fr", "eng", "en"]:
        for idx, langue in candidats:
            if langue == pref:
                return idx
    return candidats[0][0]


def langue_whisper_pour_video(video: Path, langue_par_defaut: str) -> str:
    return LANGUE_AUDIO_OVERRIDES.get(identifiant_video(video), langue_par_defaut)


def extraire_sous_titres(video: Path, sortie: Path, refaire: bool = False) -> bool:
    if sortie.exists() and not refaire:
        return True
    idx = premier_sous_titre(video)
    if idx is None:
        return False
    sortie.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
         "-map", f"0:{idx}", str(sortie)],
        text=True,
        capture_output=True,
        check=False,
    )
    return r.returncode == 0 and sortie.exists()


def transcrire_whisper(video: Path, sortie: Path, modele: str, langue: str, refaire: bool = False) -> bool:
    if sortie.exists() and not refaire:
        return True
    if shutil.which("whisper") is None:
        print("whisper est introuvable. Installez ou activez Whisper local.", file=sys.stderr)
        return False
    sortie.parent.mkdir(parents=True, exist_ok=True)
    audio = sortie.with_suffix(".wav")
    if not audio.exists() or refaire:
        piste_audio = piste_audio_whisper(video)
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
             "-map", f"0:{piste_audio}", "-vn", "-ac", "1", "-ar", "16000", str(audio)],
            text=True,
            capture_output=True,
            check=False,
        )
        if r.returncode != 0 or not audio.exists():
            print(f"Impossible d’extraire l’audio : {video.name}", file=sys.stderr)
            return False
    commande = [
        "whisper", str(audio), "--model", modele,
        "--task", "transcribe", "--output_format", "srt", "--output_dir", str(sortie.parent),
    ]
    langue = langue_whisper_pour_video(video, langue)
    if langue and langue.lower() not in {"auto", "detecter", "détecter"}:
        commande += ["--language", langue]
    r = subprocess.run(
        commande,
        text=True,
        capture_output=True,
        check=False,
    )
    genere = sortie.parent / (audio.stem + ".srt")
    if genere.exists() and genere != sortie:
        genere.replace(sortie)
    return r.returncode == 0 and sortie.exists()


def chevauche(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def rattacher_aux_plans(plans: list[dict], segments: list[dict], source: str) -> int:
    nb = 0
    curseur = 0
    for plan in plans:
        debut, fin = float(plan.get("debut", 0)), float(plan.get("fin", 0))
        dialogues = []
        while curseur < len(segments) and segments[curseur]["fin"] < debut - 0.2:
            curseur += 1
        j = curseur
        while j < len(segments) and segments[j]["debut"] <= fin + 0.2:
            seg = segments[j]
            if chevauche(debut, fin, seg["debut"], seg["fin"]) > 0.05:
                dialogues.append(seg)
            j += 1
        plan["dialogues"] = dialogues
        plan["dialogue"] = bool(dialogues)
        plan["dialogue_texte"] = " ".join(d["texte"] for d in dialogues).strip()
        plan["dialogue_source"] = source if dialogues else ""
        if dialogues:
            nb += 1
    return nb


def traiter_film(plans_json: Path, args) -> dict:
    data = json.loads(plans_json.read_text(encoding="utf-8"))
    video = Path(data.get("source", ""))
    if not video.exists():
        return {"film": plans_json.parent.name, "ok": False, "message": "source vidéo absente"}

    srt_dir = plans_json.parent / "dialogues"
    srt_sous_titres = srt_dir / "sous_titres.srt"
    srt_whisper = srt_dir / "whisper.srt"

    source = ""
    segments = []
    if extraire_sous_titres(video, srt_sous_titres, args.refaire):
        segments = lire_srt(srt_sous_titres)
        source = "sous-titres intégrés"

    if not segments and args.whisper:
        if transcrire_whisper(video, srt_whisper, args.modele_whisper, args.langue, args.refaire):
            segments = lire_srt(srt_whisper)
            source = f"whisper local ({args.modele_whisper})"

    if not segments:
        for plan in data.get("plans", []):
            plan.setdefault("dialogues", [])
            plan.setdefault("dialogue", False)
            plan.setdefault("dialogue_texte", "")
            plan.setdefault("dialogue_source", "")
        data["dialogues"] = {
            "source": "aucun dialogue indexé",
            "segments": 0,
            "plans_dialogues": 0,
            "genere": time.strftime("%Y-%m-%d %H:%M"),
        }
        plans_json.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"film": plans_json.parent.name, "ok": True, "segments": 0, "plans_dialogues": 0, "source": "aucun dialogue indexé"}

    nb = rattacher_aux_plans(data.get("plans", []), segments, source)
    data["dialogues"] = {
        "source": source,
        "segments": len(segments),
        "plans_dialogues": nb,
        "genere": time.strftime("%Y-%m-%d %H:%M"),
    }
    plans_json.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"film": plans_json.parent.name, "ok": True, "segments": len(segments), "plans_dialogues": nb, "source": source}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("racine", nargs="?", type=Path, default=Path("analyse"))
    ap.add_argument("--film", help="identifiant de film à traiter")
    ap.add_argument("--limite", type=int, help="nombre maximal de films à traiter")
    ap.add_argument("--whisper", action="store_true", help="utiliser Whisper local si aucun sous-titre n’est trouvé")
    ap.add_argument("--modele-whisper", default="base", help="tiny, base, small, medium…")
    ap.add_argument("--langue", default="auto", help="langue Whisper, ou auto pour détection automatique")
    ap.add_argument("--refaire", action="store_true")
    ap.add_argument("--index-seul", action="store_true", help="reconstruire index.json après la passe")
    args = ap.parse_args()

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        sys.exit("ffmpeg/ffprobe sont nécessaires.")

    fichiers = sorted(args.racine.glob("*/plans.json"))
    if args.film:
        fichiers = [p for p in fichiers if p.parent.name == args.film]
    if args.limite:
        fichiers = fichiers[:args.limite]
    if not fichiers:
        sys.exit("Aucun plans.json à traiter.")

    total_segments = 0
    total_plans = 0
    for i, fichier in enumerate(fichiers, 1):
        r = traiter_film(fichier, args)
        total_segments += int(r.get("segments") or 0)
        total_plans += int(r.get("plans_dialogues") or 0)
        print(f"[{i}/{len(fichiers)}] {r['film']} — {r.get('source')} — {r.get('plans_dialogues', 0)} plans avec dialogue")

    if args.index_seul:
        subprocess.run([sys.executable, "analyse_plans.py", "--sortie", str(args.racine), "--index-seul"], check=False)

    print(f"\nDialogues indexés : {total_segments} segments, {total_plans} plans.")


if __name__ == "__main__":
    main()
