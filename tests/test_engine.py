"""Moteur sans interface, sur un vrai libtorrent : deux moteurs de la même machine s'échangent des
torrents (127.0.0.1, ni DHT ni Internet).

    python3.12 -m unittest discover -s tests
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
TMP = tempfile.mkdtemp(prefix="telemaque-test-")
for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
    os.environ[var] = os.path.join(TMP, var.lower())

import libtorrent as lt  # noqa: E402

import samples  # noqa: E402
from telemaque.backend import engine as eng  # noqa: E402
from telemaque.backend.engine import Duplicate, Engine, TorrentError  # noqa: E402

_PORT = [47100]


def port() -> int:
    _PORT[0] += 1
    return _PORT[0]


def until(engines, cond, timeout=20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        for e in engines:
            e.poll()
        if cond():
            return True
        time.sleep(0.1)
    return False


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.src = os.path.join(self.dir, "src")
        self.dst = os.path.join(self.dir, "dst")
        os.makedirs(self.dst)
        self.content = samples.make_content(self.src)
        self.torrent = samples.make_torrent(self.content, os.path.join(self.dir, "serie.torrent"))
        self.seed_port = port()
        self.seeder = Engine(os.path.join(self.dir, "state-seed"), listen_port=self.seed_port, local_only=True,
                             seed_policy="always")
        self.seeder.add_torrent_file(self.torrent, self.src)
        self.engines = [self.seeder]

    def tearDown(self):
        for e in self.engines:
            e.session.pause()
        self.engines.clear()

    def leecher(self, state="state-leech", **kw) -> Engine:
        kw.setdefault("seed_policy", "always")
        e = Engine(os.path.join(self.dir, state), listen_port=port(), local_only=True, **kw)
        self.engines.append(e)
        return e

    def link(self, e: Engine, tid: str):
        e.connect_peer(tid, "127.0.0.1", self.seed_port)


class DownloadTest(Base):
    def test_full_download(self):
        info = eng.read_torrent(self.torrent)
        self.assertEqual(info.name, "Série")
        self.assertEqual(sorted(f.path for f in info.files),
                         sorted(os.path.join("Série", p) for p in ("Bonus/Making-of.mkv", "lisez-moi.txt",
                                                                    "Épisode 1.mkv")))
        e = self.leecher()
        finished = []
        e.on_finished = finished.append
        tid = e.add_torrent_file(self.torrent, self.dst)
        self.link(e, tid)
        self.assertTrue(until(self.engines, lambda: e.transfer(tid).complete))
        self.assertTrue(until(self.engines, lambda: finished == [tid]))
        for rel in ("Épisode 1.mkv", "Bonus/Making-of.mkv", "lisez-moi.txt"):
            with open(os.path.join(self.content, rel), "rb") as a, open(os.path.join(self.dst, "Série", rel), "rb") as b:
                self.assertEqual(a.read(), b.read(), rel)
        t = e.transfer(tid)
        self.assertEqual(t.state, "seeding")
        self.assertEqual(e.content_path(tid), os.path.join(self.dst, "Série"))

    def test_magnet(self):
        e = self.leecher()
        uri = lt.make_magnet_uri(lt.torrent_info(self.torrent))
        self.assertTrue(eng.is_magnet(uri))
        tid = e.add_magnet(uri, self.dst)
        self.assertIn(e.transfer(tid).state, ("metadata", "checking"))
        self.assertEqual(e.files(tid), [])
        self.link(e, tid)
        self.assertTrue(until(self.engines, lambda: e.transfer(tid).complete))
        self.assertEqual(e.transfer(tid).name, "Série")
        self.assertEqual(len(e.files(tid)), 3)

    def test_choose_files(self):
        e = self.leecher()
        info = eng.read_torrent(self.torrent)
        small = [f.index for f in info.files if f.path.endswith("lisez-moi.txt")]
        tid = e.add_torrent_file(self.torrent, self.dst, wanted=small)
        self.link(e, tid)
        self.assertTrue(until(self.engines, lambda: e.transfer(tid).complete))
        files = {os.path.basename(f.path): f for f in e.files(tid)}
        self.assertTrue(files["lisez-moi.txt"].wanted)
        self.assertFalse(files["Épisode 1.mkv"].wanted)
        self.assertLess(e.transfer(tid).size, 100_000)
        with open(os.path.join(self.dst, "Série", "lisez-moi.txt"), "rb") as fh:
            self.assertEqual(len(fh.read()), 12)
        # un fichier de plus après coup : il arrive aussi, et la fin est signalée de nouveau
        finished = []
        e.on_finished = finished.append
        e.set_wanted(tid, files["Épisode 1.mkv"].index, True)
        self.assertTrue(until(self.engines, lambda: finished == [tid]))
        self.assertEqual(os.path.getsize(os.path.join(self.dst, "Série", "Épisode 1.mkv")), 3_000_000)

    def test_duplicate_and_bad_input(self):
        e = self.leecher()
        tid = e.add_torrent_file(self.torrent, self.dst)
        with self.assertRaises(Duplicate) as cm:
            e.add_torrent_file(self.torrent, self.dst)
        self.assertEqual(cm.exception.tid, tid)
        with self.assertRaises(Duplicate):
            e.add_magnet(lt.make_magnet_uri(lt.torrent_info(self.torrent)), self.dst)
        bad = os.path.join(self.dir, "bad.torrent")
        with open(bad, "wb") as fh:
            fh.write(b"pas un torrent")
        with self.assertRaises(TorrentError):
            eng.read_torrent(bad)
        with self.assertRaises(TorrentError):
            e.add_torrent_file(bad, self.dst)
        with self.assertRaises(TorrentError):
            e.add_magnet("magnet:?xt=urn:btih:zz", self.dst)
        self.assertFalse(eng.is_magnet("https://exemple.org/x.torrent"))


class ControlTest(Base):
    def test_pause_resume_remove(self):
        e = self.leecher()
        tid = e.add_torrent_file(self.torrent, self.dst)
        # lent, pour avoir le temps de mettre en pause (les limites générales épargnent le réseau local)
        e.handles[tid].set_download_limit(200_000)
        self.link(e, tid)
        self.assertTrue(until(self.engines, lambda: e.transfer(tid).done > 0))
        e.pause(tid)
        self.assertTrue(until(self.engines, lambda: e.transfer(tid).state == "paused"))
        done = e.transfer(tid).done
        time.sleep(1)
        e.poll()
        self.assertEqual(e.transfer(tid).done, done, "en pause : plus rien n'arrive")
        e.handles[tid].set_download_limit(0)
        e.resume(tid)
        self.link(e, tid)
        self.assertTrue(until(self.engines, lambda: e.transfer(tid).complete))
        target = os.path.join(self.dst, "Série")
        self.assertTrue(os.path.isdir(target))
        e.remove(tid, delete_files=True)
        self.assertIsNone(e.transfer(tid))
        self.assertTrue(until(self.engines, lambda: not os.path.exists(os.path.join(target, "Épisode 1.mkv")), 10))
        self.assertTrue(os.path.isdir(self.content), "la source du partageur n'est jamais touchée")
        self.assertFalse(os.path.exists(os.path.join(e.state_dir, tid + ".resume")))

    def test_remove_keeps_files(self):
        e = self.leecher()
        tid = e.add_torrent_file(self.torrent, self.dst)
        self.link(e, tid)
        self.assertTrue(until(self.engines, lambda: e.transfer(tid).complete))
        e.remove(tid)
        time.sleep(0.5)
        self.assertTrue(os.path.exists(os.path.join(self.dst, "Série", "Épisode 1.mkv")))

    def test_seed_policy_never(self):
        e = self.leecher(seed_policy="never")
        tid = e.add_torrent_file(self.torrent, self.dst)
        self.link(e, tid)
        self.assertTrue(until(self.engines, lambda: e.transfer(tid).state == "done"))
        self.assertTrue(e.meta[tid].get("seed_stopped"))
        e.resume(tid)                                    # relancé à la main : partage sans fin
        e.poll()
        time.sleep(0.5)
        e.poll()
        self.assertEqual(e.transfer(tid).state, "seeding")
        self.assertTrue(e.meta[tid].get("seed_forever"))

    def test_seed_policy_ratio(self):
        """Partage jusqu'au ratio : un troisième moteur télécharge depuis le second, qui s'arrête
        une fois qu'il a envoyé autant qu'il a reçu."""
        mid = self.leecher("state-mid", seed_policy="ratio", seed_ratio=0.5)
        tid = mid.add_torrent_file(self.torrent, self.dst)
        self.link(mid, tid)
        self.assertTrue(until(self.engines, lambda: mid.transfer(tid).complete))
        mid.poll()
        self.assertEqual(mid.transfer(tid).state, "seeding", "ratio pas atteint : il partage")
        self.seeder.pause(self.seeder.transfers()[0].tid)      # seule source : le second moteur
        last = self.leecher("state-last")
        dst2 = os.path.join(self.dir, "dst2")
        os.makedirs(dst2)
        tid2 = last.add_torrent_file(self.torrent, dst2)
        last.connect_peer(tid2, "127.0.0.1", mid.listen_port())
        self.assertTrue(until(self.engines, lambda: mid.transfer(tid).state == "done", 30))
        self.assertGreaterEqual(mid.transfer(tid).ratio, 0.5)


class ExtrasTest(Base):
    def test_delete_source_after_resume_written(self):
        """« Supprimer le fichier .torrent après l'ajout » : effacé une fois les données de reprise écrites,
        et le téléchargement survit à un redémarrage sans lui."""
        copy = os.path.join(self.dir, "à supprimer.torrent")
        shutil.copy(self.torrent, copy)
        state = os.path.join(self.dir, "state-del")
        e = Engine(state, listen_port=port(), local_only=True)
        self.engines.append(e)
        tid = e.add_torrent_file(copy, self.dst, delete_source=True)
        self.assertTrue(until(self.engines, lambda: not os.path.exists(copy), 5), "fichier .torrent supprimé")
        self.assertTrue(os.path.exists(os.path.join(state, tid + ".resume")))
        self.assertTrue(os.path.exists(self.torrent), "seul le fichier ajouté est supprimé")
        e.shutdown()
        e2 = Engine(state, listen_port=port(), local_only=True)
        self.engines.append(e2)
        self.assertEqual(e2.transfer(tid).name, "Série")
        e2.connect_peer(tid, "127.0.0.1", self.seed_port)
        self.assertTrue(until(self.engines, lambda: e2.transfer(tid).complete))
        # sans l'option : le fichier reste
        keep = os.path.join(self.dir, "garder.torrent")
        single = samples.make_torrent(samples.make_single(os.path.join(self.dir, "s")), keep)
        e2.add_torrent_file(single, self.dst)
        until(self.engines, lambda: False, 1.5)
        self.assertTrue(os.path.exists(keep))

    def test_pause_all_resume_all(self):
        e = self.leecher()
        tid = e.add_torrent_file(self.torrent, self.dst)
        self.link(e, tid)
        self.assertTrue(until(self.engines, lambda: e.transfer(tid).state == "seeding"))
        other = samples.make_torrent(samples.make_single(os.path.join(self.dir, "s2")), os.path.join(self.dir, "o.torrent"))
        tid2 = e.add_torrent_file(other, self.dst, paused=True)      # déjà en pause avant
        e.pause_all()
        e.poll()
        self.assertEqual(e.transfer(tid).state, "done")
        self.assertTrue(e.paused_all(tid))
        e.resume_all()
        e.poll()
        self.assertEqual(e.transfer(tid).state, "seeding", "le partage reprend")
        self.assertIn(e.transfer(tid2).state, ("metadata", "checking", "downloading"),
                      "un téléchargement en pause reprend aussi")
        self.assertFalse(e.paused_all(tid))


class NetworkTest(unittest.TestCase):
    def test_both_families_kept(self):
        """libtorrent annonce l'adresse publique famille par famille : l'IPv6 ne remplace plus l'IPv4."""
        e = Engine(tempfile.mkdtemp(dir=TMP), listen_port=port(), local_only=True)
        try:
            e.note_external_ip("82.65.1.2")
            e.note_external_ip("2a01:e0a:1::5")
            e.note_external_ip("")
            n = e.network()
            self.assertEqual((n.ipv4, n.ipv6), ("82.65.1.2", "2a01:e0a:1::5"))
            e.note_external_ip("82.65.9.9")              # changée (box redémarrée) : remplacée
            self.assertEqual(e.network().ipv4, "82.65.9.9")
            e.network_changed()                         # VPN coupé : oubliées, en vérification
            n = e.network()
            self.assertEqual((n.ipv4, n.ipv6, n.checking), ("", "", True))
            e.note_external_ip("90.1.2.3")              # la nouvelle arrive
            n = e.network()
            self.assertEqual((n.ipv4, n.checking), ("90.1.2.3", False))
        finally:
            e.session.pause()


class PublicIpTest(unittest.TestCase):
    def test_parse(self):
        from telemaque.backend import publicip
        self.assertEqual(publicip.parse("82.65.1.2", 4), "82.65.1.2")
        self.assertEqual(publicip.parse("2a01:e0a:1::5", 6), "2a01:e0a:1::5")
        for text, v in (("192.168.1.2", 4), ("<html>", 4), ("2a01:e0a:1::5", 4), ("82.65.1.2", 6), ("", 6)):
            self.assertEqual(publicip.parse(text, v), "", text)

    def test_engine_takes_lookup(self):
        e = Engine(tempfile.mkdtemp(dir=TMP), listen_port=port(), local_only=True)
        try:
            e.network_changed()
            e.set_public_ips("82.65.1.2", "")
            n = e.network()
            self.assertEqual((n.ipv4, n.ipv6, n.checking, n.looked_up), ("82.65.1.2", "", False, True))
        finally:
            e.session.pause()


class RealNetworkTest(unittest.TestCase):
    def test_network_changed_on_real_session(self):
        """Sur une vraie session (pas le mode test) : la réouverture des prises réseau passe (en Python, le
        drapeau est obligatoire : sans lui, ArgumentError de Boost, vu au premier essai)."""
        e = Engine(tempfile.mkdtemp(dir=TMP), listen_port=port(), dht=False)
        try:
            e.note_external_ip("10.8.0.2")
            e.network_changed()
            e.poll()
            self.assertTrue(e.network().checking)
        finally:
            e.session.pause()


class MoveTest(Base):
    def test_move_after_download(self):
        e = self.leecher()
        tid = e.add_torrent_file(self.torrent, self.dst)
        self.link(e, tid)
        self.assertTrue(until(self.engines, lambda: e.transfer(tid).complete))
        moved = []
        e.on_moved = lambda t, ok, msg: moved.append((t, ok))
        dest = os.path.join(self.dir, "rangé")
        os.makedirs(dest)
        self.assertTrue(e.move(tid, dest))
        self.assertTrue(until(self.engines, lambda: moved == [(tid, True)]))
        self.assertEqual(e.content_path(tid), os.path.join(dest, "Série"))
        self.assertEqual(os.path.getsize(os.path.join(dest, "Série", "Épisode 1.mkv")), 3_000_000)
        self.assertFalse(os.path.exists(os.path.join(self.dst, "Série", "Épisode 1.mkv")), "plus à l'ancien endroit")
        self.assertFalse(e.move(tid, dest), "même dossier : rien à faire")

    def test_move_during_download_and_restart(self):
        state = os.path.join(self.dir, "state-move")
        e = Engine(state, listen_port=port(), local_only=True)
        tid = e.add_torrent_file(self.torrent, self.dst)
        e.handles[tid].set_download_limit(300_000)
        e.connect_peer(tid, "127.0.0.1", self.seed_port)
        self.assertTrue(until([self.seeder, e], lambda: e.transfer(tid).done > 100_000))
        moved = []
        e.on_moved = lambda t, ok, msg: moved.append(ok)
        dest = os.path.join(self.dir, "ailleurs")
        e.move(tid, dest)                               # dossier créé par libtorrent
        self.assertEqual(e.transfer(tid).state, "moving")
        self.assertTrue(until([self.seeder, e], lambda: moved == [True]))
        e.handles[tid].set_download_limit(0)
        e.connect_peer(tid, "127.0.0.1", self.seed_port)
        self.assertTrue(until([self.seeder, e], lambda: e.transfer(tid).complete, 30), "le téléchargement continue")
        self.assertEqual(os.path.getsize(os.path.join(dest, "Série", "Épisode 1.mkv")), 3_000_000)
        e.shutdown()
        e2 = Engine(state, listen_port=port(), local_only=True)
        self.engines.append(e2)
        self.assertEqual(e2.content_path(tid), os.path.join(dest, "Série"), "nouvel emplacement retenu")

    def test_callback_polling_again_is_safe(self):
        """Un rappel qui relance poll() (la fenêtre le fait) ne doit pas libérer les alertes en cours."""
        e = self.leecher(part_ext=".part")
        nested = []
        e.on_changed = lambda: nested.append(len(e.poll()))
        e.on_moved = lambda t, ok, msg: nested.append(len(e.poll()))
        e.on_finished = lambda t: nested.append(len(e.poll()))
        tid = e.add_torrent_file(self.torrent, self.dst)
        self.link(e, tid)
        self.assertTrue(until(self.engines, lambda: e.transfer(tid).complete))
        e.move(tid, os.path.join(self.dir, "ailleurs"))
        self.assertTrue(until(self.engines, lambda: not e.moving(tid) and e.content_path(tid).startswith(
            os.path.join(self.dir, "ailleurs"))))
        self.assertTrue(nested)

    def test_move_never_overwrites(self):
        e = self.leecher()
        tid = e.add_torrent_file(self.torrent, self.dst)
        self.link(e, tid)
        self.assertTrue(until(self.engines, lambda: e.transfer(tid).complete))
        dest = os.path.join(self.dir, "occupé")
        os.makedirs(os.path.join(dest, "Série"))
        with open(os.path.join(dest, "Série", "lisez-moi.txt"), "w") as fh:
            fh.write("précieux")
        moved = []
        e.on_moved = lambda t, ok, msg: moved.append((ok, msg))
        e.move(tid, dest)
        self.assertTrue(until(self.engines, lambda: moved))
        self.assertFalse(moved[0][0], "refusé : un fichier du même nom est déjà là")
        self.assertTrue(moved[0][1])
        with open(os.path.join(dest, "Série", "lisez-moi.txt")) as fh:
            self.assertEqual(fh.read(), "précieux", "jamais écrasé")
        self.assertEqual(e.content_path(tid), os.path.join(self.dst, "Série"), "rien n'a bougé")
        self.assertEqual(os.path.getsize(os.path.join(self.dst, "Série", "Épisode 1.mkv")), 3_000_000)


def disk_names(root):
    out = set()
    for d, _dirs, files in os.walk(root):
        out |= {os.path.relpath(os.path.join(d, f), root) for f in files}
    return out


class PartExtTest(Base):
    EP = os.path.join("Série", "Épisode 1.mkv")

    def slow_leecher(self, state="state-part", **kw):
        e = Engine(os.path.join(self.dir, state), listen_port=port(), local_only=True, part_ext=".part", **kw)
        self.engines.append(e)
        return e

    def test_part_during_then_real_name(self):
        e = self.slow_leecher()
        tid = e.add_torrent_file(self.torrent, self.dst)
        e.handles[tid].set_download_limit(300_000)
        self.link(e, tid)
        self.assertTrue(until(self.engines, lambda: self.EP + ".part" in disk_names(self.dst)))
        self.assertNotIn(self.EP, disk_names(self.dst), "jamais le vrai nom tant qu'il est incomplet")
        self.assertEqual({f.path for f in e.files(tid)} & {self.EP + ".part"}, set(), "vrais noms dans la liste")
        e.handles[tid].set_download_limit(0)
        self.assertTrue(until(self.engines, lambda: e.transfer(tid).complete
                              and disk_names(self.dst) == {os.path.join("Série", p) for p in
                                                           ("Épisode 1.mkv", "Bonus/Making-of.mkv",
                                                            "lisez-moi.txt")}, 30), disk_names(self.dst))
        with open(os.path.join(self.content, "Épisode 1.mkv"), "rb") as a, open(os.path.join(self.dst, self.EP), "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_paused_restart_and_toggle(self):
        state = "state-part2"
        e = self.slow_leecher(state)
        tid = e.add_torrent_file(self.torrent, self.dst)
        e.handles[tid].set_download_limit(300_000)
        self.link(e, tid)
        self.assertTrue(until(self.engines, lambda: e.transfer(tid).done > 400_000))
        e.pause(tid)
        e.shutdown()
        self.engines.remove(e)
        self.assertIn(self.EP + ".part", disk_names(self.dst))
        e2 = self.slow_leecher(state)
        self.assertTrue(until(self.engines, lambda: e2.transfer(tid).state == "paused"))
        until(self.engines, lambda: False, 1.0)
        self.assertIn(self.EP + ".part", disk_names(self.dst), "en pause après redémarrage : toujours .part")
        self.assertTrue(e2.content_path(tid).endswith("Série"))
        e2.set_part_ext("")                                    # fonctionnalité coupée : vrais noms tout de suite
        self.assertTrue(until(self.engines, lambda: self.EP in disk_names(self.dst)))
        self.assertFalse(any(n.endswith(".part") for n in disk_names(self.dst)))
        e2.set_part_ext(".part")
        self.assertTrue(until(self.engines, lambda: self.EP + ".part" in disk_names(self.dst)))
        e2.resume(tid)
        self.link(e2, tid)
        self.assertTrue(until(self.engines, lambda: e2.transfer(tid).complete and self.EP in disk_names(self.dst), 30))
        self.assertFalse(any(n.endswith(".part") for n in disk_names(self.dst)), disk_names(self.dst))
        self.assertEqual(os.path.getsize(os.path.join(self.dst, self.EP)), 3_000_000)

    def test_single_file_content_path(self):
        single = samples.make_torrent(os.path.join(self.content, "Épisode 1.mkv"),
                                      os.path.join(self.dir, "un.torrent"))
        self.seeder.add_torrent_file(single, self.content)
        e = self.slow_leecher()
        tid = e.add_torrent_file(single, self.dst)
        e.handles[tid].set_download_limit(300_000)
        self.link(e, tid)
        self.assertTrue(until(self.engines, lambda: "Épisode 1.mkv.part" in disk_names(self.dst)))
        self.assertTrue(until(self.engines, lambda: e.content_path(tid) == os.path.join(self.dst, "Épisode 1.mkv.part")))
        e.handles[tid].set_download_limit(0)
        self.assertTrue(until(self.engines, lambda: disk_names(self.dst) == {"Épisode 1.mkv"}, 30))
        self.assertTrue(until(self.engines, lambda: e.content_path(tid) == os.path.join(self.dst, "Épisode 1.mkv")))

    def test_unwanted_file_never_appears_as_complete(self):
        e = self.slow_leecher()
        info = eng.read_torrent(self.torrent)
        small = [f.index for f in info.files if f.path.endswith("lisez-moi.txt")]
        tid = e.add_torrent_file(self.torrent, self.dst, wanted=small)
        self.link(e, tid)
        self.assertTrue(until(self.engines, lambda: e.transfer(tid).complete
                              and os.path.join("Série", "lisez-moi.txt") in disk_names(self.dst)))
        until(self.engines, lambda: False, 0.5)
        self.assertNotIn(self.EP, disk_names(self.dst), "fichier décoché : jamais sous son vrai nom")


class RenameTest(Base):
    EP = os.path.join("Série", "Épisode 1.mkv")

    def files_of(self, e, tid):
        return {f.index: f.path for f in e.files(tid)}

    def test_rename_when_adding(self):
        e = self.leecher(part_ext=".part")
        info = eng.read_torrent(self.torrent)
        paths = {f.index: f.path for f in info.files}
        ep = next(i for i, p in paths.items() if p == self.EP)
        new = eng.renamed(paths, ep, "S01E01 - Le départ.mkv")
        new = eng.renamed(new, root="Ma série")
        tid = e.add_torrent_file(self.torrent, self.dst, names=new)
        e.handles[tid].set_download_limit(300_000)
        self.link(e, tid)
        self.assertTrue(until(self.engines, lambda: os.path.join("Ma série", "S01E01 - Le départ.mkv.part")
                              in disk_names(self.dst)), disk_names(self.dst))
        self.assertEqual(e.transfer(tid).name, "Ma série")
        self.assertEqual(e.content_path(tid), os.path.join(self.dst, "Ma série"))
        e.handles[tid].set_download_limit(0)
        self.assertTrue(until(self.engines, lambda: e.transfer(tid).complete and disk_names(self.dst) == {
            os.path.join("Ma série", p) for p in ("S01E01 - Le départ.mkv", "Bonus/Making-of.mkv",
                                                    "lisez-moi.txt")}, 30), disk_names(self.dst))
        self.assertIn(os.path.join("Ma série", "S01E01 - Le départ.mkv"), self.files_of(e, tid).values())
        with open(os.path.join(self.content, "Épisode 1.mkv"), "rb") as a, \
                open(os.path.join(self.dst, "Ma série", "S01E01 - Le départ.mkv"), "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_rename_during_download_and_restart(self):
        state = "state-rename"
        e = Engine(os.path.join(self.dir, state), listen_port=port(), local_only=True, part_ext=".part")
        self.engines.append(e)
        tid = e.add_torrent_file(self.torrent, self.dst)
        e.handles[tid].set_download_limit(300_000)
        self.link(e, tid)
        self.assertTrue(until(self.engines, lambda: e.transfer(tid).done > 300_000))
        ep = next(i for i, p in self.files_of(e, tid).items() if p == self.EP)
        self.assertTrue(e.rename(tid, ep, "Pilote.mkv"))
        self.assertTrue(e.rename(tid, root="Rangée"))
        self.assertTrue(until(self.engines, lambda: os.path.join("Rangée", "Pilote.mkv.part") in disk_names(self.dst)),
                        disk_names(self.dst))
        self.assertTrue(until(self.engines, lambda: not os.path.exists(os.path.join(self.dst, "Série"))),
                        "ancien dossier vide retiré")
        e.pause(tid)
        e.shutdown()
        self.engines.remove(e)
        e2 = Engine(os.path.join(self.dir, state), listen_port=port(), local_only=True, part_ext=".part")
        self.engines.append(e2)
        self.assertEqual(self.files_of(e2, tid)[ep], os.path.join("Rangée", "Pilote.mkv"), "noms retenus")
        self.assertEqual(e2.transfer(tid).name, "Rangée")
        e2.resume(tid)
        self.link(e2, tid)
        self.assertTrue(until(self.engines, lambda: e2.transfer(tid).complete
                              and os.path.join("Rangée", "Pilote.mkv") in disk_names(self.dst), 30))
        self.assertEqual(os.path.getsize(os.path.join(self.dst, "Rangée", "Pilote.mkv")), 3_000_000)
        self.assertFalse(any(n.endswith(".part") for n in disk_names(self.dst)), disk_names(self.dst))

    def test_refusals(self):
        e = self.leecher(part_ext=".part")
        tid = e.add_torrent_file(self.torrent, self.dst)
        self.link(e, tid)
        self.assertTrue(until(self.engines, lambda: e.transfer(tid).complete))
        files = {p: i for i, p in self.files_of(e, tid).items()}
        ep = files[self.EP]
        for name, code in (("", "empty"), ("..", "dot"), ("a/b", "slash"), ("x" * 300, "long"),
                           ("lisez-moi.txt", "taken")):
            with self.assertRaises(eng.RenameError) as cm:
                e.rename(tid, ep, name)
            self.assertEqual(cm.exception.code, code, name)
        with open(os.path.join(self.dst, "Série", "précieux.mkv"), "w") as fh:
            fh.write("ne pas écraser")
        with self.assertRaises(eng.RenameError) as cm:
            e.rename(tid, ep, "précieux.mkv")
        self.assertEqual(cm.exception.code, "exists")
        os.makedirs(os.path.join(self.dst, "Autre"))
        with self.assertRaises(eng.RenameError) as cm:
            e.rename(tid, root="Autre")
        self.assertEqual(cm.exception.code, "exists")
        with open(os.path.join(self.dst, "Série", "précieux.mkv")) as fh:
            self.assertEqual(fh.read(), "ne pas écraser")
        self.assertEqual(os.path.getsize(os.path.join(self.dst, self.EP)), 3_000_000, "rien n'a bougé")
        self.assertFalse(e.rename(tid, ep, "Épisode 1.mkv"), "même nom : rien à faire")
        # lien magnet sans informations : rien à renommer encore
        m = self.leecher(state="state-magnet")
        mt = m.add_magnet(lt.make_magnet_uri(lt.torrent_info(self.torrent)), os.path.join(self.dir, "m"))
        with self.assertRaises(eng.RenameError) as cm:
            m.rename(mt, root="x")
        self.assertEqual(cm.exception.code, "busy")

    def test_single_file(self):
        single = samples.make_torrent(os.path.join(self.content, "Épisode 1.mkv"), os.path.join(self.dir, "un.torrent"))
        self.seeder.add_torrent_file(single, self.content)
        e = self.leecher(part_ext=".part")
        tid = e.add_torrent_file(single, self.dst)
        self.link(e, tid)
        self.assertTrue(until(self.engines, lambda: e.transfer(tid).complete))
        self.assertTrue(e.rename(tid, root="Le film.mkv"))          # le dossier d'un fichier seul = ce fichier
        self.assertTrue(until(self.engines, lambda: disk_names(self.dst) == {"Le film.mkv"}), disk_names(self.dst))
        self.assertEqual(e.transfer(tid).name, "Le film.mkv")
        self.assertEqual(e.content_path(tid), os.path.join(self.dst, "Le film.mkv"))


class DeleteFileTest(Base):
    def test_delete_one_file(self):
        e = self.leecher(part_ext=".part")
        tid = e.add_torrent_file(self.torrent, self.dst)
        self.link(e, tid)
        self.assertTrue(until(self.engines, lambda: e.transfer(tid).complete))
        files = {os.path.basename(f.path): f.index for f in e.files(tid)}
        self.assertTrue(e.delete_file(tid, files["Épisode 1.mkv"]))
        self.assertFalse(os.path.exists(os.path.join(self.dst, "Série", "Épisode 1.mkv")), "effacé du disque")
        ep = files["Épisode 1.mkv"]
        self.assertTrue(until(self.engines, lambda: next(x.done for x in e.files(tid) if x.index == ep) == 0),
                        "revérifié : libtorrent sait qu'il ne l'a plus")
        self.link(e, tid)                       # (ni tracker ni DHT ici : on relie la source à la main)
        self.assertTrue(until(self.engines, lambda: e.transfer(tid).state == "seeding" and e.transfer(tid).complete, 30))
        self.assertFalse(os.path.exists(os.path.join(self.dst, "Série", "Épisode 1.mkv")), "pas retéléchargé")
        f = {os.path.basename(x.path): x for x in e.files(tid)}
        self.assertFalse(f["Épisode 1.mkv"].wanted)
        self.assertEqual(f["Épisode 1.mkv"].done, 0, "libtorrent sait qu'il ne l'a plus")
        with open(os.path.join(self.content, "Bonus", "Making-of.mkv"), "rb") as a, \
                open(os.path.join(self.dst, "Série", "Bonus", "Making-of.mkv"), "rb") as b:
            self.assertEqual(a.read(), b.read(), "voisins intacts")
        self.assertEqual(e.errors, [])
        # recoché : il revient
        e.set_wanted(tid, files["Épisode 1.mkv"], True)
        self.link(e, tid)
        self.assertTrue(until(self.engines, lambda: os.path.exists(os.path.join(self.dst, "Série", "Épisode 1.mkv"))
                              and e.transfer(tid).complete, 30))

    def test_refusals(self):
        single = samples.make_torrent(os.path.join(self.content, "Épisode 1.mkv"), os.path.join(self.dir, "un.torrent"))
        self.seeder.add_torrent_file(single, self.content)
        e = self.leecher()
        tid = e.add_torrent_file(single, self.dst)
        self.link(e, tid)
        self.assertTrue(until(self.engines, lambda: e.transfer(tid).complete))
        self.assertFalse(e.delete_file(tid, 0), "un seul fichier : c'est « Supprimer » du torrent")
        self.assertTrue(os.path.exists(os.path.join(self.dst, "Épisode 1.mkv")))


class PersistTest(Base):
    def test_restart(self):
        """Fermé en plein téléchargement puis relancé : la liste revient, la pause aussi, et le
        téléchargement repart d'où il en était."""
        state = os.path.join(self.dir, "state-restart")
        e = Engine(state, listen_port=port(), local_only=True)
        tid = e.add_torrent_file(self.torrent, self.dst)
        e.handles[tid].set_download_limit(300_000)
        e.connect_peer(tid, "127.0.0.1", self.seed_port)
        self.assertTrue(until([self.seeder, e], lambda: e.transfer(tid).done > 200_000))
        uri = lt.make_magnet_uri(lt.torrent_info(self.torrent))
        other = os.path.join(self.dir, "single")
        single = samples.make_torrent(samples.make_single(other), os.path.join(self.dir, "film.torrent"))
        tid_single = e.add_torrent_file(single, self.dst, paused=True)
        e.shutdown()
        self.assertTrue(os.path.exists(os.path.join(state, tid + ".resume")))
        e2 = Engine(state, listen_port=port(), local_only=True)
        self.engines.append(e2)
        self.assertEqual({t.tid for t in e2.transfers()}, {tid, tid_single})
        self.assertEqual(e2.transfer(tid_single).state, "paused")
        e2.connect_peer(tid, "127.0.0.1", self.seed_port)
        self.assertTrue(until(self.engines, lambda: e2.transfer(tid).complete, 30))
        self.assertEqual(eng.magnet_tid(uri), tid)

    def test_magnet_metadata_kept(self):
        """Lien magnet : après redémarrage, plus besoin de retrouver les métadonnées."""
        state = os.path.join(self.dir, "state-magnet")
        e = Engine(state, listen_port=port(), local_only=True)
        tid = e.add_magnet(lt.make_magnet_uri(lt.torrent_info(self.torrent)), self.dst, paused=False)
        e.connect_peer(tid, "127.0.0.1", self.seed_port)
        self.assertTrue(until([self.seeder, e], lambda: e.transfer(tid).has_metadata))
        e.pause(tid)
        e.shutdown()
        e2 = Engine(state, listen_port=port(), local_only=True)
        self.engines.append(e2)
        self.assertTrue(e2.transfer(tid).has_metadata)
        self.assertEqual(e2.transfer(tid).name, "Série")

    def test_broken_state_files(self):
        state = os.path.join(self.dir, "state-broken")
        os.makedirs(state)
        with open(os.path.join(state, "state.json"), "w") as fh:
            fh.write("[1, 2")
        with open(os.path.join(state, "abc.resume"), "wb") as fh:
            fh.write(b"n'importe quoi")
        e = Engine(state, listen_port=port(), local_only=True)
        self.engines.append(e)
        self.assertEqual(e.transfers(), [])
        self.assertTrue(e.errors)


class SafetyTest(Base):
    def test_evil_paths_stay_inside(self):
        """Noms en « ../ » ou absolus : rien n'est écrit hors du dossier de destination."""
        evil = samples.make_evil_torrent(os.path.join(self.dir, "evil.torrent"))
        info = eng.read_torrent(evil)
        for f in info.files:
            self.assertFalse(os.path.isabs(f.path), f.path)
            self.assertNotIn("..", f.path.split(os.sep), f.path)
        box = os.path.join(self.dir, "boite", "dedans")
        os.makedirs(box)
        e = self.leecher()
        tid = e.add_torrent_file(evil, box)
        until(self.engines, lambda: False, 1.5)
        e.remove(tid)
        for root, _dirs, files in os.walk(self.dir):
            for n in files:
                if n in ("evade.txt", "absolu.txt"):
                    self.assertTrue(os.path.join(root, n).startswith(box + os.sep), os.path.join(root, n))
        self.assertFalse(os.path.exists("/etc/absolu.txt"))



class AuditTest(Base):
    """Audit de novembre 2026 : chaque cas échouait sur l'ancien code."""

    def test_torrent_in_non_utf8_folder(self):
        """.torrent dans un dossier au nom non UTF-8 : libtorrent ne l'ouvrait pas (« pas un torrent valide ») ;
        supprimé après l'ajout : state.json ne s'écrivait plus jamais (UnicodeEncodeError)."""
        weird = os.path.join(os.fsencode(self.dir), b"caf\xe9")
        os.makedirs(weird)
        path = os.fsdecode(os.path.join(weird, b"s.torrent"))
        shutil.copy(self.torrent, path)
        self.assertEqual(eng.read_torrent(path).name, "Série")
        e = self.leecher()
        tid = e.add_torrent_file(path, self.dst, delete_source=True)
        self.link(e, tid)
        self.assertTrue(until(self.engines, lambda: not os.path.exists(path)))
        with open(os.path.join(e.state_dir, "state.json"), encoding="utf-8") as fh:
            self.assertIn(tid, json.load(fh))

    def test_non_utf8_save_path_refused(self):
        """Dossier d'enregistrement / de déplacement non UTF-8 : refus clair (PathError), plus d'UnicodeEncodeError."""
        weird = os.fsdecode(os.path.join(os.fsencode(self.dir), b"caf\xe9"))
        os.makedirs(weird)
        e = self.leecher()
        with self.assertRaises(eng.PathError):
            e.add_torrent_file(self.torrent, weird)
        self.assertEqual(e.handles, {})
        tid = e.add_torrent_file(self.torrent, self.dst, paused=True)
        with self.assertRaises(eng.PathError):
            e.move(tid, weird)
        self.assertFalse(e.moving(tid))

    def test_rewanted_file_after_seed_stop_downloads(self):
        """Fichier recoché après l'arrêt du partage (en pause par la règle) : il restait en pause pour toujours ;
        relancé, il partageait ensuite sans fin (seed_forever)."""
        e = self.leecher(seed_policy="never")
        tid = e.add_torrent_file(self.torrent, self.dst)
        small = next(f.index for f in e.files(tid) if f.path.endswith("lisez-moi.txt"))
        e.set_wanted(tid, small, False)
        self.link(e, tid)
        self.assertTrue(until(self.engines, lambda: e.transfer(tid).state == "done"))
        e.set_wanted(tid, small, True)
        self.link(e, tid)
        self.assertTrue(until(self.engines, lambda: next(f for f in e.files(tid) if f.index == small).done > 0))
        self.assertTrue(until(self.engines, lambda: e.transfer(tid).state == "done"))
        self.assertFalse(e.meta[tid].get("seed_forever"))

    def test_settings_non_utf8_folder(self):
        """Réglage au nom non UTF-8 : save() levait UnicodeEncodeError (plus rien d'enregistré), et download_dir()
        renvoyait un dossier inutilisable par libtorrent."""
        from telemaque.settings import Settings, default_download_dir
        s = Settings(os.path.join(self.dir, "settings.json"))
        weird = os.fsdecode(os.path.join(os.fsencode(self.dir), b"caf\xe9"))
        os.makedirs(weird)
        s["download_dir"] = weird
        self.assertEqual(Settings(s.path)["download_dir"], weird)
        self.assertEqual(s.download_dir(), default_download_dir())


if __name__ == "__main__":
    unittest.main()
