"""Préférences persistantes (JSON dans ~/.config/telemaque). Seules les valeurs choisies par
l'utilisateur sont écrites : une valeur par défaut corrigée plus tard s'applique à tous ceux qui
n'y ont pas touché."""

from __future__ import annotations

import json
import os
from typing import Any

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "telemaque")
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")
DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "telemaque")


def default_download_dir() -> str:
    """Dossier « Téléchargements » de l'utilisateur (xdg-user-dirs), sinon ~/Téléchargements… sinon ~."""
    try:
        from gi.repository import GLib
        d = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
        if d and os.path.isdir(d):
            return d
    except Exception:  # noqa: BLE001 — sans GLib (tests du moteur) : repli
        pass
    for name in ("Téléchargements", "Downloads"):
        p = os.path.expanduser("~/" + name)
        if os.path.isdir(p):
            return p
    return os.path.expanduser("~")


DEFAULTS: dict[str, Any] = {
    "theme": "system",              # system, light, dark
    "language": "system",           # system, fr, en, de (appliqué au prochain démarrage)
    # Téléchargements
    "download_dir": "",             # vide : le dossier Téléchargements de l'utilisateur
    "seed_policy": "ratio",         # ratio (s'arrêter à seed_ratio), always (partager sans fin), never
    "seed_ratio": 1.0,
    "down_limit": 0,                # Kio/s, 0 = sans limite
    "up_limit": 0,
    "listen_port": 6881,
    "dht": True,                    # trouver des sources sans tracker (indispensable aux liens magnet)
    "on_close": "ask",              # ask, background, quit — fermer la fenêtre pendant un téléchargement
    # Fenêtre
    "window_width": 880,
    "window_height": 520,
    "window_maximized": False,
    "sidebar_visible": True,
    "filter": "all",                # vue de la barre latérale : all, active, done
    "previous_defaults": {},        # application par défaut d'avant, par type (defaultapp.py)
    "features": {},                 # fonctionnalités activées / désactivées (voir features.py)
}


def _fits(value, default) -> bool:
    """Valeur du bon type (un settings.json abîmé ou écrit à la main ne doit rien casser)."""
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, type(default))


class Settings:
    def __init__(self, path: str = CONFIG_FILE):
        self.path = path
        self.data: dict[str, Any] = {}
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data = {k: v for k, v in loaded.items() if k in DEFAULTS and _fits(v, DEFAULTS[k])}
        except (OSError, ValueError):
            pass
        self._listeners = []

    def __getitem__(self, key):
        return self.data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key, value):
        if self[key] == value:
            return
        self.data[key] = value
        self.save()
        for cb in list(self._listeners):
            cb(key, value)

    def connect(self, cb):
        self._listeners.append(cb)

    def disconnect(self, cb):
        if cb in self._listeners:
            self._listeners.remove(cb)

    def download_dir(self) -> str:
        d = self["download_dir"]
        return d if d and os.path.isdir(d) else default_download_dir()

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass


settings = Settings()
