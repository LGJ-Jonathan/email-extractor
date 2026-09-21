#!/bin/sh
# One image, three roles. docker-compose passes its own commands; Railway sets ROLE.
set -e
case "${ROLE:-api}" in
  api)
    # One dual-stack socket: IPv4 for health checks, IPv6 for Railway's private network.
    exec python -m app.serve
    ;;
  worker)
    exec python -m app.worker
    ;;
  migrate)
    exec alembic upgrade head
    ;;
  *)
    echo "unknown ROLE: ${ROLE}" >&2
    exit 64
    ;;
esac
