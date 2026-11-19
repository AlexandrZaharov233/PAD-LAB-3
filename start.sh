#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
docker info >/dev/null
if [ ! -f .env ]; then
  docker compose build auth-service
  docker run --rm --user "$(id -u):$(id -g)" --mount "type=bind,source=$(pwd),target=/project" --workdir /project --entrypoint python parkslot/auth-service:lab3 scripts/configure.py
fi
docker compose up -d --build --wait --wait-timeout 180
echo 'ParkSlot Compose: http://127.0.0.1:8081. Credentials: ADMIN_EMAIL / ADMIN_PASSWORD in .env.'
