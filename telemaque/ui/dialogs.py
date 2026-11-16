"""Dialogues : ajouter un torrent (dossier, fichiers à garder), coller un lien magnet, fichiers d'un
téléchargement en cours."""

from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

import libtorrent as lt  # noqa: E402

from .. import format as fmt  # noqa: E402
from ..backend import engine as eng  # noqa: E402
from ..i18n import ntr, tr  # noqa: E402
from ..settings import settings  # noqa: E402
from .common import DialogWindow, choose_folder, dialog_shell, focus_later, path_row, shown  # noqa: E402

MAX_LISTED = 2000             # au-delà, pas de cases une par une (packs immenses) : tout est gardé


def _strip_root(path: str, name: str) -> str:
    """« Série/Bonus/x.mkv » → « Bonus/x.mkv » (le nom du torrent est déjà le titre)."""
    prefix = name + os.sep
    return path[len(prefix):] if path.startswith(prefix) else path


class FileList(Gtk.ListBox):
    """Fichiers avec une case chacun (garder / laisser), leur taille et, en cours, leur avancement."""

    def __init__(self, files, root_name: str, on_toggle=None):
        super().__init__(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self.on_toggle = on_toggle
        self.checks: dict[int, Gtk.CheckButton] = {}
        self.sizes: dict[int, Gtk.Label] = {}
        self.files = {f.index: f for f in files}
        for f in sorted(files, key=lambda f: f.path.casefold()):
            row = Adw.ActionRow(title=shown(_strip_root(f.path, root_name)), use_markup=False)
            row.set_title_lines(2)
            check = Gtk.CheckButton(active=f.wanted, valign=Gtk.Align.CENTER)
            check.connect("toggled", self._toggled, f.index)
            row.add_prefix(check)
            row.set_activatable_widget(check)
            label = Gtk.Label(css_classes=["dim-label", "numeric"])
            row.add_suffix(label)
            self.checks[f.index] = check
            self.sizes[f.index] = label
            self.append(row)
        self.refresh(files)

    def _toggled(self, check, index):
        if self.on_toggle:
            self.on_toggle(index, check.get_active())

    def wanted(self) -> list:
        return [i for i, c in self.checks.items() if c.get_active()]

    def set_all(self, value: bool):
        for c in self.checks.values():
            c.set_active(value)

    def refresh(self, files):
        for f in files:
            label = self.sizes.get(f.index)
            if label is None:
                continue
            if f.done and f.wanted and f.done < f.size:
                label.set_label(tr("{0} sur {1}").format(fmt.size(f.done), fmt.size(f.size)))
            else:
                label.set_label(fmt.size(f.size))
            check = self.checks[f.index]
            if check.get_active() != f.wanted:
                check.handler_block_by_func(self._toggled)
                check.set_active(f.wanted)
                check.handler_unblock_by_func(self._toggled)


class AddDialog:
    """Avant de lancer : où enregistrer, et (fichier .torrent) quels fichiers garder. Un lien magnet ne
    connaît ses fichiers qu'une fois les informations reçues : on choisit alors par « Fichiers… »."""

    def __init__(self, win, info: eng.TorrentInfo = None, torrent_path: str = "", magnet: str = ""):
        self.win = win
        self.info = info
        self.torrent_path = torrent_path
        self.magnet = magnet
        self.save_path = settings.download_dir()
        name = info.name if info else _magnet_name(magnet)
        self.dialog, page, self.action = dialog_shell(tr("Ajouter un torrent"), tr("Télécharger"),
                                                      width=560, height=640 if info and len(info.files) > 1 else 420)
        head = Adw.PreferencesGroup()
        title = Adw.ActionRow(title=shown(name) or tr("Lien magnet"), use_markup=False,
                              subtitle=(fmt.size(info.size) + " — " + ntr("{0} fichier", "{0} fichiers",
                                                                         len(info.files)).format(len(info.files))
                                        if info else tr("La liste des fichiers arrivera avec les informations du "
                                                        "torrent")))
        title.set_subtitle_selectable(False)
        title.set_title_lines(3)
        head.add(title)
        page.add(head)
        where = Adw.PreferencesGroup(title=tr("Enregistrer dans"))
        self.folder = path_row(tr("Dossier"), self.save_path, self._browse)
        self.folder.set_show_apply_button(False)
        where.add(self.folder)
        self.paused = Adw.SwitchRow(title=tr("Ajouter en pause"), active=False)
        where.add(self.paused)
        page.add(where)
        self.files = None
        if info and 1 < len(info.files) <= MAX_LISTED:
            group = Adw.PreferencesGroup(title=tr("Fichiers"))
            all_none = Gtk.Box(spacing=6, css_classes=["linked"])
            b_all = Gtk.Button(label=tr("Tout"), css_classes=["flat"])
            b_none = Gtk.Button(label=tr("Rien"), css_classes=["flat"])
            all_none.append(b_all)
            all_none.append(b_none)
            group.set_header_suffix(all_none)
            self.files = FileList(info.files, info.name, on_toggle=lambda *_: self._sync())
            b_all.connect("clicked", lambda *_: self.files.set_all(True))
            b_none.connect("clicked", lambda *_: self.files.set_all(False))
            group.add(self.files)
            page.add(group)
        self.action.connect("clicked", lambda *_: self._start())
        self._sync()

    def present(self, parent=None):
        self.dialog.present(parent or self.win)

    def _browse(self, row):
        choose_folder(self.dialog, tr("Enregistrer dans"), row.get_text(), lambda p: row.set_text(p))

    def _sync(self):
        if self.files is None:
            self.action.set_sensitive(True)
            return
        wanted = self.files.wanted()
        self.action.set_sensitive(bool(wanted))
        keep = set(wanted)
        total = sum(f.size for f in self.info.files if f.index in keep)
        self.action.set_tooltip_text(fmt.size(total))

    def _start(self):
        folder = os.path.expanduser(self.folder.get_text().strip())
        if not folder:
            return
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            from .common import alert
            alert(self.dialog, tr("Impossible d'utiliser ce dossier"), str(e))
            return
        if not os.access(folder, os.W_OK):
            from .common import alert
            alert(self.dialog, tr("Impossible d'utiliser ce dossier"), tr("Vous n'avez pas le droit d'y écrire."))
            return
        wanted = self.files.wanted() if self.files is not None else None
        self.dialog.close()
        self.win.start(torrent_path=self.torrent_path, magnet=self.magnet, save_path=folder, wanted=wanted,
                       paused=self.paused.get_active())


def _magnet_name(uri: str) -> str:
    try:
        return lt.parse_magnet_uri(uri).name or ""
    except (RuntimeError, ValueError):
        return ""


class MagnetEntryDialog(DialogWindow):
    """« Ajouter un lien magnet » : champ prérempli si le presse-papiers en contient un."""

    def __init__(self, win):
        super().__init__(title=tr("Ajouter un lien magnet"), content_width=520, content_height=-1)
        self.win = win
        tv = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label=tr("Annuler"))
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.ok = Gtk.Button(label=tr("Continuer"), css_classes=["suggested-action"], sensitive=False)
        self.ok.connect("clicked", lambda *_: self._go())
        header.pack_end(self.ok)
        tv.add_top_bar(header)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=18, margin_bottom=24,
                      margin_start=18, margin_end=18)
        group = Adw.PreferencesGroup()
        self.entry = Adw.EntryRow(title=tr("Lien magnet"))
        self.entry.connect("changed", lambda *_: self.ok.set_sensitive(eng.is_magnet(self.entry.get_text())))
        self.entry.connect("entry-activated", lambda *_: self._go())
        group.add(self.entry)
        box.append(group)
        tv.set_content(box)
        self.set_child(tv)
        self.get_clipboard().read_text_async(None, self._clipboard)
        focus_later(self.entry)

    def _clipboard(self, cb, res):
        try:
            text = (cb.read_text_finish(res) or "").strip()
        except GLib.Error:
            return
        if eng.is_magnet(text) and not self.entry.get_text():
            self.entry.set_text(text)

    def _go(self):
        text = self.entry.get_text().strip()
        if not eng.is_magnet(text):
            return
        self.close()
        self.win.add_magnet(text)


class FilesDialog(DialogWindow):
    """Fichiers d'un téléchargement : cocher / décocher en cours de route, avancement de chacun."""

    def __init__(self, win, tid: str):
        t = win.app.engine.transfer(tid)
        super().__init__(title=tr("Fichiers"), content_width=560, content_height=560)
        self.win = win
        self.tid = tid
        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=tr("Fichiers"), subtitle=shown(t.name) if t else ""))
        tv.add_top_bar(header)
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(description=tr("Décochez un fichier pour ne pas le télécharger."))
        files = win.app.engine.files(tid)
        self.list = FileList(files, t.name if t else "", on_toggle=self._toggle)
        group.add(self.list)
        page.add(group)
        tv.set_content(page)
        self.set_child(tv)

    def _toggle(self, index: int, wanted: bool):
        self.win.app.engine.set_wanted(self.tid, index, wanted)
        self.win.app.refresh()

    def refresh(self):
        self.list.refresh(self.win.app.engine.files(self.tid))
