#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Démarre un processus local en le détachant vraiment du script .command."""
from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("commande", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    if not args.commande:
        raise SystemExit("Commande absente.")
    if args.commande and args.commande[0] == "--":
        args.commande = args.commande[1:]

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("a", encoding="utf-8")
    subprocess.Popen(
        args.commande,
        cwd=args.cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )


if __name__ == "__main__":
    main()
