#!/usr/bin/env bash
# VideoNode installer for Raspberry Pi OS Lite (Bookworm) on Raspberry Pi 3 / 4.
#
#   bash install.sh               full install
#   bash install.sh --no-samba    skip the Windows network share
#   bash install.sh --no-usb      skip USB drive auto-mounting
#   bash install.sh --no-kiosk    keep the login prompt/cursor on the TV
#
# Safe to re-run: use it to update after a `git pull`. Your videonode.conf is
# never overwritten.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WITH_SAMBA=1 WITH_USB=1 WITH_KIOSK=1

usage() { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; }
for arg in "$@"; do
    case "$arg" in
        --no-samba) WITH_SAMBA=0 ;;
        --no-usb)   WITH_USB=0 ;;
        --no-kiosk) WITH_KIOSK=0 ;;
        -h|--help)  usage; exit 0 ;;
        *) echo "Unknown option: $arg"; usage; exit 1 ;;
    esac
done

info() { printf '\n\033[1;33m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;31m!!  %s\033[0m\n' "$*"; }

# --------------------------------------------------------------- checks
if [[ $EUID -eq 0 ]]; then
    echo "Run this as your normal user (e.g. pi), not with sudo: bash install.sh"
    exit 1
fi
RUN_USER="$(id -un)"
RUN_HOME="$HOME"
INSTALL_DIR="$RUN_HOME/videonode"
VIDEO_DIR="$RUN_HOME/videos"

MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"
case "$MODEL" in
    *"Pi 4"*|*"Compute Module 4"*) PI=4 ;;   # includes Pi 400
    *"Pi 3"*|*"Zero 2"*)                       PI=3 ;;
    *) PI=other ;;
esac

. /etc/os-release
info "VideoNode installer"
note "Board:  $MODEL"
note "OS:     $PRETTY_NAME"
note "User:   $RUN_USER  (install to $INSTALL_DIR, media in $VIDEO_DIR)"
[[ $PI == other ]] && warn "Only tested on Raspberry Pi 3 and 4. Continuing anyway."
[[ ${VERSION_CODENAME:-} != bookworm ]] && warn "Only tested on Bookworm. Continuing anyway."

sudo -v  # ask for the password once, up front

# --------------------------------------------------------------- packages
info "Installing packages"
PKGS=(mpv ffmpeg python3-pygame python3-evdev)
(( WITH_SAMBA )) && PKGS+=(samba wsdd)
(( WITH_USB ))   && PKGS+=(ntfs-3g)
sudo apt-get update -qq
sudo apt-get install -y "${PKGS[@]}"

# --------------------------------------------------------------- app files
info "Installing VideoNode to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR" "$VIDEO_DIR"/{long,short,images}
if [[ "$SRC_DIR" != "$INSTALL_DIR" ]]; then
    install -m 755 "$SRC_DIR/videonode.py" "$INSTALL_DIR/videonode.py"
fi

CONF="$INSTALL_DIR/videonode.conf"
# The Pi 3's GPU needs cheaper scaling to play 1080p smoothly.
PI3_PLAYER='mpv --hwdec=v4l2m2m-copy --vo=gpu --gpu-context=drm --fullscreen --no-osc --osd-level=0 --input-terminal=no --msg-level=all=error --scale=bilinear --dscale=bilinear --cscale=bilinear --dither=no --correct-downscaling=no --linear-downscaling=no --sigmoid-upscaling=no --deband=no --framedrop=vo'
make_conf() {
    sed -e "s|/home/pi|$RUN_HOME|g" "$SRC_DIR/videonode.conf.example" > "$1"
    if [[ $PI == 3 ]]; then
        sed -i "s|^player_cmd = .*|player_cmd = $PI3_PLAYER|" "$1"
    fi
}
if [[ -f "$CONF" ]]; then
    make_conf "$CONF.new"
    note "Kept your existing videonode.conf."
    note "The latest defaults are in videonode.conf.new, if you want to compare."
else
    make_conf "$CONF"
    note "Created videonode.conf$([[ $PI == 3 ]] && echo ' (with Pi 3 playback tuning)')"
fi

# Lets you run it by hand from SSH for testing (the service sets these itself)
sudo usermod -aG video,render,input,audio "$RUN_USER"

# --------------------------------------------------------------- service
info "Installing systemd service"
sed -e "s|@USER@|$RUN_USER|g" -e "s|@HOME@|$RUN_HOME|g" \
    "$SRC_DIR/systemd/videonode.service" | sudo tee /etc/systemd/system/videonode.service > /dev/null

# Allow the menu's "Shut Down" option (and nothing else) without a password
SUDOERS=/etc/sudoers.d/videonode
echo "$RUN_USER ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff" > /tmp/videonode.sudoers
if sudo visudo -cf /tmp/videonode.sudoers > /dev/null; then
    sudo install -m 440 /tmp/videonode.sudoers "$SUDOERS"
else
    warn "Could not install sudoers rule; the menu's Shut Down option may not work"
fi
rm -f /tmp/videonode.sudoers

# --------------------------------------------------------------- USB
if (( WITH_USB )); then
    info "Setting up USB drive auto-mount (read-only at /media/usb)"
    sed -e "s|@USER@|$RUN_USER|g" "$SRC_DIR/scripts/videonode-usb-mount" \
        | sudo tee /usr/local/bin/videonode-usb-mount > /dev/null
    sudo chmod 755 /usr/local/bin/videonode-usb-mount
    sudo install -m 644 "$SRC_DIR/systemd/videonode-usb@.service" /etc/systemd/system/
    sudo install -m 644 "$SRC_DIR/udev/99-videonode-usb.rules" /etc/udev/rules.d/
    sudo mkdir -p /media/usb
    sudo udevadm control --reload-rules
fi

# --------------------------------------------------------------- Samba
if (( WITH_SAMBA )); then
    info "Setting up Windows network share"
    SMB=/etc/samba/smb.conf
    if sudo grep -q '^# BEGIN videonode' "$SMB"; then
        note "Share already configured by a previous install."
    elif sudo grep -qE '^\[(videos|videonode-config)\]' "$SMB"; then
        note "Found existing [videos]/[videonode-config] shares in $SMB; leaving them alone."
    else
        sudo tee -a "$SMB" > /dev/null << EOF

# BEGIN videonode (managed by install.sh)
[videos]
   comment = VideoNode media
   path = $VIDEO_DIR
   browseable = yes
   read only = no
   valid users = $RUN_USER
   force user = $RUN_USER
   create mask = 0644
   directory mask = 0755

[videonode-config]
   comment = VideoNode settings
   path = $INSTALL_DIR
   browseable = yes
   read only = no
   valid users = $RUN_USER
   force user = $RUN_USER
   create mask = 0644
# END videonode
EOF
    fi
    if ! sudo pdbedit -L 2>/dev/null | grep -q "^$RUN_USER:"; then
        note "Choose a password for the network share (user: $RUN_USER):"
        sudo smbpasswd -a "$RUN_USER"
    fi
    sudo systemctl enable --now smbd wsdd > /dev/null 2>&1 || true
    sudo systemctl restart smbd
fi

# --------------------------------------------------------------- kiosk tweaks
REBOOT_NEEDED=0
if (( WITH_KIOSK )); then
    info "Hiding the console on the TV"
    # The login prompt on tty1 would receive keystrokes meant for the menu.
    sudo systemctl disable getty@tty1 > /dev/null 2>&1 || true
    note "Disabled the TV login prompt (log in over SSH instead)."

    CMDLINE=/boot/firmware/cmdline.txt
    [[ -f $CMDLINE ]] || CMDLINE=/boot/cmdline.txt
    if [[ -f $CMDLINE ]] && ! grep -q 'vt.global_cursor_default=0' "$CMDLINE"; then
        sudo cp "$CMDLINE" "$CMDLINE.videonode-backup"
        sudo sed -i '1 s/$/ vt.global_cursor_default=0/' "$CMDLINE"
        note "Hid the blinking console cursor (backup: $CMDLINE.videonode-backup)."
        REBOOT_NEEDED=1
    fi
fi

# --------------------------------------------------------------- start
info "Enabling VideoNode"
sudo systemctl daemon-reload
sudo systemctl enable videonode > /dev/null 2>&1

HOST="$(hostname)"
info "Done!"
note "Put media in $VIDEO_DIR/long, short and images"
(( WITH_SAMBA )) && note "  or from Windows:  \\\\$HOST.local\\videos"
(( WITH_USB ))   && note "  or on a USB drive in a top-level 'videos' folder"
note ""
note "Settings:  $CONF"
note "Logs:      journalctl -u videonode -f"
echo
if [[ -t 0 ]]; then
    read -rp "Reboot now to start VideoNode? [Y/n] " ans
    if [[ ! $ans =~ ^[Nn] ]]; then
        sudo reboot
    fi
fi
if (( REBOOT_NEEDED )); then
    note "Reboot when you're ready:  sudo reboot"
else
    sudo systemctl restart videonode
    note "VideoNode (re)started."
fi
