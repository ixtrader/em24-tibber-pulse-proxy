#!/usr/bin/env sh

set -eu

cd "$(dirname "$0")"
BUILD_VERSION="$(git rev-parse --short HEAD 2>/dev/null || printf 'dev')"
export BUILD_VERSION

exec docker compose "$@"