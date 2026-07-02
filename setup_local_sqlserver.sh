#!/usr/bin/env bash
# Repo-root entry point for local SQL Server 2022 Docker setup.
exec "$(cd "$(dirname "$0")" && pwd)/setup/setup_local_sqlserver.sh" "$@"