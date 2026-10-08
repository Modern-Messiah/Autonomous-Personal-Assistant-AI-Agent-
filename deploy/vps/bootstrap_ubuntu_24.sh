#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script with sudo on Ubuntu 24." >&2
  exit 1
fi

TARGET_USER="${SUDO_USER:-}"

if [[ -z "${TARGET_USER}" ]]; then
  echo "SUDO_USER is not set. Run via sudo for the target deploy user." >&2
  exit 1
fi

# One compose tooling everywhere: the CD pipeline drives `docker compose` on
# this server, so the bootstrap installs the same stack — Docker Engine with
# the compose plugin, configured rootless for the deploy user — instead of a
# parallel podman-compose install.
apt-get update
apt-get install -y ca-certificates curl ufw uidmap dbus-user-session fuse-overlayfs
curl -fsSL https://get.docker.com | sh
apt-get install -y docker-ce-rootless-extras

ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw --force enable
loginctl enable-linger "${TARGET_USER}"

# Rootless docker daemon for the deploy user (no root daemon, no socket access).
runuser -l "${TARGET_USER}" -c \
  'XDG_RUNTIME_DIR=/run/user/'"$(id -u "${TARGET_USER}")"' dockerd-rootless-setuptool.sh install'
runuser -l "${TARGET_USER}" -c \
  'XDG_RUNTIME_DIR=/run/user/'"$(id -u "${TARGET_USER}")"' systemctl --user daemon-reload'

cat <<BANNER
Bootstrap complete for ${TARGET_USER} (rootless docker + compose plugin).

Next steps as ${TARGET_USER}:
  1. Clone or update the repository.
  2. Fill in .env with production secrets.
  3. Run ./deploy/systemd/install_user_service.sh
  4. Start the stack with: systemctl --user start krisha-agent-compose.service
BANNER
