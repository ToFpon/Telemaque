"""Adresses publiques demandées à ipify.org (libre, sans compte), au démarrage et quand le réseau change
(fonctionnalité « public_ip », choix de Tof). libtorrent n'apprend l'adresse publique qu'aux réponses des
trackers et du DHT, interface par interface : un VPN activé APRÈS le lancement restait sur « vérification… »
(vu chez Tof), les anciennes interfaces continuant de « voter » pour l'ancienne adresse.

Deux requêtes HTTPS : api.ipify.org (IPv4) et api6.ipify.org (IPv6 seulement : échoue s'il n'y a pas d'IPv6).
Dans un fil ; la réponse est vérifiée (une vraie adresse de la bonne famille, sinon rien).
"""

from __future__ import annotations

import ipaddress
import threading
import urllib.request
from typing import Callable

URLS = {4: "https://api.ipify.org", 6: "https://api6.ipify.org"}
TIMEOUT = 8


def fetch(version: int, timeout: float = TIMEOUT) -> str:
    """Adresse publique de cette famille, ou "" (pas de connexion de cette famille, service muet…)."""
    try:
        req = urllib.request.Request(URLS[version], headers={"User-Agent": "Telemaque"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read(64).decode("ascii", "replace").strip()
        return parse(text, version)
    except Exception:  # noqa: BLE001 — réseau absent, délai, proxy… : pas d'adresse, rien de plus
        return ""


def parse(text: str, version: int) -> str:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return ""
    return str(ip) if ip.version == version and ip.is_global else ""


def lookup(callback: Callable[[str, str], None]):
    """callback(ipv4, ipv6) depuis le fil de recherche (à repasser au fil principal par l'appelant)."""
    def run():
        results = {}
        threads = [threading.Thread(target=lambda v=v: results.__setitem__(v, fetch(v)), daemon=True)
                   for v in (4, 6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        callback(results.get(4, ""), results.get(6, ""))
    threading.Thread(target=run, daemon=True).start()
