"""Torrents d'essai fabriqués sur place : un dossier de plusieurs fichiers, un fichier seul, et un
torrent piégé (noms en « ../ », chemin absolu) écrit à la main en bencode."""

import os

import libtorrent as lt


def make_content(root: str, name: str = "Série", sizes=(3_000_000, 700_000, 12)) -> str:
    """Dossier root/name avec quelques fichiers de contenu aléatoire."""
    folder = os.path.join(root, name)
    os.makedirs(os.path.join(folder, "Bonus"), exist_ok=True)
    names = ["Épisode 1.mkv", os.path.join("Bonus", "Making-of.mkv"), "lisez-moi.txt"]
    for n, size in zip(names, sizes):
        with open(os.path.join(folder, n), "wb") as fh:
            fh.write(os.urandom(size))
    return folder


def make_single(root: str, name: str = "film.mkv", size: int = 1_500_000) -> str:
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, name)
    with open(path, "wb") as fh:
        fh.write(os.urandom(size))
    return path


def make_torrent(content: str, out: str, piece: int = 16384) -> str:
    """Fichier .torrent de content (dossier ou fichier), sans tracker (les tests relient eux-mêmes)."""
    fs = lt.file_storage()
    lt.add_files(fs, content)
    t = lt.create_torrent(fs, piece)
    t.set_creator("tests de Télémaque")
    lt.set_piece_hashes(t, os.path.dirname(content))
    with open(out, "wb") as fh:
        fh.write(lt.bencode(t.generate()))
    return out


def make_evil_torrent(out: str) -> str:
    """Torrent dont les fichiers veulent sortir du dossier de destination."""
    import hashlib
    piece = 16384
    payload = b"x" * 10
    info = {
        b"name": b"piege",
        b"piece length": piece,
        b"files": [
            {b"length": 5, b"path": [b"..", b"..", b"evade.txt"]},
            {b"length": 5, b"path": [b"/etc", b"absolu.txt"]},
        ],
        b"pieces": hashlib.sha1(payload).digest(),
    }
    with open(out, "wb") as fh:
        fh.write(lt.bencode({b"info": info}))
    return out
