# catalogueur de films et d‘images

Outil local pour cataloguer des films et des images avec des modèles IA locaux, construire un catalogue visuel, rechercher dans les plans, consulter des fiches films et relancer des analyses ciblées.

Le projet est pensé pour un usage local : serveur web sur `localhost`, modèles via Ollama ou MLX, et dossiers de médias choisis sur le Mac.

## Plateforme testée

Ce code a été testé sur un Mac M5 sous le système 26.6.2.

## Fonctionnalités

- Analyse de films en plans, scènes et fiches consultables localement.
- Recherche dans un catalogue de plans avec filtres croisés.
- Analyse de photos avec choix du modèle IA.
- Relance d’une seule photo depuis son détail, avec choix du modèle.
- Suivi de progression : analyse active, photo ou film courant, barre de progression.
- Interface HTML locale, sans compte cloud requis.

## Licence

Ce projet est publié sous **licence MIT** : usage, modification, redistribution et usage commercial autorisés, avec conservation de la notice de copyright et de licence.

Voir [`LICENSE`](LICENSE).

## Ce qui n’est pas inclus dans le dépôt

Le dépôt open source ne contient pas :

- les films ;
- les photos personnelles ;
- les vignettes générées ;
- les index d’analyse ;
- les fichiers audio temporaires ;
- les logs ;
- l’environnement virtuel Python ;
- la configuration locale personnelle.

Ces éléments sont volontairement exclus par `.gitignore`.

## Installation locale

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
cp films_fiches.example.json films_fiches.json
```

Éditez ensuite `config.json` pour indiquer vos dossiers locaux :

```json
{
  "dossier_films": "/chemin/vers/vos/films",
  "dossier_photos": "/chemin/vers/vos/photos"
}
```

## Lancement

Sur macOS, les lanceurs `.command` peuvent être utilisés depuis le Finder.

En ligne de commande :

```bash
. .venv/bin/activate
python controle_analyse.py
```

Puis ouvrez :

```text
http://127.0.0.1:8002/accueil.html
```

ou directement :

```text
http://127.0.0.1:8002/index.html
http://127.0.0.1:8002/photos.html
```

## Modèles IA

Le projet attend des modèles locaux, notamment via Ollama ou, si vous le configurez, via Apple MLX. Aucun modèle précis n’est imposé dans le dépôt public.

1. Installez ou lancez vos modèles locaux.
2. Regardez les modèles Ollama disponibles :

```bash
ollama list
```

3. Indiquez vos choix dans `config.json` ou choisissez-les dans l’interface :

```json
{
  "modele_analyse": "votre-modele-vision:latest",
  "photos_modele_analyse": "votre-modele-vision:latest",
  "modeles_analyse_disponibles": {
    "mon-modele": {
      "nom": "votre-modele-vision:latest",
      "label": "Ollama · mon modèle local",
      "moteur": "ollama"
    }
  }
}
```

Vous pouvez aussi utiliser les variables d’environnement `BANC_MODELE_ANALYSE`, `BANC_MODELE_AFFINAGE`, `BANC_MODELE_PHOTOS`, `BANC_MODELE_MLX` si vous préférez ne pas modifier `config.json`.

## Structure principale

- `controle_analyse.py` — serveur de contrôle local et endpoints.
- `analyse_plans.py` — analyse visuelle des films.
- `analyse_photos.py` — analyse et indexation des photos.
- `catalogueur_utils.py` — utilitaires partagés entre scripts.
- `scripts/verifier_depot.py` — vérification du dépôt public avant contribution.
- `accueil.html` — tableau de bord local.
- `index.html` — catalogue des plans de films.
- `photos.html` — catalogue et analyse des photos.
- `fiches.html` — fiches films.
- `film.html` — lecteur film local.
- `requirements.txt` — dépendances Python.

## Note légale sur les médias

Le code est libre. Les médias analysés, images générées, captures, vignettes, transcriptions et index peuvent dépendre de droits tiers ou de données personnelles : ils ne sont pas inclus dans ce dépôt.
