"""
PR #10 acceptance test: sp-parameter-identification
Run from: /Users/utsavverma/git/SQL-Optimizer/sp-optimizer/
Uses the project venv: ../venv/bin/python3
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
import pyodbc
from scripts import discover

conn = pyodbc.connect(os.environ["SQL_CONNECTION_STRING"], autocommit=True)
cur = conn.cursor()
proc = "dbo.zz_OptTest_GetOrders"

print("=== PARAMETER SIGNATURES ===")
params = discover.get_signature(cur, proc)
for p in params:
    has_def = getattr(p, 'has_default', '<ATTR MISSING>')
    print(f"  {p.name:22s} {p.sql_type:14s} has_default={has_def}")

print("\n=== DISCOVERY COMBOS ===")
_, combos = discover.discover(cur, proc, max_combos=20)
print(f"{len(combos)} combos total:")
for c in combos:
    print(f"  [{c.weight:.2f}] {c.label:38s} -> {c.values}")

# Spot-check: pick first real CustomerID value from a non-NULL combo and verify it exists
print("\n=== SPOT CHECK ===")
for c in combos:
    if c.values.get('@CustomerID') is not None and c.values['@CustomerID'] not in (0, 1, 1000):
        cid = c.values['@CustomerID']
        cur.execute(f"SELECT COUNT(*) FROM Sales.Orders WHERE CustomerID = {cid}")
        cnt = cur.fetchone()[0]
        print(f"CustomerID={cid} -> {cnt} rows in Sales.Orders (should be > 0)")
        break
else:
    print("No non-NULL CustomerID found in combos to spot-check")

print("\n=== ACCEPTANCE CRITERIA CHECK ===")
optional_params = ['@CustomerID', '@SalespersonID', '@IsUndersupplied']
param_map = {p.name: p for p in params}

# AC1: optional params show has_default=True
ac1_pass = True
for pname in optional_params:
    p = param_map.get(pname)
    if p is None:
        print(f"  AC1 FAIL: {pname} not found in signature")
        ac1_pass = False
    elif not hasattr(p, 'has_default'):
        print(f"  AC1 FAIL: {pname} ProcParam missing has_default attribute")
        ac1_pass = False
    elif not p.has_default:
        print(f"  AC1 FAIL: {pname} has_default=False (expected True)")
        ac1_pass = False
    else:
        print(f"  AC1 OK  : {pname} has_default=True")
if ac1_pass:
    print("  AC1 OVERALL: PASS")

# AC3: NULL combos for individual optional params
null_labels = [c.label for c in combos if 'NULL' in c.label]
print(f"\n  AC3 NULL combo labels: {null_labels}")
individual_null_found = any('optional NULL' in l for l in null_labels)
multi_null_found = any(l.count('NULL') > 1 or 'all optional' in l.lower() for l in null_labels)
print(f"  AC3 individual optional NULL: {'PASS' if individual_null_found else 'FAIL'}")
print(f"  AC3 multi-param NULL combo  : {'PASS' if multi_null_found else 'FAIL (none found)'}")

# AC4: date range combos
date_labels = [c.label for c in combos]
date_keywords = ['narrow', 'medium', 'wide', 'empty', 'date']
date_found = [kw for kw in date_keywords if any(kw in l.lower() for l in date_labels)]
print(f"\n  AC4 date range keywords found: {date_found}")
print(f"  AC4: {'PASS' if len(date_found) >= 2 else 'PARTIAL/FAIL'}")
