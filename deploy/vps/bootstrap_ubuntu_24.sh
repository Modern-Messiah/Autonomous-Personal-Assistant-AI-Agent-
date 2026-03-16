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

apt-get update
apt-get install -y podman podman-compose uidmap slirp4netns fuse-overlayfs
loginctl enable-linger "${TARGET_USER}"

runuser -l "${TARGET_USER}" -c 'systemctl --user daemon-reload'

cat <<EOF
Bootstrap complete for ${TARGET_USER}.

Next steps as ${TARGET_USER}:
  1. Clone or update the repository.
  2. Fill in .env with production secrets.
  3. Run ./deploy/systemd/install_user_service.sh
  4. Start the stack with: systemctl --user start krisha-agent-compose.service
EOF
