#!/usr/bin/env bash
# PR #10 acceptance test runner
# Run from: /Users/utsavverma/git/SQL-Optimizer/
# Usage: bash run_pr10_test.sh 2>&1 | tee /tmp/pr10_test.log
set -e

REPO="/Users/utsavverma/git/SQL-Optimizer"
VENV="$REPO/venv/bin/python3"
BRANCH="claude/sp-parameter-identification-d4a35j"
LOG="/tmp/pr10_test.log"

cd "$REPO"

echo "============================================================"
echo "STEP 1: ODBC driver check"
echo "============================================================"
odbcinst -q -d 2>/dev/null || true
$VENV -c "import pyodbc; print('pyodbc', pyodbc.version); print('drivers:', pyodbc.drivers())"

echo ""
echo "============================================================"
echo "STEP 2: Checkout PR branch"
echo "============================================================"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git log --oneline -3

echo ""
echo "============================================================"
echo "STEP 3: Install Python deps"
echo "============================================================"
$VENV -m pip install -r "$REPO/requirements.txt" -q
$VENV -m pip install python-dotenv -q

echo ""
echo "============================================================"
echo "STEP 3b: Verify DB connection"
echo "============================================================"
$VENV -c "
import os; from dotenv import load_dotenv; load_dotenv('$REPO/.env')
import pyodbc
conn = pyodbc.connect(os.environ['SQL_CONNECTION_STRING'], autocommit=True)
cur = conn.cursor()
cur.execute('SELECT DB_NAME(), @@VERSION')
row = cur.fetchone()
print('Connected to:', row[0])
print('Version:', row[1][:80])
"

echo ""
echo "============================================================"
echo "STEP 4: Create test stored procedure"
echo "============================================================"
$VENV -c "
import os; from dotenv import load_dotenv; load_dotenv('$REPO/.env')
import pyodbc
conn = pyodbc.connect(os.environ['SQL_CONNECTION_STRING'], autocommit=True)
cur = conn.cursor()
try:
    cur.execute('SELECT COUNT(*) FROM Sales.Orders')
    count = cur.fetchone()[0]
    print(f'Sales.Orders row count: {count}')
    has_wwi = True
except Exception as e:
    has_wwi = False
    print('Sales.Orders not found:', e)

if has_wwi:
    sql = '''
    CREATE OR ALTER PROCEDURE dbo.zz_OptTest_GetOrders
        @FromDate        date,
        @ToDate          date,
        @CustomerID      int = NULL,
        @SalespersonID   int = NULL,
        @IsUndersupplied bit = NULL
    AS
    BEGIN
        SET NOCOUNT ON;
        SELECT o.OrderID, o.OrderDate, o.CustomerID, o.SalespersonPersonID
        FROM Sales.Orders o
        WHERE o.OrderDate >= @FromDate
          AND o.OrderDate <= @ToDate
          AND (@CustomerID      IS NULL OR o.CustomerID          = @CustomerID)
          AND (@SalespersonID   IS NULL OR o.SalespersonPersonID = @SalespersonID)
          AND (@IsUndersupplied IS NULL OR o.IsUndersupplied     = @IsUndersupplied);
    END
    '''
    cur.execute(sql)
    print('Created dbo.zz_OptTest_GetOrders successfully')
"

echo ""
echo "============================================================"
echo "STEP 5: Run discovery test"
echo "============================================================"
cd "$REPO/sp-optimizer"
$VENV scratch_discovery_test.py

echo ""
echo "============================================================"
echo "STEP 6: Capture-only optimize run"
echo "============================================================"
echo '[]' > /tmp/nochange.json
$VENV -m scripts.optimize \
  --proc "dbo.zz_OptTest_GetOrders" \
  --actual --max-combos 20 \
  --backend file --decisions /tmp/nochange.json 2>&1

echo ""
echo "============================================================"
echo "STEP 7: Ollama check"
echo "============================================================"
if ollama list 2>/dev/null | grep -q gemma; then
  echo "Ollama running, proceeding with LLM run..."
  $VENV -m scripts.optimize \
    --proc "dbo.zz_OptTest_GetOrders" \
    --actual --max-combos 20 \
    --backend litellm --model ollama_chat/gemma4 --max-iterations 3 2>&1 | tee /tmp/ollama_run.log
else
  echo "Ollama not running or gemma model not found — skipping LLM step"
  ollama list 2>/dev/null || echo "ollama not in PATH"
fi

echo ""
echo "============================================================"
echo "STEP 8: Cleanup"
echo "============================================================"
cd "$REPO"
$VENV -c "
import os; from dotenv import load_dotenv; load_dotenv('$REPO/.env')
import pyodbc
conn = pyodbc.connect(os.environ['SQL_CONNECTION_STRING'], autocommit=True)
cur = conn.cursor()
cur.execute('DROP PROCEDURE IF EXISTS dbo.zz_OptTest_GetOrders')
cur.execute(\"\"\"
SELECT 'DROP PROCEDURE ' + QUOTENAME(SCHEMA_NAME(schema_id)) + '.' + QUOTENAME(name)
FROM sys.procedures WHERE name LIKE 'zz_OptTest_GetOrders_opt_v%'
\"\"\")
for row in cur.fetchall():
    print('Dropping:', row[0])
    cur.execute(row[0])
print('Cleanup done')
"
rm -f "$REPO/sp-optimizer/scratch_discovery_test.py"

echo ""
echo "============================================================"
echo "TEST RUN COMPLETE — log at /tmp/pr10_test.log"
echo "============================================================"
