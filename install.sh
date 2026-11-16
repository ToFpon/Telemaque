#!/bin/sh
# Installation de Télémaque (client BitTorrent) pour l'utilisateur courant (~/.local), sans droits root.
#   ./install.sh             installe
#   ./install.sh --uninstall désinstalle (la liste des téléchargements et les réglages restent)
set -e

PREFIX="${PREFIX:-$HOME/.local}"
SHARE="$PREFIX/share"
APPDIR="$PREFIX/lib/telemaque"        # le programme ; les données de l'utilisateur restent dans $SHARE/telemaque
SRC="$(cd "$(dirname "$0")" && pwd)"
APP_ID="io.github.telemaque.Telemaque"
DESKTOP="$APP_ID.desktop"

if [ "$1" = "--uninstall" ]; then
    rm -rf "$APPDIR" "$PREFIX/bin/telemaque" "$SHARE/applications/$DESKTOP" \
        "$SHARE/icons/hicolor/scalable/apps/$APP_ID.svg" \
        "$SHARE/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg"
    update-desktop-database "$SHARE/applications" 2>/dev/null || true
    if [ -f "$SHARE/icons/hicolor/icon-theme.cache" ]; then
        gtk-update-icon-cache -f -t "$SHARE/icons/hicolor" 2>/dev/null || rm -f "$SHARE/icons/hicolor/icon-theme.cache"
    fi
    echo "Télémaque a été désinstallé."
    exit 0
fi

missing=""
python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Adw; assert Adw.MINOR_VERSION >= 5" 2>/dev/null \
    || missing="$missing python3-gi gir1.2-gtk-4.0 gir1.2-adw-1"
python3 -c "import libtorrent; assert libtorrent.__version__ >= '2.0'" 2>/dev/null || missing="$missing python3-libtorrent"
if [ -n "$missing" ]; then
    echo "Dépendances manquantes. Debian/Ubuntu/Zorin : sudo apt install$missing"
    exit 1
fi

mkdir -p "$APPDIR" "$PREFIX/bin" "$SHARE/applications" "$SHARE/icons/hicolor/scalable/apps" \
    "$SHARE/icons/hicolor/symbolic/apps"
rm -rf "$APPDIR/telemaque" "$APPDIR/data"
cp -r "$SRC/telemaque" "$SRC/data" "$APPDIR/"
find "$APPDIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
cat > "$PREFIX/bin/telemaque" <<LAUNCH
#!/bin/sh
# -P : pas de dossier courant dans les chemins d'import (un dossier supprimé faisait planter le
# démarrage, et un « gi.py » posé dans le dossier courant aurait été exécuté)
exec python3 -P -c 'import sys; sys.path.insert(0, "$APPDIR"); from telemaque.app import main; sys.exit(main(sys.argv))' "\$@"
LAUNCH
chmod +x "$PREFIX/bin/telemaque"
sed "s|^Exec=telemaque|Exec=$PREFIX/bin/telemaque|" "$SRC/data/$DESKTOP" > "$SHARE/applications/$DESKTOP"
cp "$SRC/data/icons/hicolor/scalable/apps/$APP_ID.svg" "$SHARE/icons/hicolor/scalable/apps/"
cp "$SRC/data/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg" "$SHARE/icons/hicolor/symbolic/apps/"
update-desktop-database "$SHARE/applications" 2>/dev/null || true
gtk-update-icon-cache -f -t "$SHARE/icons/hicolor" 2>/dev/null || true

echo "Télémaque est installé. Lancez-le depuis le menu des applications ou avec : telemaque"
echo "Pour qu'il reçoive les liens magnet et les fichiers .torrent :"
echo "  xdg-mime default $DESKTOP x-scheme-handler/magnet application/x-bittorrent"
case ":$PATH:" in *":$PREFIX/bin:"*) ;; *) echo "Astuce : ajoutez $PREFIX/bin à votre PATH." ;; esac
