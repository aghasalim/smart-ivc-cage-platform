#!/usr/bin/env bash
set -euo pipefail

# Convenience script — boot the full stack with Docker Compose.

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  cp .env.example .env
  echo "wrote .env from .env.example"
fi

docker compose up --build "$@"
