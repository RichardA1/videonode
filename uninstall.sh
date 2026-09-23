#!/usr/bin/env bash
# Removes the VideoNode service and system changes made by install.sh.
# Your media (~/videos) and settings (~/videonode) are left in place.
set -euo pipefail

if [[ $EUID -eq 0 ]]; then
    echo "Run this as your normal user, not with sudo: bash uninstall.sh"
    exit 1
fi
info() { printf '\n\033[1;33m==> %s\033[0m\n' "$*"; }

info "Stopping VideoNode"
sudo systemctl disable --now videonode 2>/dev/null || true
sudo rm -f /etc/systemd/system/videonode.service /etc/sudoers.d/videonode

info "Removing USB auto-mount"
sudo rm -f /etc/udev/rules.d/99-videonode-usb.rules \
           /etc/systemd/system/videonode-usb@.service \
           /usr/local/bin/videonode-usb-mount
sudo udevadm control --reload-rules

info "Removing network share"
if [[ -f /etc/samba/smb.conf ]] && sudo grep -q '^# BEGIN videonode' /etc/samba/smb.conf; then
    sudo sed -i '/^# BEGIN videonode/,/^# END videonode/d' /etc/samba/smb.conf
    sudo systemctl restart smbd 2>/dev/null || true
fi

info "Restoring the TV console"
sudo systemctl enable getty@tty1 2>/dev/null || true
for f in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
    if [[ -f $f ]]; then
        sudo sed -i 's/ vt.global_cursor_default=0//' "$f"
    fi
done

sudo systemctl daemon-reload
info "Done. Media and settings were kept in ~/videos and ~/videonode."
echo "    Reboot to restore the console:  sudo reboot"
