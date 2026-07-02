#!/usr/bin/env bash
# Local SQL Server 2022 setup via Docker (Apple Silicon + Intel compatible)
# Usage: bash setup_local_sqlserver.sh
set -euo pipefail

SA_PASSWORD='@ATISecure1'

echo "======================================================"
echo "  SQL Server 2022 Local Setup"
echo "======================================================"

# ── 1. Check Docker is running ────────────────────────────
if ! docker info &>/dev/null; then
  echo "ERROR: Docker is not running. Please start Docker Desktop and re-run."
  exit 1
fi
echo "[1/5] Docker is running ✓"

# ── 2. Start SQL Server container ─────────────────────────
mkdir -p ~/sql-backups

if docker ps -a --format '{{.Names}}' | grep -q '^sqlserver$'; then
  echo "[2/5] Container 'sqlserver' already exists — starting if stopped..."
  docker start sqlserver 2>/dev/null || true
else
  echo "[2/5] Creating SQL Server 2022 container..."
  docker run --platform linux/amd64 \
    -e "ACCEPT_EULA=Y" \
    -e "MSSQL_SA_PASSWORD=${SA_PASSWORD}" \
    -p 1433:1433 \
    -v sqldata:/var/opt/mssql \
    -v ~/sql-backups:/backups \
    --name sqlserver \
    --restart unless-stopped \
    -d mcr.microsoft.com/mssql/server:2022-latest
fi

# Detect sqlcmd path early (used for readiness + restore)
if docker exec sqlserver test -f /opt/mssql-tools18/bin/sqlcmd 2>/dev/null; then
  SQLCMD="/opt/mssql-tools18/bin/sqlcmd"
else
  SQLCMD="/opt/mssql-tools/bin/sqlcmd"
fi

# ── 3. Wait for SQL Server to be ready ────────────────────
echo "[3/5] Waiting for SQL Server to be ready..."
for i in $(seq 1 60); do
  if docker exec sqlserver "$SQLCMD" \
      -S localhost -U sa -P "${SA_PASSWORD}" -C -Q "SELECT 1" &>/dev/null; then
    echo "      SQL Server is ready ✓"
    break
  fi
  echo "      Waiting... ($i/60)"
  sleep 3
  if [ $i -eq 60 ]; then
    echo "WARNING: Timed out waiting. Check logs: docker logs -f sqlserver"
  fi
done

# ── 4. Download WideWorldImporters backup ─────────────────
BAK="$HOME/sql-backups/WideWorldImporters-Full.bak"
if [ -f "$BAK" ]; then
  echo "[4/5] WideWorldImporters-Full.bak already present — skipping download."
else
  echo "[4/5] Downloading WideWorldImporters backup (~200 MB)..."
  curl -L --progress-bar \
    -o "$BAK" \
    "https://github.com/Microsoft/sql-server-samples/releases/download/wide-world-importers-v1.0/WideWorldImporters-Full.bak"
  echo "      Download complete ✓"
fi

# ── 5. Restore database ───────────────────────────────────
echo "[5/5] Restoring WideWorldImporters..."

# Check if already restored
ALREADY=$(docker exec sqlserver "$SQLCMD" \
  -S localhost -U sa -P "${SA_PASSWORD}" -C -h -1 \
  -Q "SET NOCOUNT ON; SELECT COUNT(*) FROM sys.databases WHERE name='WideWorldImporters'" \
  2>/dev/null | tr -d '[:space:]' || echo "0")

if [ "$ALREADY" = "1" ]; then
  echo "      WideWorldImporters already exists — skipping restore."
else
  docker exec sqlserver "$SQLCMD" \
    -S localhost -U sa -P "${SA_PASSWORD}" -C \
    -Q "RESTORE DATABASE WideWorldImporters \
        FROM DISK = '/backups/WideWorldImporters-Full.bak' \
        WITH MOVE 'WWI_Primary'          TO '/var/opt/mssql/data/WideWorldImporters.mdf', \
             MOVE 'WWI_UserData'         TO '/var/opt/mssql/data/WideWorldImporters_UserData.ndf', \
             MOVE 'WWI_Log'              TO '/var/opt/mssql/data/WideWorldImporters.ldf', \
             MOVE 'WWI_InMemory_Data_1'  TO '/var/opt/mssql/data/WideWorldImporters_InMemory_Data_1'"
  echo "      Restore complete ✓"
fi

# ── Done ──────────────────────────────────────────────────
echo ""
echo "======================================================"
echo "  SQL Server is ready!"
echo "  Host:     localhost,1433"
echo "  Login:    sa"
echo "  Password: ${SA_PASSWORD}"
echo "  Database: WideWorldImporters"
echo ""
echo "  pyodbc connection string:"
echo "  DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost,1433;DATABASE=WideWorldImporters;UID=sa;PWD=${SA_PASSWORD};Encrypt=yes;TrustServerCertificate=yes;"
echo "======================================================"
