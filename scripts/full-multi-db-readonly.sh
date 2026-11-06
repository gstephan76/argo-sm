#!/usr/bin/env bash
# Validate/restrict only the existing four MariaDB application accounts.
set -Eeuo pipefail
exec python3 "$(dirname "$0")/full-multi-db-readonly.py" "$@"
