#!/bin/sh
# Monte les partages du NAS horus au démarrage de WSL.
#
# Pourquoi /mnt/wsl ? Docker Desktop tourne dans une distro WSL séparée qui ne
# voit le système de fichiers d'Ubuntu QUE via /mnt/wsl (propagation "shared",
# partagée entre distros). Un montage CIFS placé sous /mnt/horus a une
# propagation "private" : il n'apparaît donc jamais dans les conteneurs (le bind
# du devcontainer ne voit qu'un dossier vide).
#
# Solution : monter le CIFS réel sous /mnt/wsl/horus/* (propagation héritée du
# parent partagé -> visible par docker-desktop), puis faire un bind-miroir vers
# /mnt/horus/* pour les scripts lancés localement (NAS_MOUNT=/mnt/horus).
#
# /mnt/wsl est un tmpfs recréé à chaque démarrage : on (re)crée donc les points
# de montage à chaque exécution. Script idempotent, relançable à la main.
#
# Référencé depuis /etc/wsl.conf :
#   [boot]
#   command = /home/gege/projects/HorusScripts/scripts/mount-nas.sh
set -eu

HOST=//192.168.1.182
CRED=/home/gege/.nas-credentials
OPTS="credentials=$CRED,uid=1000,gid=1000,iocharset=utf8,nofail"
SHARES="photos movies tvshows cartoons"

for share in $SHARES; do
    wsl_dir=/mnt/wsl/horus/$share   # montage CIFS réel (visible par Docker)
    host_dir=/mnt/horus/$share      # miroir bind pour l'usage local

    mkdir -p "$wsl_dir" "$host_dir"

    # Montage CIFS sous /mnt/wsl : propagation partagée vers la distro Docker.
    if ! mountpoint -q "$wsl_dir"; then
        mount -t cifs "$HOST/$share" "$wsl_dir" -o "$OPTS"
    fi
    mount --make-shared "$wsl_dir"

    # Miroir bind vers /mnt/horus pour les exécutions hors conteneur.
    if ! mountpoint -q "$host_dir"; then
        mount --bind "$wsl_dir" "$host_dir"
    fi
done
