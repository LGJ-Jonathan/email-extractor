#!/bin/sh
# One image, three roles. docker-compose passes its own commands; Railway sets ROLE.
set -e
case "${ROLE:-api}" in
  api)
    # "::" so Railway's private network (IPv6) reaches it; on Linux it also takes IPv4.
    exec uvicorn app.main:app --host "${HOST:-::}" --port "${PORT:-8000}" --proxy-headers
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
