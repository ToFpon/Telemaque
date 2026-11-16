#!/usr/bin/env python3
"""Test de fumée de l'interface : une vraie fenêtre (sous xvfb-run) et un vrai échange torrent local
(un second moteur dans le même processus partage les fichiers), et toute exception ou tout
avertissement critique de GTK fait échouer.

    xvfb-run -a -s "-screen 0 1280x900x24" python3.12 tests/ui_smoke.py   # doit finir par « Aucune erreur. »
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
TMP = tempfile.mkdtemp(prefix="telemaque-ui-")
os.environ["HOME"] = os.path.join(TMP, "home")
for var, sub in (("XDG_CONFIG_HOME", ".config"), ("XDG_DATA_HOME", ".local/share"), ("XDG_CACHE_HOME", ".cache")):
    os.environ[var] = os.path.join(TMP, "home", sub)
os.makedirs(os.environ["HOME"])

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

import libtorrent as lt  # noqa: E402

errors = []


def hook(t, v, tb):
    errors.append("".join(traceback.format_exception(t, v, tb)))
    sys.__excepthook__(t, v, tb)


sys.excepthook = hook

import samples  # noqa: E402
from telemaque.app import TelemaqueApp  # noqa: E402
from telemaque.backend.engine import Engine  # noqa: E402
from telemaque.i18n import ntr, tr  # noqa: E402
from telemaque.settings import settings  # noqa: E402
from telemaque.ui.window import status_text  # noqa: E402

SRC = os.path.join(TMP, "partage")
DST = os.path.join(TMP, "home", "Téléchargements")
os.makedirs(DST)
CONTENT = samples.make_content(SRC)
TORRENT = samples.make_torrent(CONTENT, os.path.join(TMP, "Série.torrent"))
FILM = samples.make_single(SRC, "film & co.mkv")
FILM_TORRENT = samples.make_torrent(FILM, os.path.join(TMP, "film.torrent"))
MAGNET = lt.make_magnet_uri(lt.torrent_info(FILM_TORRENT))
BAD = os.path.join(TMP, "abîmé.torrent")
with open(BAD, "wb") as fh:
    fh.write(b"rien du tout")

SEED_PORT = 47811
seeder = Engine(os.path.join(TMP, "seed"), listen_port=SEED_PORT, local_only=True, seed_policy="always")
seeder.add_torrent_file(TORRENT, SRC)
seeder.add_torrent_file(FILM_TORRENT, SRC)
GLib.timeout_add(200, lambda: (seeder.poll(), True)[1])

settings["download_dir"] = DST
settings["seed_policy"] = "always"
leecher = Engine(os.path.join(TMP, "home", ".local", "share", "telemaque", "torrents"), listen_port=47812,
                 local_only=True, seed_policy="always")
app = TelemaqueApp(engine=leecher)
notified = []
app.send_notification = lambda nid, n: notified.append(nid)
steps = []
log = []


def step(fn):
    steps.append(fn)
    return fn


class Wait:
    """Les étapes sont des générateurs : « yield Wait(s) » rend la main à la vraie boucle GLib."""
    def __init__(self, seconds=0.4, until=None):
        self.seconds = seconds
        self.until = until


CRITICALS = []
_err_r, _err_w = os.pipe()
_err_orig = os.dup(2)
os.dup2(_err_w, 2)


def _tee_stderr():
    with os.fdopen(_err_r, "rb", 0) as f:
        for line in iter(f.readline, b""):
            os.write(_err_orig, line)
            if b"-CRITICAL **" in line or b"-WARNING **" in line and b"a11y" not in line \
                    and b"does not have a minimum size" not in line:
                CRITICALS.append(line.decode("utf-8", "replace").strip())


threading.Thread(target=_tee_stderr, daemon=True).start()


def check(cond, msg):
    if not cond:
        errors.append("ÉCHEC : " + msg)
        print("✗", msg)
    else:
        print("✓", msg)


def run_all(win):
    def driver():
        for fn in steps:
            log.append(fn.__name__)
            gen = fn(win)
            if gen is not None and hasattr(gen, "__next__"):
                yield from gen
            yield Wait(0.2)

    it = driver()

    def tick():
        try:
            w = next(it)
        except StopIteration:
            app.quit_all()
            return False
        except Exception:
            errors.append(traceback.format_exc())
            app.quit_all()
            return False
        start = time.time()

        def poll():
            if w.until is not None:
                if w.until() or time.time() - start > w.seconds:
                    GLib.timeout_add(50, tick)
                    return False
                return True
            GLib.timeout_add(50, tick)
            return False
        if w.until is not None:
            GLib.timeout_add(50, poll)
        else:
            GLib.timeout_add(int(w.seconds * 1000), poll)
        return False
    tick()
    return False


def pump(seconds=0.4):
    return Wait(seconds)


def dialogs(win, cls=None):
    out = []
    for w in Gtk.Window.list_toplevels():
        if w is win or not w.get_visible():
            continue
        if cls is None or isinstance(w, cls):
            out.append(w)
    return out


def find(widget, cls, pred=lambda w: True):
    found = []

    def walk(w):
        if isinstance(w, cls) and pred(w):
            found.append(w)
        c = w.get_first_child()
        while c is not None:
            walk(c)
            c = c.get_next_sibling()
    walk(widget)
    return found


def by_name(name):
    return next((t for t in app.transfers if t.name == name), None)


def press_action_button(dialog):
    """Bouton d'action d'un dialog_shell (en haut à droite)."""
    btn = find(dialog, Gtk.Button, lambda b: "suggested-action" in b.get_css_classes() and b.get_label())
    btn[0].emit("clicked")


def respond(dialog, response):
    dialog.response(response)


@step
def accueil(win):
    yield pump(0.4)
    check(win.stack.get_visible_child_name() == "empty", "accueil : « Aucun téléchargement »")


@step
def torrent_abime(win):
    win.add_torrent_file(BAD)
    yield pump(0.4)
    alerts = dialogs(win, Adw.MessageDialog)
    check(bool(alerts) and tr("Ce fichier n'est pas un torrent valide") in alerts[0].get_heading(),
          "fichier abîmé : message clair")
    for a in alerts:
        a.close()
    yield pump(0.2)


@step
def ajouter_un_torrent(win):
    win.add_torrent_file(TORRENT)
    yield pump(0.5)
    dl = [d for d in dialogs(win) if d.get_title() == tr("Ajouter un torrent")]
    check(len(dl) == 1, "dialogue d'ajout ouvert")
    if not dl:
        return
    d = dl[0]
    checks = find(d, Gtk.CheckButton)
    check(len(checks) == 3 and all(c.get_active() for c in checks), "trois fichiers, tous cochés")
    rows = find(d, Adw.ActionRow, lambda r: "Making-of" in r.get_title())
    making = find(rows[0], Gtk.CheckButton)[0] if rows else None
    if making:
        making.set_active(False)                       # pas le making-of
    entry = find(d, Adw.EntryRow)[0]
    check(entry.get_text() == DST, "dossier proposé : Téléchargements")
    press_action_button(d)
    yield Wait(3, until=lambda: by_name("Série") is not None)
    t = by_name("Série")
    check(t is not None and win.stack.get_visible_child_name() == "list", "le téléchargement apparaît dans la liste")
    leecher.connect_peer(t.tid, "127.0.0.1", SEED_PORT)
    yield Wait(20, until=lambda: by_name("Série") and by_name("Série").complete)
    t = by_name("Série")
    check(t is not None and t.complete, "téléchargé (échange local)")
    check(os.path.getsize(os.path.join(DST, "Série", "Épisode 1.mkv")) == 3_000_000, "fichier complet sur le disque")
    check(not os.path.exists(os.path.join(DST, "Série", "Bonus", "Making-of.mkv")), "fichier décoché : pas téléchargé")
    yield Wait(3, until=lambda: notified)
    check(any(t.tid in n for n in notified), "notification de fin envoyée")
    row = win.rows[t.tid]
    check(row.status.get_label().startswith(tr("Terminé, en partage — ↑ {0}").split("{0}")[0]),
          "ligne : « Terminé, en partage »")
    from telemaque.ui.window import peers_text
    check(row.peers.get_label() == peers_text(by_name("Série")) and tr("ratio {0}").split("{0}")[0] in row.peers.get_label(),
          "à droite : sources, pairs, ratio")


@step
def fichiers_en_cours(win):
    t = by_name("Série")
    win.show_files(t.tid)
    yield pump(0.5)
    d = win.files_dialogs.get(t.tid)
    check(d is not None and d.get_visible(), "fenêtre « Fichiers »")
    rows = find(d, Adw.ActionRow, lambda r: "Making-of" in r.get_title())
    check(bool(rows), "Making-of listé")
    if rows:
        find(rows[0], Gtk.CheckButton)[0].set_active(True)
    leecher.connect_peer(t.tid, "127.0.0.1", SEED_PORT)     # (ni tracker ni DHT dans le test)
    # (juste après le changement, libtorrent recalcule ce qui reste : « complete » peut être en retard)
    yield Wait(15, until=lambda: app.engine.files(t.tid) and all(f.done == f.size for f in app.engine.files(t.tid)))
    check(all(f.done == f.size for f in app.engine.files(t.tid)), "coché après coup : téléchargé")
    d.close()
    yield pump(0.3)
    check(t.tid not in win.files_dialogs, "fenêtre « Fichiers » oubliée à la fermeture")


@step
def pause_reprise(win):
    t = by_name("Série")
    row = win.rows[t.tid]
    row.toggle.emit("clicked")
    yield Wait(3, until=lambda: by_name("Série").state in ("paused", "done"))
    check(by_name("Série").state in ("paused", "done"), "pause")
    check(row.toggle.get_icon_name() == "media-playback-start-symbolic", "bouton : reprendre")
    row.toggle.emit("clicked")
    leecher.connect_peer(t.tid, "127.0.0.1", SEED_PORT)
    yield Wait(5, until=lambda: by_name("Série").state == "seeding")
    check(by_name("Série").state == "seeding", "reprise (partage)")


@step
def lien_magnet(win):
    win.add_magnet(MAGNET)
    yield pump(0.5)
    dl = [d for d in dialogs(win) if d.get_title() == tr("Ajouter un torrent")]
    check(len(dl) == 1, "lien magnet : dialogue d'ajout")
    if not dl:
        return
    check(not find(dl[0], Gtk.CheckButton), "lien magnet : pas encore de fichiers à cocher")
    press_action_button(dl[0])
    yield Wait(3, until=lambda: len(app.transfers) == 2)
    tid = next(t.tid for t in app.transfers if t.name != "Série")
    leecher.connect_peer(tid, "127.0.0.1", SEED_PORT)
    yield Wait(20, until=lambda: app.engine.transfer(tid).complete)
    t = app.engine.transfer(tid)
    check(t.complete and t.name == "film & co.mkv", "lien magnet téléchargé, nom retrouvé")
    check(win.rows[tid].name.get_label() == "film & co.mkv", "nom avec « & » affiché tel quel")
    win.add_magnet(MAGNET)
    yield pump(0.3)
    check(not [d for d in dialogs(win) if d.get_title() == tr("Ajouter un torrent")],
          "déjà dans la liste : pas de second ajout")


@step
def barre_laterale(win):
    check(win.split.get_show_sidebar(), "barre latérale affichée")
    app.refresh()                       # (le film s'est téléchargé entre deux relectures)
    counts = {fid: lab.get_label() for fid, lab in win.side_counts.items()}
    check(counts == {"all": "2", "active": "", "done": "2"}, f"compteurs Tous / En cours / Terminés : {counts}")
    win.set_filter("active")
    yield pump(0.3)
    check(win.stack.get_visible_child_name() == "none" and win.none.get_title() == tr("Rien en cours"),
          "« En cours » vide : page à part")
    check(win.title_widget.get_title() == tr("En cours"), "titre : la vue choisie")
    win.set_filter("done")
    yield pump(0.3)
    shown_rows = [r for r in win.rows.values() if r.get_child_visible()]
    check(win.stack.get_visible_child_name() == "list" and len(shown_rows) == 2, "« Terminés » : les deux")
    check(settings["filter"] == "done", "vue retenue")
    win.activate_action("win.sidebar", None)
    yield pump(0.3)
    check(not win.split.get_show_sidebar() and not settings["sidebar_visible"], "F9 : barre masquée, retenu")
    win.activate_action("win.sidebar", None)
    win.set_filter("all")
    yield pump(0.2)


@step
def barre_d_etat(win):
    from telemaque.backend.engine import NetInfo
    from telemaque.ui.window import dht_text, ip_text
    check(win.dht_label.get_label() == tr("DHT : désactivé"), "barre d'état : DHT désactivé (moteur de test)")
    check(win.ip_label.get_label().startswith(tr("IP publique : pas encore connue")), "barre d'état : IP")
    n = NetInfo(ipv4="82.65.1.2", ipv6="2a01:e0a:1::5", port=6881, reachable=True, dht_enabled=True, dht_nodes=342)
    port = tr("port {0}").format(6881) + " " + tr("(joignable)")
    check(ip_text(n) == " · ".join([tr("IPv4 : {0}").format("82.65.1.2"), tr("IPv6 : {0}").format("2a01:e0a:1::5"), port]),
          "IPv4 et IPv6 publiques, port joignable")
    n.ipv4 = ""
    check(ip_text(n).startswith(tr("IPv4 : {0}").format(tr("pas encore connue"))), "IPv4 pas encore connue : dit")
    n.ipv4 = "82.65.1.2"
    n.looked_up = True
    n.ipv6 = ""
    check(ip_text(n).startswith(" · ".join([tr("IPv4 : {0}").format("82.65.1.2"), tr("IPv6 : {0}").format(tr("aucune"))])),
          "vérifiée, pas d'IPv6 : « aucune »")
    n.looked_up = False
    win.app.engine.network_changed()                    # VPN coupé : adresses à revérifier
    win._sync_network()
    check(win.ip_label.get_label().startswith(tr("IP publique : vérification…")), "réseau changé : « vérification… »")
    from telemaque.i18n import ntr
    check(dht_text(n) == ntr("DHT : actif, {0} nœud", "DHT : actif, {0} nœuds", 342).format(342),
          "DHT actif, nombre de nœuds")
    n.dht_nodes = 0
    check(dht_text(n) == tr("DHT : connexion…"), "DHT sans nœud : connexion")
    n.listen_error = "Address already in use"
    check("Address already in use" in ip_text(n), "port indisponible : signalé")
    yield pump(0.1)


@step
def textes_d_etat(win):
    from telemaque.backend.engine import Transfer
    t = Transfer("x", "x", "downloading", 0.5, 2_000_000_000, 1_000_000_000, down_rate=2_100_000, eta=725, peers=4)
    check(status_text(t).count("—") == 2 and tr("{0} restantes").format(tr("{0} min").format(12)) in status_text(t),
          "en cours : taille, vitesse, temps restant")
    t.down_rate = 0
    from telemaque import format as fmt
    of = tr("{0} sur {1}").format(fmt.size(t.done), fmt.size(t.size))
    check(status_text(t) == tr("{0} — en attente de sources").format(of), "sans vitesse : en attente de sources")
    from telemaque.i18n import ntr
    from telemaque.ui.window import peers_text
    t.seeds = 1
    from telemaque.ui.window import _ratio
    check(peers_text(t) == " · ".join([ntr("{0} source", "{0} sources", 1).format(1),
                                       ntr("{0} pair", "{0} pairs", 3).format(3), tr("ratio {0}").format(_ratio(0))]),
          "sources · pairs · ratio")


@step
def supprimer(win):
    tid = next(t.tid for t in app.transfers if t.name != "Série")
    win.ask_remove(tid)
    yield pump(0.4)
    alerts = dialogs(win, Adw.MessageDialog)
    check(bool(alerts), "question « Supprimer ? »")
    if alerts:
        respond(alerts[0], "delete")
    yield Wait(5, until=lambda: not os.path.exists(os.path.join(DST, "film & co.mkv")))
    check(app.engine.transfer(tid) is None and tid not in win.rows, "retiré de la liste")
    check(not os.path.exists(os.path.join(DST, "film & co.mkv")), "fichiers supprimés aussi")
    check(os.path.exists(FILM), "la source n'est pas touchée")


@step
def supprimer_le_fichier_torrent(win):
    from telemaque import features
    extra = samples.make_single(os.path.join(SRC, "x"), "petit.bin", 50_000)
    seeder.add_torrent_file(samples.make_torrent(extra, os.path.join(TMP, "seed-petit.torrent")), os.path.dirname(extra))
    copy = os.path.join(TMP, "home", "petit.torrent")
    shutil.copy(os.path.join(TMP, "seed-petit.torrent"), copy)
    features.set_enabled("delete_torrent", True)
    try:
        win.add_torrent_file(copy)
        yield pump(0.5)
        dl = [d for d in dialogs(win) if d.get_title() == tr("Ajouter un torrent")]
        if dl:
            press_action_button(dl[0])
        yield Wait(5, until=lambda: not os.path.exists(copy))
        check(not os.path.exists(copy), "option : fichier .torrent supprimé après l'ajout")
        check(by_name("petit.bin") is not None, "le téléchargement, lui, est bien là")
    finally:
        features.set_enabled("delete_torrent", False)


@step
def selection_multiple(win):
    """Cases rondes : sélection de plusieurs téléchargements, pause / reprise / déplacement / suppression."""
    app.refresh()
    tids = [t.tid for t in app.transfers]
    check(len(tids) == 2, f"deux téléchargements pour l'essai ({[t.name for t in app.transfers]})")
    check(not win.sel_revealer.get_reveal_child(), "rien de coché : pas de barre de sélection")
    for tid in tids:
        win.rows[tid].check.set_active(True)              # comme un clic sur la case
    yield pump(0.3)
    check(win.sel_revealer.get_reveal_child() and win.selected == set(tids), "deux cochés : barre de sélection")
    check(win.sel_label.get_label() == ntr("{0} sélectionné", "{0} sélectionnés", 2).format(2), "« 2 sélectionnés »")
    win.activate_action("win.pause-selected", None)
    yield Wait(4, until=lambda: all(app.engine.transfer(t).state in ("paused", "done") for t in tids))
    check(all(app.engine.transfer(t).state in ("paused", "done") for t in tids), "pause des deux")
    win.activate_action("win.resume-selected", None)
    yield Wait(4, until=lambda: all(app.engine.transfer(t).state not in ("paused", "done") for t in tids))
    check(all(app.engine.transfer(t).state not in ("paused", "done") for t in tids), "reprise des deux")
    dest = os.path.join(TMP, "home", "Rangés")
    win.move_to(tids, dest)
    yield Wait(10, until=lambda: all(app.engine.content_path(t).startswith(dest + os.sep) and not app.engine.moving(t)
                                     for t in tids))
    check(all(app.engine.content_path(t).startswith(dest + os.sep) for t in tids), "déplacés ensemble")
    check(os.path.getsize(os.path.join(dest, "Série", "Épisode 1.mkv")) == 3_000_000, "fichiers bien arrivés")
    win.activate_action("win.select-none", None)
    yield pump(0.2)
    check(not win.selected and not win.sel_revealer.get_reveal_child(), "Échap : sélection vidée")
    win.activate_action("win.select-all", None)
    yield pump(0.2)
    check(win.selected == set(tids), "Ctrl+A : tout sélectionné")
    win.set_selected(tids[1], False)
    win.activate_action("win.remove-selected", None)
    yield pump(0.4)
    alerts = dialogs(win, Adw.MessageDialog)
    check(bool(alerts), "Suppr : question « Supprimer ? »")
    if alerts:
        respond(alerts[0], "remove")
    yield Wait(3, until=lambda: tids[0] not in win.rows)
    check(tids[0] not in win.rows and tids[1] in win.rows and not win.selected, "seul le coché est retiré")
    check(os.path.exists(os.path.join(dest, "Série", "Épisode 1.mkv")), "« Retirer de la liste » : fichiers gardés")


@step
def preferences(win):
    app._prefs()
    yield pump(0.4)
    prefs = [w for w in dialogs(win) if isinstance(w, Adw.PreferencesWindow)]
    check(bool(prefs), "Préférences ouvertes")
    for w in prefs:
        w.close()
    settings["down_limit"] = 500
    yield pump(0.2)
    check(app.engine.session.get_settings()["download_rate_limit"] == 500 * 1024, "limite appliquée tout de suite")
    settings["down_limit"] = 0


@step
def fermer_en_arriere_plan(win):
    win.close()
    yield pump(0.4)
    alerts = dialogs(win, Adw.MessageDialog)
    check(bool(alerts) and tr("Continuer en arrière-plan ?") in alerts[0].get_heading(),
          "partage en cours : « Continuer en arrière-plan ? »")
    if alerts:
        respond(alerts[0], "background")
    yield pump(0.4)
    check(not win.get_visible() and app.window is win, "fenêtre cachée, Télémaque continue")
    check(app.tray is not None and app.tray.active, "arrière-plan : icône dans la barre du haut")
    app.activate()
    yield pump(0.4)
    check(win.get_visible(), "relancé : la fenêtre revient")
    check(not app.tray.active, "fenêtre revenue : icône retirée")


def on_window(_a, w):
    if not log:
        log.append("start")
        GLib.timeout_add(500, run_all, w)


app.connect("window-added", on_window)
app.run([sys.argv[0]])
seeder.session.pause()
shutil.rmtree(TMP, ignore_errors=True)
sys.stderr.flush()
time.sleep(0.2)
for line in CRITICALS:
    errors.append("ÉCHEC : avertissement critique de GTK / GLib : " + line)
print("\n".join(errors) if errors else "\nAucune erreur.")
sys.exit(1 if errors else 0)
