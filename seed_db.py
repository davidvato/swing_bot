"""
seed_db.py — Exporta trades.db local a seed_trades.sql para deploy en Render.
Ejecutar localmente cuando quieras sincronizar historial con produccion:
    python seed_db.py
"""
import sqlite3
import os

DB_PATH = "trades.db"
SEED_FILE = "seed_trades.sql"

def export_seed():
    if not os.path.exists(DB_PATH):
        print(f"No se encontro {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    lines = []

    # Export trades table
    lines.append("-- seed_trades.sql: generado automaticamente por seed_db.py")
    lines.append("-- Ejecutar solo si la DB esta vacia (lo hace dashboard_app.py al arrancar)")
    lines.append("")
    
    for row in conn.execute("SELECT * FROM trades ORDER BY id ASC"):
        id_, date, ticker, trade_type, notional, entry_price, exit_price, qty, pnl, pnl_pct, kelly_pct = row
        def sql_val(v):
            if v is None:
                return "NULL"
            if isinstance(v, str):
                return f"'{v.replace(chr(39), chr(39)+chr(39))}'"
            return str(v)
        vals = ", ".join([sql_val(v) for v in row])
        lines.append(
            f"INSERT OR IGNORE INTO trades (id, date, ticker, trade_type, notional, "
            f"entry_price, exit_price, qty, pnl, pnl_pct, kelly_pct) VALUES ({vals});"
        )

    # Export crypto_trades table
    lines.append("")
    for row in conn.execute("SELECT * FROM crypto_trades ORDER BY id ASC"):
        id_, date, ticker, trade_type, notional, entry_price, exit_price, qty, pnl, pnl_pct, kelly_pct, atr_at_entry = row
        def sql_val(v):
            if v is None:
                return "NULL"
            if isinstance(v, str):
                return f"'{v.replace(chr(39), chr(39)+chr(39))}'"
            return str(v)
        vals = ", ".join([sql_val(v) for v in row])
        lines.append(
            f"INSERT OR IGNORE INTO crypto_trades (id, date, ticker, trade_type, notional, "
            f"entry_price, exit_price, qty, pnl, pnl_pct, kelly_pct, atr_at_entry) VALUES ({vals});"
        )

    conn.close()

    with open(SEED_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"Seed exportado a {SEED_FILE}")

if __name__ == "__main__":
    export_seed()
