"""Préférences (Adw.PreferencesWindow : vraie fenêtre, le thème du système s'y applique)."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from .. import features  # noqa: E402
from ..i18n import LANGUAGE_NAMES, tr  # noqa: E402
from ..settings import settings  # noqa: E402
from .common import choose_folder, combo_row, path_row, shown  # noqa: E402


def apply_theme():
    Adw.StyleManager.get_default().set_color_scheme({
        "light": Adw.ColorScheme.FORCE_LIGHT,
        "dark": Adw.ColorScheme.FORCE_DARK,
    }.get(settings["theme"], Adw.ColorScheme.DEFAULT))


def _choice(title: str, key: str, labels: list[str], values: list, subtitle: str = "") -> Adw.ComboRow:
    cur = settings[key]
    row = combo_row(title, labels, values.index(cur) if cur in values else 0, subtitle)
    row.connect("notify::selected", lambda r, *_: settings.__setitem__(key, values[r.get_selected()]))
    return row


def _switch(title: str, key: str, subtitle: str = "") -> Adw.SwitchRow:
    row = Adw.SwitchRow(title=title, active=bool(settings[key]))
    if subtitle:
        row.set_subtitle(subtitle)
    row.connect("notify::active", lambda r, *_: settings.__setitem__(key, r.get_active()))
    return row


def _spin(title: str, key: str, lower: float, upper: float, step: float, digits: int = 0,
          subtitle: str = "") -> Adw.SpinRow:
    row = Adw.SpinRow.new_with_range(lower, upper, step)
    row.set_title(title)
    row.set_digits(digits)
    if subtitle:
        row.set_subtitle(subtitle)
    row.set_value(settings[key])
    cast = float if digits else int
    row.connect("notify::value", lambda r, *_: settings.__setitem__(key, cast(r.get_value())))
    return row


def default_app_row(title: str, what: str, app_name: str) -> Adw.SwitchRow:
    """Interrupteur « application par défaut » (defaultapp.py), comme celui d'Odysseus : l'état affiché est
    celui du système ; grisé quand l'application tourne depuis les sources (pas de lanceur installé)."""
    from .. import defaultapp
    row = Adw.SwitchRow(title=title, active=defaultapp.is_default())
    row.set_subtitle(what + "\n" + defaultapp.COMMAND)
    row.set_subtitle_lines(0)
    if not defaultapp.installed():
        row.set_subtitle(row.get_subtitle() + "\n" + tr("{0} n'est pas installé (lancé depuis les sources)").format(app_name))
        row.set_sensitive(False)
        return row

    def toggled(r, *_):
        if r.get_active() != defaultapp.is_default() and not defaultapp.set_default(r.get_active()):
            r.set_active(defaultapp.is_default())
    row.connect("notify::active", toggled)
    return row


class PreferencesWindow(Adw.PreferencesWindow):
    def __init__(self):
        super().__init__(title=tr("Préférences"), search_enabled=False, default_width=620, default_height=680)
        self.add(self._general())
        self.add(self._network())
        self.add(self._features())

    def _general(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=tr("Général"), icon_name="preferences-system-symbolic", name="general")
        system = Adw.PreferencesGroup()
        system.add(default_app_row(tr("Application par défaut pour les torrents"),
                                    tr("Les liens magnet (Firefox…) et les fichiers .torrent s'ouvrent avec Télémaque ; coupé, l'application d'avant reprend la main."),
                                    "Télémaque"))
        page.add(system)
        dl = Adw.PreferencesGroup(title=tr("Téléchargements"))
        self.folder = path_row(tr("Dossier par défaut"), settings.download_dir(), self._browse)
        self.folder.set_show_apply_button(True)
        self.folder.connect("apply", lambda r: settings.__setitem__("download_dir", r.get_text().strip()))
        dl.add(self.folder)
        dl.add(_choice(tr("En fermant la fenêtre"), "on_close",
                       [tr("Demander"), tr("Continuer en arrière-plan"), tr("Quitter")],
                       ["ask", "background", "quit"],
                       tr("Quand un téléchargement ou un partage est en cours")))
        page.add(dl)
        share = Adw.PreferencesGroup(title=tr("Partage"),
                                     description=tr("Un torrent vit du partage : renvoyer au moins autant qu'on a "
                                                    "reçu (ratio 1) est la règle de politesse."))
        policy = _choice(tr("Une fois le téléchargement terminé"), "seed_policy",
                         [tr("Partager jusqu'au ratio"), tr("Partager sans limite"), tr("Arrêter tout de suite")],
                         ["ratio", "always", "never"])
        share.add(policy)
        self.ratio = _spin(tr("Ratio"), "seed_ratio", 0.1, 20, 0.1, digits=1,
                           subtitle=tr("Envoyé ÷ reçu : 1,0 = autant envoyé que reçu"))
        share.add(self.ratio)
        policy.connect("notify::selected", lambda *_: self.ratio.set_sensitive(settings["seed_policy"] == "ratio"))
        self.ratio.set_sensitive(settings["seed_policy"] == "ratio")
        page.add(share)
        speed = Adw.PreferencesGroup(title=tr("Vitesse"), description=tr("0 : sans limite"))
        speed.add(_spin(tr("Réception maximale (Ko/s)"), "down_limit", 0, 1_000_000, 100))
        speed.add(_spin(tr("Envoi maximal (Ko/s)"), "up_limit", 0, 1_000_000, 50))
        page.add(speed)
        look = Adw.PreferencesGroup(title=tr("Apparence"))
        theme = _choice(tr("Style"), "theme", [tr("Comme le système"), tr("Clair"), tr("Sombre")],
                        ["system", "light", "dark"])
        theme.connect("notify::selected", lambda *_: apply_theme())
        look.add(theme)
        codes = list(LANGUAGE_NAMES)
        look.add(_choice(tr("Langue"), "language", [tr("Comme le système")] + [LANGUAGE_NAMES[c] for c in codes[1:]],
                         codes, tr("Appliquée au prochain démarrage")))
        page.add(look)
        return page

    def _browse(self, row):
        def chosen(path):
            row.set_text(shown(path))
            settings["download_dir"] = path
        choose_folder(self, tr("Dossier par défaut"), row.get_text(), chosen)

    def _network(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=tr("Réseau"), icon_name="network-wired-symbolic", name="network")
        group = Adw.PreferencesGroup()
        group.add(_spin(tr("Port d'écoute"), "listen_port", 1024, 65535, 1,
                        subtitle=tr("Ouvert sur la box automatiquement quand elle le permet (UPnP)")))
        group.add(_switch(tr("Réseau DHT"), "dht",
                          tr("Trouver des sources sans tracker ; indispensable aux liens magnet")))
        page.add(group)
        return page

    def _features(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=tr("Fonctionnalités"), icon_name="application-x-addon-symbolic",
                                   name="features")
        group = Adw.PreferencesGroup(description=tr("Chaque fonctionnalité s'active ou se coupe tout de "
                                                    "suite, sans redémarrer Télémaque"))
        self.feature_rows = {}
        for f in features.FEATURES:
            row = Adw.SwitchRow(title=f.title, subtitle=f.description, active=features.enabled(f.id))
            row.add_prefix(Gtk.Image(icon_name=f.icon))
            row.connect("notify::active", lambda r, *_, fid=f.id: features.set_enabled(fid, r.get_active()))
            self.feature_rows[f.id] = row
            group.add(row)
        page.add(group)
        return page
