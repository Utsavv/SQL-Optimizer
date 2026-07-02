#!/usr/bin/env bash
# Verify local SQL Server container accepts sa login and serves WideWorldImporters.
set -euo pipefail

SA_PASSWORD='@ATISecure1'
CONTAINER='sqlserver'

if ! docker info &>/dev/null; then
  echo "ERROR: Docker is not running." >&2
  exit 1
fi

if ! docker ps --filter "name=^${CONTAINER}$" --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
  echo "ERROR: Container '${CONTAINER}' is not running." >&2
  exit 1
fi

if docker exec "${CONTAINER}" test -f /opt/mssql-tools18/bin/sqlcmd 2>/dev/null; then
  SQLCMD="/opt/mssql-tools18/bin/sqlcmd"
else
  SQLCMD="/opt/mssql-tools/bin/sqlcmd"
fi

auth_out=$(docker exec "${CONTAINER}" "${SQLCMD}" \
  -S localhost -U sa -P "${SA_PASSWORD}" -C -Q "SELECT 1 AS ok;" -h -1 2>&1)
echo "${auth_out}" | grep -q '^[[:space:]]*1[[:space:]]*$' || {
  echo "ERROR: sa authentication failed:" >&2
  echo "${auth_out}" >&2
  exit 1
}

db_out=$(docker exec "${CONTAINER}" "${SQLCMD}" \
  -S localhost -U sa -P "${SA_PASSWORD}" -C -d WideWorldImporters \
  -Q "SELECT DB_NAME() AS db;" -h -1 2>&1)
echo "${db_out}" | grep -q 'WideWorldImporters' || {
  echo "ERROR: WideWorldImporters not accessible:" >&2
  echo "${db_out}" >&2
  exit 1
}

echo "OK: sa/@ATISecure1 can query WideWorldImporters on localhost:1433"