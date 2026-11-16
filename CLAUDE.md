# Télémaque — notes de travail

Client BitTorrent GTK4 / libadwaita en Python sur libtorrent (python3-libtorrent 2.0, le moteur de
qBittorrent et Deluge), dans la famille d'Odysseus, Nausicaa et Calypso (même ossature : i18n, réglages,
fonctionnalités, vrais dialogues, tests). Idée de Tof : qBittorrent et Varia sont en Flatpak (ou Qt) ;
ici du torrent de base, en .deb, au look GNOME. Nom : le fils d'Ulysse, et « télé-charger ».

## Règles

- Répondre à Tof en français, tutoiement, avis francs. Messages de commit en français.
- Cible : Zorin OS 18.1 (Ubuntu 24.04, GNOME 46, X11, GTK 4.14, libadwaita 1.5, python3-libtorrent
  2.0.10, Python 3.12). Pas d'API plus récente (pas d'Adw.Dialog, Adw.AlertDialog…). Paquets apt seulement.
- PÉRIMÈTRE (choix de Tof) : du torrent de base, rien d'autre — pas de yt-dlp, pas de HTTP, pas de RSS, pas
  de moteur de recherche, pas de planificateur. Proposer un ajout seulement s'il sert ce périmètre.
- Thème GTK du système : `data/base.css` ne fait que de la mise en page (couleurs du thème : @success_color,
  @error_color).
- Dialogues : de vraies fenêtres (`common.AlertDialog`, `common.DialogWindow`, `Adw.PreferencesWindow`,
  `Adw.AboutWindow`).
- Chaque fonctionnalité facultative est dans `telemaque/features.py` (tray, delete_torrent, notify, inhibit) et se consulte au
  moment de l'usage.
- Tout texte visible passe par `tr()` / `ntr()`. Après modification : `python3.12 tools/update-pot.py`,
  compléter `data/locale/en.po` et `de.po`, puis `msgfmt -c --statistics` et `msgcmp`.
- Toute fonction passée à `idle_add` / `timeout_add` renvoie False (sauf répétition voulue).
- Travail directement sur main (accord de Tof).

## Architecture

- `backend/engine.py` (sans GTK) : `Engine` = une `lt.session` + ses téléchargements.
  - Identité = empreinte v1 (`_tid_of` : `info_hashes().v1`). PIÈGE : libtorrent 2 fabrique des torrents
    hybrides v1 + v2 ; `info_hash()` d'un torrent_info et l'empreinte d'un lien magnet ne concordaient pas.
  - Rien en « auto_managed » : pas de file de libtorrent, une pause reste une pause.
  - `poll()` (une fois par seconde, par l'application) : alertes (données de reprise, fin, métadonnées,
    erreurs), règle de partage, données de reprise toutes les 60 s pour ceux qui ont changé. Renvoie des
    `Transfer` (instantanés : état metadata / checking / downloading / seeding / paused / done / error,
    progression sur les fichiers VOULUS, vitesses, temps restant, ratio).
  - Vitesses : moyennes glissantes de libtorrent → mises à 0 pour un téléchargement complet ou en pause.
  - Partage (`seed_policy`) : ratio (envoyé ÷ voulu ≥ `seed_ratio`), always, never ; arrêt = pause +
    `seed_stopped`. Relancé à la main après coup → `seed_forever`.
  - Reprise après redémarrage : `<id>.resume` (`write_resume_data_buf`, avec le dictionnaire d'infos :
    un lien magnet n'a pas à retrouver ses métadonnées) + `state.json` (ordre d'ajout, fin signalée, partage
    arrêté). `shutdown()` attend les données de reprise (5 s au plus). Fichiers abîmés : ignorés (`errors`).
  - Fichiers : `files()`, `set_wanted` (priorité 0 / 4). Recoché après la fin : la fin est signalée de
    nouveau. Coché ou repris : `_find_sources` (force_reannounce + DHT) — un torrent « fini » laisse partir
    les sources complètes (deux complets n'ont rien à s'échanger).
  - Noms piégés (« ../ », absolus) : libtorrent les ramène dans le dossier (`piege/evade.txt`), testé.
  - Limites de vitesse générales : NE S'APPLIQUENT PAS au réseau local (classe de pairs locale de
    libtorrent, voulu). Tests et captures : `handle.set_download_limit` (limite propre au téléchargement).
  - `local_only` (tests) : 127.0.0.1, ni DHT / LSD / UPnP, `min_reconnect_time` 1, TCP seulement — en uTP
    sur la boucle locale, une source quittée n'était jamais recontactée.
- `app.py` : `TelemaqueApp` garde le moteur et la boucle (`_poll`, protégée : une exception ne doit pas
  figer la liste pour de bon), une seule fenêtre (`show_window`), arrière-plan (`hide_to_background` :
  `hold()`, fenêtre cachée, toujours à jour), notifications (Gio.Notification ; actions DÉTAILLÉES
  « app.show-transfer::<id> » — PyGObject n'a pas `set_default_action_and_target_value`), inhibition de la
  veille pendant un téléchargement, HANDLES_OPEN (fichiers .torrent, URI magnet:), `--version` répondu
  avant GTK. Moteur injectable (`TelemaqueApp(engine=…)`) pour les tests.
- Icône de la barre du haut (`telemaque/tray.py`, fonctionnalité `tray`, demande de Tof ; REPRISE D'ITHACA,
  `backend/tray.py`) : StatusNotifierItem + com.canonical.dbusmenu écrits à la main (libayatana-appindicator
  est en GTK 3), enregistrés auprès de org.kde.StatusNotifierWatcher (extension AppIndicator de Zorin /
  Ubuntu ; sans elle, rien ne s'affiche, rien ne casse). Montrée en arrière-plan (`hide_to_background`),
  retirée quand la fenêtre revient ou qu'on quitte. Menu : état (« 2 téléchargements en cours — ↓ … »),
  Afficher Télémaque, Tout mettre en pause / Tout reprendre (`Engine.pause_all` / `resume_all` : marque
  `paused_all`, « reprendre » relance ceux-là et les téléchargements en pause, pas les partages arrêtés par
  la règle), Quitter ; clic sur l'icône = fenêtre. Republié seulement si le menu change (relecture chaque
  seconde). Pièges d'Ithaca gardés : `hicolor/index.theme` dans data/icons (sinon « ••• »), `menu_label`
  (l'extension avale le premier « _ »). Testé : `test_tray.py` (fausse barre, sous dbus-run-session).
- Supprimer le fichier .torrent après l'ajout (fonctionnalité `delete_torrent`, COUPÉE par défaut, demande de
  Tof) : `add_torrent_file(delete_source=True)` → noté dans `state.json`, fichier effacé seulement une fois
  les données de reprise ÉCRITES et contenant le torrent (`_delete_source`) : un plantage juste après l'ajout
  ne perd rien. Jamais un lien, jamais dans le dossier d'état. Testé : `ExtrasTest`, ui_smoke.
- Barre latérale (idée de Tof) : Adw.OverlaySplitView, vues Tous / En cours (pas complets, pause comprise) /
  Terminés (complets) avec leur nombre (`FILTERS`, `matches`), filtre du Gtk.ListBox relancé à chaque relecture
  (un téléchargement fini change de vue), page vide propre à la vue (`none`), titre du contenu = la vue,
  F9 / bouton = masquer (`sidebar_visible`, retenu seulement hors repli), vue retenue (`filter`). Fenêtre
  < 560 sp : barre repliée par-dessus la liste (Adw.Breakpoint), refermée après un choix.
- Ligne d'un téléchargement (demande de Tof) : à gauche l'état (`status_text`, sans ratio), à droite sous la
  barre « N sources · N pairs · ratio » (`peers_text` : sources = seeds reliés, pairs = les autres ; ratio
  seul en pause / terminé, rien que les sources pendant la recherche des infos). En anglais : seeds / peers.
- Barre d'état en bas (demande de Tof) : IP publique (`external_ip_alert` : celle du VPN s'il y en a un),
  locale (`listen_succeeded_alert`), port + « joignable » (compteur `net.has_incoming_connections`), DHT
  désactivé / connexion… / actif avec N nœuds (compteur `dht.dht_nodes`) ; compteurs par
  `post_session_stats` à chaque relecture (`session.status()` est dépréciée). `Engine.network()` → `NetInfo`.
- `ui/window.py` : liste (Gtk.ListBox boxed-list dans un Adw.Clamp, `TransferRow` : nom, barre, état,
  pause / reprise, menu Ouvrir / Afficher dans le dossier / Fichiers… / Supprimer…), accueil
  (Adw.StatusPage), Ctrl+O / Ctrl+V / glisser, question de fermeture (« Continuer en arrière-plan ? », case
  « Ne plus demander » → réglage `on_close`), `status_text`.
- `ui/dialogs.py` : `AddDialog` (dossier, « Ajouter en pause », fichiers à cocher ; lien magnet : pas
  encore de fichiers), `MagnetEntryDialog` (prérempli depuis le presse-papiers), `FilesDialog` (cocher en
  cours de route, avancement par fichier). `ui/preferences.py` : Général (dossier, fermeture, partage,
  vitesse, apparence), Réseau (port, DHT), Fonctionnalités.

## Tests

```sh
python3.12 -m unittest discover -s tests            # moteur : vrais échanges entre deux moteurs locaux
dbus-run-session -- python3.12 -m unittest discover -s tests -p "test_tray.py"   # icône de la barre
xvfb-run -a -s "-screen 0 1280x900x24" python3.12 tests/ui_smoke.py   # doit finir par « Aucune erreur. »
LANGUAGE=en … ; LANGUAGE=de …
python3 -m pyflakes telemaque tests tools
```

Une fois (sur une dizaine de passages), 5 échecs du moteur non reproduits ensuite : à surveiller (ports ?).
`tests/samples.py` : contenus aléatoires, `make_torrent` (sans tracker), `make_evil_torrent` (bencode à la
main). ui_smoke : un moteur « partageur » dans le même processus, reliés par `connect_peer` ; notifications
interceptées (`app.send_notification`). PIÈGE de test : juste après `set_wanted`, l'état de libtorrent est
en retard (« complete » encore vrai) — attendre la taille des fichiers.
Captures : `LANGUAGE=fr xvfb-run -a -s "-screen 0 1400x1000x24" python3.12 tools/screenshot.py SORTIE.png
[--dark] [--empty] [--add] [--files] [--prefs=PAGE] [--close]` (faux contenus partagés en local, états
mis en scène). README : `docs/captures/` en français.

## Publication (comme Calypso)

Paquet, commande et module : `telemaque` (nom libre : Ubuntu toutes versions et Debian vérifiés avant la
1.0.0). PPA `ppa:nourpon/telemaque` (créé par Tof) : `tools/ppa.sh` chez Tof (noble puis resolute, garde-fou
« à » en fin de ligne du changelog, puis le site par `tools/site.sh`, dossier Telemaque des pages perso Free).
Nouvelle version : version dans `telemaque/__init__.py`, `debian/telemaque.1`, `<release>` de la fiche
AppStream, entrée de `debian/changelog` (« X~noble1 », date RÉELLE — `date -R`), commit « Version X » sur main :
`.github/workflows/release-deb.yml` publie le .deb dans les Releases (seulement pour un commit « Version… »).
Site : `site/index.html` (anglais, tons verts ; Odysseus bleu, Ithaca violet, Nausicaa orange, Calypso
turquoise), captures de `site/img/` en anglais et en sombre sauf light.png (`LANGUAGE=en … tools/screenshot.py
site/img/dark.png --dark`, add `--dark --add`, files `--dark --files`, light) ; version dans la pastille et
l'accroche, à tenir à jour. Paquet vérifié ici : dpkg-buildpackage, lintian (aucune remarque), appstreamcli.
Dans le bac à sable, /usr/bin/python3 n'a pas `gi` : vérifier le paquet installé avec python3.12.

## Feuille de route

1. FAIT et publié en 1.0.0 : moteur, liste, ajout (.torrent / magnet), fichiers, pause / reprise /
   suppression, partage au ratio, limites, notifications, arrière-plan + icône, barre latérale, barre d'état,
   sources / pairs, suppression du .torrent, reprise au redémarrage, traductions, paquet, PPA, site.
