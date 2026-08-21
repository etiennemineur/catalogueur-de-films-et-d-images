# Contributing

Code contributions are welcome.

Please never share or add media files to this repository.

This includes, for example:

- videos (`.mp4`, `.mov`, `.mkv`, etc.);
- images (`.jpg`, `.jpeg`, `.png`, `.webp`, etc.);
- personal photos;
- generated thumbnails;
- audio files;
- exports or analysis indexes generated from your own media files.

The repository must contain only the programs, documentation, and example files required so that each person can install the tool and use it with their own films, images, and local AI models.

Do not share your personal local configuration either (`config.json`). Use `config.example.json` to document options.

Before proposing a change, run:

```bash
python scripts/verifier_depot.py
```

This command checks Python syntax, inline JavaScript in HTML pages, and the absence of media files or personal configuration tracked by Git.

The project is licensed under the MIT License: your code contributions will be published under the same license.
