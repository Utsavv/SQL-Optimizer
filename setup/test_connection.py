"""Verify SQL Server connectivity using SQL_CONNECTION_STRING from .env."""
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    print("Warning: python-dotenv not installed. Run: pip install python-dotenv")

conn_str = os.environ.get("SQL_CONNECTION_STRING")
if not conn_str:
    print("ERROR: SQL_CONNECTION_STRING is not set. Check your .env file.", file=sys.stderr)
    sys.exit(1)

try:
    import pyodbc
except ImportError:
    print("ERROR: pyodbc is not installed. Run: pip install pyodbc", file=sys.stderr)
    sys.exit(1)

print(f"Connecting to: {conn_str.split(';')[1] if ';' in conn_str else '(see .env)'}")
try:
    conn = pyodbc.connect(conn_str, timeout=10)
    cursor = conn.cursor()
    cursor.execute("SELECT @@VERSION AS version, DB_NAME() AS db;")
    row = cursor.fetchone()
    print(f"Connected successfully!")
    print(f"  Server: {row.version.splitlines()[0]}")
    print(f"  Database: {row.db}")
    conn.close()
except pyodbc.Error as e:
    print(f"Connection failed: {e}", file=sys.stderr)
    sys.exit(1)
