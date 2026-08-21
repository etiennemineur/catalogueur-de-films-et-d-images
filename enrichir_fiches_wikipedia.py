#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Complète les fiches films locales depuis les infobox de Wikipédia anglais.

Objectif : remplir surtout réalisateur, scénaristes et quelques crédits simples
sans relancer l’analyse image et sans écraser les valeurs déjà saisies.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
ANALYSE = ROOT / "analyse"
CATALOGUE = ROOT / "films_fiches.json"
CONFIG = ROOT / "config.json"
WIKI_API = "https://en.wikipedia.org/w/api.php"
EXTENSIONS = {".mkv", ".mp4", ".mov", ".avi", ".m4v", ".webm", ".mpg", ".ts"}

PAGE_OVERRIDES = {
    "2001 A Space Odyssey (1968).mkv": "2001: A Space Odyssey",
    "A Clockwork Orange (1971).mkv": "A Clockwork Orange (film)",
    "Alien (1979).mkv": "Alien (film)",
    "Barbarella (1968).mkv": "Barbarella (film)",
    "Capricorn One (1977).mkv": "Capricorn One",
    "Close Encounters of the Third Kind (1977).mkv": "Close Encounters of the Third Kind",
    "Colossus The Forbin Project (1970).mkv": "Colossus: The Forbin Project",
    "Dark Star (1974).mp4": "Dark Star (film)",
    "Death Race 2000 (1975).mp4": "Death Race 2000",
    "Demon Seed (1977).mkv": "Demon Seed",
    "Eolomea (1972).mkv": "Eolomea",
    "Fantastic Voyage (1966).mkv": "Fantastic Voyage",
    "Futureworld (1976).mkv": "Futureworld",
    "Ikarie XB-1 (1963).mkv": "Ikarie XB-1",
    "Im Staub der Sterne (1976).mkv": "In the Dust of the Stars",
    "Je T'aime Je T'aime (1968).mkv": "Je t'aime, je t'aime",
    "Journey to the Far Side of the Sun (1969).mkv": "Doppelgänger (1969 film)",
    "Jubilee (1978).mkv": "Jubilee (1978 film)",
    "La Jetée (1962).mkv": "La Jetée",
    "Logans Run (1976).mkv": "Logan's Run (film)",
    "Mad Max (1979).mkv": "Mad Max (film)",
    "Marooned (1969).mp4": "Marooned (1969 film)",
    "Morel's Invention (1974).mkv": "Morel's Invention (film)",
    "On The Silver Globe (1988).mp4": "On the Silver Globe (film)",
    "Phase IV (1974).mkv": "Phase IV (1974 film)",
    "Planet Of The Apes (1968).mp4": "Planet of the Apes (1968 film)",
    "Rollerball (1975).mp4": "Rollerball (1975 film)",
    "Seconds (1966).mkv": "Seconds (1966 film)",
    "Silent Running (1972).mkv": "Silent Running",
    "Solaris (1971).mp4": "Solaris (1972 film)",
    "Stalker (1979).mkv": "Stalker (1979 film)",
    "Star Trek (1979).mkv": "Star Trek: The Motion Picture",
    "THX 1138 (1971).mkv": "THX 1138",
    "Welt am Draht (1973) Part.1.avi": "World on a Wire",
    "Welt am Draht (1973) Part.2.avi": "World on a Wire",
    "The Andromeda Strain (1971).mp4": "The Andromeda Strain (film)",
    "The Clonus Horror (1979).mkv": "Parts: The Clonus Horror",
    "The Man Who Fell To Earth (1976).mp4": "The Man Who Fell to Earth",
    "The Omega Man (1971).mp4": "The Omega Man",
    "Tron (1982).mp4": "Tron",
    "Westworld (1973).mp4": "Westworld (film)",
    "Zardoz (1974).mkv": "Zardoz",
}

MANUAL_FICHE_OVERRIDES = {
    "Quest (1984).mp4": {
        "titre": "Quest",
        "titre_original": "Quest",
        "annee": 1984,
        "date_sortie": "avril 1984 (États-Unis)",
        "pays": "États-Unis",
        "langue": "anglais",
        "pitch": "Un enfant n'a qu'une durée de vie de 8 jours pour accomplir un voyage mystérieux entre la société troglodyte dans laquelle il est né et une dernière porte d'entrée, objet du folklore.",
        "synopsis": "Un enfant n'a qu'une durée de vie de 8 jours pour accomplir un voyage mystérieux entre la société troglodyte dans laquelle il est né et une dernière porte d'entrée, objet du folklore. Les jeux d'apprentissage abstraits enseignés à l'enfant ressemblent étrangement à des tâches gigantesques et à des obstacles qui l'entravent dans son cheminement.",
        "scenario": "Un enfant n'a qu'une durée de vie de 8 jours pour accomplir un voyage mystérieux entre la société troglodyte dans laquelle il est né et une dernière porte d'entrée, objet du folklore. Les jeux d'apprentissage abstraits enseignés à l'enfant ressemblent étrangement à des tâches gigantesques et à des obstacles qui l'entravent dans son cheminement.",
        "poster_url": "https://m.media-amazon.com/images/M/MV5BNDFjZTc4ZDUtMjZmZC00ZjAyLWI0MDQtOTRmNTBiZDBmYWFkXkEyXkFqcGc@._V1_QL75_UY281_CR8,0,190,281_.jpg",
        "poster_fichier": "",
        "poster_legende": "Affiche IMDb de Quest",
        "realisateur": "Elaine Bass, Saul Bass",
        "scenaristes": ["Ray Bradbury"],
        "acteurs": [
            "John Abbott",
            "Noah Hathaway",
            "David Comfort",
            "Michael Mancini",
            "Damian Cavalieri",
            "Jay W. MacIntosh",
            "Bill Erwin",
            "Les Tremayne",
            "Sam Fontana",
            "Michael Wagner",
        ],
        "societes_production": ["M. Okada International Association"],
        "ratio": "1.85 : 1",
        "couleur": "couleur",
        "genres": ["Aventure", "Court-métrage", "Mystère", "Science-fiction"],
        "sources": ["https://www.imdb.com/fr/title/tt0086162/"],
        "notes": "Fiche corrigée manuellement à partir d’IMDb (tt0086162) après une confusion dans l’enrichissement automatique.",
    },
    "Welt am Draht (1973) Part.1.avi": {
        "titre": "Welt am Draht (1973) Part.1",
        "titre_original": "Welt am Draht",
        "realisateur": "Rainer Werner Fassbinder",
        "annee": 1973,
        "date_sortie": "1973",
        "pays": "Allemagne de l’Ouest",
        "langue": "allemand",
        "pitch": "Dans un institut de cybernétique, Fred Stiller reprend un programme de simulation après la mort suspecte de son concepteur et découvre peu à peu que le monde qu’il habite pourrait lui aussi n’être qu’une simulation.",
        "synopsis": "Quand le responsable d’un gigantesque simulateur social meurt dans des circonstances troubles, l’ingénieur Fred Stiller hérite d’un système peuplé de milliers d’identités artificielles. En enquêtant sur des disparitions, des souvenirs effacés et des incohérences du réel, il comprend que la frontière entre simulation et réalité est plus instable qu’il ne l’imaginait.",
        "poster_url": "https://en.wikipedia.org/wiki/Special:FilePath/Welt_Am_Draht_poster.jpg",
        "poster_fichier": "Welt Am Draht poster.jpg",
        "poster_legende": "Informations de presse (« Presseheft ») couverture avant",
        "acteurs": ["Klaus Löwitsch", "Barbara Valentin", "Mascha Rabben", "Karl-Heinz Vosgerau", "Wolfgang Schenck", "Günter Lamprecht", "Ulli Lommel"],
        "scenaristes": ["Fritz Müller-Scherz", "Rainer Werner Fassbinder"],
        "producteurs": ["Peter Märthesheimer", "Alexander Wesemann"],
        "directeur_photo": "Michael Ballhaus",
        "musique": "Gottfried Hüngsberg",
        "genres": ["science-fiction", "drame", "thriller"],
        "sources": ["https://en.wikipedia.org/wiki/World_on_a_Wire"],
        "notes": "Fiche corrigée manuellement pour éviter une confusion avec la biographie de Fassbinder lors de l’enrichissement automatique.",
    },
    "Welt am Draht (1973) Part.2.avi": {
        "titre": "Welt am Draht (1973) Part.2",
        "titre_original": "Welt am Draht",
        "realisateur": "Rainer Werner Fassbinder",
        "annee": 1973,
        "date_sortie": "1973",
        "pays": "Allemagne de l’Ouest",
        "langue": "allemand",
        "pitch": "Dans un institut de cybernétique, Fred Stiller reprend un programme de simulation après la mort suspecte de son concepteur et découvre peu à peu que le monde qu’il habite pourrait lui aussi n’être qu’une simulation.",
        "synopsis": "Quand le responsable d’un gigantesque simulateur social meurt dans des circonstances troubles, l’ingénieur Fred Stiller hérite d’un système peuplé de milliers d’identités artificielles. En enquêtant sur des disparitions, des souvenirs effacés et des incohérences du réel, il comprend que la frontière entre simulation et réalité est plus instable qu’il ne l’imaginait.",
        "poster_url": "https://en.wikipedia.org/wiki/Special:FilePath/Welt_Am_Draht_poster.jpg",
        "poster_fichier": "Welt Am Draht poster.jpg",
        "poster_legende": "Informations de presse (« Presseheft ») couverture avant",
        "acteurs": ["Klaus Löwitsch", "Barbara Valentin", "Mascha Rabben", "Karl-Heinz Vosgerau", "Wolfgang Schenck", "Günter Lamprecht", "Ulli Lommel"],
        "scenaristes": ["Fritz Müller-Scherz", "Rainer Werner Fassbinder"],
        "producteurs": ["Peter Märthesheimer", "Alexander Wesemann"],
        "directeur_photo": "Michael Ballhaus",
        "musique": "Gottfried Hüngsberg",
        "genres": ["science-fiction", "drame", "thriller"],
        "sources": ["https://en.wikipedia.org/wiki/World_on_a_Wire"],
        "notes": "Fiche corrigée manuellement pour garantir un pitch de film plutôt qu’un texte biographique sur Fassbinder.",
    }
}

FICHE_MODELE = {
    "titre": "", "titre_original": "", "realisateur": "", "annee": None,
    "date_sortie": "", "pays": "", "langue": "", "pitch": "", "synopsis": "", "scenario": "",
    "poster_url": "", "poster_fichier": "", "poster_legende": "",
    "acteurs": [], "scenaristes": [], "producteurs": [], "directeur_photo": "",
    "chef_decorateur": "", "monteur": "", "musique": "", "costumes": "",
    "effets_speciaux": [], "societes_production": [], "distributeurs": [],
    "format": "", "ratio": "", "couleur": "", "genres": [], "sources": [],
    "notes": "",
}

FIELD_MAP = {
    "director": "realisateur",
    "directed_by": "realisateur",
    "writer": "scenaristes",
    "writers": "scenaristes",
    "screenplay": "scenaristes",
    "screenplay_by": "scenaristes",
    "story": "scenaristes",
    "story_by": "scenaristes",
    "based_on": None,
    "starring": "acteurs",
    "producer": "producteurs",
    "producers": "producteurs",
    "cinematography": "directeur_photo",
    "editing": "monteur",
    "music": "musique",
}

SINGLE = {"realisateur", "directeur_photo", "monteur", "musique"}
POSTER_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".tif", ".tiff", ".svg")


def api(params: dict) -> dict:
    url = WIKI_API + "?" + urlencode({**params, "format": "json"})
    req = Request(url, headers={"User-Agent": "BancDePlansLocal/1.0"})
    with urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def slug(texte: str) -> str:
    t = unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "-", t).strip("-").lower() or "film"


def titre_annee(nom: str) -> tuple[str, int | None]:
    stem = Path(nom).stem
    m = re.search(r"\((18[9]\d|19\d{2}|20\d{2})\)", stem)
    annee = int(m.group(1)) if m else None
    titre = re.sub(r"\s*\((18[9]\d|19\d{2}|20\d{2})\)\s*$", "", stem).strip()
    return titre, annee


def dossier_films() -> Path | None:
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        dossier = data.get("dossier_films")
        return Path(dossier) if dossier else None
    except Exception:
        return None


def sources_video() -> list[Path]:
    dossier = dossier_films()
    if not dossier or not dossier.exists():
        return []
    return [p for p in sorted(dossier.rglob("*")) if p.is_file() and p.suffix.lower() in EXTENSIONS and not p.name.startswith(".")]


def page_title(video: Path) -> str:
    if video.name in PAGE_OVERRIDES:
        return PAGE_OVERRIDES[video.name]
    titre, annee = titre_annee(video.name)
    q = f"{titre} {annee} film" if annee else f"{titre} film"
    data = api({"action": "query", "list": "search", "srsearch": q, "srlimit": 1})
    hits = data.get("query", {}).get("search", [])
    return hits[0]["title"] if hits else titre


def page_wikitext(title: str) -> str:
    url = "https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_")) + "?action=raw"
    req = Request(url, headers={"User-Agent": "BancDePlansLocal/1.0"})
    with urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8")


def retirer_template_initial(texte: str, nom: str) -> str:
    """Retire le premier gros template `nom` pour nettoyer l’introduction."""
    start = texte.lower().find("{{" + nom.lower())
    if start < 0:
        return texte
    depth = 0
    i = start
    while i < len(texte) - 1:
        two = texte[i:i+2]
        if two == "{{":
            depth += 1
            i += 2
            continue
        if two == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                return texte[:start] + texte[i:]
            continue
        i += 1
    return texte


def nettoyer_wikitexte(texte: str, limite: int = 2400) -> str:
    """Nettoie assez le wikitexte pour produire un résumé lisible."""
    t = texte or ""
    t = re.sub(r"<!--.*?-->", " ", t, flags=re.S)
    t = re.sub(r"<ref[^>]*>.*?</ref>", " ", t, flags=re.I | re.S)
    t = re.sub(r"<ref[^/]*/>", " ", t, flags=re.I)
    t = re.sub(r"\{\|.*?\|\}", " ", t, flags=re.S)
    t = re.sub(r"\[\[File:[^\]]+\]\]", " ", t, flags=re.I)
    t = re.sub(r"\[\[Image:[^\]]+\]\]", " ", t, flags=re.I)
    # Supprime les templates simples puis les derniers fragments de templates.
    for _ in range(8):
        nouveau = re.sub(r"\{\{[^{}]*\}\}", " ", t)
        if nouveau == t:
            break
        t = nouveau
    t = re.sub(r"\[\[([^|\]]+)\|([^\]]+)\]\]", r"\2", t)
    t = re.sub(r"\[\[([^\]]+)\]\]", r"\1", t)
    t = re.sub(r"\[https?://[^\s\]]+\s+([^\]]+)\]", r"\1", t)
    t = re.sub(r"'{2,5}", "", t)
    t = re.sub(r"^=+.*?=+$", " ", t, flags=re.M)
    t = re.sub(r"^\s*[\*#;:].*$", " ", t, flags=re.M)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > limite:
        t = t[:limite].rsplit(" ", 1)[0].rstrip(" .,;:") + "…"
    return t


def extraire_intro(wikitext: str) -> str:
    """Premier paragraphe public, utilisé comme pitch."""
    t = retirer_template_initial(wikitext, "Infobox")
    # Retire les templates d’en-tête avant le premier paragraphe.
    for _ in range(12):
        t2 = re.sub(r"^\s*\{\{[^{}]*\}\}\s*", "", t, flags=re.S)
        if t2 == t:
            break
        t = t2
    avant_sections = re.split(r"(?m)^==", t, maxsplit=1)[0]
    paragraphes = [p.strip() for p in avant_sections.split("\n\n") if nettoyer_wikitexte(p, 80)]
    if not paragraphes:
        return ""
    return nettoyer_wikitexte(paragraphes[0], 700)


def extraire_section(wikitext: str, titres: tuple[str, ...]) -> str:
    """Extrait une section Wikipédia comme Plot, Synopsis ou Premise."""
    pattern = re.compile(r"(?im)^==+\s*(" + "|".join(re.escape(t) for t in titres) + r")\s*==+\s*$")
    m = pattern.search(wikitext)
    if not m:
        return ""
    debut = m.end()
    suivant = re.search(r"(?m)^==+\s*[^=].*?==+\s*$", wikitext[debut:])
    fin = debut + suivant.start() if suivant else len(wikitext)
    return nettoyer_wikitexte(wikitext[debut:fin], 2200)


def extraire_infobox(wikitext: str) -> dict[str, str]:
    start = wikitext.lower().find("{{infobox")
    if start < 0:
        return {}
    depth = 0
    end = None
    i = start
    while i < len(wikitext) - 1:
        two = wikitext[i:i+2]
        if two == "{{":
            depth += 1
            i += 2
            continue
        if two == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                end = i
                break
            continue
        i += 1
    if end is None:
        return {}
    box = wikitext[start:end]
    champs = {}
    cur = None
    buf = []
    depth_curly = depth_square = 0
    for line in box.splitlines()[1:]:
        if line.startswith("|") and "=" in line and depth_curly == 0 and depth_square == 0:
            if cur:
                champs[cur] = "\n".join(buf).strip()
            key, val = line[1:].split("=", 1)
            cur = key.strip().lower().replace(" ", "_")
            buf = [val.strip()]
        else:
            buf.append(line.strip())
        text = line
        depth_curly += text.count("{{") - text.count("}}")
        depth_square += text.count("[[") - text.count("]]")
    if cur:
        champs[cur] = "\n".join(buf).strip()
    return champs


def nettoyer_nom(valeur: str) -> list[str]:
    v = valeur or ""
    v = re.sub(r"<!--.*?-->", " ", v, flags=re.S)
    v = re.sub(r"<br\s*/?>", "\n", v, flags=re.I)
    v = re.sub(r"<ref[^>]*>.*?</ref>", " ", v, flags=re.I | re.S)
    v = re.sub(r"<ref[^/]*/>", " ", v, flags=re.I)
    v = re.sub(r"\{\{plainlist\|", "", v, flags=re.I)
    v = re.sub(r"\{\{ubl\|", "", v, flags=re.I)
    v = re.sub(r"\{\{unbulleted list\|", "", v, flags=re.I)
    v = re.sub(r"\{\{nowrap\|([^{}]*)\}\}", r"\1", v, flags=re.I)
    v = re.sub(r"\{\{small\|[^{}]*\}\}", " ", v, flags=re.I)
    v = re.sub(r"\{\{based on\|.*?\}\}", " ", v, flags=re.I | re.S)
    v = v.replace("{{", "").replace("}}", "")
    v = re.sub(r"\[\[([^|\]]+)\|([^\]]+)\]\]", r"\2", v)
    v = re.sub(r"\[\[([^\]]+)\]\]", r"\1", v)
    v = re.sub(r"\[https?://[^\s\]]+\s+([^\]]+)\]", r"\1", v)
    v = re.sub(r"'{2,5}", "", v)
    v = html.unescape(v)
    morceaux = re.split(r"\n|\*|\||<br>|;|\s+and\s+|\s*&\s*|\s*,\s*(?=[A-ZÉÈÀÂÎÔÛÇ])", v)
    noms = []
    for m in morceaux:
        m = re.sub(r"\([^)]*\)", "", m)
        m = re.sub(r"\s+", " ", m).strip(" -–—·,.;:\t\r\n")
        if not m:
            continue
        low = m.lower()
        if low in {"plainlist", "plainlist |", "plain list", "plain list|", "ubl", "unbulleted list", "adaptation"}:
            continue
        if any(x in low for x in ["based on", "novel", "screenplay", "story", "written by", "directed by", "produced by", "plainlist", "unbulleted list"]):
            continue
        if len(m) > 80 or re.search(r"\d", m):
            continue
        if m not in noms:
            noms.append(m)
    return noms[:12]


def nettoyer_fichier_poster(valeur: str) -> str:
    v = valeur or ""
    v = re.sub(r"<!--.*?-->", " ", v, flags=re.S)
    v = re.sub(r"<ref[^>]*>.*?</ref>", " ", v, flags=re.I | re.S)
    v = re.sub(r"<ref[^/]*/>", " ", v, flags=re.I)
    v = re.sub(r"\[\[(?:File|Image):([^\]|]+).*?\]\]", r"\1", v, flags=re.I)
    v = re.sub(r"\{\{[^{}]*\}\}", " ", v)
    v = html.unescape(v).replace("_", " ").strip()
    for morceau in re.split(r"\n|\|", v):
        m = morceau.strip().strip("[] ")
        if not m:
            continue
        m = re.sub(r"^(?:File|Image):", "", m, flags=re.I).strip()
        low = m.lower()
        if any(low.endswith(ext) for ext in POSTER_EXTENSIONS):
            return m
    m = re.search(r"([^|\n]+?\.(?:jpe?g|png|webp|gif|tiff?|svg))", v, flags=re.I)
    return m.group(1).strip() if m else ""


def poster_url_depuis_fichier(fichier: str) -> str:
    nom = (fichier or "").strip()
    if not nom:
        return ""
    return "https://en.wikipedia.org/wiki/Special:FilePath/" + quote(nom.replace(" ", "_"), safe="/:()'+,.-")


def legende_poster(valeur: str) -> str:
    return nettoyer_wikitexte(valeur or "", 320)


def fiche_depuis_wikipedia(video: Path) -> dict:
    manual = MANUAL_FICHE_OVERRIDES.get(video.name)
    if manual:
        fiche = dict(FICHE_MODELE)
        fiche.update(manual)
        return fiche

    title = page_title(video)
    wt = page_wikitext(title)
    info = extraire_infobox(wt)
    fiche = dict(FICHE_MODELE)
    titre, annee = titre_annee(video.name)
    fiche["titre"] = titre
    fiche["annee"] = annee
    fiche["sources"] = ["https://en.wikipedia.org/wiki/" + title.replace(" ", "_")]
    pitch = extraire_intro(wt)
    scenario = extraire_section(wt, ("Plot", "Synopsis", "Premise", "Plot summary"))
    if pitch:
        fiche["pitch"] = pitch
        fiche["synopsis"] = pitch
    if scenario:
        fiche["scenario"] = scenario
    poster_fichier = nettoyer_fichier_poster(info.get("image") or info.get("poster") or info.get("image_name") or "")
    if poster_fichier:
        fiche["poster_fichier"] = poster_fichier
        fiche["poster_url"] = poster_url_depuis_fichier(poster_fichier)
    poster_legende = legende_poster(info.get("caption") or info.get("image_caption") or info.get("alt") or "")
    if poster_legende:
        fiche["poster_legende"] = poster_legende
    for key, raw in info.items():
        dest = FIELD_MAP.get(key)
        if not dest:
            continue
        noms = nettoyer_nom(raw)
        if not noms:
            continue
        if dest in SINGLE:
            fiche[dest] = noms[0]
        else:
            fiche[dest] = noms
    fiche["notes"] = "Crédits, pitch et résumé de scénario complétés automatiquement depuis Wikipédia anglais."
    return fiche


def valeur_vide(v) -> bool:
    return v in (None, "", [])


def merge_prudent(existant: dict, nouveau: dict) -> dict:
    out = {**FICHE_MODELE, **existant}
    for k, v in nouveau.items():
        if k == "sources":
            src = list(out.get("sources") or [])
            for s in v or []:
                if s and s not in src:
                    src.append(s)
            out["sources"] = src
        elif k == "notes":
            if v and v not in (out.get("notes") or ""):
                out["notes"] = ((out.get("notes") or "") + " " + v).strip()
        elif not valeur_vide(v) and valeur_vide(out.get(k)):
            out[k] = v
    return out


def fiche_locale_path(video: Path) -> Path:
    return ANALYSE / slug(video.stem) / "fiche.json"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limite", type=int)
    ap.add_argument("--force", action="store_true", help="remplace aussi les champs déjà renseignés")
    args = ap.parse_args()

    catalogue = json.loads(CATALOGUE.read_text(encoding="utf-8")) if CATALOGUE.exists() else {}
    videos = sources_video()
    if args.limite:
        videos = videos[:args.limite]
    touches = 0
    erreurs = []
    for i, video in enumerate(videos, 1):
        try:
            nouveau = fiche_depuis_wikipedia(video)
        except Exception as exc:
            erreurs.append(f"{video.name}: {type(exc).__name__}: {exc}")
            print(f"[{i}/{len(videos)}] erreur : {video.name} — {exc}")
            continue
        actuel = catalogue.get(video.name) or catalogue.get(video.stem) or catalogue.get(slug(video.stem)) or {}
        fusion = {**FICHE_MODELE, **actuel, **nouveau} if args.force else merge_prudent(actuel, nouveau)
        catalogue[video.name] = fusion
        local = fiche_locale_path(video)
        if local.exists():
            existant_local = json.loads(local.read_text(encoding="utf-8"))
            fusion_local = {**FICHE_MODELE, **existant_local, **nouveau} if args.force else merge_prudent(existant_local, nouveau)
            if not args.dry_run:
                local.write_text(json.dumps(fusion_local, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        if not args.dry_run:
            CATALOGUE.write_text(json.dumps(catalogue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[{i}/{len(videos)}] {video.name} · réal: {fusion.get('realisateur') or '—'} · scénario: {', '.join(fusion.get('scenaristes') or []) or '—'}")
        touches += 1
        time.sleep(0.25)
    if not args.dry_run:
        CATALOGUE.write_text(json.dumps(catalogue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"films_traités": touches, "erreurs": erreurs[:10]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
