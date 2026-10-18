#!/bin/sh
# Install cloudsync for the current user (Linux): command, menu entry, timers
# and the GNOME Shell search extension. Run again after updating.
set -e
cd "$(dirname "$0")"
DATA=${XDG_DATA_HOME:-$HOME/.local/share}
CONF=${XDG_CONFIG_HOME:-$HOME/.config}
UUID=cloudsync-search@htlemke.github.io

mkdir -p "$HOME/.local/bin" "$DATA/applications" "$DATA/gnome-shell/extensions" "$CONF/systemd/user"
chmod +x cloudsync.py
ln -sf "$PWD/cloudsync.py" "$HOME/.local/bin/cloudsync"
sed "s|^Exec=cloudsync|Exec=$HOME/.local/bin/cloudsync|" cloudsync.desktop > "$DATA/applications/cloudsync.desktop"
cp systemd/*.service systemd/*.timer "$CONF/systemd/user/"
rm -rf "$DATA/gnome-shell/extensions/$UUID"
cp -r "search/$UUID" "$DATA/gnome-shell/extensions/"
systemctl --user daemon-reload

cat <<MSG
Installed. Next:
  cloudsync init --machine NAME          # once per computer
  cloudsync gui                          # pick folders, then
  systemctl --user enable --now cloudsync.timer cloudsync-index.timer cloudsync-watch.service
  gnome-extensions enable $UUID   # after logging out and in once (Wayland)
MSG
