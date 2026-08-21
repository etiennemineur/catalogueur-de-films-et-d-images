#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Check that the public repository remains publishable and coherent.

Checks performed:
- Python syntax for tracked files;
- inline JavaScript syntax in tracked HTML pages;
- absence of media files, generated data, personal config and large files;
- absence of personal local paths and values that look like secrets.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
MEDIA_EXTENSIONS = {
    ".mkv", ".mp4", ".mov", ".avi", ".webm", ".m4v", ".mpg", ".ts",
    ".wav", ".aiff", ".aif", ".mp3", ".flac",
    ".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".heic", ".heif",
    ".zip",
}
FORBIDDEN_NAMES = {"config.json", "films_fiches.json"}
FORBIDDEN_PREFIXES = (
    "analyse/", "archives/", "tmp/", ".venv/", "venv/", "logs/", "__pycache__/",
)
SECRET_RE = re.compile(
    r"(gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"-----BEGIN (RSA|OPENSSH|PRIVATE) KEY-----|"
    r"(?i:(password|api[_-]?key|secret|token|authorization|bearer|client_secret)"
    r"\s*[:=]\s*[\"']?[A-Za-z0-9_./+=-]{16,}))"
)
USER_HOME_PATTERN = "/" + "Users" + r"/[^/\s]+"
PROJECT_PATH_PATTERN = "Desktop/" + "films"
VOLUMES_PATTERN = "/" + "Volumes" + "/"
PERSONAL_PATH_RE = re.compile(
    rf"({USER_HOME_PATTERN}|{PROJECT_PATH_PATTERN}/new films|{PROJECT_PATH_PATTERN}/photos|{VOLUMES_PATTERN})"
)
INLINE_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)


def tracked_files() -> list[str]:
    result = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    return [p for p in result.decode("utf-8", "surrogateescape").split("\0") if p]


def check_public_files(files: list[str]) -> list[str]:
    errors: list[str] = []
    for name in files:
        path = ROOT / name
        if name in FORBIDDEN_NAMES or any(name.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
            errors.append(f"forbidden file tracked by Git: {name}")
        if pathlib.Path(name).suffix.lower() in MEDIA_EXTENSIONS:
            errors.append(f"forbidden media file tracked by Git: {name}")
        if path.exists() and path.is_file() and path.stat().st_size > 1_000_000:
            errors.append(f"tracked file is too large (>1 MB): {name}")
    return errors


def check_text_safety(files: list[str]) -> list[str]:
    errors: list[str] = []
    for name in files:
        path = ROOT / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            if SECRET_RE.search(line):
                errors.append(f"possible sensitive value: {name}:{line_number}")
            if PERSONAL_PATH_RE.search(line):
                errors.append(f"possible personal path: {name}:{line_number}")
    return errors


def check_python(files: list[str]) -> list[str]:
    py_files = [name for name in files if name.endswith(".py")]
    if not py_files:
        return []
    result = run([sys.executable, "-m", "py_compile", *py_files])
    if result.returncode == 0:
        return []
    return ["invalid Python syntax:\n" + (result.stdout + result.stderr)]


def check_inline_js(files: list[str]) -> list[str]:
    if run(["bash", "-lc", "command -v node"]).returncode != 0:
        return []
    errors: list[str] = []
    for name in files:
        if not name.endswith(".html"):
            continue
        text = (ROOT / name).read_text(encoding="utf-8", errors="replace")
        for index, code in enumerate(INLINE_SCRIPT_RE.findall(text), 1):
            if not code.strip():
                continue
            with tempfile.NamedTemporaryFile("w", suffix=f"-{pathlib.Path(name).stem}-{index}.js", delete=False, encoding="utf-8") as tmp:
                tmp.write(code)
                tmp_path = pathlib.Path(tmp.name)
            try:
                result = subprocess.run(["node", "--check", str(tmp_path)], text=True, capture_output=True)
            finally:
                tmp_path.unlink(missing_ok=True)
            if result.returncode != 0:
                errors.append(f"invalid inline JavaScript: {name} script {index}\n{result.stderr}")
    return errors


def main() -> int:
    files = tracked_files()
    errors: list[str] = []
    errors.extend(check_public_files(files))
    errors.extend(check_text_safety(files))
    errors.extend(check_python(files))
    errors.extend(check_inline_js(files))

    report = {
        "ok": not errors,
        "tracked_files": len(files),
        "errors": errors,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
