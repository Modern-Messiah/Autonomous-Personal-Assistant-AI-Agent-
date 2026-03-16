#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
SERVICE_NAME="krisha-agent-compose.service"
TEMPLATE_PATH="${SCRIPT_DIR}/${SERVICE_NAME}.template"
TARGET_PATH="${SYSTEMD_USER_DIR}/${SERVICE_NAME}"

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl is required but not installed" >&2
  exit 1
fi

if ! command -v podman-compose >/dev/null 2>&1; then
  echo "podman-compose is required but not installed" >&2
  exit 1
fi

mkdir -p "${SYSTEMD_USER_DIR}"
sed "s|__PROJECT_ROOT__|${PROJECT_ROOT}|g" "${TEMPLATE_PATH}" > "${TARGET_PATH}"

systemctl --user daemon-reload
systemctl --user enable "${SERVICE_NAME}"

cat <<EOF
Installed ${SERVICE_NAME} to ${TARGET_PATH}

Next steps:
  systemctl --user start ${SERVICE_NAME}
  systemctl --user status ${SERVICE_NAME}
EOF
