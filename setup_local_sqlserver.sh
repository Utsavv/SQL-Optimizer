#!/usr/bin/env bash
# Repo-root entry point: delegates to setup/setup_local_sqlserver.sh.
exec "$(cd "$(dirname "$0")" && pwd)/setup/setup_local_sqlserver.sh" "$@"
