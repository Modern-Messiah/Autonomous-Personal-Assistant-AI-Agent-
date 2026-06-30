#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="${1:-podman-compose.yml}"
TIMEOUT_SECONDS="${DATASTORE_WAIT_TIMEOUT_SECONDS:-120}"
deadline=$((SECONDS + TIMEOUT_SECONDS))

until podman-compose -f "${COMPOSE_FILE}" exec -T postgres \
  pg_isready -U "${DB__USER}" -d "${DB__NAME}" >/dev/null 2>&1 &&
  podman-compose -f "${COMPOSE_FILE}" exec -T redis sh -c \
  'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" ping' >/dev/null 2>&1; do
  if (( SECONDS >= deadline )); then
    echo "Datastores did not become ready within ${TIMEOUT_SECONDS}s" >&2
    exit 1
  fi
  sleep 1
done
