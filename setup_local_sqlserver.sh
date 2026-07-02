#!/usr/bin/env bash
# Wrapper so the setup script is runnable from the repo root.
exec "$(cd "$(dirname "$0")" && pwd)/setup/setup_local_sqlserver.sh" "$@"