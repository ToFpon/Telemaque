"""Fenêtre principale : la liste des téléchargements.

Chaque ligne : nom, barre de progression, état (« 1,2 Go sur 3,4 Go — 2,1 Mo/s — 12 min restantes »),
bouton pause / reprise et menu (Ouvrir, Afficher dans le dossier, Fichiers…, Supprimer…). Ajouter : un
fichier .torrent (bouton, Ctrl+O, glisser) ou un lien magnet (bouton, Ctrl+V, glisser du texte).
"""

from __future__ import annotations

import os
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from .. import APP_ID, APP_NAME, features, format as fmt  # noqa: E402
from ..backend import engine as eng  # noqa: E402
from ..i18n import ntr, tr  # noqa: E402
from ..settings import default_download_dir, settings  # noqa: E402
from .common import (RenameDialog, alert, choose_files, choose_folder, folder_usable, open_with_default,  # noqa: E402
                     show_in_folder, shown, toast)

STATE_ICONS = {"paused": "media-playback-start-symbolic", "done": "media-playback-start-symbolic",
               "error": "view-refresh-symbolic"}


def status_text(t) -> str:
    """Ligne d'état sous le nom d'un téléchargement."""
    of = tr("{0} sur {1}").format(fmt.size(t.done), fmt.size(t.size))
    if t.state == "error":
        return tr("Erreur : {0}").format(t.error)
    if t.state == "metadata":
        return tr("Recherche des informations du torrent…")
    if t.state == "moving":
        return tr("Déplacement des fichiers…")
    if t.state == "checking":
        return tr("Vérification des fichiers… {0}").format(fmt.percent(t.progress))
    if t.state == "paused":
        return tr("En pause — {0}").format(of)
    if t.state == "done":
        return tr("Terminé — {0}").format(fmt.size(t.size))
    if t.state == "seeding":
        return tr("Terminé, en partage — ↑ {0}").format(fmt.speed(t.up_rate))
    if t.down_rate <= 0:
        return tr("{0} — en attente de sources").format(of)
    text = tr("{0} — ↓ {1}").format(of, fmt.speed(t.down_rate))
    if t.eta >= 0:
        text += " — " + tr("{0} restantes").format(fmt.duration(t.eta))
    return text


FILTERS = (("all", tr("Tous"), "view-list-symbolic"),
           ("active", tr("En cours"), "folder-download-symbolic"),
           ("done", tr("Terminés"), "object-select-symbolic"))


def matches(filter_id: str, t) -> bool:
    if filter_id == "active":
        return not t.complete
    if filter_id == "done":
        return t.complete
    return True


def peers_text(t) -> str:
    """À droite sous la barre : « 3 sources · 12 pairs · ratio 0,45 ». Sources = pairs qui ont tout
    (seeds), pairs = les autres ; ceux auxquels on est relié en ce moment."""
    parts = []
    if t.state in ("metadata", "checking", "downloading", "seeding"):
        others = max(0, t.peers - t.seeds)
        parts.append(ntr("{0} source", "{0} sources", t.seeds).format(t.seeds))
        parts.append(ntr("{0} pair", "{0} pairs", others).format(others))
    if t.state != "metadata":
        parts.append(tr("ratio {0}").format(_ratio(t.ratio)))
    return " · ".join(parts)


def ip_text(n) -> str:
    """« IPv4 : 82.65.1.2 · IPv6 : 2a01:e0a::1 · port 6881 (joignable) » : les adresses PUBLIQUES (celles
    du VPN s'il y en a un), une par famille ; la locale ne sert à rien ici (choix de Tof)."""
    if n.listen_error:
        return tr("Port {0} indisponible : {1}").format(n.port, n.listen_error)
    if not n.ipv4 and not n.ipv6:
        parts = [tr("IP publique : vérification…") if n.checking else tr("IP publique : inconnue") if n.looked_up
                 else tr("IP publique : pas encore connue")]
    else:
        unknown = (tr("vérification…") if n.checking else tr("aucune") if n.looked_up   # pas d'IPv6, par ex.
                   else tr("pas encore connue"))
        parts = [tr("IPv4 : {0}").format(n.ipv4 or unknown), tr("IPv6 : {0}").format(n.ipv6 or unknown)]
    port = tr("port {0}").format(n.port)
    if n.reachable:
        port += " " + tr("(joignable)")
    parts.append(port)
    return " · ".join(parts)


def dht_text(n) -> str:
    if not n.dht_enabled:
        return tr("DHT : désactivé")
    if n.dht_nodes <= 0:
        return tr("DHT : connexion…")
    return ntr("DHT : actif, {0} nœud", "DHT : actif, {0} nœuds", n.dht_nodes).format(n.dht_nodes)


def _ratio(r: float) -> str:
    from ..i18n import decimal
    return decimal(r, 2 if r < 10 else 1)


MAX_FILE_LINES = 200                # au-delà (packs immenses) : « … et N autres fichiers »


class FileLine(Gtk.Box):
    """Un fichier d'un torrent dépliable : état (fini / en cours / non téléchargé), nom, barre, pourcentage."""

    def __init__(self, f, label: str, on_toggle=None, on_delete=None):
        super().__init__(spacing=10, css_classes=["file-line"])
        self.path = f.path
        self.index = f.index
        self.wanted = f.wanted
        self.icon = Gtk.Image(pixel_size=16)
        self.append(self.icon)
        self.label = Gtk.Label(label=shown(label), xalign=0, hexpand=True, ellipsize=2,
                               tooltip_text=shown(label), css_classes=["caption"])
        self.append(self.label)
        self.bar = Gtk.ProgressBar(valign=Gtk.Align.CENTER, width_request=110, css_classes=["file-progress"])
        self.append(self.bar)
        self.value = Gtk.Label(xalign=1, width_chars=9, css_classes=["dim-label", "caption", "numeric"])
        self.append(self.value)
        # arrêter / reprendre CE fichier, et le supprimer du disque (demande de Tof)
        self.toggle = Gtk.Button(valign=Gtk.Align.CENTER, css_classes=["flat", "circular", "file-action"])
        self.toggle.connect("clicked", lambda *_: on_toggle and on_toggle(self.index, not self.wanted))
        self.append(self.toggle)
        self.delete = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                 tooltip_text=tr("Supprimer ce fichier…"),
                                 css_classes=["flat", "circular", "file-action"])
        self.delete.connect("clicked", lambda *_: on_delete and on_delete(self.index, label))
        self.append(self.delete)

    def update(self, f):
        self.wanted = f.wanted
        done = f.size and f.done >= f.size
        frac = (f.done / f.size) if f.size else 1.0
        self.bar.set_fraction(min(frac, 1.0))
        if not f.wanted and not done:
            self.icon.set_from_icon_name("action-unavailable-symbolic")
            self.icon.set_tooltip_text(tr("Non téléchargé (décoché dans « Fichiers… »)"))
            self.value.set_label(tr("décoché"))
            self.add_css_class("unwanted")
        else:
            self.remove_css_class("unwanted")
            if done:
                self.icon.set_from_icon_name("object-select-symbolic")
                self.icon.set_tooltip_text(tr("Fini"))
                self.value.set_label(fmt.size(f.size))
            else:
                self.icon.set_from_icon_name("folder-download-symbolic")
                self.icon.set_tooltip_text(tr("En cours"))
                self.value.set_label(tr("{0} %").format(int(frac * 100)))
        (self.add_css_class if done else self.remove_css_class)("complete")
        # fini : rien à arrêter (place gardée, boutons alignés)
        stoppable = not (done and f.wanted)
        self.toggle.set_opacity(1 if stoppable else 0)
        self.toggle.set_can_target(stoppable)
        self.toggle.set_can_focus(stoppable)
        if f.wanted:
            self.toggle.set_icon_name("media-playback-pause-symbolic")
            self.toggle.set_tooltip_text(tr("Arrêter ce fichier"))
        else:
            self.toggle.set_icon_name("media-playback-start-symbolic")
            self.toggle.set_tooltip_text(tr("Reprendre ce fichier"))


class TransferRow(Gtk.ListBoxRow):
    def __init__(self, win: "MainWindow", t):
        super().__init__(activatable=False, selectable=False)
        self.win = win
        self.tid = t.tid
        self.transfer = t
        box = Gtk.Box(spacing=12, margin_top=10, margin_bottom=10, margin_start=12, margin_end=8)
        # torrent de plusieurs fichiers (une saison…) : flèche qui déplie ses fichiers, chacun avec sa barre
        # (demande de Tof : voir où en est chaque épisode, pas seulement l'ensemble)
        self.expander = Gtk.Button(icon_name="pan-end-symbolic", valign=Gtk.Align.CENTER,
                                   css_classes=["flat", "circular", "files-expander"],
                                   tooltip_text=tr("Afficher les fichiers"))
        self.expander.connect("clicked", lambda *_: self.set_expanded(not self.expanded))
        box.append(self.expander)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True, valign=Gtk.Align.CENTER)
        self.name = Gtk.Label(xalign=0, ellipsize=2, css_classes=["heading"])        # … au milieu
        self.bar = Gtk.ProgressBar(css_classes=["transfer-progress"])
        self.status = Gtk.Label(xalign=0, ellipsize=3, hexpand=True, css_classes=["dim-label", "caption", "numeric"])
        self.peers = Gtk.Label(xalign=1, css_classes=["dim-label", "caption", "numeric"],
                               tooltip_text=tr("Sources : pairs qui ont tout le fichier ; pairs : les autres. "
                                               "Ceux auxquels Télémaque est relié en ce moment."))
        line = Gtk.Box(spacing=12)
        line.append(self.status)
        line.append(self.peers)
        text.append(self.name)
        text.append(self.bar)
        text.append(line)
        box.append(text)
        self.toggle = Gtk.Button(valign=Gtk.Align.CENTER, css_classes=["flat", "circular"])
        self.toggle.connect("clicked", lambda *_: win.toggle_transfer(self.tid))
        box.append(self.toggle)
        menu = Gio.Menu()
        first = Gio.Menu()
        first.append(tr("Ouvrir"), "row.open")
        first.append(tr("Afficher dans le dossier"), "row.show")
        menu.append_section(None, first)
        second = Gio.Menu()
        second.append(tr("Fichiers…"), "row.files")
        second.append(tr("Renommer…"), "row.rename")
        second.append(tr("Déplacer…"), "row.move")
        menu.append_section(None, second)
        third = Gio.Menu()
        third.append(tr("Supprimer…"), "row.remove")
        menu.append_section(None, third)
        self.menu_button = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER,
                                          menu_model=menu, css_classes=["flat", "circular"],
                                          tooltip_text=tr("Plus d'actions"))
        box.append(self.menu_button)
        # case ronde à droite (demande de Tof) : sélection de un ou plusieurs téléchargements
        self.check = Gtk.CheckButton(valign=Gtk.Align.CENTER, css_classes=["selection-mode"],
                                     tooltip_text=tr("Sélectionner"))
        self.check.connect("toggled", lambda c: win.set_selected(self.tid, c.get_active()))
        box.append(self.check)
        group = Gio.SimpleActionGroup()
        self.actions = {}
        for name, cb in (("open", lambda: win.open_transfer(self.tid)),
                         ("show", lambda: win.show_transfer(self.tid)),
                         ("files", lambda: win.show_files(self.tid)),
                         ("rename", lambda: win.ask_rename(self.tid)),
                         ("move", lambda: win.ask_move([self.tid])),
                         ("remove", lambda: win.ask_remove(self.tid))):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, cb=cb: cb())
            group.add_action(a)
            self.actions[name] = a
        self.insert_action_group("row", group)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.append(box)
        self.files_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_start=56, margin_end=60,
                                 margin_bottom=10, css_classes=["transfer-files"])
        self.files_revealer = Gtk.Revealer(child=self.files_box, reveal_child=False,
                                           transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        outer.append(self.files_revealer)
        self.set_child(outer)
        self.expanded = False
        self.file_lines: dict = {}             # indice → FileLine
        self.update(t)
        if t.tid in win.expanded:
            self.set_expanded(True)

    def set_expanded(self, on: bool):
        self.expanded = on
        (self.win.expanded.add if on else self.win.expanded.discard)(self.tid)
        self.expander.set_icon_name("pan-down-symbolic" if on else "pan-end-symbolic")
        self.expander.set_tooltip_text(tr("Masquer les fichiers") if on else tr("Afficher les fichiers"))
        if on:
            self.refresh_files()
        self.files_revealer.set_reveal_child(on)

    def refresh_files(self):
        """Fichiers et leur avancement (seulement quand la ligne est dépliée : file_progress coûte)."""
        from .dialogs import _strip_root, root_of
        files = self.win.app.engine.files(self.tid)
        if [f.index for f in files] != sorted(self.file_lines) or any(
                self.file_lines[f.index].path != f.path for f in files):
            while (c := self.files_box.get_first_child()) is not None:
                self.files_box.remove(c)
            self.file_lines = {}
            root = root_of({f.index: f.path for f in files})
            shown_files = sorted(files, key=lambda f: f.path.casefold())
            for f in shown_files[:MAX_FILE_LINES]:
                line = FileLine(f, _strip_root(f.path, root),
                                on_toggle=lambda i, w: self.win.set_file_wanted(self.tid, i, w),
                                on_delete=lambda i, name: self.win.ask_delete_file(self.tid, i, name))
                self.file_lines[f.index] = line
                self.files_box.append(line)
            if len(files) > MAX_FILE_LINES:
                more = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"],
                                 label=ntr("… et {0} autre fichier (voir « Fichiers… »)",
                                           "… et {0} autres fichiers (voir « Fichiers… »)",
                                           len(files) - MAX_FILE_LINES).format(len(files) - MAX_FILE_LINES))
                self.files_box.append(more)
        for f in files:
            line = self.file_lines.get(f.index)
            if line is not None:
                line.update(f)

    def update(self, t):
        self.transfer = t
        self.name.set_label(shown(t.name))
        self.name.set_tooltip_text(shown(t.name))
        self.bar.set_fraction(t.progress)
        self.status.set_label(status_text(t))
        self.peers.set_label(peers_text(t))
        for cls, on in (("paused", t.state in ("paused", "done")), ("error", t.state == "error"),
                        ("complete", t.complete)):
            (self.add_css_class if on else self.remove_css_class)(cls)
        if t.state in ("paused", "done", "error"):
            self.toggle.set_icon_name(STATE_ICONS[t.state])
            self.toggle.set_tooltip_text(tr("Reprendre") if t.state != "done" else tr("Partager de nouveau"))
        else:
            self.toggle.set_icon_name("media-playback-pause-symbolic")
            self.toggle.set_tooltip_text(tr("Mettre en pause"))
        self.toggle.set_sensitive(t.state != "moving")
        self.actions["move"].set_enabled(t.state != "moving")
        exists = bool(t.has_metadata and t.done > 0)
        self.actions["open"].set_enabled(exists and t.complete)
        self.actions["show"].set_enabled(exists)
        self.actions["files"].set_enabled(t.has_metadata)
        self.actions["rename"].set_enabled(t.has_metadata and t.state not in ("moving", "checking"))
        # place gardée même sans fichiers à déplier : les noms de toutes les lignes restent alignés
        many = t.file_count > 1
        self.expander.set_opacity(1 if many else 0)
        self.expander.set_sensitive(many)
        self.expander.set_can_target(many)
        self.expander.set_can_focus(many)
        if self.expanded and t.file_count > 1:
            self.refresh_files()


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME, icon_name=APP_ID)
        self.app = app
        self.set_default_size(settings["window_width"], settings["window_height"])
        if settings["window_maximized"]:
            self.maximize()
        self.set_size_request(360, 300)
        self.rows: dict[str, TransferRow] = {}
        self.files_dialogs: dict = {}
        self.selected: set[str] = set()
        self.expanded: set[str] = set()          # téléchargements dépliés (leurs fichiers), le temps de la session

        self.filter = settings["filter"] if settings["filter"] in [f[0] for f in FILTERS] else "all"
        self.toast_overlay = Adw.ToastOverlay()
        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle(title=APP_NAME)
        header.set_title_widget(self.title_widget)
        self.sidebar_button = Gtk.ToggleButton(icon_name="sidebar-show-symbolic",
                                               tooltip_text=tr("Barre latérale (F9)"))
        header.pack_start(self.sidebar_button)
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text=tr("Ouvrir un fichier torrent (Ctrl+O)"),
                         action_name="win.open")
        header.pack_start(add)
        magnet = Gtk.Button(icon_name="telemaque-magnet-symbolic", tooltip_text=tr("Ajouter un lien magnet"),
                            action_name="win.add-magnet")
        header.pack_start(magnet)
        main_menu = Gio.Menu()
        s1 = Gio.Menu()
        s1.append(tr("Préférences"), "app.preferences")
        s1.append(tr("À propos de Télémaque"), "app.about")
        main_menu.append_section(None, s1)
        s2 = Gio.Menu()
        s2.append(tr("Quitter"), "app.quit")
        main_menu.append_section(None, s2)
        self.main_menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=main_menu,
                                               tooltip_text=tr("Menu principal"), primary=True)
        header.pack_end(self.main_menu_button)
        tv.add_top_bar(header)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.empty = Adw.StatusPage(icon_name=APP_ID + "-symbolic", title=tr("Aucun téléchargement"),
                                    description=tr("Ouvrez un fichier .torrent ou collez un lien magnet (Ctrl+V). "
                                                   "Vous pouvez aussi les glisser ici."))
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        b1 = Gtk.Button(label=tr("Ouvrir un fichier torrent…"), action_name="win.open",
                        css_classes=["pill", "suggested-action"])
        b2 = Gtk.Button(label=tr("Ajouter un lien magnet…"), action_name="win.add-magnet", css_classes=["pill"])
        buttons.append(b1)
        buttons.append(b2)
        self.empty.set_child(buttons)
        self.stack.add_named(self.empty, "empty")
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"],
                                valign=Gtk.Align.START)
        clamp = Adw.Clamp(maximum_size=900, margin_top=18, margin_bottom=18, margin_start=12, margin_end=12)
        clamp.set_child(self.list)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroller.set_child(clamp)
        self.stack.add_named(scroller, "list")
        self.none = Adw.StatusPage(icon_name=APP_ID + "-symbolic")
        self.stack.add_named(self.none, "none")
        tv.set_content(self.stack)
        # barre d'état : par où passe Télémaque (VPN ?) et si le DHT travaille
        status = Gtk.Box(spacing=12, css_classes=["telemaque-status"])
        self._ip_help = tr("Adresse publique : celle que voient les autres (celle du VPN, s'il y en a un). "
                           "« Joignable » : des connexions entrantes arrivent, le port est ouvert.")
        self.ip_label = Gtk.Label(xalign=0, hexpand=True, ellipsize=3, selectable=False,
                                  css_classes=["dim-label", "caption", "numeric"], tooltip_text=self._ip_help)
        self.dht_icon = Gtk.Image(icon_name="network-transmit-receive-symbolic", css_classes=["dim-label"])
        self.dht_label = Gtk.Label(xalign=1, css_classes=["dim-label", "caption", "numeric"],
                                   tooltip_text=tr("Le DHT trouve des sources sans tracker ; indispensable aux liens "
                                                   "magnet. Préférences → Réseau."))
        status.append(self.ip_label)
        status.append(self.dht_icon)
        status.append(self.dht_label)
        tv.add_bottom_bar(self._build_selection_bar())
        tv.add_bottom_bar(status)
        self.list.set_filter_func(lambda row: matches(self.filter, row.transfer))

        # barre latérale : Tous / En cours / Terminés, avec leur nombre
        side = Adw.ToolbarView()
        side_header = Adw.HeaderBar()
        side_header.set_title_widget(Adw.WindowTitle(title=APP_NAME))
        side.add_top_bar(side_header)
        self.side_list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self.side_rows: dict = {}
        self.side_counts: dict = {}
        for fid, label, icon in FILTERS:
            row = Gtk.ListBoxRow()
            row.fid = fid
            box = Gtk.Box(spacing=12, margin_start=6, margin_end=6)
            box.append(Gtk.Image(icon_name=icon))
            box.append(Gtk.Label(label=label, xalign=0, hexpand=True, ellipsize=3))
            count = Gtk.Label(css_classes=["dim-label", "numeric"])
            box.append(count)
            row.set_child(box)
            self.side_list.append(row)
            self.side_rows[fid] = row
            self.side_counts[fid] = count
        self.side_list.select_row(self.side_rows[self.filter])
        self.title_widget.set_title(dict((f[0], f[1]) for f in FILTERS)[self.filter])
        self.side_list.connect("row-selected", self._on_filter_row)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroll.set_child(self.side_list)
        side.set_content(scroll)

        self.split = Adw.OverlaySplitView(sidebar=side, content=tv, min_sidebar_width=180, max_sidebar_width=240,
                                          show_sidebar=settings["sidebar_visible"])
        self.split.bind_property("show-sidebar", self.sidebar_button, "active",
                                 GObject.BindingFlags.SYNC_CREATE | GObject.BindingFlags.BIDIRECTIONAL)
        self.split.connect("notify::show-sidebar", self._on_sidebar_shown)
        # fenêtre étroite : la barre passe par-dessus la liste et se referme après un choix
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 560sp"))
        narrow.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(narrow)
        self.toast_overlay.set_child(self.split)
        self.set_content(self.toast_overlay)

        self._install_actions()
        self._sync_selection()
        self._install_drop()
        self.connect("close-request", self._on_close_request)
        # cachée (arrière-plan) : toujours à jour ; détruite (Quitter) : plus d'écouteur
        self.connect("destroy", lambda *_: app.disconnect_transfers(self._on_transfers))
        app.connect_transfers(self._on_transfers)
        self._on_transfers(app.transfers)

    # -- actions --------------------------------------------------------------------------------
    def _install_actions(self):
        for name, cb, accels in (("sidebar", lambda: self.split.set_show_sidebar(not self.split.get_show_sidebar()),
                                  ["F9"]),
                                 ("open", self.open_dialog, ["<Control>o"]),
                                 ("select-all", self.select_all, ["<Control>a"]),
                                 ("select-none", self.select_none, ["Escape"]),
                                 ("pause-selected", self.pause_selected, []),
                                 ("resume-selected", self.resume_selected, []),
                                 ("move-selected", lambda: self.ask_move(list(self.selected)), []),
                                 ("remove-selected", lambda: self.ask_remove(list(self.selected)), ["Delete"]),
                                 ("add-magnet", self.magnet_dialog, []),
                                 ("paste", self.paste, ["<Control>v"]),
                                 ("close", self.close, ["<Control>w"])):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, cb=cb: cb())
            self.add_action(a)
            if accels:
                self.app.set_accels_for_action(f"win.{name}", accels)

    def _install_drop(self):
        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        target.set_gtypes([Gdk.FileList, str])
        target.connect("drop", self._on_drop)
        self.add_controller(target)

    def _on_drop(self, _target, value, _x, _y):
        if isinstance(value, str):                 # plusieurs liens magnet déposés : tous, pas le premier seul
            for line in value.split():
                if eng.is_magnet(line):
                    self.add_magnet(line)
                elif line.startswith("file://"):
                    path = Gio.File.new_for_uri(line).get_path()
                    if path:
                        self.add_torrent_file(path)
            return True
        for f in value.get_files():
            if f.get_path():
                self.add_torrent_file(f.get_path())
        return True

    def paste(self):
        """Ctrl+V : un lien magnet dans le presse-papiers est ajouté."""
        focus = self.get_focus()
        if isinstance(focus, Gtk.Text) or isinstance(focus, Gtk.Editable):
            focus.activate_action("clipboard.paste", None)
            return

        def got(cb, res):
            try:
                text = cb.read_text_finish(res) or ""
            except GLib.Error:
                text = ""
            text = text.strip()
            if eng.is_magnet(text):
                self.add_magnet(text)
            elif text.endswith(".torrent") and os.path.isfile(text):
                self.add_torrent_file(text)
            else:
                toast(self, tr("Pas de lien magnet dans le presse-papiers"))
        self.get_clipboard().read_text_async(None, got)

    # -- ajout ------------------------------------------------------------------------------------
    def open_dialog(self):
        # là où le navigateur pose les .torrent : le dossier Téléchargements de xdg-user-dirs, quelle que soit la
        # langue (« ~/Téléchargements » écrit en dur manquait « Downloads », « Downloads » allemand…)
        choose_files(self, tr("Ouvrir des fichiers torrent"), default_download_dir(),
                     lambda paths: [self.add_torrent_file(p) for p in paths],
                     filters=[(tr("Fichiers torrent"), ["*.torrent"]), (tr("Tous les fichiers"), ["*"])])

    def magnet_dialog(self):
        from .dialogs import MagnetEntryDialog
        MagnetEntryDialog(self).present(self)

    def add_torrent_file(self, path: str):
        try:
            info = eng.read_torrent(path)
        except eng.TorrentError:
            alert(self, tr("Ce fichier n'est pas un torrent valide"), shown(os.path.basename(path)))
            return
        if info.tid in self.app.engine.handles:
            toast(self, tr("« {0} » est déjà dans la liste").format(shown(info.name)))
            return
        from .dialogs import AddDialog
        AddDialog(self, info=info, torrent_path=path).present(self)

    def add_magnet(self, uri: str):
        try:
            tid = eng.magnet_tid(uri)
        except eng.TorrentError:
            alert(self, tr("Ce lien magnet n'est pas valide"))
            return
        if tid in self.app.engine.handles:
            t = self.app.engine.transfer(tid)
            toast(self, tr("« {0} » est déjà dans la liste").format(shown(t.name if t else tid)))
            return
        from .dialogs import AddDialog
        AddDialog(self, magnet=uri).present(self)

    def start(self, *, torrent_path: str = "", magnet: str = "", save_path: str, wanted=None, paused=False,
              names=None):
        """Appelé par le dialogue d'ajout (names : noms choisis, {indice: chemin})."""
        try:
            if torrent_path:
                self.app.engine.add_torrent_file(torrent_path, save_path, wanted=wanted, paused=paused,
                                                 delete_source=features.enabled("delete_torrent"), names=names)
            else:
                self.app.engine.add_magnet(magnet, save_path, paused=paused)
        except eng.Duplicate:
            toast(self, tr("Déjà dans la liste"))
        except eng.PathError as e:
            folder_usable(self, e.path)
        except eng.TorrentError as e:
            alert(self, tr("Impossible d'ajouter ce torrent"), str(e))
        self.app.refresh()

    # -- liste ------------------------------------------------------------------------------------
    def _on_transfers(self, transfers):
        seen = set()
        for i, t in enumerate(transfers):
            seen.add(t.tid)
            row = self.rows.get(t.tid)
            if row is None:
                row = TransferRow(self, t)
                self.rows[t.tid] = row
                self.list.insert(row, i)
            else:
                row.update(t)
        for tid in [k for k in self.rows if k not in seen]:
            self.list.remove(self.rows.pop(tid))
        if self.selected - set(self.rows):
            self._sync_selection()
        self.transfers = transfers
        for fid, _l, _i in FILTERS:
            n = sum(1 for t in transfers if matches(fid, t))
            self.side_counts[fid].set_label(str(n) if n else "")
        self.list.invalidate_filter()            # un téléchargement fini passe d'« En cours » à « Terminés »
        self._sync_page()
        down = sum(t.down_rate for t in transfers)
        up = sum(t.up_rate for t in transfers)
        active = any(t.active for t in transfers)
        self.title_widget.set_subtitle(tr("↓ {0} · ↑ {1}").format(fmt.speed(down), fmt.speed(up)) if active else "")
        for tid, d in list(self.files_dialogs.items()):
            d.refresh()
        self._sync_network()

    def _sync_network(self):
        n = self.app.engine.network() if self.app.engine is not None else None
        if n is None:
            return
        self.ip_label.set_label(ip_text(n))
        self.ip_label.set_tooltip_text(ip_text(n) + "\n\n" + self._ip_help)
        (self.ip_label.add_css_class if n.listen_error else self.ip_label.remove_css_class)("error")
        self.dht_label.set_label(dht_text(n))
        self.dht_icon.set_from_icon_name("network-transmit-receive-symbolic" if n.dht_enabled and n.dht_nodes
                                         else "network-offline-symbolic")

    def _sync_page(self):
        transfers = getattr(self, "transfers", [])
        if not transfers:
            self.stack.set_visible_child_name("empty")
        elif not any(matches(self.filter, t) for t in transfers):
            title, text = {"active": (tr("Rien en cours"), tr("Tous les téléchargements sont terminés.")),
                           "done": (tr("Aucun téléchargement terminé"), tr("Ils apparaîtront ici une fois "
                                                                            "complets."))}[self.filter]
            self.none.set_title(title)
            self.none.set_description(text)
            self.stack.set_visible_child_name("none")
        else:
            self.stack.set_visible_child_name("list")

    def set_filter(self, fid: str):
        if fid not in self.side_rows:
            return
        if self.side_list.get_selected_row() is not self.side_rows[fid]:
            self.side_list.select_row(self.side_rows[fid])      # (rappelle _on_filter_row)
            return
        self.filter = fid
        settings["filter"] = fid
        self.title_widget.set_title(dict((f[0], f[1]) for f in FILTERS)[fid])
        self.list.invalidate_filter()
        self._sync_page()

    def _on_filter_row(self, _box, row):
        if row is None:
            return
        self.set_filter(row.fid)
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

    def _on_sidebar_shown(self, *_):
        if not self.split.get_collapsed():
            settings["sidebar_visible"] = self.split.get_show_sidebar()

    # -- sélection -------------------------------------------------------------------------------
    def _build_selection_bar(self) -> Gtk.Widget:
        """Barre d'actions de la sélection (cases rondes), comme dans Fichiers de GNOME : visible dès qu'un
        téléchargement est coché."""
        bar = Gtk.ActionBar()
        self.sel_label = Gtk.Label(css_classes=["heading"])
        bar.set_center_widget(self.sel_label)
        none = Gtk.Button(icon_name="edit-clear-symbolic", tooltip_text=tr("Tout désélectionner (Échap)"),
                          action_name="win.select-none")
        every = Gtk.Button(icon_name="edit-select-all-symbolic", tooltip_text=tr("Tout sélectionner (Ctrl+A)"),
                           action_name="win.select-all")
        bar.pack_start(none)
        bar.pack_start(every)
        for icon, tip, action in (("user-trash-symbolic", tr("Supprimer…"), "win.remove-selected"),
                                  ("folder-symbolic", tr("Déplacer…"), "win.move-selected"),
                                  ("media-playback-pause-symbolic", tr("Mettre en pause"), "win.pause-selected"),
                                  ("media-playback-start-symbolic", tr("Reprendre"), "win.resume-selected")):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip, action_name=action)
            if action == "win.remove-selected":
                b.add_css_class("destructive-action")
            bar.pack_end(b)
        self.sel_revealer = Gtk.Revealer(child=bar, reveal_child=False,
                                         transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        return self.sel_revealer

    def set_selected(self, tid: str, on: bool):
        if on:
            self.selected.add(tid)
        else:
            self.selected.discard(tid)
        self._sync_selection()

    def select_all(self):
        """Ctrl+A : les téléchargements de la vue affichée."""
        for tid, row in self.rows.items():
            if matches(self.filter, row.transfer):
                self.selected.add(tid)
        self._sync_selection()

    def select_none(self):
        self.selected.clear()
        self._sync_selection()

    def _sync_selection(self):
        self.selected &= set(self.rows)
        for tid, row in self.rows.items():
            on = tid in self.selected
            if row.check.get_active() != on:
                row.check.set_active(on)
            (row.add_css_class if on else row.remove_css_class)("selected-transfer")
        n = len(self.selected)
        self.sel_revealer.set_reveal_child(n > 0)
        self.sel_label.set_label(ntr("{0} sélectionné", "{0} sélectionnés", n).format(n))
        for name in ("pause-selected", "resume-selected", "move-selected", "remove-selected", "select-none"):
            self.lookup_action(name).set_enabled(n > 0)

    def _selected_transfers(self) -> list:
        out = []
        for tid in list(self.selected):
            t = self.app.engine.transfer(tid)
            if t is not None:
                out.append(t)
        return out

    def pause_selected(self):
        for t in self._selected_transfers():
            if t.state not in ("paused", "done", "moving"):
                self.app.engine.pause(t.tid)
        self.app.refresh()

    def resume_selected(self):
        for t in self._selected_transfers():
            if t.state in ("paused", "done", "error"):
                self.app.engine.resume(t.tid)
        self.app.refresh()

    def ask_move(self, tids: list):
        """« Déplacer… » : un dossier, puis les fichiers y partent (pendant ou après le téléchargement)."""
        tids = [t for t in tids if self.app.engine.transfer(t) is not None]
        if not tids:
            return
        first = self.app.engine.transfer(tids[0])
        choose_folder(self, tr("Déplacer vers"), first.save_path, lambda folder: self.move_to(tids, folder))

    def move_to(self, tids: list, folder: str):
        if not folder_usable(self, folder):
            return
        started = [tid for tid in tids if self.app.engine.move(tid, folder)]
        if started:
            toast(self, ntr("Déplacement de {0} téléchargement…", "Déplacement de {0} téléchargements…",
                            len(started)).format(len(started)))
        self.app.refresh()

    def moved(self, tid: str, ok: bool, message: str):
        """Fin d'un déplacement (moteur) : seul un échec est signalé."""
        if not ok:
            t = self.app.engine.transfer(tid)
            alert(self, tr("Impossible de déplacer « {0} »").format(shown(t.name if t else tid)),
                  message + "\n\n" + tr("Les fichiers sont restés où ils étaient. Un fichier du même nom "
                                          "dans le dossier choisi n'est jamais écrasé."))
        self.app.refresh()

    def toggle_transfer(self, tid: str):
        t = self.app.engine.transfer(tid)
        if t is None:
            return
        if t.state in ("paused", "done", "error"):
            self.app.engine.resume(tid)
        else:
            self.app.engine.pause(tid)
        self.app.refresh()

    def open_transfer(self, tid: str):
        path = self.app.engine.content_path(tid)
        if path and os.path.exists(path):
            open_with_default(self, path, folder=False)
        else:
            toast(self, tr("Les fichiers ne sont plus là"))

    def show_transfer(self, tid: str):
        path = self.app.engine.content_path(tid)
        if path and os.path.exists(path):
            show_in_folder(self, path)
        else:
            toast(self, tr("Les fichiers ne sont plus là"))

    def set_file_wanted(self, tid: str, index: int, wanted: bool):
        """⏸ / ▶ d'un fichier déplié : le même réglage que la case de « Fichiers… »."""
        self.app.engine.set_wanted(tid, index, wanted)
        self.app.refresh()
        d = self.files_dialogs.get(tid)
        if d is not None:
            d.refresh()

    def ask_delete_file(self, tid: str, index: int, name: str):
        """🗑 d'un fichier déplié : effacé du disque après confirmation, les autres fichiers ne bougent pas."""
        def done(response):
            if response != "delete":
                return
            try:
                ok = self.app.engine.delete_file(tid, index)
            except eng.RenameError:
                toast(self, tr("Impossible pour l'instant (vérification ou déplacement en cours)"))
                return
            if ok:
                toast(self, tr("« {0} » supprimé").format(os.path.basename(name)))
            self.app.refresh()
            d = self.files_dialogs.get(tid)
            if d is not None:
                d.refresh()
        alert(self, tr("Supprimer « {0} » ?").format(os.path.basename(name)),
              tr("Le fichier est effacé du disque et ne sera plus téléchargé. Les autres fichiers du torrent ne "
                 "sont pas touchés ; le torrent est revérifié, ce qui peut prendre un moment pour un gros torrent."),
              responses=(("cancel", tr("Annuler")), ("delete", tr("Supprimer"))), default="cancel",
              destructive="delete", callback=done)

    def ask_rename(self, tid: str):
        """« Renommer… » d'une ligne : le dossier du torrent, ou son fichier s'il est seul (comme qBittorrent).
        Les fichiers d'un dossier se renomment un par un dans « Fichiers… »."""
        from .dialogs import root_of
        paths = self.app.engine.paths(tid)
        if not paths:
            return None
        root = root_of(paths)
        folder = bool(root)
        current = root or next(iter(paths.values()))
        d = RenameDialog(tr("Renommer le dossier") if folder else tr("Renommer le fichier"), current,
                         lambda name: self.rename(tid, root=name), folder=folder)
        d.present(self)
        return d

    def rename(self, tid: str, index=None, name: str = "", root=None):
        """Renomme (moteur) : message d'erreur à afficher, ou None."""
        from .dialogs import rename_error
        try:
            self.app.engine.rename(tid, index=index, name=name, root=root)
        except eng.RenameError as e:
            return rename_error(e.code)
        self.app.refresh()
        d = self.files_dialogs.get(tid)
        if d is not None:
            d.refresh()
        return None

    def show_files(self, tid: str):
        from .dialogs import FilesDialog
        d = self.files_dialogs.get(tid)
        if d is None:
            d = FilesDialog(self, tid)
            self.files_dialogs[tid] = d
            d.connect("closed", lambda *_: self.files_dialogs.pop(tid, None))
        d.present(self)

    def ask_remove(self, tids):
        """Un téléchargement (menu de la ligne) ou plusieurs (sélection)."""
        if isinstance(tids, str):
            tids = [tids]
        transfers = [t for t in (self.app.engine.transfer(x) for x in tids) if t is not None]
        if not transfers:
            return

        def answered(response):
            if response in ("remove", "delete"):
                for t in transfers:
                    d = self.files_dialogs.pop(t.tid, None)
                    if d is not None:
                        d.close()
                    self.app.engine.remove(t.tid, delete_files=response == "delete")
                    self.selected.discard(t.tid)
                self.app.refresh()
        heading = (tr("Supprimer « {0} » ?").format(shown(transfers[0].name)) if len(transfers) == 1 else
                   ntr("Supprimer {0} téléchargement ?", "Supprimer {0} téléchargements ?",
                       len(transfers)).format(len(transfers)))
        alert(self, heading,
              tr("« Retirer de la liste » garde les fichiers déjà téléchargés."),
              responses=(("cancel", tr("Annuler")), ("remove", tr("Retirer de la liste")),
                         ("delete", tr("Supprimer aussi les fichiers"))),
              default="remove", destructive="delete", callback=answered)

    # -- fermeture -------------------------------------------------------------------------------
    def save_state(self):
        if self.get_visible():
            settings["window_width"], settings["window_height"] = self.get_default_size()
            settings["window_maximized"] = self.is_maximized()

    def _on_close_request(self, *_):
        self.save_state()
        busy = any(t.active for t in self.app.transfers)
        choice = settings["on_close"]
        if not busy or choice == "quit":
            GLib.idle_add(lambda: (self.app.quit_all(), False)[1])
            return True
        if choice == "background":
            self.app.hide_to_background()
            return True
        remember = Gtk.CheckButton(label=tr("Ne plus demander"), halign=Gtk.Align.CENTER)

        def answered(response):
            if response not in ("quit", "background"):
                return
            if remember.get_active():
                settings["on_close"] = response
            if response == "background":
                self.app.hide_to_background()
            else:
                self.app.quit_all()
        alert(self, tr("Continuer en arrière-plan ?"),
              tr("Télémaque peut continuer à télécharger et à partager sans fenêtre. Relancez-le pour la "
                 "retrouver."),
              responses=(("cancel", tr("Annuler")), ("quit", tr("Quitter")),
                         ("background", tr("Continuer en arrière-plan"))),
              default="background", suggested="background", callback=answered, extra=remember)
        return True

