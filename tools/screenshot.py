#!/usr/bin/env python3
"""Capture de la fenêtre de Télémaque (Gtk.WidgetPaintable), pour la documentation et les essais.
Un second moteur local partage de faux contenus : la liste montre de vrais téléchargements (en cours,
terminé, en pause, en partage).

    xvfb-run -a -s "-screen 0 1400x1000x24" python3.12 tools/screenshot.py SORTIE.png [options]

  --size=LxH        taille de la fenêtre (760x520)
  --dark            thème sombre
  --empty           accueil (aucun téléchargement)
  --add             dialogue d'ajout d'un torrent (choix des fichiers)
  --files           fenêtre « Fichiers » d'un téléchargement
  --prefs=PAGE      Préférences (general, network, features)
  --close           question « Continuer en arrière-plan ? »
  --select          deux téléchargements cochés (barre d'actions de la sélection)
  --delay=MS        attente avant la capture (5000)
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="telemaque-shot-")
os.environ["HOME"] = TMP
os.environ["XDG_CONFIG_HOME"] = os.path.join(TMP, ".config")
os.environ["XDG_DATA_HOME"] = os.path.join(TMP, ".local", "share")
os.environ["XDG_CACHE_HOME"] = os.path.join(TMP, ".cache")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Graphene, Gtk  # noqa: E402

import samples  # noqa: E402

args = {}
for a in sys.argv[2:]:
    k, _, v = a.lstrip("-").partition("=")
    args[k] = v or True
out = os.path.abspath(sys.argv[1])

from telemaque.app import TelemaqueApp  # noqa: E402
from telemaque.backend import engine as eng  # noqa: E402
from telemaque.settings import settings  # noqa: E402

DST = os.path.join(TMP, "Téléchargements")
SRC = os.path.join(TMP, "partage")
os.makedirs(DST)
settings["download_dir"] = DST
settings["window_width"], settings["window_height"] = map(int, str(args.get("size", "900x520")).split("x"))
if args.get("dark"):
    settings["theme"] = "dark"

DEMO = [  # nom, tailles des fichiers, état voulu
    ("The Odyssey — Audiobook (Librivox)", (9_000_000, 7_000_000, 5_000_000), "downloading"),
    ("Big Buck Bunny 1080p", (24_000_000,), "seeding"),
    ("ubuntu-24.04.1-desktop-amd64", (30_000_000,), "paused"),
    ("Sintel — Blender Open Movie", (18_000_000, 120_000), "done"),
]
seeder = eng.Engine(os.path.join(TMP, "seed"), listen_port=47911, local_only=True, seed_policy="always")
torrents = []
for name, sizes, state in DEMO:
    folder = os.path.join(SRC, name)
    os.makedirs(folder, exist_ok=True)
    for i, size in enumerate(sizes):
        with open(os.path.join(folder, f"partie {i + 1}.bin"), "wb") as fh:
            fh.write(os.urandom(size))
    t = samples.make_torrent(folder, os.path.join(TMP, name + ".torrent"), piece=262144)
    seeder.add_torrent_file(t, SRC)
    torrents.append((t, state))
GLib.timeout_add(200, lambda: (seeder.poll(), True)[1])

leecher = eng.Engine(os.path.join(TMP, ".local", "share", "telemaque", "torrents"), listen_port=47912,
                     local_only=True, seed_policy="always")
app = TelemaqueApp(engine=leecher)
app.send_notification = lambda *a: None


def setup(win):
    if args.get("empty"):
        GLib.timeout_add(500, later, win)
        return
    for path, state in torrents:                    # d'abord ceux qui seront complets
        if state in ("seeding", "done"):
            tid = leecher.add_torrent_file(path, DST)
            leecher.connect_peer(tid, "127.0.0.1", 47911)
    GLib.timeout_add(2000, slow, win)


def slow(win):
    for path, state in torrents:
        tid = eng.read_torrent(path).tid
        if state == "done":
            leecher.pause(tid)
        elif state in ("downloading", "paused"):
            tid = leecher.add_torrent_file(path, DST)
            # (les limites générales ne touchent pas le réseau local : limite propre au téléchargement)
            leecher.handles[tid].set_download_limit(1_400_000 if state == "downloading" else 3_000_000)
            leecher.connect_peer(tid, "127.0.0.1", 47911)
    GLib.timeout_add(2500, later, win)


def later(win):
    for path, state in torrents:
        if state == "paused":
            leecher.pause(eng.read_torrent(path).tid)
    app.refresh()
    if args.get("add"):
        extra = os.path.join(SRC, "Le Voyage de Télémaque")
        os.makedirs(os.path.join(extra, "Bonus"), exist_ok=True)
        for n, size in (("01 - Le départ.mkv", 70_000_000), ("02 - Pylos.mkv", 65_000_000),
                        ("03 - Sparte.mkv", 69_000_000), (os.path.join("Bonus", "Making-of.mkv"), 12_000_000),
                        ("lisez-moi.txt", 2_000)):
            with open(os.path.join(extra, n), "wb") as fh:
                fh.truncate(size)
        t = samples.make_torrent(extra, os.path.join(TMP, "telemaque.torrent"), piece=4 * 1024 * 1024)
        win.add_torrent_file(t)
    if args.get("select"):                          # deux téléchargements cochés : barre de la sélection
        tids = [eng.read_torrent(path).tid for path, _s in torrents[:2]]
        GLib.timeout_add(1200, lambda: ([win.set_selected(t, True) for t in tids], False)[1])
    if args.get("files"):
        tid = eng.read_torrent(torrents[0][0]).tid
        GLib.timeout_add(1500, lambda: (win.show_files(tid), False)[1])
    if args.get("prefs"):
        from telemaque.ui.preferences import PreferencesWindow
        p = PreferencesWindow()
        p.set_transient_for(win)
        p.set_visible_page_name(str(args["prefs"]))
        p.present()
    if args.get("close"):
        GLib.timeout_add(1500, lambda: (win.close(), False)[1])
    return False


def shot(win, delay):
    def do():
        paint = Gtk.WidgetPaintable.new(win)
        w, h = win.get_width(), win.get_height()
        snapshot = Gtk.Snapshot()
        paint.snapshot(snapshot, w, h)
        for d in Gtk.Window.list_toplevels():
            if d is not win and d.get_visible() and d.get_transient_for() is win:
                dw, dh = d.get_width(), d.get_height()
                snapshot.save()
                snapshot.translate(Graphene.Point().init(max((w - dw) / 2, 0), max((h - dh) / 2, 0)))
                Gtk.WidgetPaintable.new(d).snapshot(snapshot, dw, dh)
                snapshot.restore()
        tex = win.get_renderer().render_texture(snapshot.to_node(), None)
        tex.save_to_png(out)
        print("capture", out, w, h)
        settings["on_close"] = "quit"
        app.quit_all()
        seeder.session.pause()
        shutil.rmtree(TMP, ignore_errors=True)
        return False
    GLib.timeout_add(delay, do)


def on_window(_a, win):
    if getattr(app, "_shot", False):
        return
    app._shot = True
    setup(win)
    shot(win, int(args.get("delay", 6500)))


app.connect("window-added", on_window)
app.run([sys.argv[0]])
