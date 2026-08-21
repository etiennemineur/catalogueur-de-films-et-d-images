#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
moteur_vision.py — couche d'abstraction du moteur de description visuelle.

Deux implémentations derrière une seule signature :

- « mlx »    : serveur mlx-vlm local (Apple MLX, Neural Accelerators du M5),
               API OpenAI-compatible, sortie contrainte par schéma JSON,
               batching continu et cache de préfixe automatique (APC) ;
- « ollama » : le chemin historique, conservé comme référence de
               non-régression. Aucune ligne de comportement n'y change.

Le module ne dépend que de la bibliothèque standard côté MLX (urllib), pour
ne rien ajouter à l'environnement de production.

Deux idées portent tout le gain :

1. Découpage du prompt. Le bloc de vocabulaire contrôlé (~1 700 tokens) est
   identique pour les 14 700 plans du corpus. Placé dans un message `system`
   AVANT les images, il devient un préfixe stable que l'APC réutilise au lieu
   de le recalculer à chaque plan.

2. Parallélisme. `decrire_lot()` envoie plusieurs plans simultanément : le
   serveur mlx-vlm les fait rejoindre le même lot de décodage, et le GPU cesse
   d'attendre ffmpeg entre deux plans.

Usage minimal, en remplacement direct de l'ancien `interroger()` :

    from moteur_vision import creer_moteur, interroger
    client = creer_moteur("mlx", modele=os.environ.get("BANC_MODELE_MLX", "<modele-mlx-local>"))
    analyse = interroger(client, modele, prompt, images, schema, tenant=fid)

Vérification rapide de l'installation :

    python3 moteur_vision.py --test --images frame1.jpg frame2.jpg frame3.jpg
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable, Sequence

# ─────────────────────────────────────────────────────────────────────────────
#  Réglages par défaut
#  Surchargeables par variables d'environnement, pour ne pas toucher au code
#  en phase de calibrage.
# ─────────────────────────────────────────────────────────────────────────────

MLX_URL = os.environ.get("MLX_VLM_URL", "http://127.0.0.1:8080")
MLX_MODELE = os.environ.get("MLX_VLM_MODELE", os.environ.get("BANC_MODELE_MLX", ""))
MLX_TIMEOUT = float(os.environ.get("MLX_VLM_TIMEOUT", "300"))
MLX_MAX_TOKENS = int(os.environ.get("MLX_VLM_MAX_TOKENS", "1800"))
MLX_TEMPERATURE = float(os.environ.get("MLX_VLM_TEMPERATURE", "0.2"))
MLX_ENCODAGE = os.environ.get("MLX_VLM_ENCODAGE", "chemin")  # « chemin » ou « base64 »

# Nombre de plans envoyés simultanément au serveur. 4 à 8 sur 128 Go ;
# au-delà, le cache KV grossit plus vite que le débit ne progresse.
CONCURRENCE = int(os.environ.get("MLX_VLM_CONCURRENCE", "6"))


# ─────────────────────────────────────────────────────────────────────────────
#  1. Découpage du prompt — la pièce qui rend le cache de préfixe utile
# ─────────────────────────────────────────────────────────────────────────────

# Marqueurs présents dans les gabarits de `analyse_plans.py`. S'ils disparaissent
# lors d'une refonte des prompts, le module bascule sur un message unique :
# le résultat reste correct, seul le cache de préfixe est perdu.
MARQUE_FILM = "Contexte du film, à utiliser avec prudence :"
MARQUE_SCENE = "Contexte de scène, à utiliser pour désambiguïser le plan :"
MARQUES_INSTRUCTIONS = ("Question unique :", "Réponds uniquement par un objet JSON")

_alerte_decoupage_emise = False


def _debut_de_ligne(texte: str, index: int) -> int:
    """Recule jusqu'au début de la ligne contenant `index`."""
    if index <= 0:
        return max(index, 0)
    coupe = texte.rfind("\n", 0, index)
    return 0 if coupe < 0 else coupe + 1


def decouper_prompt(prompt: str) -> tuple[str, str, str]:
    """Sépare le prompt en (instructions constantes, contexte film, contexte scène).

    L'ordre d'origine est : intro · film · scène · instructions. On recompose en
    plaçant les instructions juste après l'intro, de sorte que le préfixe
    « intro + instructions » soit rigoureusement identique pour tout le corpus,
    et « + contexte film » identique pour tous les plans d'un même film.
    """
    global _alerte_decoupage_emise

    i_instr = -1
    for marque in MARQUES_INSTRUCTIONS:
        trouve = prompt.find(marque)
        if trouve >= 0 and (i_instr < 0 or trouve < i_instr):
            i_instr = trouve
    if i_instr < 0:
        if not _alerte_decoupage_emise:
            print("    ⚠ prompt non reconnu : cache de préfixe désactivé", file=sys.stderr)
            _alerte_decoupage_emise = True
        return prompt, "", ""
    i_instr = _debut_de_ligne(prompt, i_instr)

    i_film = prompt.find(MARQUE_FILM, 0, i_instr)
    i_scene = prompt.find(MARQUE_SCENE, 0, i_instr)
    if i_film >= 0:
        i_film = _debut_de_ligne(prompt, i_film)
    if i_scene >= 0:
        i_scene = _debut_de_ligne(prompt, i_scene)

    bornes = [b for b in (i_film, i_scene, i_instr) if b >= 0]
    intro = prompt[: min(bornes)].strip()

    fin_film = i_scene if i_scene > i_film >= 0 else i_instr
    bloc_film = prompt[i_film:fin_film].strip() if i_film >= 0 else ""
    bloc_scene = prompt[i_scene:i_instr].strip() if i_scene >= 0 else ""
    instructions = prompt[i_instr:].strip()

    constant = f"{intro}\n\n{instructions}" if intro else instructions
    return constant, bloc_film, bloc_scene


# ─────────────────────────────────────────────────────────────────────────────
#  2. Schéma JSON — normalisation pour le décodage contraint
# ─────────────────────────────────────────────────────────────────────────────

def simplifier_oneof(schema: Any) -> Any:
    """Remplace les `oneOf` scalaire/tableau par la seule forme tableau.

    Les compilateurs de grammaire acceptent mal l'union « string | array of
    string » utilisée par la clé "lumiere". La forme tableau est incluse dans
    le schéma d'origine, donc la sortie reste valide au regard de celui-ci.
    Appelé uniquement en repli, si le serveur refuse le schéma.
    """
    if isinstance(schema, dict):
        variantes = schema.get("oneOf")
        if isinstance(variantes, list) and variantes:
            tableaux = [v for v in variantes if isinstance(v, dict) and v.get("type") == "array"]
            retenu = tableaux[0] if tableaux else variantes[0]
            reste = {k: v for k, v in schema.items() if k != "oneOf"}
            return {**reste, **simplifier_oneof(retenu)}
        return {k: simplifier_oneof(v) for k, v in schema.items()}
    if isinstance(schema, list):
        return [simplifier_oneof(v) for v in schema]
    return schema


def enveloppe_schema(schema: dict, nom: str = "analyse_plan", strict: bool = False) -> dict:
    """Emballe le schéma au format `response_format` d'OpenAI.

    `strict` reste faux par défaut : le mode strict impose que toutes les clés
    soient requises, ce qui contredit les clés volontairement optionnelles du
    vocabulaire contrôlé (« interface », omise si aucun écran n'est lisible).
    """
    return {
        "type": "json_schema",
        "json_schema": {"name": nom, "strict": bool(strict), "schema": schema},
    }


# ─────────────────────────────────────────────────────────────────────────────
#  3. Moteur MLX — client HTTP du serveur mlx-vlm
# ─────────────────────────────────────────────────────────────────────────────

def _extraire_json(brut: str) -> dict:
    """Tolérant : le décodage contraint rend du JSON pur, mais on se protège."""
    texte = (brut or "").strip()
    if texte.startswith("```"):
        texte = texte.split("```")[1] if texte.count("```") >= 2 else texte
        texte = texte.removeprefix("json").strip()
    try:
        return json.loads(texte)
    except json.JSONDecodeError:
        debut, fin = texte.find("{"), texte.rfind("}")
        if debut >= 0 and fin > debut:
            return json.loads(texte[debut : fin + 1])
        raise


class MoteurMLX:
    """Client du serveur `mlx_vlm.server`, sûr en usage multi-thread."""

    def __init__(
        self,
        modele: str = MLX_MODELE,
        url: str = MLX_URL,
        timeout: float = MLX_TIMEOUT,
        max_tokens: int = MLX_MAX_TOKENS,
        temperature: float = MLX_TEMPERATURE,
        encodage: str = MLX_ENCODAGE,
        concurrence: int = CONCURRENCE,
    ) -> None:
        self.modele = modele
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.encodage = encodage
        self.concurrence = max(1, concurrence)
        # Le serveur accepte deux formes de bloc image ; on retient celle qui
        # passe, pour ne pas payer un aller-retour d'essai à chaque plan.
        self._forme_image = "image_url"
        self._schemas_simplifies: dict[int, dict] = {}
        self._verrou = threading.Lock()
        self.stats = {"appels": 0, "echecs": 0, "secondes": 0.0}

    # -- construction de la requête --------------------------------------

    def _bloc_image(self, chemin: Path, forme: str) -> dict:
        if self.encodage == "base64":
            donnees = base64.b64encode(Path(chemin).read_bytes()).decode()
            url = f"data:image/jpeg;base64,{donnees}"
        else:
            url = str(Path(chemin).resolve())
        if forme == "input_image":
            return {"type": "input_image", "image_url": url}
        return {"type": "image_url", "image_url": {"url": url}}

    def _messages(self, prompt: str, images: Sequence[Path], forme: str) -> list[dict]:
        constant, bloc_film, bloc_scene = decouper_prompt(prompt)

        # Système = préfixe stable. Corpus entier d'abord, film ensuite.
        systeme = constant if not bloc_film else f"{constant}\n\n{bloc_film}"

        # Utilisateur = images + la seule partie qui change d'un plan à l'autre.
        contenu: list[dict] = [self._bloc_image(p, forme) for p in images]
        suffixe = bloc_scene or "Analyse ces images selon les consignes ci-dessus."
        contenu.append({"type": "text", "text": suffixe})

        return [
            {"role": "system", "content": systeme},
            {"role": "user", "content": contenu},
        ]

    def _corps(self, prompt: str, images: Sequence[Path], schema: dict,
               forme: str, nom_schema: str) -> dict:
        return {
            "model": self.modele,
            "messages": self._messages(prompt, images, forme),
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": False,
            "response_format": enveloppe_schema(schema, nom_schema),
        }

    # -- transport --------------------------------------------------------

    def _poster(self, chemin: str, corps: dict, tenant: str = "") -> dict:
        donnees = json.dumps(corps, ensure_ascii=False).encode("utf-8")
        entetes = {"Content-Type": "application/json"}
        if tenant:
            # Isole les préfixes en cache par film : deux films n'ont pas le
            # même bloc de contexte, inutile de les faire concourir.
            entetes["X-APC-Tenant"] = tenant
        requete = urllib.request.Request(
            f"{self.url}{chemin}", data=donnees, headers=entetes, method="POST"
        )
        with urllib.request.urlopen(requete, timeout=self.timeout) as reponse:
            return json.loads(reponse.read().decode("utf-8"))

    # -- API publique -----------------------------------------------------

    def decrire(self, prompt: str, images: Sequence[Path], schema: dict,
                essais: int = 2, tenant: str = "", nom_schema: str = "analyse_plan") -> dict:
        """Retourne l'objet JSON décrit par le schéma, ou {} en cas d'échec."""
        images = [Path(p) for p in images]
        cle_schema = id(schema)
        depart = time.time()

        for tentative in range(max(1, essais)):
            schema_actif = self._schemas_simplifies.get(cle_schema, schema)
            try:
                reponse = self._poster(
                    "/v1/chat/completions",
                    self._corps(prompt, images, schema_actif, self._forme_image, nom_schema),
                    tenant=tenant,
                )
                brut = reponse["choices"][0]["message"]["content"]
                resultat = _extraire_json(brut)
                with self._verrou:
                    self.stats["appels"] += 1
                    self.stats["secondes"] += time.time() - depart
                return resultat

            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:400]
                except Exception:
                    pass

                # 1er repli : l'autre forme de bloc image.
                if exc.code in (400, 422) and self._forme_image == "image_url":
                    self._forme_image = "input_image"
                    print("    ⚠ bascule sur le format d'image « input_image »", file=sys.stderr)
                    continue

                # 2e repli : schéma trop exotique pour le compilateur de grammaire.
                if exc.code in (400, 422) and cle_schema not in self._schemas_simplifies:
                    self._schemas_simplifies[cle_schema] = simplifier_oneof(copy.deepcopy(schema))
                    print("    ⚠ schéma simplifié (oneOf réduit au tableau)", file=sys.stderr)
                    continue

                print(f"    ⚠ HTTP {exc.code} — {detail}", file=sys.stderr)
                if tentative == essais - 1:
                    break
                time.sleep(2)

            except json.JSONDecodeError:
                if tentative == essais - 1:
                    break

            except Exception as exc:  # réseau, serveur redémarré, délai dépassé
                print(f"    ⚠ {type(exc).__name__}: {exc}", file=sys.stderr)
                if tentative == essais - 1:
                    break
                time.sleep(2)

        with self._verrou:
            self.stats["echecs"] += 1
            self.stats["secondes"] += time.time() - depart
        return {}

    def decrire_lot(self, taches: Sequence[dict], concurrence: int | None = None) -> list[dict]:
        """Traite plusieurs plans en parallèle, dans l'ordre d'entrée.

        Chaque tâche est un dict {prompt, images, schema, tenant}. Le serveur
        les fait rejoindre le même lot de décodage : c'est ici que se joue le
        gros du débit.
        """
        n = concurrence or self.concurrence
        if n <= 1 or len(taches) <= 1:
            return [self.decrire(**t) for t in taches]
        with ThreadPoolExecutor(max_workers=n) as pool:
            return list(pool.map(lambda t: self.decrire(**t), taches))

    # -- diagnostic -------------------------------------------------------

    def sante(self) -> dict:
        with urllib.request.urlopen(f"{self.url}/health", timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def cache_stats(self) -> dict:
        try:
            with urllib.request.urlopen(f"{self.url}/v1/cache/stats", timeout=10) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as exc:
            return {"erreur": f"{type(exc).__name__}: {exc}"}

    def metriques(self) -> dict:
        try:
            with urllib.request.urlopen(f"{self.url}/v1/metrics", timeout=10) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as exc:
            return {"erreur": f"{type(exc).__name__}: {exc}"}


# ─────────────────────────────────────────────────────────────────────────────
#  4. Moteur Ollama — référence de non-régression, comportement inchangé
# ─────────────────────────────────────────────────────────────────────────────

class MoteurOllama:
    """Reprise fidèle de l'ancien `interroger()` de analyse_plans.py."""

    def __init__(self, modele: str = "", **_) -> None:
        import ollama
        self.client = ollama.Client()
        self.modele = modele
        self.stats = {"appels": 0, "echecs": 0, "secondes": 0.0}

    def decrire(self, prompt: str, images: Sequence[Path], schema: dict,
                essais: int = 2, tenant: str = "", nom_schema: str = "") -> dict:
        encodees = [base64.b64encode(Path(p).read_bytes()).decode() for p in images]
        depart = time.time()
        for tentative in range(max(1, essais)):
            try:
                kwargs = dict(
                    model=self.modele,
                    format=schema,
                    options={"temperature": 0.2, "num_ctx": 8192},
                    messages=[{"role": "user", "content": prompt, "images": encodees}],
                )
                try:
                    reponse = self.client.chat(think=False, **kwargs)
                except TypeError:
                    reponse = self.client.chat(**kwargs)
                resultat = json.loads(reponse["message"]["content"])
                self.stats["appels"] += 1
                self.stats["secondes"] += time.time() - depart
                return resultat
            except json.JSONDecodeError:
                if tentative == essais - 1:
                    break
            except Exception as exc:
                print(f"    ⚠ {exc}", file=sys.stderr)
                time.sleep(2)
        self.stats["echecs"] += 1
        self.stats["secondes"] += time.time() - depart
        return {}

    def decrire_lot(self, taches: Sequence[dict], concurrence: int | None = None) -> list[dict]:
        # Ollama gère la concurrence via OLLAMA_NUM_PARALLEL ; par prudence on
        # reste séquentiel ici pour que la référence demeure comparable.
        return [self.decrire(**t) for t in taches]


# ─────────────────────────────────────────────────────────────────────────────
#  5. Fabrique et compatibilité descendante
# ─────────────────────────────────────────────────────────────────────────────

MOTEURS = {"mlx": MoteurMLX, "ollama": MoteurOllama}


def creer_moteur(nom: str = "mlx", **kwargs) -> MoteurMLX | MoteurOllama:
    """Instancie un moteur. `nom` vaut « mlx » ou « ollama »."""
    nom = (nom or "mlx").lower().strip()
    if nom not in MOTEURS:
        raise SystemExit(f"Moteur inconnu : {nom} (attendu : {', '.join(sorted(MOTEURS))})")
    return MOTEURS[nom](**kwargs)


def interroger(client, modele: str, prompt: str, images: list,
               schema: dict, essais: int = 2, tenant: str = "") -> dict:
    """Signature identique à l'ancienne fonction de analyse_plans.py.

    `client` peut être un moteur de ce module, ou un `ollama.Client` brut
    (auquel cas on retombe sur le chemin historique).
    """
    if isinstance(client, (MoteurMLX, MoteurOllama)):
        if isinstance(client, MoteurOllama) and modele and not client.modele:
            client.modele = modele
        return client.decrire(prompt, images, schema, essais=essais, tenant=tenant)

    # Client ollama nu : chemin historique intégral.
    moteur = MoteurOllama.__new__(MoteurOllama)
    moteur.client = client
    moteur.modele = modele
    moteur.stats = {"appels": 0, "echecs": 0, "secondes": 0.0}
    return moteur.decrire(prompt, images, schema, essais=essais)


# ─────────────────────────────────────────────────────────────────────────────
#  6. Auto-test
# ─────────────────────────────────────────────────────────────────────────────

def _auto_test(images: Iterable[Path], modele: str, url: str) -> int:
    moteur = MoteurMLX(modele=modele, url=url)

    print(f"Serveur   : {url}")
    try:
        print(f"Santé     : {json.dumps(moteur.sante(), ensure_ascii=False)}")
    except Exception as exc:
        print(f"✗ serveur injoignable ({type(exc).__name__}: {exc})", file=sys.stderr)
        print("  Lancez : mlx_vlm.server --model <repo> --port 8080", file=sys.stderr)
        return 1

    prompt = (
        "Tu observes 3 images extraites d'un même plan de film.\n\n"
        "Contexte du film, à utiliser avec prudence :\n- Film : essai\n\n"
        "Contexte de scène, à utiliser pour désambiguïser le plan :\n- Scène : essai\n\n"
        "Réponds uniquement par un objet JSON, en français, avec ces clés :\n"
        '"echelle" : une valeur parmi [\'gros plan\', \'plan moyen\', \'plan large\']\n'
        '"lieu" : "intérieur" ou "extérieur"'
    )
    constant, film, scene = decouper_prompt(prompt)
    print(f"Découpage : constant {len(constant)} car. · film {len(film)} · scène {len(scene)}")
    if not film or not scene:
        print("✗ découpage du prompt incorrect", file=sys.stderr)
        return 1

    schema = {
        "type": "object",
        "properties": {
            "echelle": {"type": "string", "enum": ["gros plan", "plan moyen", "plan large"]},
            "lieu": {"type": "string", "enum": ["intérieur", "extérieur"]},
        },
        "required": ["echelle", "lieu"],
    }

    chemins = [Path(p) for p in images]
    manquants = [p for p in chemins if not p.exists()]
    if manquants:
        print(f"✗ images introuvables : {manquants}", file=sys.stderr)
        return 1

    depart = time.time()
    resultat = moteur.decrire(prompt, chemins, schema, tenant="autotest")
    duree = time.time() - depart
    print(f"Réponse   : {json.dumps(resultat, ensure_ascii=False)}  ({duree:.1f} s)")

    depart = time.time()
    lot = moteur.decrire_lot(
        [{"prompt": prompt, "images": chemins, "schema": schema, "tenant": "autotest"}] * 4,
        concurrence=4,
    )
    duree_lot = time.time() - depart
    valides = sum(1 for r in lot if r)
    print(f"Lot de 4  : {valides}/4 valides en {duree_lot:.1f} s "
          f"({duree_lot / 4:.1f} s/plan — comparez au séquentiel ci-dessus)")
    print(f"Cache APC : {json.dumps(moteur.cache_stats(), ensure_ascii=False)[:300]}")

    return 0 if resultat else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--test", action="store_true", help="vérifier l'installation")
    ap.add_argument("--images", nargs="*", type=Path, default=[],
                    help="1 à 3 images de test (idéalement des frames réelles)")
    ap.add_argument("--modele", default=MLX_MODELE)
    ap.add_argument("--url", default=MLX_URL)
    args = ap.parse_args()

    if args.test:
        if not args.images:
            print("Fournissez --images avec au moins une image.", file=sys.stderr)
            raise SystemExit(2)
        raise SystemExit(_auto_test(args.images, args.modele, args.url))

    ap.print_help()


if __name__ == "__main__":
    main()
