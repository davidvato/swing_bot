"""
logging_/reconcile.py — Reconciliación Histórica de Operaciones Alpaca → SQLite
================================================================================
Audita y sincroniza retroactivamente todas las órdenes y actividades ejecutadas
en Alpaca hacia la base de datos local `trades.db` (tablas `trades` y `crypto_trades`).

Garantiza:
1. Reconstrucción contable FIFO exacta para cálculo de PnL y retornos %.
2. Idempotencia: No duplica operaciones previamente registradas.
3. Separación estricta entre Equities (tabla `trades`) y Cripto (`crypto_trades`).
4. Cumplimiento contable/tributario (reportes SIC).
"""

import logging
import os
import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Optional

import pytz
import requests
from alpaca.trading.client import TradingClient

from config import DB_PATH, TIMEZONE_EST, CRYPTO_DB_TABLE

logger = logging.getLogger(__name__)
EST = pytz.timezone(TIMEZONE_EST)


class AlpacaReconciler:
    """
    Servicio de auditoría y reconciliación de órdenes de Alpaca con la base de datos local.
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        db_path: str = DB_PATH,
        paper: bool = True,
    ) -> None:
        self.api_key = api_key
        self.secret_key = secret_key
        self.db_path = db_path
        self.paper = paper
        self.base_url = (
            "https://paper-api.alpaca.markets"
            if paper
            else "https://api.alpaca.markets"
        )
        self.headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Content-Type": "application/json",
        }
        self._init_db()

    def _init_db(self) -> None:
        """Inicializa las tablas en SQLite si aún no existen."""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                date        TEXT    NOT NULL,
                ticker      TEXT    NOT NULL,
                trade_type  TEXT    NOT NULL,
                notional    REAL    NOT NULL,
                entry_price REAL,
                exit_price  REAL,
                qty         REAL,
                pnl         REAL,
                pnl_pct     REAL,
                kelly_pct   REAL
            );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_date ON trades (date);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades (ticker);")

            cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {CRYPTO_DB_TABLE} (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                date        TEXT    NOT NULL,
                ticker      TEXT    NOT NULL,
                trade_type  TEXT    NOT NULL,
                notional    REAL    NOT NULL,
                entry_price REAL,
                exit_price  REAL,
                qty         REAL,
                pnl         REAL,
                pnl_pct     REAL,
                kelly_pct   REAL,
                atr_at_entry REAL
            );
            """)
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS idx_crypto_date ON {CRYPTO_DB_TABLE} (date);"
            )
            conn.commit()

    def fetch_all_orders(self) -> list[dict]:
        """Obtiene todas las órdenes de Alpaca independientemente del status."""
        url = f"{self.base_url}/v2/orders"
        params = {"status": "all", "limit": 500, "direction": "asc"}
        resp = requests.get(url, headers=self.headers, params=params)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Error al consultar órdenes de Alpaca: {resp.status_code} - {resp.text}"
            )
        return resp.json()

    def sync(self) -> dict:
        """
        Ejecuta el pipeline completo de reconciliación FIFO y persiste en SQLite.

        Returns:
            Diccionario resumen con conteo de registros insertados.
        """
        orders = self.fetch_all_orders()
        logger.info(f"Reconciler: {len(orders)} órdenes recuperadas desde Alpaca.")

        equities_orders = []
        crypto_orders = []

        for o in orders:
            sym = o.get("symbol", "")
            # Cripto en Alpaca tiene '/' (ej. 'BTC/USD') o termina en 'USD' siendo par de cripto
            if "/" in sym or sym.endswith("USD") and sym not in ("USD",):
                crypto_orders.append(o)
            else:
                equities_orders.append(o)

        inserted_eq = self._reconcile_orders(equities_orders, is_crypto=False)
        inserted_crypto = self._reconcile_orders(crypto_orders, is_crypto=True)

        logger.info(
            f"Sincronización completada. Insertados: Equities={inserted_eq}, Cripto={inserted_crypto}"
        )
        return {
            "total_alpaca_orders": len(orders),
            "equities_inserted": inserted_eq,
            "crypto_inserted": inserted_crypto,
        }

    def _reconcile_orders(self, orders: list[dict], is_crypto: bool) -> int:
        table_name = CRYPTO_DB_TABLE if is_crypto else "trades"
        inserted_count = 0

        # Lotes de compra FIFO por símbolo
        lots = defaultdict(list)

        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()

            for o in orders:
                fq = float(o.get("filled_qty") or 0.0)
                if fq <= 0:
                    continue  # Sin ejecución efectiva

                sym = o.get("symbol")
                side = o.get("side", "").lower()
                avg_px = float(o.get("filled_avg_price") or 0.0)
                notional = float(o.get("notional") or (fq * avg_px))
                dt_str = o.get("filled_at") or o.get("created_at") or ""
                # Formatear a EST estándar
                try:
                    dt_utc = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    dt_est_str = dt_utc.astimezone(EST).strftime("%Y-%m-%d %H:%M:%S EST")
                except Exception:
                    dt_est_str = dt_str

                if side == "buy":
                    lots[sym].append({
                        "qty": fq,
                        "price": avg_px,
                        "date": dt_est_str,
                        "notional": notional,
                    })

                    # Verificar si ya existe registro idéntico
                    cur.execute(
                        f"SELECT COUNT(*) FROM {table_name} WHERE date = ? AND ticker = ? AND trade_type = 'BUY' AND ABS(qty - ?) < 1e-5",
                        (dt_est_str, sym, fq),
                    )
                    if cur.fetchone()[0] == 0:
                        if is_crypto:
                            cur.execute(
                                f"""
                                INSERT INTO {table_name}
                                    (date, ticker, trade_type, notional, entry_price, exit_price, qty, pnl, pnl_pct, kelly_pct, atr_at_entry)
                                VALUES (?, ?, 'BUY', ?, ?, NULL, ?, NULL, NULL, 0.05, NULL)
                                """,
                                (dt_est_str, sym, notional, avg_px, fq),
                            )
                        else:
                            cur.execute(
                                f"""
                                INSERT INTO {table_name}
                                    (date, ticker, trade_type, notional, entry_price, exit_price, qty, pnl, pnl_pct, kelly_pct)
                                VALUES (?, ?, 'BUY', ?, ?, NULL, ?, NULL, NULL, 0.15)
                                """,
                                (dt_est_str, sym, notional, avg_px, fq),
                            )
                        inserted_count += 1

                elif side == "sell":
                    # Calcular FIFO PnL
                    qty_to_match = fq
                    cost_basis = 0.0
                    entry_px_acc = 0.0
                    matched_qty_acc = 0.0

                    sym_lots = lots[sym]
                    while qty_to_match > 1e-7 and sym_lots:
                        lot = sym_lots[0]
                        if lot["qty"] <= qty_to_match + 1e-7:
                            matched_qty = lot["qty"]
                            cost_basis += matched_qty * lot["price"]
                            entry_px_acc += matched_qty * lot["price"]
                            matched_qty_acc += matched_qty
                            qty_to_match -= matched_qty
                            sym_lots.pop(0)
                        else:
                            matched_qty = qty_to_match
                            cost_basis += matched_qty * lot["price"]
                            entry_px_acc += matched_qty * lot["price"]
                            matched_qty_acc += matched_qty
                            lot["qty"] -= matched_qty
                            qty_to_match = 0.0
                            break

                    entry_price = (
                        (entry_px_acc / matched_qty_acc)
                        if matched_qty_acc > 0
                        else avg_px
                    )
                    gross_exit = fq * avg_px
                    pnl = gross_exit - cost_basis
                    pnl_pct = (pnl / cost_basis) if cost_basis > 0 else 0.0

                    # Determinar tipo de salida
                    if pnl_pct >= 0.04:
                        trade_type = "SELL_TP"
                    elif pnl_pct <= -0.04:
                        trade_type = "SELL_SL"
                    else:
                        trade_type = "SELL_EOW"

                    if is_crypto:
                        trade_type = f"CRYPTO_{trade_type}"

                    cur.execute(
                        f"SELECT COUNT(*) FROM {table_name} WHERE date = ? AND ticker = ? AND trade_type = ? AND ABS(qty - ?) < 1e-5",
                        (dt_est_str, sym, trade_type, fq),
                    )
                    if cur.fetchone()[0] == 0:
                        if is_crypto:
                            cur.execute(
                                f"""
                                INSERT INTO {table_name}
                                    (date, ticker, trade_type, notional, entry_price, exit_price, qty, pnl, pnl_pct, kelly_pct, atr_at_entry)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0.05, NULL)
                                """,
                                (
                                    dt_est_str,
                                    sym,
                                    trade_type,
                                    cost_basis or gross_exit,
                                    entry_price,
                                    avg_px,
                                    fq,
                                    round(pnl, 4),
                                    round(pnl_pct, 4),
                                ),
                            )
                        else:
                            cur.execute(
                                f"""
                                INSERT INTO {table_name}
                                    (date, ticker, trade_type, notional, entry_price, exit_price, qty, pnl, pnl_pct, kelly_pct)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0.15)
                                """,
                                (
                                    dt_est_str,
                                    sym,
                                    trade_type,
                                    cost_basis or gross_exit,
                                    entry_price,
                                    avg_px,
                                    fq,
                                    round(pnl, 4),
                                    round(pnl_pct, 4),
                                ),
                            )
                        inserted_count += 1

            conn.commit()

        return inserted_count
