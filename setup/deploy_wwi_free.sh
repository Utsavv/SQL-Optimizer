#!/usr/bin/env bash
# Deploy WorldWideImporters (OLTP) to Azure SQL Database free tier.
# Usage:
#   export SQL_ADMIN_PASSWORD='YourStr0ngP@ssword!'
#   bash deploy_wwi_free.sh
set -euo pipefail

# ─── Configuration ────────────────────────────────────────────────────────────
AZURE_REGION="${AZURE_REGION:-eastus}"
RESOURCE_GROUP="${RESOURCE_GROUP:-wwi-free-rg}"
SQL_SERVER="${SQL_SERVER:-}"          # auto-generated if empty
SQL_DB="${SQL_DB:-WideWorldImporters}"
SQL_ADMIN="${SQL_ADMIN:-sqladmin}"
STORAGE_ACCOUNT="${STORAGE_ACCOUNT:-}"  # auto-generated if empty
BACPAC_URL="https://github.com/microsoft/sql-server-samples/releases/download/wide-world-importers-v1.0/WideWorldImporters-Standard.bacpac"
BACPAC_FILE="${TMPDIR:-/tmp}/WideWorldImporters-Standard.bacpac"

# ─── Preflight: password ──────────────────────────────────────────────────────
if [[ -z "${SQL_ADMIN_PASSWORD:-}" ]]; then
  echo "ERROR: SQL_ADMIN_PASSWORD is required." >&2
  echo "  export SQL_ADMIN_PASSWORD='YourStr0ngP@ssword!'" >&2
  exit 1
fi

# ─── Preflight: Azure CLI ─────────────────────────────────────────────────────
if ! command -v az &>/dev/null; then
  echo "Azure CLI not found. Installing via Homebrew..."
  if ! command -v brew &>/dev/null; then
    echo "ERROR: Homebrew is required to install Azure CLI." >&2
    echo "  Install Homebrew: https://brew.sh" >&2
    exit 1
  fi
  brew install azure-cli
fi

# ─── Preflight: Azure login ───────────────────────────────────────────────────
if ! az account show &>/dev/null 2>&1; then
  echo "Not logged in. Running az login..."
  az login
fi

# ─── Generate unique names ────────────────────────────────────────────────────
TIMESTAMP=$(date +%s)
if [[ -z "$SQL_SERVER" ]]; then
  SQL_SERVER="wwi-free-${TIMESTAMP}"
fi
if [[ -z "$STORAGE_ACCOUNT" ]]; then
  # 3-24 chars, lowercase letters and numbers only
  STORAGE_ACCOUNT="wwibacpac${TIMESTAMP: -8}"
fi

echo "──────────────────────────────────────────────────────────"
echo "  SQL-Optimizer: WorldWideImporters on Azure SQL Free Tier"
echo "──────────────────────────────────────────────────────────"
echo "  Region:         $AZURE_REGION"
echo "  Resource Group: $RESOURCE_GROUP"
echo "  SQL Server:     ${SQL_SERVER}.database.windows.net"
echo "  Database:       $SQL_DB"
echo "  Admin Login:    $SQL_ADMIN"
echo "──────────────────────────────────────────────────────────"
echo ""

# ─── 1. Resource group ────────────────────────────────────────────────────────
echo "[1/8] Creating resource group '$RESOURCE_GROUP'..."
az group create \
  --name "$RESOURCE_GROUP" \
  --location "$AZURE_REGION" \
  --output none

# ─── 2. SQL logical server ────────────────────────────────────────────────────
echo "[2/8] Creating SQL logical server '$SQL_SERVER'..."
az sql server create \
  --name "$SQL_SERVER" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$AZURE_REGION" \
  --admin-user "$SQL_ADMIN" \
  --admin-password "$SQL_ADMIN_PASSWORD" \
  --output none

# ─── 3. Firewall rules ────────────────────────────────────────────────────────
echo "[3/8] Configuring firewall rules..."
# Allow Azure services (required for the BACPAC import operation)
az sql server firewall-rule create \
  --server "$SQL_SERVER" \
  --resource-group "$RESOURCE_GROUP" \
  --name "AllowAzureServices" \
  --start-ip-address 0.0.0.0 \
  --end-ip-address 0.0.0.0 \
  --output none

# Allow current machine IP
CURRENT_IP=$(curl -sf https://api.ipify.org || curl -sf https://icanhazip.com || echo "")
if [[ -n "$CURRENT_IP" ]]; then
  az sql server firewall-rule create \
    --server "$SQL_SERVER" \
    --resource-group "$RESOURCE_GROUP" \
    --name "LocalMachine" \
    --start-ip-address "$CURRENT_IP" \
    --end-ip-address "$CURRENT_IP" \
    --output none
  echo "  Allowed current IP: $CURRENT_IP"
fi

# ─── 4. Free-tier database ────────────────────────────────────────────────────
echo "[4/8] Creating free-tier database '$SQL_DB'..."
az sql db create \
  --server "$SQL_SERVER" \
  --resource-group "$RESOURCE_GROUP" \
  --name "$SQL_DB" \
  --edition GeneralPurpose \
  --family Gen5 \
  --capacity 1 \
  --compute-model Serverless \
  --auto-pause-delay 60 \
  --use-free-limit \
  --free-limit-exhaustion-behavior AutoPause \
  --output none

# ─── 5. Download BACPAC ───────────────────────────────────────────────────────
if [[ -f "$BACPAC_FILE" ]]; then
  echo "[5/8] BACPAC already at $BACPAC_FILE, skipping download."
else
  echo "[5/8] Downloading WideWorldImporters-Standard.bacpac..."
  curl -L --progress-bar -o "$BACPAC_FILE" "$BACPAC_URL"
fi

# ─── 6. Temp storage account for BACPAC upload ───────────────────────────────
echo "[6/8] Creating temp storage account '$STORAGE_ACCOUNT'..."
az storage account create \
  --name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$AZURE_REGION" \
  --sku Standard_LRS \
  --allow-blob-public-access false \
  --output none

STORAGE_KEY=$(az storage account keys list \
  --account-name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --query "[0].value" --output tsv)

az storage container create \
  --name "bacpac" \
  --account-name "$STORAGE_ACCOUNT" \
  --account-key "$STORAGE_KEY" \
  --output none

echo "  Uploading BACPAC (~100 MB)..."
az storage blob upload \
  --container-name "bacpac" \
  --name "WideWorldImporters-Standard.bacpac" \
  --file "$BACPAC_FILE" \
  --account-name "$STORAGE_ACCOUNT" \
  --account-key "$STORAGE_KEY" \
  --overwrite \
  --output none

BACPAC_URI="https://${STORAGE_ACCOUNT}.blob.core.windows.net/bacpac/WideWorldImporters-Standard.bacpac"

# ─── 7. Import BACPAC (blocks until complete, typically 5-15 min) ─────────────
echo "[7/8] Importing BACPAC into Azure SQL (this takes 5-15 minutes)..."
az sql db import \
  --server "$SQL_SERVER" \
  --resource-group "$RESOURCE_GROUP" \
  --name "$SQL_DB" \
  --storage-uri "$BACPAC_URI" \
  --storage-key "$STORAGE_KEY" \
  --storage-key-type StorageAccessKey \
  --admin-user "$SQL_ADMIN" \
  --admin-password "$SQL_ADMIN_PASSWORD" \
  --output none

# ─── 8. Cleanup temp storage ──────────────────────────────────────────────────
echo "[8/8] Deleting temp storage account..."
az storage account delete \
  --name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --yes \
  --output none

# ─── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "Done! WorldWideImporters is live on Azure SQL."
echo ""
echo "pyodbc connection string for --conn argument:"
echo "  DRIVER={ODBC Driver 18 for SQL Server};SERVER=${SQL_SERVER}.database.windows.net;DATABASE=${SQL_DB};UID=${SQL_ADMIN};PWD=\${SQL_ADMIN_PASSWORD};Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
echo ""
echo "Example optimizer run:"
echo "  python optimize.py \\"
echo "    --proc Sales.usp_InsertCustomerOrders \\"
echo "    --backend claude \\"
echo "    --conn \"DRIVER={ODBC Driver 18 for SQL Server};SERVER=${SQL_SERVER}.database.windows.net;DATABASE=${SQL_DB};UID=${SQL_ADMIN};PWD=\${SQL_ADMIN_PASSWORD};Encrypt=yes;\""
