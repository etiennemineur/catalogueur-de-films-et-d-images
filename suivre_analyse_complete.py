#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Surveille l’analyse complète et relance proprement s’il reste des films."""
from __future__ import annotations
import json
import subprocess
import time
from urllib.request import Request, urlopen
from urllib.error import URLError

ETAT = 'http://127.0.0.1:8765/etat'
ANALYSER = 'http://127.0.0.1:8765/analyser'
REINDEXER = 'http://127.0.0.1:8765/reindexer'


def lire(url: str, method: str = 'GET') -> dict:
    req = Request(url, method=method)
    with urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode('utf-8'))


def pgrep_analyse() -> bool:
    r = subprocess.run(['pgrep', '-f', r'analyse_plans.py .*--mode (triage|complet)'], text=True, capture_output=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def attendre_fin_analyse():
    while pgrep_analyse():
        try:
            etat = lire(ETAT)
            idx = etat.get('index') or {}
            print(f"analyse active · films indexés {idx.get('films')} · plans {idx.get('plans')} · index {idx.get('genere')}", flush=True)
        except Exception as exc:
            print(f'état inaccessible pendant attente: {exc}', flush=True)
        time.sleep(300)


def main():
    cycles = 0
    while cycles < 20:
        cycles += 1
        attendre_fin_analyse()
        try:
            lire(REINDEXER, 'POST')
            time.sleep(5)
            etat = lire(ETAT)
        except Exception as exc:
            print(f'arrêt: serveur de contrôle inaccessible: {exc}', flush=True)
            return
        manquants = etat.get('films_sans_analyse') or []
        idx = etat.get('index') or {}
        print(f"cycle {cycles} · films source {etat.get('films_source')} · index {idx.get('films')} films / {idx.get('plans')} plans · manquants {len(manquants)}", flush=True)
        if not manquants:
            print('ANALYSE_COMPLETE_SURVEILLEE_OK', flush=True)
            return
        # Relance une passe complète reprenable. Le serveur refuse s’il y a déjà une analyse.
        rep = lire(ANALYSER, 'POST')
        print(f"relance analyse: {rep.get('status')} · {rep.get('message')}", flush=True)
        time.sleep(30)
    print('ARRÊT: trop de cycles de relance sans convergence', flush=True)


if __name__ == '__main__':
    main()
