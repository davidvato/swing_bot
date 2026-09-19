import sqlite3
import pprint

conn = sqlite3.connect('trades.db')
cursor = conn.cursor()
cursor.execute("SELECT * FROM trades")
pprint.pprint(cursor.fetchall())
conn.close()
