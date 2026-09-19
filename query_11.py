import sqlite3
import pandas as pd

conn = sqlite3.connect('trades.db')
df = pd.read_sql_query("SELECT * FROM trades WHERE date LIKE '%2026-09-11%' OR id >= 10", conn)
print(df.to_string())
conn.close()
