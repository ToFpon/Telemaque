"""Point d'entrée de l'application Télémaque.

Une seule instance, qui garde la session BitTorrent : la fenêtre peut se fermer pendant que les
téléchargements continuent (« Continuer en arrière-plan »), un nouveau lancement la fait revenir.
"""

from __future__ import annotations

import os
import signal
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, APP_NAME, VERSION, features  # noqa: E402
from .i18n import tr  # noqa: E402
from .settings import DATA_DIR as STATE_ROOT, settings  # noqa: E402

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
POLL_MS = 1000
INHIBIT_GRACE = 300      # s : veille encore empêchée après la dernière activité (une source qui revient)


def keeps_awake(transfers, last_active: float, now: float) -> tuple:
    """(empêcher la veille ?, dernière activité) — seulement pour une VRAIE activité : des données qui arrivent,
    une vérification, un déplacement ; puis INHIBIT_GRACE s de marge (débit qui retombe un instant). Un lien magnet
    mort ou un téléchargement sans source ne bloque plus la veille pour toujours (choix de Tof). Le partage seul
    ne l'a jamais bloquée."""
    if any(t.state in ("checking", "moving") or (t.downloading and t.down_rate > 0) for t in transfers):
        last_active = now
    pending = any(t.downloading or t.state == "moving" for t in transfers)
    return pending and last_active > 0 and now - last_active < INHIBIT_GRACE, last_active


class TelemaqueApp(Adw.Application):
    def __init__(self, engine=None):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE | Gio.ApplicationFlags.HANDLES_OPEN)
        GLib.set_application_name(APP_NAME)
        self.add_main_option("version", ord("v"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             tr("Afficher la version"), None)
        self.engine = engine
        self.window = None
        self.transfers: list = []
        self._listeners: list = []
        self._poll_id = 0
        self._inhibit = 0
        self._last_active = 0.0
        self._held = False
        self._quitting = False
        self.tray = None

    # -- démarrage ------------------------------------------------------------------------------
    def do_startup(self):
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        if display is None:              # « telemaque --version » sans écran (ssh) : rien à dessiner
            return
        css = Gtk.CssProvider()
        css.load_from_path(os.path.join(DATA_DIR, "base.css"))
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        icons = os.path.join(DATA_DIR, "icons")
        if os.path.isdir(icons):
            Gtk.IconTheme.get_for_display(display).add_search_path(icons)
        from .ui.preferences import apply_theme
        apply_theme()
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: (self.quit_all(), False)[1])
        for name, cb, accels in (("preferences", self._prefs, ["<Control>comma"]),
                                 ("about", self._about, []),
                                 ("quit", self.quit_all, ["<Control>q"])):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, cb=cb: cb())
            self.add_action(a)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)
        # actions des notifications (« Ouvrir », « Afficher dans le dossier ») : l'identifiant en paramètre
        for name, cb in (("open-transfer", self._open_transfer), ("show-transfer", self._show_transfer)):
            a = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            a.connect("activate", lambda _a, p, cb=cb: cb(p.get_string()))
            self.add_action(a)
        self._start_engine()
        settings.connect(self._on_setting)

    def _start_engine(self):
        if self.engine is None:
            from .backend.engine import Engine
            self.engine = Engine(os.path.join(STATE_ROOT, "torrents"), listen_port=settings["listen_port"],
                                 dht=settings["dht"], seed_policy=settings["seed_policy"],
                                 seed_ratio=settings["seed_ratio"], down_limit=settings["down_limit"],
                                 up_limit=settings["up_limit"], part_ext=self._part_ext())
        else:
            self.engine.set_part_ext(self._part_ext())
        self.engine.on_finished = self._on_finished
        self.engine.on_moved = lambda tid, ok, msg: self.window is not None and self.window.moved(tid, ok, msg)
        # VPN coupé ou remis, autre réseau : les adresses publiques sont à revérifier (Tof). Le signal arrive
        # en rafale pendant que le réseau se réorganise : une seule réaction, 2 s après le dernier
        self._net_timer = 0
        self._ip_seq = 0
        Gio.NetworkMonitor.get_default().connect("network-changed", self._on_network_changed)
        self._poll()
        self.lookup_public_ip()
        self._poll_id = GLib.timeout_add(POLL_MS, self._poll)

    def _on_network_changed(self, *_):
        if self._net_timer:
            GLib.source_remove(self._net_timer)
        self._net_timer = GLib.timeout_add(2000, self._network_settled)

    def _network_settled(self):
        self._net_timer = 0
        if self.engine is not None and not self._quitting:
            self.engine.network_changed()
            self.refresh()
            self.lookup_public_ip()
        return False

    def lookup_public_ip(self):
        """Adresses publiques demandées à ipify.org (fonctionnalité « public_ip ») ; une recherche plus
        récente (réseau encore changé entre-temps) l'emporte sur une plus ancienne."""
        if not features.enabled("public_ip") or getattr(self.engine, "local_only", False):
            return
        from .backend import publicip
        self._ip_seq += 1
        seq = self._ip_seq

        def done(v4, v6):
            def apply():
                if seq == self._ip_seq and self.engine is not None and not self._quitting:
                    self.engine.set_public_ips(v4, v6)
                    self.refresh()
                return False
            GLib.idle_add(apply)
        publicip.lookup(done)

    @staticmethod
    def _part_ext() -> str:
        return ".part" if features.enabled("part_suffix") else ""

    def _on_setting(self, key, value):
        e = self.engine
        if e is None:
            return
        if key == "features":
            if e.part_ext != self._part_ext():
                e.set_part_ext(self._part_ext())
            return
        if key in ("down_limit", "up_limit"):
            e.set_limits(settings["down_limit"], settings["up_limit"])
        elif key in ("listen_port", "dht"):
            e.set_network(settings["listen_port"], settings["dht"])
        elif key == "seed_policy":
            e.seed_policy = value
        elif key == "seed_ratio":
            e.seed_ratio = float(value)

    # -- boucle -----------------------------------------------------------------------------------
    def connect_transfers(self, cb):
        self._listeners.append(cb)

    def disconnect_transfers(self, cb):
        if cb in self._listeners:
            self._listeners.remove(cb)

    def _poll(self):
        if self.engine is None:
            return False
        # une exception ici ne doit pas arrêter la boucle : la liste resterait figée pour de bon
        try:
            self.transfers = self.engine.poll()
            self._sync_inhibit()
            for cb in list(self._listeners):
                cb(self.transfers)
        except Exception:  # noqa: BLE001
            sys.excepthook(*sys.exc_info())
        return True

    def refresh(self):
        """État relu tout de suite (après un ajout, une pause…), sans attendre la seconde suivante."""
        self._poll()

    def _sync_inhibit(self):
        import time
        busy, self._last_active = keeps_awake(self.transfers, self._last_active, time.monotonic())
        busy = busy and features.enabled("inhibit")
        if busy and not self._inhibit:
            self._inhibit = self.inhibit(self.window, Gtk.ApplicationInhibitFlags.SUSPEND,
                                         tr("Téléchargement en cours"))
        elif not busy and self._inhibit:
            self.uninhibit(self._inhibit)
            self._inhibit = 0

    # -- fenêtre ----------------------------------------------------------------------------------
    def show_window(self):
        if self.window is None:
            from .ui.window import MainWindow
            self.window = MainWindow(self)
        if self._held:
            self.release()
            self._held = False
        if self.tray is not None:
            self.tray.hide()
        self.window.present()
        return self.window

    def hide_to_background(self):
        """La fenêtre disparaît, les téléchargements continuent (l'application est retenue)."""
        if not self._held:
            self.hold()
            self._held = True
        if self.window is not None:
            self.window.set_visible(False)
        if features.enabled("tray"):
            if self.tray is None:
                from .tray import Tray
                self.tray = Tray(self, self.show_window, self.quit_all)
            self.tray.show()

    def do_activate(self):
        self.show_window()

    def do_open(self, files, n, hint):
        win = self.show_window()
        for f in files:
            uri = f.get_uri() or ""
            if uri.lower().startswith("magnet:"):
                win.add_magnet(uri)
            elif f.get_path():
                win.add_torrent_file(f.get_path())

    def do_command_line(self, cmd):
        opts = cmd.get_options_dict().end().unpack()
        if opts.get("version"):
            cmd.print_literal(f"{APP_NAME} {VERSION}\n")
            return 0
        cwd = cmd.get_cwd() or os.path.expanduser("~")
        if not os.path.isdir(cwd):
            cwd = os.path.expanduser("~")
        args = cmd.get_arguments()[1:]
        if not args:
            self.do_activate()
            return 0
        win = self.show_window()
        for a in args:
            if a.lower().startswith("magnet:"):
                win.add_magnet(a)
            else:
                f = Gio.File.new_for_commandline_arg_and_cwd(a, cwd)
                if f.get_path():
                    win.add_torrent_file(f.get_path())
        return 0

    # -- fin d'un téléchargement --------------------------------------------------------------------
    def _on_finished(self, tid: str):
        t = self.engine.transfer(tid)
        if t is None or not features.enabled("notify"):
            return
        from .ui.common import shown
        n = Gio.Notification.new(tr("Téléchargement terminé"))
        n.set_body(shown(t.name))
        n.set_icon(Gio.ThemedIcon.new(APP_ID))
        # actions « détaillées » (nom::paramètre) : les variantes *_target_value manquent à PyGObject
        n.set_default_action(f"app.show-transfer::{tid}")
        n.add_button(tr("Ouvrir"), f"app.open-transfer::{tid}")
        n.add_button(tr("Afficher dans le dossier"), f"app.show-transfer::{tid}")
        self.send_notification("finished-" + tid, n)

    def _open_transfer(self, tid: str):
        from .ui.common import open_with_default
        path = self.engine.content_path(tid)
        if path and os.path.exists(path):
            open_with_default(self.window, path, folder=False)

    def _show_transfer(self, tid: str):
        from .ui.common import show_in_folder
        path = self.engine.content_path(tid)
        if path and os.path.exists(path):
            show_in_folder(self.window, path)

    # -- fin ----------------------------------------------------------------------------------------
    def quit_all(self):
        """Ctrl+Q, menu « Quitter » : tout s'arrête (les téléchargements reprendront au prochain lancement)."""
        if self._quitting:
            return
        self._quitting = True
        if self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = 0
        if self._inhibit:
            self.uninhibit(self._inhibit)
            self._inhibit = 0
        if self.tray is not None:
            self.tray.hide()
        if self.window is not None:
            self.window.save_state()
            self.window.destroy()
            self.window = None
        if self.engine is not None:
            self.engine.shutdown()
        if self._held:
            self.release()
            self._held = False
        self.quit()

    # -- fenêtres annexes ----------------------------------------------------------------------------
    def _prefs(self):
        from .ui.preferences import PreferencesWindow
        d = PreferencesWindow()
        d.set_transient_for(self.get_active_window())
        d.present()

    def _about(self):
        about = Adw.AboutWindow(transient_for=self.get_active_window(), modal=True,
                                application_name=APP_NAME, application_icon=APP_ID, version=VERSION,
                                developer_name="Tof",
                                comments=tr("Un client BitTorrent tout simple pour GNOME."),
                                website="https://github.com/ToFpon/Telemaque",
                                issue_url="https://github.com/ToFpon/Telemaque/issues",
                                license_type=Gtk.License.GPL_3_0)
        about.add_acknowledgement_section(tr("Moteur"), ["libtorrent https://libtorrent.org"])
        about.present()


def _process_name(name: str):
    """Nom du processus pour le noyau : « telemaque » dans le Moniteur système, ps, killall."""
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(15, ctypes.c_char_p(name.encode()[:15]), 0, 0, 0)     # 15 = PR_SET_NAME
    except (OSError, AttributeError):
        pass


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv
    if any(a in ("--version", "-v") for a in argv[1:]):
        print(f"{APP_NAME} {VERSION}")          # sans démarrer GTK (pas d'écran : avertissements)
        return 0
    _process_name("telemaque")
    import faulthandler
    faulthandler.enable(all_threads=True)
    GLib.set_prgname(APP_ID)
    app = TelemaqueApp()
    return app.run(argv)
