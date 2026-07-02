#!/usr/bin/env bash
# Deploy WorldWideImporters (OLTP) to an existing Azure SQL Server using sqlpackage.
#
# DB details are read from SQL_CONNECTION_STRING in the repo-root .env file.
# .env takes priority over any pre-set environment variables; if a value isn't
# present in .env, these fall back to the environment, then to a hardcoded
# default:
#   export SQL_SERVER='your-server.database.windows.net'
#   export SQL_DB='WideWorldImporters'
#   export SQL_ADMIN='sqladmin'
#   export SQL_ADMIN_PASSWORD='YourStr0ngP@ssword!'
#   bash deploy_wwi_existing.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-${SCRIPT_DIR}/../.env}"

# ─── Load .env (repo root); .env values override already-exported env vars ────
if [[ -f "$ENV_FILE" ]]; then
  while IFS='=' read -r key value; do
    [[ -z "$key" || "$key" == \#* ]] && continue
    value="${value%\"}"
    value="${value#\"}"
    export "$key=$value"
  done < <(grep -v '^[[:space:]]*#' "$ENV_FILE" | grep '=')
  echo "Loaded DB defaults from $ENV_FILE (takes priority over environment)"
fi

# ─── Pull a field (SERVER/DATABASE/UID/PWD) out of a pyodbc-style connection
# string. Always returns 0 so a missing field never trips `set -e`. ───────────
conn_field() {
  local conn="${1:-}" field_lower part k v
  field_lower="$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')"
  local IFS=';'
  local -a parts
  read -ra parts <<< "$conn" || true
  for part in "${parts[@]}"; do
    [[ "$part" != *=* ]] && continue
    k="${part%%=*}"
    v="${part#*=}"
    if [[ "$(printf '%s' "$k" | tr '[:upper:]' '[:lower:]')" == "$field_lower" ]]; then
      printf '%s' "$v"
      return 0
    fi
  done
  return 0
}

# ─── Configuration: .env's SQL_CONNECTION_STRING takes priority; falls back to
# the environment, then to a hardcoded default ─────────────────────────────────
SQL_SERVER_FROM_ENV="${SQL_SERVER:-}"
SQL_DB_FROM_ENV="${SQL_DB:-}"
SQL_ADMIN_FROM_ENV="${SQL_ADMIN:-}"
SQL_ADMIN_PASSWORD_FROM_ENV="${SQL_ADMIN_PASSWORD:-}"

SQL_SERVER="$(conn_field "${SQL_CONNECTION_STRING:-}" SERVER)"
SQL_SERVER="${SQL_SERVER:-$SQL_SERVER_FROM_ENV}"
SQL_DB="$(conn_field "${SQL_CONNECTION_STRING:-}" DATABASE)"
SQL_DB="${SQL_DB:-$SQL_DB_FROM_ENV}"
SQL_DB="${SQL_DB:-WideWorldImporters}"
SQL_ADMIN="$(conn_field "${SQL_CONNECTION_STRING:-}" UID)"
SQL_ADMIN="${SQL_ADMIN:-$SQL_ADMIN_FROM_ENV}"
SQL_ADMIN="${SQL_ADMIN:-sqladmin}"
SQL_ADMIN_PASSWORD="$(conn_field "${SQL_CONNECTION_STRING:-}" PWD)"
SQL_ADMIN_PASSWORD="${SQL_ADMIN_PASSWORD:-$SQL_ADMIN_PASSWORD_FROM_ENV}"
BACPAC_URL="https://github.com/microsoft/sql-server-samples/releases/download/wide-world-importers-v1.0/WideWorldImporters-Standard.bacpac"
BACPAC_FILE="${TMPDIR:-/tmp}/WideWorldImporters-Standard.bacpac"

# ─── Preflight: required, never-defaulted credentials ─────────────────────────
# The password must never be baked into a committed script; it must come from
# .env's SQL_CONNECTION_STRING or the environment (same contract as
# deploy_wwi_free.sh).
if [[ -z "$SQL_SERVER" ]]; then
  echo "ERROR: SQL_SERVER is required (e.g. your-server.database.windows.net)." >&2
  echo "  export SQL_SERVER='your-server.database.windows.net'" >&2
  exit 1
fi
if [[ -z "${SQL_ADMIN_PASSWORD:-}" ]]; then
  echo "ERROR: SQL_ADMIN_PASSWORD is required." >&2
  echo "  export SQL_ADMIN_PASSWORD='YourStr0ngP@ssword!'" >&2
  exit 1
fi

echo "──────────────────────────────────────────────────────────"
echo "  SQL-Optimizer: WorldWideImporters Import (direct)"
echo "──────────────────────────────────────────────────────────"
echo "  SQL Server:  $SQL_SERVER"
echo "  Database:    $SQL_DB"
echo "  Admin Login: $SQL_ADMIN"
echo "──────────────────────────────────────────────────────────"
echo ""

# ─── 1. Preflight: sqlpackage, sqlcmd ─────────────────────────────────────────
echo "[1/4] Checking for sqlpackage..."

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

if ! command -v sqlcmd &>/dev/null; then
  echo "  sqlcmd not found. Installing via Homebrew (microsoft/mssql-release tap)..."
  brew tap microsoft/mssql-release 2>/dev/null || true
  ACCEPT_EULA=Y brew install msodbcsql18 mssql-tools18
fi

SQLCMD_BIN="$(command -v sqlcmd)"
echo "  Using sqlcmd: $SQLCMD_BIN"

# ─── 2. Drop the target database if it already exists ────────────────────────
# sqlpackage's /Action:Import requires the target database not to already
# exist, so a stale/prior copy must be dropped first (connecting to master,
# since you can't drop the database you're connected to).
echo "[2/4] Checking whether database '$SQL_DB' already exists on $SQL_SERVER..."
SQ="'"
SQL_DB_LITERAL="${SQL_DB//$SQ/$SQ$SQ}"
SQL_DB_IDENT="${SQL_DB//]/]]}"
"$SQLCMD_BIN" \
  -S "$SQL_SERVER" \
  -d master \
  -U "$SQL_ADMIN" \
  -P "$SQL_ADMIN_PASSWORD" \
  -N -l 30 -b \
  -Q "IF DB_ID(N'${SQL_DB_LITERAL}') IS NOT NULL BEGIN PRINT 'Dropping existing database [${SQL_DB_IDENT}]...'; DROP DATABASE [${SQL_DB_IDENT}]; END ELSE PRINT 'Database [${SQL_DB_IDENT}] does not exist yet, nothing to drop.';"

# ─── 3. Download BACPAC ───────────────────────────────────────────────────────
if [[ -f "$BACPAC_FILE" ]]; then
  echo "[3/4] BACPAC already at $BACPAC_FILE, skipping download."
else
  echo "[3/4] Downloading WideWorldImporters-Standard.bacpac (~100 MB)..."
  curl -L --progress-bar -o "$BACPAC_FILE" "$BACPAC_URL"
fi

# ─── 4. Import BACPAC directly into Azure SQL ─────────────────────────────────
echo "[4/4] Importing BACPAC into '${SQL_SERVER}/${SQL_DB}'..."
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
