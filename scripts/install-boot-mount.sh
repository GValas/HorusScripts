#!/bin/sh
# Installe le montage automatique du NAS horus au démarrage de WSL.
# À lancer UNE fois en root :  sudo scripts/install-boot-mount.sh
#
# Ce que ça fait, de façon idempotente :
#   1. Sauvegarde /etc/wsl.conf et /etc/fstab (*.bak, sans écraser une sauvegarde existante).
#   2. Configure /etc/wsl.conf pour appeler scripts/mount-nas.sh au boot.
#   3. Neutralise les lignes CIFS de /etc/fstab (elles montaient en propagation
#      "private" sous /mnt/horus -> invisibles dans les conteneurs Docker).
#   4. Lance scripts/mount-nas.sh tout de suite (pas besoin de redémarrer pour tester).
#
# Voir scripts/mount-nas.sh pour le détail du pourquoi /mnt/wsl.
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Ce script doit être lancé en root : sudo $0" >&2
    exit 1
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
MOUNT_SCRIPT="$SCRIPT_DIR/mount-nas.sh"

if [ ! -x "$MOUNT_SCRIPT" ]; then
    echo "Introuvable ou non exécutable : $MOUNT_SCRIPT" >&2
    exit 1
fi

# 1. Sauvegardes (une seule fois)
[ -f /etc/wsl.conf.bak ] || { [ -f /etc/wsl.conf ] && cp /etc/wsl.conf /etc/wsl.conf.bak; }
[ -f /etc/fstab.bak ]    || cp /etc/fstab /etc/fstab.bak

# 2. /etc/wsl.conf : boot -> script de montage
cat > /etc/wsl.conf <<EOF
[boot]
systemd=true
command = $MOUNT_SCRIPT
EOF

# 3. Commenter les lignes CIFS de /etc/fstab (le script gère le montage)
sed -i -E '/[[:space:]]cifs[[:space:]]/ s/^([^#])/# [remplacé par scripts\/mount-nas.sh] \1/' /etc/fstab

# 4. Appliquer maintenant
"$MOUNT_SCRIPT"

echo
echo "OK. /etc/wsl.conf et /etc/fstab mis à jour, partages montés :"
findmnt -o TARGET,SOURCE,PROPAGATION | grep -E 'wsl/horus|/mnt/horus/' || true
echo
echo "Pour valider le déclenchement au boot : 'wsl --shutdown' (PowerShell) puis relance Ubuntu."
