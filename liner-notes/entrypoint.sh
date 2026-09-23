#!/bin/sh
# Start as root just long enough to make the data folder writable, then drop to PUID:PGID.
set -e
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "$DATA_DIR"
if [ "$(id -u)" = "0" ]; then
  chown -R "$PUID:$PGID" "$DATA_DIR"
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi
exec "$@"
