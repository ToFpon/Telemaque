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
from .common import (DialogWindow, RenameDialog, choose_folder, dialog_shell, focus_later, folder_usable,  # noqa: E402
                     path_row, shown)

MAX_LISTED = 2000             # au-delà, pas de cases une par une (packs immenses) : tout est gardé


def rename_error(code: str) -> str:
    """Message d'un nom refusé par le moteur (RenameError.code)."""
    return {
        "empty": tr("Le nom ne peut pas être vide."),
        "dot": tr("« . » et « .. » ne sont pas des noms possibles."),
        "slash": tr("Le nom ne peut pas contenir de « / »."),
        "long": tr("Ce nom est trop long."),
        "taken": tr("Un autre fichier de ce torrent porte déjà ce nom."),
        "exists": tr("Un fichier ou un dossier de ce nom existe déjà à cet endroit. Il n'est jamais écrasé."),
        "busy": tr("Impossible pour l'instant (informations du torrent attendues, vérification ou déplacement "
                   "en cours) : réessayez dans un moment."),
    }.get(code, code)


def root_of(paths: dict) -> str:
    """Dossier d'un torrent de plusieurs fichiers (premier élément commun), "" pour un fichier seul."""
    if not paths or not all(os.sep in p for p in paths.values()):
        return ""
    return next(iter(paths.values())).split(os.sep, 1)[0]


def _strip_root(path: str, name: str) -> str:
    """« Série/Bonus/x.mkv » → « Bonus/x.mkv » (le nom du torrent est déjà le titre)."""
    prefix = name + os.sep
    return path[len(prefix):] if path.startswith(prefix) else path


class FileList(Gtk.ListBox):
    """Fichiers avec une case chacun (garder / laisser), leur taille et, en cours, leur avancement."""

    def __init__(self, files, root_name: str, on_toggle=None, on_rename=None):
        super().__init__(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self.on_toggle = on_toggle
        self.root = root_name
        self.checks: dict[int, Gtk.CheckButton] = {}
        self.sizes: dict[int, Gtk.Label] = {}
        self.rows: dict[int, Adw.ActionRow] = {}
        self.files = {f.index: f for f in files}
        for f in sorted(files, key=lambda f: f.path.casefold()):
            row = Adw.ActionRow(title=shown(_strip_root(f.path, root_name)), use_markup=False)
            row.set_title_lines(2)
            self.rows[f.index] = row
            check = Gtk.CheckButton(active=f.wanted, valign=Gtk.Align.CENTER)
            check.connect("toggled", self._toggled, f.index)
            row.add_prefix(check)
            row.set_activatable_widget(check)
            label = Gtk.Label(css_classes=["dim-label", "numeric"])
            row.add_suffix(label)
            if on_rename is not None:
                pen = Gtk.Button(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER,
                                 tooltip_text=tr("Renommer…"), css_classes=["flat", "rename-file"])
                pen.connect("clicked", lambda _b, i=f.index: on_rename(i))
                row.add_suffix(pen)
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

    def set_paths(self, paths: dict, root: str):
        """Noms changés (renommés) : titres des lignes mis à jour."""
        self.root = root
        for i, p in paths.items():
            row = self.rows.get(i)
            if row is not None:
                title = shown(_strip_root(p, root))
                if row.get_title() != title:
                    row.set_title(title)

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
        self.title = title
        self.paths = {f.index: f.path for f in info.files} if info else {}
        if info:
            pen = Gtk.Button(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                             tooltip_text=tr("Renommer le dossier…") if root_of(self.paths)
                             else tr("Renommer le fichier…"))
            pen.connect("clicked", lambda *_: self.ask_rename())
            title.add_suffix(pen)
            self.rename_button = pen
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
            self.files = FileList(info.files, info.name, on_toggle=lambda *_: self._sync(),
                                  on_rename=self.ask_rename)
            b_all.connect("clicked", lambda *_: self.files.set_all(True))
            b_none.connect("clicked", lambda *_: self.files.set_all(False))
            group.add(self.files)
            page.add(group)
        self.action.connect("clicked", lambda *_: self._start())
        self._sync()

    def present(self, parent=None):
        self.dialog.present(parent or self.win)

    def ask_rename(self, index=None):
        """Crayon du titre (dossier du torrent, ou le fichier s'il est seul) ou d'un fichier."""
        root = root_of(self.paths)
        if index is None and not root:
            index = next(iter(self.paths))
        folder = index is None
        current = root if folder else os.path.basename(self.paths[index])
        d = RenameDialog(tr("Renommer le dossier") if folder else tr("Renommer le fichier"), current,
                         lambda name: self.rename(index, name), folder=folder)
        d.present(self.dialog)
        return d

    def rename(self, index, name: str):
        """Nouveau nom (rien n'existe encore sur le disque) : message d'erreur, ou None."""
        try:
            self.paths = (eng.renamed(self.paths, root=name) if index is None
                          else eng.renamed(self.paths, index, name))
        except eng.RenameError as e:
            return rename_error(e.code)
        root = root_of(self.paths)
        self.title.set_title(shown(root or next(iter(self.paths.values()))))
        if self.files is not None:
            self.files.set_paths(self.paths, root)
        return None

    def _browse(self, row):
        def chosen(p):
            if folder_usable(self.dialog, p):
                row.set_text(p)
        choose_folder(self.dialog, tr("Enregistrer dans"), row.get_text(), chosen)

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
        if not os.path.isabs(folder):        # « Films » tapé à la main : dans le dossier personnel, pas
            folder = os.path.join(os.path.expanduser("~"), folder)       # dans le dossier courant du processus
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
        names = None
        if self.info is not None and any(self.paths[f.index] != f.path for f in self.info.files):
            names = self.paths
        self.dialog.close()
        self.win.start(torrent_path=self.torrent_path, magnet=self.magnet, save_path=folder, wanted=wanted,
                       paused=self.paused.get_active(), names=names)


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
        self.list = FileList(files, root_of({f.index: f.path for f in files}), on_toggle=self._toggle,
                             on_rename=self.ask_rename)
        group.add(self.list)
        page.add(group)
        tv.set_content(page)
        self.set_child(tv)

    def _toggle(self, index: int, wanted: bool):
        self.win.app.engine.set_wanted(self.tid, index, wanted)
        self.win.app.refresh()

    def ask_rename(self, index: int):
        paths = self.win.app.engine.paths(self.tid)
        if index not in paths:
            return None
        d = RenameDialog(tr("Renommer le fichier"), os.path.basename(paths[index]),
                         lambda name: self.win.rename(self.tid, index=index, name=name))
        d.present(self)
        return d

    def refresh(self):
        files = self.win.app.engine.files(self.tid)
        self.list.refresh(files)
        paths = {f.index: f.path for f in files}
        self.list.set_paths(paths, root_of(paths))
