"""Moteur BitTorrent de Télémaque : une session libtorrent (celle de qBittorrent et Deluge) et ses
téléchargements, sans GTK — testable seul (deux moteurs sur la même machine s'échangent un torrent).

- Identité d'un téléchargement : l'empreinte (info-hash v1, en hexadécimal).
- Tout est hors de la file automatique de libtorrent (pas d'« auto_managed ») : un téléchargement
  démarré tourne, un téléchargement en pause reste en pause. Simple et prévisible.
- Reprise après redémarrage : données de reprise de libtorrent (`<id>.resume`, avec le dictionnaire
  d'informations : un lien magnet n'a pas à retrouver ses métadonnées), plus nos propres notes dans
  `state.json` (ordre d'ajout, fin déjà signalée, arrêt du partage).
- Partage : une fois les fichiers voulus complets, s'arrêter au ratio choisi, ne jamais s'arrêter, ou
  s'arrêter tout de suite. Relancé à la main après coup : partage sans limite (`seed_forever`).

poll() est appelé régulièrement par l'interface (une fois par seconde) : il traite les alertes de
libtorrent, applique la règle de partage et renvoie l'état de chaque téléchargement.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import libtorrent as lt

RESUME_EVERY = 60                 # secondes : données de reprise des téléchargements qui ont changé


class TorrentError(Exception):
    """Fichier .torrent illisible, lien magnet invalide…"""


class Duplicate(TorrentError):
    def __init__(self, tid: str):
        super().__init__(tid)
        self.tid = tid


@dataclass
class FileEntry:
    index: int
    path: str                     # chemin dans le torrent (« Dossier/sous/fichier.mkv »)
    size: int
    done: int = 0
    wanted: bool = True


@dataclass
class TorrentInfo:
    """Contenu d'un fichier .torrent, pour le dialogue d'ajout (avant de le lancer)."""
    tid: str
    name: str
    size: int
    files: list = field(default_factory=list)       # FileEntry


@dataclass
class NetInfo:
    """Barre d'état : par où passe Télémaque (utile derrière un VPN) et si le DHT travaille."""
    # adresses publiques vues par les trackers / le DHT, une par famille ("" : pas encore connue)
    ipv4: str = ""
    ipv6: str = ""
    checking: bool = False        # réseau changé (VPN coupé / remis) : adresses en cours de vérification
    looked_up: bool = False       # adresses vérifiées auprès d'ipify.org : une famille vide = il n'y en a pas
    port: int = 0
    reachable: bool = False       # des connexions entrantes arrivent : le port est ouvert
    dht_enabled: bool = False
    dht_nodes: int = 0            # nœuds DHT connus : 0 avec le DHT actif = il se connecte
    listen_error: str = ""


@dataclass
class Transfer:
    """État d'un téléchargement à un instant (tout ce qu'affiche une ligne de la liste)."""
    tid: str
    name: str
    state: str                    # metadata, checking, downloading, seeding, paused, done, error
    progress: float = 0.0         # 0..1, sur les fichiers voulus
    size: int = 0                 # octets voulus
    done: int = 0
    down_rate: int = 0            # octets / s
    up_rate: int = 0
    eta: int = -1                 # secondes, -1 : inconnu
    peers: int = 0
    seeds: int = 0
    ratio: float = 0.0
    uploaded: int = 0
    save_path: str = ""
    error: str = ""
    has_metadata: bool = True
    complete: bool = False        # fichiers voulus tous là

    @property
    def active(self) -> bool:
        """Quelque chose se passe encore (pour la question « Continuer en arrière-plan ? »)."""
        return self.state in ("metadata", "checking", "downloading", "seeding", "moving")

    @property
    def downloading(self) -> bool:
        return self.state in ("metadata", "checking", "downloading")


def _tid_of(hashes) -> str:
    """Identité : l'empreinte v1 si elle existe. Un torrent « hybride » (v1 + v2, ce que fabrique
    libtorrent 2) n'a pas la même empreinte « au mieux » lue dans le fichier et dans son lien magnet."""
    if hashes.has_v1():
        return str(hashes.v1)
    return str(hashes.get_best())


def _is_pad(fs, i: int) -> bool:
    try:
        return bool(fs.file_flags(i) & lt.file_storage.flag_pad_file)
    except AttributeError:
        return False


def read_torrent(path: str) -> TorrentInfo:
    """Lit un fichier .torrent sans l'ajouter."""
    try:
        ti = lt.torrent_info(path)
    except (RuntimeError, ValueError, OSError) as e:
        raise TorrentError(str(e)) from e
    fs = ti.files()
    files = [FileEntry(i, fs.file_path(i), fs.file_size(i)) for i in range(fs.num_files()) if not _is_pad(fs, i)]
    return TorrentInfo(_tid_of(ti.info_hashes()), ti.name(), sum(f.size for f in files), files)


def magnet_tid(uri: str) -> str:
    try:
        params = lt.parse_magnet_uri(uri)
    except (RuntimeError, ValueError) as e:
        raise TorrentError(str(e)) from e
    return _tid_of(params.info_hashes)


def is_magnet(text: str) -> bool:
    return text.strip().lower().startswith("magnet:?")


class Engine:
    def __init__(self, state_dir: str, *, listen_port: int = 6881, dht: bool = True, local_only: bool = False,
                 seed_policy: str = "ratio", seed_ratio: float = 1.0, down_limit: int = 0, up_limit: int = 0,
                 part_ext: str = ""):
        """local_only : pour les tests — écoute sur 127.0.0.1, ni DHT, ni découverte locale, ni UPnP.
        part_ext : extension des fichiers incomplets (« .part »), vide = noms définitifs dès le début."""
        self.part_ext = part_ext
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        self.seed_policy = seed_policy
        self.seed_ratio = seed_ratio
        self.local_only = local_only
        pack = {
            "listen_interfaces": (f"127.0.0.1:{listen_port}" if local_only
                                  else f"0.0.0.0:{listen_port},[::]:{listen_port}"),
            "enable_dht": dht and not local_only,
            "enable_lsd": not local_only,
            "enable_upnp": not local_only,
            "enable_natpmp": not local_only,
            "alert_mask": (lt.alert_category.error | lt.alert_category.status | lt.alert_category.storage
                           | lt.alert_category.file_progress),
            "user_agent": "Telemaque/0.1 libtorrent/" + lt.__version__,
            "download_rate_limit": max(0, int(down_limit)) * 1024,
            "upload_rate_limit": max(0, int(up_limit)) * 1024,
        }
        if local_only:
            pack["allow_multiple_connections_per_ip"] = True
            pack["min_reconnect_time"] = 1     # une seule source : la recontacter tout de suite (60 s sinon)
            pack["enable_outgoing_utp"] = False
            pack["enable_incoming_utp"] = False
        self.session = lt.session(pack)
        self.handles: dict[str, lt.torrent_handle] = {}
        self.meta: dict[str, dict] = {}            # nos notes par téléchargement (state.json)
        self._pending_saves: set[str] = set()
        self._last_resume = time.monotonic()
        self.on_finished: Optional[Callable[[str], None]] = None     # fin d'un téléchargement (une fois)
        self.on_changed: Optional[Callable[[], None]] = None         # liste changée (ajout, retrait, métadonnées)
        # déplacement fini : on_moved(tid, ok, message) — message d'erreur de libtorrent si raté
        self.on_moved: Optional[Callable[[str, bool, str], None]] = None
        self._moving: set[str] = set()
        self._in_alerts = False
        self.errors: list[str] = []
        self.dht_enabled = bool(pack["enable_dht"])
        # libtorrent annonce l'adresse publique famille par famille (external_ip_alert) : garder les deux,
        # sinon la dernière annoncée (souvent l'IPv6) remplaçait l'autre (vu chez Tof)
        self.ipv4 = ""
        self.ipv6 = ""
        self.ip_checking = False
        self.ip_looked_up = False
        self.listen_error = ""
        self.stats: dict = {}
        self._load()

    # -- réglages ------------------------------------------------------------------------------
    def set_limits(self, down_kib: int, up_kib: int):
        self.session.apply_settings({"download_rate_limit": max(0, int(down_kib)) * 1024,
                                     "upload_rate_limit": max(0, int(up_kib)) * 1024})

    def set_network(self, listen_port: int, dht: bool):
        if self.local_only:
            return
        self.session.apply_settings({"listen_interfaces": f"0.0.0.0:{listen_port},[::]:{listen_port}",
                                     "enable_dht": bool(dht)})
        self.dht_enabled = bool(dht)
        self.listen_error = ""

    def listen_port(self) -> int:
        return self.session.listen_port()

    # -- ajout ---------------------------------------------------------------------------------
    def _add_params(self, params, save_path: str, paused: bool):
        params.save_path = save_path
        flags = params.flags
        flags &= ~lt.torrent_flags.auto_managed
        if paused:
            flags |= lt.torrent_flags.paused
        else:
            flags &= ~lt.torrent_flags.paused
        params.flags = flags
        return params

    def add_torrent_file(self, path: str, save_path: str, wanted: Optional[list] = None,
                         paused: bool = False, delete_source: bool = False) -> str:
        """wanted : indices des fichiers à télécharger (None : tous). delete_source : le fichier .torrent
        est supprimé une fois ses données de reprise ÉCRITES (elles contiennent tout le torrent) — un
        plantage juste après l'ajout ne fait rien perdre."""
        try:
            ti = lt.torrent_info(path)
        except (RuntimeError, ValueError, OSError) as e:
            raise TorrentError(str(e)) from e
        tid = _tid_of(ti.info_hashes())
        if tid in self.handles:
            raise Duplicate(tid)
        params = lt.add_torrent_params()
        params.ti = ti
        if wanted is not None:
            keep = set(wanted)
            params.file_priorities = [4 if i in keep else 0 for i in range(ti.num_files())]
        meta = {"added": time.time()}
        if delete_source and path.lower().endswith(".torrent"):
            meta["delete_source"] = os.path.abspath(path)
        return self._add(tid, self._add_params(params, save_path, paused), meta)

    def add_magnet(self, uri: str, save_path: str, paused: bool = False) -> str:
        try:
            params = lt.parse_magnet_uri(uri.strip())
        except (RuntimeError, ValueError) as e:
            raise TorrentError(str(e)) from e
        tid = magnet_tid(uri.strip())
        if tid in self.handles:
            raise Duplicate(tid)
        return self._add(tid, self._add_params(params, save_path, paused))

    def _add(self, tid: str, params, meta: Optional[dict] = None) -> str:
        try:
            h = self.session.add_torrent(params)
        except RuntimeError as e:
            raise TorrentError(str(e)) from e
        self.handles[tid] = h
        self.meta[tid] = meta or {"added": time.time()}
        self._save_meta()
        self._sync_names(tid)
        self._request_save(tid)
        if self.on_changed:
            self.on_changed()
        return tid

    # -- commandes -----------------------------------------------------------------------------
    def pause(self, tid: str):
        h = self.handles.get(tid)
        if h is None:
            return
        h.unset_flags(lt.torrent_flags.auto_managed)
        h.pause()
        self._request_save(tid)

    def resume(self, tid: str):
        h = self.handles.get(tid)
        if h is None:
            return
        m = self.meta.setdefault(tid, {})
        m.pop("paused_all", None)
        if m.pop("seed_stopped", False):
            m["seed_forever"] = True            # relancé à la main après l'arrêt du partage
            self._save_meta()
        h.clear_error()
        h.resume()
        self._find_sources(h)
        self._request_save(tid)

    def pause_all(self):
        """Tout ce qui tourne passe en pause ; « Tout reprendre » relancera exactement ceux-là (et les
        téléchargements en pause), pas ceux dont le partage était déjà terminé."""
        for tid, h in self.handles.items():
            if not h.status().flags & lt.torrent_flags.paused:
                self.meta.setdefault(tid, {})["paused_all"] = True
                self.pause(tid)
        self._save_meta()

    def paused_all(self, tid: str) -> bool:
        return bool(self.meta.get(tid, {}).get("paused_all"))

    def resume_all(self):
        for t in self.transfers():
            if t.state == "paused" or (t.state == "done" and self.paused_all(t.tid)):
                self.meta.get(t.tid, {}).pop("paused_all", None)
                h = self.handles[t.tid]
                h.clear_error()
                h.resume()
                self._find_sources(h)
                self._request_save(t.tid)
        self._save_meta()

    def move(self, tid: str, dest: str) -> bool:
        """Déplace les fichiers d'un téléchargement (en cours, en pause ou fini) vers le dossier `dest`, comme
        qBittorrent : libtorrent les déplace dans son propre fil (simple renommage sur le même disque, copie
        puis suppression sinon) et continue de télécharger au nouvel endroit. Rien n'est jamais écrasé
        (fail_if_exist) : un fichier du même nom déjà là-bas fait échouer le déplacement, tout reste en
        place. Fin annoncée par on_moved. False si rien n'a été lancé (même dossier, inconnu…)."""
        h = self.handles.get(tid)
        if h is None or tid in self._moving:
            return False
        dest = os.path.abspath(os.path.expanduser(dest))
        if os.path.abspath(h.status().save_path) == dest:
            return False
        self._moving.add(tid)
        h.move_storage(dest, lt.move_flags_t.fail_if_exist)
        return True

    def moving(self, tid: str) -> bool:
        return tid in self._moving

    # -- fichiers incomplets (« .part ») ---------------------------------------------------------
    def set_part_ext(self, ext: str):
        """Extension des fichiers incomplets changée (fonctionnalité coupée ou remise) : appliquée tout de
        suite à tous les téléchargements."""
        self.part_ext = ext
        for tid in list(self.handles):
            self._sync_names(tid)

    def _sync_names(self, tid: str):
        """Comme qBittorrent (« .!qB ») : un fichier pas encore complet porte l'extension `part_ext` sur le
        disque, il reprend son vrai nom dès qu'il est complet — jamais de confusion entre un fichier fini et
        un fichier en cours ou en pause. libtorrent renomme lui-même (fichier ouvert compris) et retient les
        noms dans les données de reprise. Rien pendant une vérification (avancement pas encore connu) ni
        pendant un déplacement."""
        h = self.handles.get(tid)
        if h is None or not h.is_valid() or tid in self._moving:
            return
        st = h.status()
        S = lt.torrent_status.states
        if not st.has_metadata or st.state in (S.checking_files, S.checking_resume_data):
            return
        ti = h.torrent_file()
        if ti is None:
            return
        fs, orig = ti.files(), ti.orig_files()
        progress = h.file_progress()
        for i in range(fs.num_files()):
            if _is_pad(fs, i):
                continue
            base = orig.file_path(i)
            complete = i < len(progress) and progress[i] >= fs.file_size(i)
            want = base + self.part_ext if self.part_ext and not complete else base
            if fs.file_path(i) != want:
                h.rename_file(i, want)

    def remove(self, tid: str, delete_files: bool = False):
        h = self.handles.pop(tid, None)
        if h is None:
            return
        self.session.remove_torrent(h, lt.session.delete_files if delete_files else 0)
        self._moving.discard(tid)
        self.meta.pop(tid, None)
        self._pending_saves.discard(tid)
        self._save_meta()
        try:
            os.remove(self._resume_path(tid))
        except OSError:
            pass
        if self.on_changed:
            self.on_changed()

    def files(self, tid: str) -> list:
        """Fichiers d'un téléchargement ([] tant que les métadonnées d'un lien magnet manquent)."""
        h = self.handles.get(tid)
        if h is None or not h.status().has_metadata:
            return []
        ti = h.torrent_file()
        if ti is None:
            return []
        fs, orig = ti.files(), ti.orig_files()
        prio = h.get_file_priorities()
        progress = h.file_progress()
        out = []
        for i in range(fs.num_files()):
            if _is_pad(fs, i):
                continue
            out.append(FileEntry(i, orig.file_path(i), fs.file_size(i),
                                 progress[i] if i < len(progress) else 0,
                                 (prio[i] if i < len(prio) else 4) > 0))
        return out

    def set_wanted(self, tid: str, index: int, wanted: bool):
        h = self.handles.get(tid)
        if h is None:
            return
        h.file_priority(index, 4 if wanted else 0)
        self._sync_names(tid)
        if wanted:
            self._find_sources(h)        # complet, il avait laissé partir les sources complètes
        m = self.meta.get(tid, {})
        if wanted and m.get("notified"):
            m.pop("notified", None)              # un fichier de plus : une nouvelle fin à signaler
            self._save_meta()
        self._request_save(tid)

    @staticmethod
    def _find_sources(h):
        """Redemande des sources au tracker et au DHT tout de suite (sinon : à la prochaine annonce,
        jusqu'à 30 min plus tard)."""
        try:
            h.force_reannounce()
            h.force_dht_announce()
        except RuntimeError:
            pass

    def connect_peer(self, tid: str, host: str, port: int):
        h = self.handles.get(tid)
        if h is not None:
            h.connect_peer((host, port))

    def content_path(self, tid: str) -> str:
        """Fichier (torrent d'un seul fichier) ou dossier téléchargé."""
        h = self.handles.get(tid)
        if h is None:
            return ""
        st = h.status()
        ti = h.torrent_file() if st.has_metadata else None
        if ti is not None:                   # un seul fichier : son nom du moment (« x.mkv.part » en cours)
            fs = ti.files()
            real = [i for i in range(fs.num_files()) if not _is_pad(fs, i)]
            if len(real) == 1 and os.sep not in fs.file_path(real[0]).rstrip(os.sep):
                return os.path.join(st.save_path, fs.file_path(real[0]))
        return os.path.join(st.save_path, st.name) if st.name else st.save_path

    # -- état ----------------------------------------------------------------------------------
    def transfers(self) -> list:
        out = [self._snapshot(tid, h) for tid, h in self.handles.items()]
        out.sort(key=lambda t: self.meta.get(t.tid, {}).get("added", 0))
        return out

    def transfer(self, tid: str) -> Optional[Transfer]:
        h = self.handles.get(tid)
        return self._snapshot(tid, h) if h is not None else None

    def _snapshot(self, tid: str, h) -> Transfer:
        st = h.status()
        S = lt.torrent_status.states
        paused = bool(st.flags & lt.torrent_flags.paused)
        complete = st.has_metadata and st.total_wanted > 0 and st.total_wanted_done >= st.total_wanted
        uploaded = st.all_time_upload
        ratio = uploaded / st.total_wanted if st.total_wanted else 0.0
        error = ""
        if tid in self._moving or getattr(st, "moving_storage", False):
            state = "moving"
        elif st.errc and st.errc.value() != 0:
            error = st.errc.message()
            state = "error"
        elif paused:
            state = "done" if complete else "paused"
        elif st.state in (S.checking_files, S.checking_resume_data, S.queued_for_checking, S.allocating):
            state = "checking"
        elif st.state == S.downloading_metadata or not st.has_metadata:
            state = "metadata"
        elif complete or st.state in (S.finished, S.seeding):
            state = "seeding"
        else:
            state = "downloading"
        # (vitesses = moyennes glissantes : elles traînent quelques secondes après la fin ou la pause)
        rate = 0 if complete or paused else st.download_payload_rate
        up = 0 if paused else st.upload_payload_rate
        left = max(0, st.total_wanted - st.total_wanted_done)
        eta = int(left / rate) if rate > 0 and left else (0 if complete else -1)
        return Transfer(tid=tid, name=st.name or self.meta.get(tid, {}).get("name", "") or tid,
                        state=state, progress=(st.total_wanted_done / st.total_wanted if st.total_wanted else 0.0),
                        size=st.total_wanted, done=st.total_wanted_done, down_rate=rate,
                        up_rate=up, eta=eta, peers=st.num_peers, seeds=st.num_seeds,
                        ratio=ratio, uploaded=uploaded, save_path=st.save_path, error=error,
                        has_metadata=st.has_metadata, complete=complete)

    # -- boucle --------------------------------------------------------------------------------
    def note_external_ip(self, ip: str):
        if ":" in ip:
            self.ipv6 = ip
        elif ip:
            self.ipv4 = ip
        if ip:
            self.ip_checking = False

    def set_public_ips(self, ipv4: str, ipv6: str):
        """Réponse d'ipify.org (backend/publicip.py) : elle fait foi pour les deux familles."""
        self.ipv4, self.ipv6 = ipv4, ipv6
        self.ip_checking = False
        self.ip_looked_up = True

    def network_changed(self):
        """Le réseau a changé (VPN coupé ou remis, Wi-Fi…) : les adresses publiques connues ne valent plus
        rien. libtorrent ne l'apprend que par les réponses des trackers et du DHT — jusqu'à 30 min plus
        tard (vu chez Tof : l'adresse du VPN restait affichée). Adresses oubliées, prises réseau rouvertes
        sur les interfaces du moment, trackers et DHT interrogés tout de suite."""
        self.ipv4 = self.ipv6 = ""
        self.ip_checking = True
        self.ip_looked_up = False
        if self.local_only:
            return
        try:
            # (en Python, le drapeau est obligatoire : sans lui, ArgumentError de Boost) ; reopen_map_ports :
            # rouvre aussi le port sur la box (UPnP / NAT-PMP) pour la nouvelle interface
            self.session.reopen_network_sockets(lt.session.reopen_map_ports)
        except (RuntimeError, AttributeError, TypeError):
            pass
        for h in self.handles.values():
            if not h.status().flags & lt.torrent_flags.paused:
                self._find_sources(h)

    def network(self) -> NetInfo:
        st = self.stats
        return NetInfo(ipv4=self.ipv4, ipv6=self.ipv6, checking=self.ip_checking, looked_up=self.ip_looked_up,
                       port=self.listen_port(),
                       reachable=bool(st.get("net.has_incoming_connections", 0)),
                       dht_enabled=self.dht_enabled, dht_nodes=int(st.get("dht.dht_nodes", 0)),
                       listen_error=self.listen_error)

    def poll(self) -> list:
        self.session.post_session_stats()              # nœuds DHT, connexions entrantes (alerte suivante)
        self._handle_alerts()
        for tid, h in list(self.handles.items()):
            self._apply_seed_policy(tid, h)
        if time.monotonic() - self._last_resume > RESUME_EVERY:
            self._last_resume = time.monotonic()
            for tid, h in self.handles.items():
                if h.need_save_resume_data():
                    self._request_save(tid)
        return self.transfers()

    def _apply_seed_policy(self, tid: str, h):
        st = h.status()
        if st.flags & lt.torrent_flags.paused or not st.has_metadata:
            return
        if not (st.total_wanted > 0 and st.total_wanted_done >= st.total_wanted):
            return
        if st.state not in (lt.torrent_status.states.finished, lt.torrent_status.states.seeding):
            return                                    # encore en vérification
        m = self.meta.setdefault(tid, {})
        if m.get("seed_forever") or self.seed_policy == "always":
            return
        if self.seed_policy == "ratio" and st.total_wanted and st.all_time_upload / st.total_wanted < self.seed_ratio:
            return
        m["seed_stopped"] = True
        self._save_meta()
        h.unset_flags(lt.torrent_flags.auto_managed)
        h.pause()
        self._request_save(tid)

    def _find(self, handle) -> Optional[str]:
        for tid, h in self.handles.items():
            if h == handle:
                return tid
        return None

    def _handle_alerts(self):
        # Une alerte de libtorrent ne vit que jusqu'au pop_alerts() suivant. Un rappel (on_moved, on_finished,
        # on_changed) qui relance poll() pendant la boucle libérait les alertes pas encore lues : plantage
        # (segfault dans polymorphic_id_generator<alert>, vu avec « .part » et un déplacement). Pas de réentrée.
        if self._in_alerts:
            return
        self._in_alerts = True
        try:
            self._handle_alert_list(self.session.pop_alerts())
        finally:
            self._in_alerts = False

    def _handle_alert_list(self, alerts):
        for a in alerts:
            if isinstance(a, lt.save_resume_data_alert):
                tid = self._find(a.handle)
                if tid is not None:
                    self._write_resume(tid, a.params)
                    self._pending_saves.discard(tid)
                    self._delete_source(tid, a.params)
            elif isinstance(a, lt.save_resume_data_failed_alert):
                self._pending_saves.discard(self._find(a.handle) or "")
            elif isinstance(a, lt.torrent_finished_alert):
                tid = self._find(a.handle)
                if tid is None:
                    continue
                self._sync_names(tid)
                self._request_save(tid)
                m = self.meta.setdefault(tid, {})
                if not m.get("notified"):
                    m["notified"] = True
                    self._save_meta()
                    if self.on_finished:
                        self.on_finished(tid)
            elif isinstance(a, (lt.storage_moved_alert, lt.storage_moved_failed_alert)):
                tid = self._find(a.handle)
                if tid is None:
                    continue
                self._moving.discard(tid)
                ok = isinstance(a, lt.storage_moved_alert)
                if ok:
                    self._sync_names(tid)                  # fichiers finis pendant le déplacement
                    self._request_save(tid)                # nouvel emplacement retenu au redémarrage
                if self.on_moved:
                    self.on_moved(tid, ok, "" if ok else a.message())
            elif isinstance(a, (lt.file_completed_alert, lt.torrent_checked_alert)):
                tid = self._find(a.handle)
                if tid is not None:
                    self._sync_names(tid)
            elif isinstance(a, lt.file_rename_failed_alert):
                self.errors.append(a.message())
            elif isinstance(a, lt.metadata_received_alert):
                tid = self._find(a.handle)
                if tid is not None:
                    self.meta.setdefault(tid, {})["name"] = a.handle.status().name
                    self._save_meta()
                    self._sync_names(tid)
                    self._request_save(tid)
                    if self.on_changed:
                        self.on_changed()
            elif isinstance(a, lt.session_stats_alert):
                values = a.values
                if isinstance(values, dict):
                    self.stats = values
            elif isinstance(a, lt.external_ip_alert):
                self.note_external_ip(str(a.external_address))
            elif isinstance(a, lt.listen_succeeded_alert):
                self.listen_error = ""
            elif isinstance(a, lt.listen_failed_alert):
                self.listen_error = a.message()
            elif isinstance(a, (lt.torrent_error_alert, lt.file_error_alert)):
                self.errors.append(a.message())

    # -- enregistrement ------------------------------------------------------------------------
    def _resume_path(self, tid: str) -> str:
        return os.path.join(self.state_dir, tid + ".resume")

    def _request_save(self, tid: str):
        h = self.handles.get(tid)
        if h is None or not h.is_valid():
            return
        h.save_resume_data(lt.torrent_handle.save_info_dict | lt.torrent_handle.flush_disk_cache)
        self._pending_saves.add(tid)

    def _write_resume(self, tid: str, params):
        data = lt.write_resume_data_buf(params)
        _write_atomic(self._resume_path(tid), bytes(data))

    def _delete_source(self, tid: str, params):
        """Fichier .torrent d'origine supprimé une fois les données de reprise écrites, et seulement si
        elles contiennent bien le torrent (dictionnaire d'informations)."""
        m = self.meta.get(tid, {})
        src = m.get("delete_source")
        if not src or params.ti is None:
            return
        m.pop("delete_source", None)
        self._save_meta()
        if os.path.dirname(os.path.abspath(src)) == os.path.abspath(self.state_dir):
            return
        try:
            if os.path.isfile(src) and not os.path.islink(src):
                os.remove(src)
        except OSError as e:
            self.errors.append(f"{src} : {e}")

    def _save_meta(self):
        _write_atomic(os.path.join(self.state_dir, "state.json"),
                      json.dumps(self.meta, indent=1, ensure_ascii=False).encode("utf-8"))

    def _load(self):
        try:
            with open(os.path.join(self.state_dir, "state.json"), encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        for name in sorted(os.listdir(self.state_dir)):
            if not name.endswith(".resume"):
                continue
            tid = name[:-len(".resume")]
            try:
                with open(os.path.join(self.state_dir, name), "rb") as fh:
                    params = lt.read_resume_data(fh.read())
                h = self.session.add_torrent(params)
            except (OSError, RuntimeError, ValueError) as e:
                self.errors.append(f"{name} : {e}")
                continue
            self.handles[tid] = h
            m = meta.get(tid)
            self.meta[tid] = m if isinstance(m, dict) else {"added": time.time()}

    def save_all(self, timeout: float = 5.0):
        """Avant de quitter : données de reprise de tout le monde, attendues au plus `timeout` s."""
        for tid in list(self.handles):
            self._request_save(tid)
        end = time.monotonic() + timeout
        while self._pending_saves and time.monotonic() < end:
            self.session.wait_for_alert(200)
            self._handle_alerts()
        self._save_meta()

    def shutdown(self):
        self.save_all()
        self.session.pause()


def _write_atomic(path: str, data: bytes):
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
