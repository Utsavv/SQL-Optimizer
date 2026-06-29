#!/usr/bin/env bash
# Deploy WorldWideImporters (OLTP) to an existing Azure SQL Server using sqlpackage.
# Targets: utsavsqlserver.database.windows.net / WorldWideImport
# Usage:
#   bash deploy_wwi_existing.sh
set -euo pipefail

# ─── Configuration ────────────────────────────────────────────────────────────
SQL_SERVER="utsavsqlserver.database.windows.net"
SQL_DB="WorldWideImport"
SQL_ADMIN="utsav"
SQL_ADMIN_PASSWORD="${SQL_ADMIN_PASSWORD:-@ATISecure1}"
BACPAC_URL="https://github.com/microsoft/sql-server-samples/releases/download/wide-world-importers-v1.0/WideWorldImporters-Standard.bacpac"
BACPAC_FILE="${TMPDIR:-/tmp}/WideWorldImporters-Standard.bacpac"

echo "──────────────────────────────────────────────────────────"
echo "  SQL-Optimizer: WorldWideImporters Import (direct)"
echo "──────────────────────────────────────────────────────────"
echo "  SQL Server:  $SQL_SERVER"
echo "  Database:    $SQL_DB"
echo "  Admin Login: $SQL_ADMIN"
echo "──────────────────────────────────────────────────────────"
echo ""

# ─── 1. Preflight: sqlpackage ─────────────────────────────────────────────────
echo "[1/3] Checking for sqlpackage..."

if ! command -v dotnet &>/dev/null; then
  echo "  dotnet not found. Installing via Homebrew..."
  brew install dotnet
fi

# Ensure DOTNET_ROOT points to the actual runtime (Homebrew installs to non-default path)
if [[ -z "${DOTNET_ROOT:-}" ]]; then
  for candidate in \
    /opt/homebrew/opt/dotnet/libexec \
    /usr/local/share/dotnet \
    "$HOME/.dotnet"; do
    if [[ -d "$candidate/shared" ]]; then
      export DOTNET_ROOT="$candidate"
      break
    fi
  done
fi
echo "  DOTNET_ROOT: ${DOTNET_ROOT:-<unset>}"

export PATH="$PATH:$HOME/.dotnet/tools"

if ! command -v sqlpackage &>/dev/null; then
  echo "  sqlpackage not found. Installing via dotnet tool..."
  dotnet tool install -g microsoft.sqlpackage
fi

SQLPACKAGE_BIN="$(command -v sqlpackage)"
echo "  Using sqlpackage: $SQLPACKAGE_BIN"

# ─── 2. Download BACPAC ───────────────────────────────────────────────────────
if [[ -f "$BACPAC_FILE" ]]; then
  echo "[2/3] BACPAC already at $BACPAC_FILE, skipping download."
else
  echo "[2/3] Downloading WideWorldImporters-Standard.bacpac (~100 MB)..."
  curl -L --progress-bar -o "$BACPAC_FILE" "$BACPAC_URL"
fi

# ─── 3. Import BACPAC directly into Azure SQL ─────────────────────────────────
echo "[3/3] Importing BACPAC into '${SQL_SERVER}/${SQL_DB}'..."
echo "      This typically takes 5-20 minutes..."

"$SQLPACKAGE_BIN" \
  /Action:Import \
  /SourceFile:"$BACPAC_FILE" \
  /TargetServerName:"$SQL_SERVER" \
  /TargetDatabaseName:"$SQL_DB" \
  /TargetUser:"$SQL_ADMIN" \
  /TargetPassword:"$SQL_ADMIN_PASSWORD" \
  /TargetEncryptConnection:True \
  /TargetTrustServerCertificate:False \
  /p:CommandTimeout=1200

# ─── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "Done! WorldWideImporters is live at ${SQL_SERVER}/${SQL_DB}"
echo ""
echo "pyodbc connection string:"
echo "  DRIVER={ODBC Driver 18 for SQL Server};SERVER=${SQL_SERVER};DATABASE=${SQL_DB};UID=${SQL_ADMIN};PWD=${SQL_ADMIN_PASSWORD};Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
echo ""
echo "Example optimizer run:"
echo "  python optimize.py \\"
echo "    --proc Sales.usp_InsertCustomerOrders \\"
echo "    --backend claude \\"
echo "    --conn \"DRIVER={ODBC Driver 18 for SQL Server};SERVER=${SQL_SERVER};DATABASE=${SQL_DB};UID=${SQL_ADMIN};PWD=${SQL_ADMIN_PASSWORD};Encrypt=yes;\""
