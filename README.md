# catalogueur de films et d‘images

A local tool for cataloguing films and images with local AI models. It builds a visual catalogue, lets you search through film shots, browse film records, analyse photos, and rerun targeted analyses.

The project is designed for local use: a `localhost` web server, AI models through Ollama or MLX, and media folders selected on your Mac.

## Tested platform

This code has been tested on a Mac M5 running system 26.6.2.

## Features

- Film analysis into shots, scenes, and locally browsable film records.
- Search in a shot catalogue with crossed/intersection filters.
- Photo analysis with user-selected local AI models.
- Rerun analysis for a single photo from its detail view, with model selection.
- Progress tracking: active analysis, current photo or film, progress bar.
- Local HTML interface, with no cloud account required.

## License

This project is released under the **MIT License**: use, modification, redistribution, and commercial use are allowed, provided that the copyright and license notice are kept.

See [`LICENSE`](LICENSE).

## What is not included in this repository

The open-source repository does not contain:

- films;
- personal photos;
- generated thumbnails;
- analysis indexes;
- temporary audio files;
- logs;
- the Python virtual environment;
- personal local configuration.

These files are intentionally excluded by `.gitignore`.

## Local installation

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
cp films_fiches.example.json films_fiches.json
```

Then edit `config.json` to set your local folders:

```json
{
  "dossier_films": "/path/to/your/films",
  "dossier_photos": "/path/to/your/photos"
}
```

The configuration keys are currently kept in French because the local application reads those exact names.

## Launch

On macOS, the `.command` launchers can be opened from the Finder.

From the command line:

```bash
. .venv/bin/activate
python controle_analyse.py
```

Then open:

```text
http://127.0.0.1:8002/accueil.html
```

or directly:

```text
http://127.0.0.1:8002/index.html
http://127.0.0.1:8002/photos.html
```

## AI models

The project expects local models, especially through Ollama or, if configured, Apple MLX. No specific model is imposed by the public repository.

1. Install or start your local models.
2. List the available Ollama models:

```bash
ollama list
```

3. Set your choices in `config.json`, or choose them from the interface:

```json
{
  "modele_analyse": "your-vision-model:latest",
  "photos_modele_analyse": "your-vision-model:latest",
  "modeles_analyse_disponibles": {
    "my-model": {
      "nom": "your-vision-model:latest",
      "label": "Ollama · my local model",
      "moteur": "ollama"
    }
  }
}
```

You can also use the environment variables `BANC_MODELE_ANALYSE`, `BANC_MODELE_AFFINAGE`, `BANC_MODELE_PHOTOS`, and `BANC_MODELE_MLX` if you prefer not to edit `config.json`.

## Main structure

- `controle_analyse.py` — local control server and endpoints.
- `analyse_plans.py` — visual film analysis.
- `analyse_photos.py` — photo analysis and indexing.
- `catalogueur_utils.py` — shared utilities used by multiple scripts.
- `scripts/verifier_depot.py` — public repository verification before contribution.
- `accueil.html` — local dashboard.
- `index.html` — film shot catalogue.
- `photos.html` — photo catalogue and analysis.
- `fiches.html` — film records.
- `film.html` — local film player.
- `requirements.txt` — Python dependencies.

## Legal note about media files

The code is free. Analysed media, generated images, captures, thumbnails, transcriptions, and indexes may depend on third-party rights or personal data. They are not included in this repository.
