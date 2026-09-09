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

    def fetch_open_positions(self) -> list[dict]:
        """Obtiene las posiciones abiertas actuales en Alpaca."""
        url = f"{self.base_url}/v2/positions"
        resp = requests.get(url, headers=self.headers)
        if resp.status_code != 200:
            return []
        return resp.json()

    def sync(self) -> dict:
        """
        Ejecuta el pipeline completo de reconciliación FIFO y persiste en SQLite.

        1. Usa el campo `asset_class` de Alpaca para clasificar equity vs cripto
           (elimina la heurística de símbolo que causaba falsos positivos).
        2. Reconcilia posiciones abiertas en Alpaca que no tienen BUY en DB
           (inserta registros faltantes de compras recientes).

        Returns:
            Diccionario resumen con conteo de registros insertados.
        """
        orders = self.fetch_all_orders()
        logger.info(f"Reconciler: {len(orders)} órdenes recuperadas desde Alpaca.")

        equities_orders = []
        crypto_orders = []

        for o in orders:
            # Usar asset_class del objeto orden si está disponible;
            # si no, distinguir por '/' en el símbolo (formato cripto de Alpaca)
            asset_class = o.get("asset_class", "")
            sym = o.get("symbol", "")
            if asset_class == "crypto" or "/" in sym:
                crypto_orders.append(o)
            else:
                equities_orders.append(o)

        inserted_eq = self._reconcile_orders(equities_orders, is_crypto=False)
        inserted_crypto = self._reconcile_orders(crypto_orders, is_crypto=True)

        # Paso 2: sincronizar posiciones abiertas que el bot tiene en Alpaca
        # pero cuya compra no quedó registrada en trades.db (ej. compras recientes
        # posteriores al último --sync-trades)
        pos_inserted = self._reconcile_open_positions()

        total_inserted = inserted_eq + inserted_crypto + pos_inserted
        logger.info(
            f"Sincronización completada. Insertados: Equities={inserted_eq}, "
            f"Cripto={inserted_crypto}, Posiciones abiertas faltantes={pos_inserted}"
        )
        return {
            "total_alpaca_orders": len(orders),
            "equities_inserted": inserted_eq,
            "crypto_inserted": inserted_crypto,
            "open_positions_inserted": pos_inserted,
        }

    def _reconcile_open_positions(self) -> int:
        """
        Consulta las posiciones abiertas en Alpaca e inserta un registro BUY en DB
        para cualquier posición que no tenga un BUY sin parear en la tabla correspondiente.

        Resuelve el problema de compras recientes que ocurren entre ejecuciones de sync.

        Returns:
            Número de registros BUY insertados.
        """
        from collections import defaultdict, deque

        open_positions = self.fetch_open_positions()
        if not open_positions:
            return 0

        inserted = 0

        # Construir el conjunto de tickers con BUYs sin parear en cada tabla
        def get_unmatched_buys(table: str) -> set:
            pending = defaultdict(deque)
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.cursor()
                # Check table exists
                cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
                )
                if not cur.fetchone():
                    return set()
                cur.execute(f"SELECT ticker, trade_type FROM {table} ORDER BY date ASC, id ASC")
                for ticker, trade_type in cur.fetchall():
                    if trade_type == "BUY":
                        pending[ticker].append(True)
                    elif trade_type.startswith("SELL") and pending[ticker]:
                        pending[ticker].popleft()
            return {t for t, q in pending.items() if q}

        eq_open = get_unmatched_buys("trades")
        crypto_open = get_unmatched_buys(CRYPTO_DB_TABLE)

        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            for pos in open_positions:
                if not isinstance(pos, dict):
                    continue

                sym = pos.get("symbol", "")
                asset_class = pos.get("asset_class", "")
                qty = float(pos.get("qty", 0) or 0)
                market_val = abs(float(pos.get("market_value", 0) or 0))

                # Skip dust
                if qty < 1e-6 or market_val < 1.0:
                    continue

                avg_entry = float(pos.get("avg_entry_price", 0) or 0)
                is_crypto = asset_class == "crypto" or "/" in sym

                # Normalize ticker: for crypto, use 'BTC/USD' format
                if is_crypto and "/" not in sym and sym.endswith("USD"):
                    ticker = f"{sym[:-3]}/USD"
                else:
                    ticker = sym

                table = CRYPTO_DB_TABLE if is_crypto else "trades"
                already_open = crypto_open if is_crypto else eq_open

                if ticker in already_open or sym in already_open:
                    continue  # Already has an unmatched BUY in DB

                notional = market_val
                now_str = datetime.now(
                    __import__("pytz").timezone("America/New_York")
                ).strftime("%Y-%m-%d %H:%M:%S EST")

                # Check not already inserted (idempotency on avg_entry_price)
                cur.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE ticker = ? AND trade_type = 'BUY' AND ABS(entry_price - ?) < 0.01",
                    (ticker, avg_entry),
                )
                if cur.fetchone()[0] > 0:
                    continue

                if is_crypto:
                    cur.execute(
                        f"""
                        INSERT INTO {table}
                            (date, ticker, trade_type, notional, entry_price, exit_price, qty, pnl, pnl_pct, kelly_pct, atr_at_entry)
                        VALUES (?, ?, 'BUY', ?, ?, NULL, ?, NULL, NULL, 0.05, NULL)
                        """,
                        (now_str, ticker, notional, avg_entry, qty),
                    )
                else:
                    cur.execute(
                        f"""
                        INSERT INTO {table}
                            (date, ticker, trade_type, notional, entry_price, exit_price, qty, pnl, pnl_pct, kelly_pct)
                        VALUES (?, ?, 'BUY', ?, ?, NULL, ?, NULL, NULL, 0.15)
                        """,
                        (now_str, ticker, notional, avg_entry, qty),
                    )

                logger.info(
                    f"[Reconciler] Posición abierta insertada en DB: {ticker} (tabla={table}) @ ${avg_entry}"
                )
                inserted += 1

            conn.commit()

        return inserted

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
