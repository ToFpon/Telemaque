"""Icône de la barre du haut (StatusNotifierItem + dbusmenu) face à une fausse barre
(org.kde.StatusNotifierWatcher), repris d'Ithaca. Sauté sans bus de session :

    dbus-run-session -- python3.12 -m unittest discover -s tests -p "test_tray.py"
"""

import os
import re
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gi.repository import Gio, GLib  # noqa: E402

from telemaque import tray as tray_mod  # noqa: E402
from telemaque.backend.engine import Transfer  # noqa: E402
from telemaque.i18n import tr  # noqa: E402

WATCHER_XML = """<node><interface name="org.kde.StatusNotifierWatcher">
  <method name="RegisterStatusNotifierItem"><arg name="service" type="s" direction="in"/></method>
</interface></node>"""


def wait(cond, timeout=10.0) -> bool:
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while ctx.pending():
            ctx.iteration(False)
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class FakeEngine:
    def __init__(self):
        self.calls = []

    def pause_all(self):
        self.calls.append("pause_all")

    def resume_all(self):
        self.calls.append("resume_all")

    def paused_all(self, tid):
        return False


class FakeApp:
    def __init__(self):
        self.engine = FakeEngine()
        self.transfers = [Transfer("a", "Film_1", "downloading", 0.5, 100, 50, down_rate=2_000_000),
                          Transfer("b", "Série", "paused", 0.2, 100, 20)]
        self.listeners = []

    def connect_transfers(self, cb):
        self.listeners.append(cb)

    def disconnect_transfers(self, cb):
        self.listeners.remove(cb)

    def refresh(self):
        for cb in list(self.listeners):
            cb(self.transfers)


class MenuLabelTest(unittest.TestCase):
    def test_underscores_survive_the_extension(self):
        shown = lambda s: re.sub(r"_([^_])", r"\1", s, count=1)    # noqa: E731  (l'extension)
        for t in ("01_Intro", "a_b_c", "__x", "x_", "sans", "_debut", "a__b"):
            self.assertEqual(shown(tray_mod.menu_label(t)), t, t)

    def test_state_text(self):
        app = FakeApp()
        from telemaque.i18n import ntr
        self.assertTrue(tray_mod.state_text(app.transfers).startswith(
            ntr("{0} téléchargement en cours", "{0} téléchargements en cours", 1).format(1) + " — ↓ "))
        self.assertEqual(tray_mod.state_text([]), tr("Rien en cours"))


@unittest.skipUnless(os.environ.get("DBUS_SESSION_BUS_ADDRESS"), "pas de bus de session")
class TrayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registered = []
        info = Gio.DBusNodeInfo.new_for_xml(WATCHER_XML).interfaces[0]

        def acquired(conn, _name):
            def call(_c, _s, _p, _i, method, params, inv):
                cls.registered.append(params.unpack()[0])
                inv.return_value(None)
            conn.register_object("/StatusNotifierWatcher", info, call, None, None)
            cls.watcher_ready = True
        cls.watcher_ready = False
        cls.owner = Gio.bus_own_name(Gio.BusType.SESSION, tray_mod.WATCHER, Gio.BusNameOwnerFlags.NONE,
                                     acquired, None, None)
        assert wait(lambda: cls.watcher_ready, 5)

    def setUp(self):
        self.app = FakeApp()
        self.shown, self.quit = [], []
        self.t = tray_mod.Tray(self.app, lambda: self.shown.append(1), lambda: self.quit.append(1))
        self.t.show()
        self.assertTrue(wait(lambda: self.t.registered, 5), "icône enregistrée auprès de la barre")
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        flags = Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES
        self.menu = Gio.DBusProxy.new_sync(bus, flags, None, self.t.name, tray_mod.MENU_PATH,
                                           "com.canonical.dbusmenu", None)
        self.item = Gio.DBusProxy.new_sync(bus, flags, None, self.t.name, tray_mod.ITEM_PATH,
                                           "org.kde.StatusNotifierItem", None)

    def tearDown(self):
        self.t.hide()

    def call(self, proxy, method, args=None):
        out = []
        proxy.call(method, args, Gio.DBusCallFlags.NONE, 2000, None, lambda p, r: out.append(p.call_finish(r)))
        self.assertTrue(wait(lambda: out, 3), method)
        return out[0].unpack() if out[0] is not None else None

    def items(self):
        _rev, (_root, _props, children) = self.call(self.menu, "GetLayout", GLib.Variant("(iias)", (0, -1, [])))
        return {i: p for i, p, _c in children}

    def test_icon_resolvable(self):
        """Comme la cherche l'extension AppIndicator : chemin vide = thème du système, sinon un dossier de
        thème AVEC index.theme (sinon icône d'attente « ••• », vu dans Ithaca)."""
        name = self.call(self.item, "org.freedesktop.DBus.Properties.Get",
                         GLib.Variant("(ss)", ("org.kde.StatusNotifierItem", "IconName")))[0]
        path = self.call(self.item, "org.freedesktop.DBus.Properties.Get",
                         GLib.Variant("(ss)", ("org.kde.StatusNotifierItem", "IconThemePath")))[0]
        self.assertEqual(name, tray_mod.ICON_NAME)
        if path:
            index = os.path.join(path, "hicolor", "index.theme")
            self.assertTrue(os.path.exists(index), "sans index.theme, la barre montre « ••• »")
            keys = GLib.KeyFile()
            keys.load_from_file(index, GLib.KeyFileFlags.NONE)
            dirs = keys.get_string("Icon Theme", "Directories").split(",")
            for variant in (name, name + "-symbolic"):
                self.assertTrue(any(os.path.exists(os.path.join(path, "hicolor", d, variant + ".svg")) for d in dirs),
                                f"{variant} absente des dossiers déclarés")

    def test_menu_and_actions(self):
        self.assertIn(self.t.name, self.registered)
        items = self.items()
        self.assertEqual(items[tray_mod.STATE]["label"], tray_mod.menu_label(tray_mod.state_text(self.app.transfers)))
        self.assertFalse(items[tray_mod.STATE]["enabled"], "ligne d'état : pas cliquable")
        self.assertTrue(items[tray_mod.PAUSE_ALL]["enabled"])
        self.assertTrue(items[tray_mod.RESUME_ALL]["enabled"], "un téléchargement en pause : Tout reprendre")
        self.assertEqual(items[tray_mod.SEP]["type"], "separator")
        for item, expected in ((tray_mod.PAUSE_ALL, "pause_all"), (tray_mod.RESUME_ALL, "resume_all")):
            self.call(self.menu, "Event", GLib.Variant("(isvu)", (item, "clicked", GLib.Variant("s", ""), 0)))
            self.assertTrue(wait(lambda: expected in self.app.engine.calls, 3), expected)
        self.call(self.item, "Activate", GLib.Variant("(ii)", (0, 0)))
        self.assertTrue(wait(lambda: self.shown, 3), "clic sur l'icône : fenêtre")
        self.call(self.menu, "Event", GLib.Variant("(isvu)", (tray_mod.QUIT, "clicked", GLib.Variant("s", ""), 0)))
        self.assertTrue(wait(lambda: self.quit, 3), "Quitter")

    def test_menu_follows_state(self):
        """Menu republié quand l'état change (et seulement alors : la relecture a lieu chaque seconde)."""
        rev = self.t.revision
        self.app.refresh()
        self.app.refresh()
        self.assertEqual(self.t.revision, rev + 1, "même état : une seule publication")
        self.app.transfers = []
        self.app.refresh()
        self.assertEqual(self.t.revision, rev + 2)
        items = self.items()
        self.assertEqual(items[tray_mod.STATE]["label"], tr("Rien en cours"))
        self.assertFalse(items[tray_mod.PAUSE_ALL]["enabled"])

    def test_hide(self):
        self.t.hide()
        self.assertFalse(self.t.active)
        self.t.show()
        self.assertTrue(wait(lambda: self.t.registered, 5))


if __name__ == "__main__":
    unittest.main()
