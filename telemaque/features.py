"""Fonctionnalités facultatives de Télémaque, toutes désactivables sans redémarrage.

Chaque fonctionnalité se consulte au moment de l'usage (enabled(id)), jamais à l'import :
Préférences → Fonctionnalités l'active ou la coupe tout de suite.
"""

from __future__ import annotations

from dataclasses import dataclass

from .i18n import tr
from .settings import settings


@dataclass(frozen=True)
class Feature:
    id: str
    title: str
    description: str
    icon: str
    default: bool = True


FEATURES = [
    Feature("tray", tr("Icône dans la barre du haut"),
            tr("En arrière-plan, une icône garde Télémaque à portée : état, Afficher, Tout mettre en pause, "
               "Quitter (extension AppIndicator, présente sur Zorin et Ubuntu)"),
            "view-app-grid-symbolic"),
    Feature("public_ip", tr("Vérifier l'adresse publique"),
            tr("Au démarrage et quand le réseau change (VPN activé ou coupé), demander l'adresse IPv4 et IPv6 "
               "à ipify.org pour la barre d'état ; sinon, seulement ce que les trackers et le DHT renvoient"),
            "network-workgroup-symbolic"),
    Feature("part_suffix", tr("Extension .part pour les fichiers incomplets"),
            tr("Un fichier en cours ou en pause se termine par .part : impossible de le confondre avec un "
               "fichier complet. Il reprend son vrai nom dès qu'il est complet"),
            "document-save-symbolic"),
    Feature("delete_torrent", tr("Supprimer le fichier .torrent après l'ajout"),
            tr("Moins de fichiers qui traînent : Télémaque garde tout ce qu'il faut de son côté"),
            "user-trash-symbolic", default=False),
    Feature("notify", tr("Prévenir à la fin d'un téléchargement"),
            tr("Une notification du bureau, avec « Ouvrir » et « Afficher dans le dossier »"),
            "preferences-system-notifications-symbolic"),
    Feature("inhibit", tr("Empêcher la mise en veille"),
            tr("L'ordinateur ne se met pas en veille tant qu'un téléchargement est en cours"),
            "system-suspend-symbolic"),
]

BY_ID = {f.id: f for f in FEATURES}


def enabled(fid: str) -> bool:
    f = BY_ID.get(fid)
    if f is None:
        return False
    value = settings["features"].get(fid, f.default)
    return value if isinstance(value, bool) else f.default     # « "no" » écrit à la main ne vaut pas vrai


def set_enabled(fid: str, value: bool):
    feats = dict(settings["features"])
    feats[fid] = bool(value)
    settings["features"] = feats
