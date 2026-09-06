"""
logging_/trade_log.py — Bitacora de Operaciones (SQLite + CSV)
==============================================================
Registra todas las operaciones del bot en una base de datos SQLite local
para cumplimiento contable. Provee exportacion a CSV mensual para reportes
tributarios bajo el esquema SIC mexicano (10% sobre ganancias de capital).

ESQUEMA DE DATOS:
    Cada fila representa una operacion individual (compra o venta).
    Las compras (BUY) y ventas (SELL_*) se registran por separado,
    permitiendo calcular el P&L neto por operacion completa (round trip).

TIPOS DE OPERACION:
    BUY       — Compra inicial por señal de mean reversion.
    SELL_TP   — Venta por Take-Profit (+10%).
    SELL_SL   — Venta por Stop-Loss (-5%).
    SELL_EOW  — Liquidacion de viernes (End-Of-Week).
    SELL_5D   — Salida por maximo de 5 dias de holding.

CUMPLIMIENTO SIC (MEXICO):
    Las ganancias de capital en mercados extranjeros para personas fisicas
    mexicanas tributan bajo el regimen de ISR. El CSV mensual exportado
    permite al contador consolidar ingresos/perdidas por periodo fiscal
    y determinar la base gravable del 10% sobre ganancia neta.
"""

import csv
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

import pytz

from config import DB_PATH, TIMEZONE_EST

logger = logging.getLogger(__name__)

EST = pytz.timezone(TIMEZONE_EST)

# ─── Definicion del esquema SQL ───────────────────────────────────────────────
CREATE_TABLE_SQL = """
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
"""

# Indices para acelerar consultas por fecha y ticker
CREATE_INDEX_DATE_SQL = """
CREATE INDEX IF NOT EXISTS idx_trades_date   ON trades (date);
"""
CREATE_INDEX_TICKER_SQL = """
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades (ticker);
"""

# Columnas del CSV exportado para reporte SIC
CSV_COLUMNS = [
    "id",
    "date",
    "ticker",
    "trade_type",
    "notional",
    "entry_price",
    "exit_price",
    "qty",
    "pnl",
    "pnl_pct",
    "kelly_pct",
]


class TradeLogger:
    """
    Bitacora de operaciones con persistencia en SQLite y exportacion CSV.

    Thread-safe para uso desde asyncio via run_in_executor.
    """

    def __init__(self, db_path: str = DB_PATH) -> None:
        """
        Inicializa la base de datos SQLite y crea la tabla si no existe.

        Args:
            db_path: Ruta al archivo SQLite (default: 'trades.db').
        """
        self._db_path = db_path
        self._initialize_db()
        logger.info(f"TradeLogger inicializado. Base de datos: {os.path.abspath(db_path)}")

    def _initialize_db(self) -> None:
        """Crea la tabla y los indices si no existen."""
        with self._get_connection() as conn:
            conn.execute(CREATE_TABLE_SQL)
            conn.execute(CREATE_INDEX_DATE_SQL)
            conn.execute(CREATE_INDEX_TICKER_SQL)
            conn.commit()

    @contextmanager
    def _get_connection(self):
        """Context manager para conexiones SQLite con manejo automatico de cierre."""
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _now_est_str(self) -> str:
        """Retorna la fecha y hora actual en EST como string ISO."""
        return datetime.now(EST).strftime("%Y-%m-%d %H:%M:%S %Z")

    def log_entry(self, trade_data: dict) -> int:
        """
        Registra una operacion de compra (BUY) en la base de datos.

        Args:
            trade_data: Diccionario con los campos de la operacion.
                Campos requeridos: ticker, notional, entry_price, qty, kelly_pct.
                El trade_type es forzado a 'BUY'.

        Returns:
            ID de la fila insertada en SQLite.

        Ejemplo:
            >>> logger.log_entry({
            ...     "ticker": "AAPL",
            ...     "notional": 1500.00,
            ...     "entry_price": 182.50,
            ...     "qty": 8.2191,
            ...     "kelly_pct": 0.15,
            ... })
        """
        insert_sql = """
        INSERT INTO trades
            (date, ticker, trade_type, notional, entry_price, exit_price,
             qty, pnl, pnl_pct, kelly_pct)
        VALUES
            (:date, :ticker, :trade_type, :notional, :entry_price, :exit_price,
             :qty, :pnl, :pnl_pct, :kelly_pct)
        """
        row = {
            "date": self._now_est_str(),
            "ticker": trade_data.get("ticker"),
            "trade_type": "BUY",
            "notional": trade_data.get("notional"),
            "entry_price": trade_data.get("entry_price"),
            "exit_price": None,  # No disponible al momento de la compra
            "qty": trade_data.get("qty"),
            "pnl": None,         # No disponible al momento de la compra
            "pnl_pct": None,     # No disponible al momento de la compra
            "kelly_pct": trade_data.get("kelly_pct"),
        }

        with self._get_connection() as conn:
            cursor = conn.execute(insert_sql, row)
            conn.commit()
            row_id = cursor.lastrowid

        logger.info(
            f"[LOG] BUY registrado: {row['ticker']} | "
            f"${row['notional']:.2f} nocional | "
            f"Precio entrada: ${row['entry_price']:.2f} | "
            f"ID: {row_id}"
        )
        return row_id

    def log_exit(self, trade_data: dict) -> int:
        """
        Registra una operacion de venta (SELL_TP, SELL_SL, SELL_EOW, SELL_5D).

        Args:
            trade_data: Diccionario con los campos de la operacion de salida.
                Campos requeridos: ticker, trade_type, notional, entry_price,
                exit_price, qty, pnl, pnl_pct, kelly_pct.

        Returns:
            ID de la fila insertada en SQLite.
        """
        insert_sql = """
        INSERT INTO trades
            (date, ticker, trade_type, notional, entry_price, exit_price,
             qty, pnl, pnl_pct, kelly_pct)
        VALUES
            (:date, :ticker, :trade_type, :notional, :entry_price, :exit_price,
             :qty, :pnl, :pnl_pct, :kelly_pct)
        """
        row = {
            "date": self._now_est_str(),
            "ticker": trade_data.get("ticker"),
            "trade_type": trade_data.get("trade_type"),
            "notional": trade_data.get("notional"),
            "entry_price": trade_data.get("entry_price"),
            "exit_price": trade_data.get("exit_price"),
            "qty": trade_data.get("qty"),
            "pnl": trade_data.get("pnl"),
            "pnl_pct": trade_data.get("pnl_pct"),
            "kelly_pct": trade_data.get("kelly_pct"),
        }

        with self._get_connection() as conn:
            cursor = conn.execute(insert_sql, row)
            conn.commit()
            row_id = cursor.lastrowid

        pnl_symbol = "+" if (row["pnl"] or 0) >= 0 else ""
        logger.info(
            f"[LOG] {row['trade_type']} registrado: {row['ticker']} | "
            f"P&L: {pnl_symbol}${row['pnl']:.2f} ({pnl_symbol}{(row['pnl_pct'] or 0)*100:.2f}%) | "
            f"ID: {row_id}"
        )
        return row_id

    def get_latest_buy(self, ticker: str) -> dict:
        """
        Recupera el ultimo registro de compra para un ticker especifico.

        Util para la rehidratacion de estado tras un reinicio del bot.

        Args:
            ticker: Simbolo bursatil.

        Returns:
            Diccionario con la fila de SQLite, o vacio si no hay registro.
        """
        query = "SELECT * FROM trades WHERE ticker = ? AND trade_type = 'BUY' ORDER BY date DESC LIMIT 1"
        with self._get_connection() as conn:
            cursor = conn.execute(query, (ticker,))
            row = cursor.fetchone()
        
        return dict(row) if row else {}

    def export_csv(self, month: Optional[str] = None) -> str:
        """
        Exporta el trade log a un archivo CSV para reporte SIC.

        El CSV contiene todas las operaciones del periodo especificado,
        ordenadas cronologicamente. Cada fila representa una entrada o
        salida individual, permitiendo al contador calcular el P&L neto
        por operacion completa (round trip BUY -> SELL).

        Args:
            month: Filtro de mes en formato 'YYYY-MM' (e.g., '2026-08').
                   Si es None, exporta todos los registros disponibles.

        Returns:
            Ruta absoluta del archivo CSV generado.

        Raises:
            IOError: Si no se puede crear el archivo CSV.

        Ejemplo:
            >>> path = logger.export_csv(month='2026-08')
            >>> print(path)  # trades_2026_08.csv
        """
        # Determinar nombre del archivo
        if month:
            filename = f"trades_{month.replace('-', '_')}.csv"
            date_filter = month  # Formato YYYY-MM
        else:
            filename = "trades_all.csv"
            date_filter = None

        # Consultar registros
        if date_filter:
            query = "SELECT * FROM trades WHERE date LIKE ? ORDER BY date ASC"
            params = (f"{date_filter}%",)
        else:
            query = "SELECT * FROM trades ORDER BY date ASC"
            params = ()

        rows = []
        with self._get_connection() as conn:
            cursor = conn.execute(query, params)
            rows = [dict(row) for row in cursor.fetchall()]

        # Escribir CSV
        filepath = os.path.abspath(filename)
        with open(filepath, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(
                csvfile,
                fieldnames=CSV_COLUMNS,
                extrasaction="ignore",
            )
            writer.writeheader()
            writer.writerows(rows)

        logger.info(
            f"CSV exportado: {filepath} | {len(rows)} registros "
            f"{'(mes: ' + month + ')' if month else '(todos)'}"
        )
        return filepath

    def get_monthly_summary(self, month: str) -> dict:
        """
        Calcula el resumen de P&L mensual para reporte SIC.

        Args:
            month: Mes en formato 'YYYY-MM'.

        Returns:
            Diccionario con metricas de P&L del mes:
                - total_trades: Numero total de operaciones cerradas.
                - total_pnl_usd: P&L neto del mes en USD.
                - winning_trades: Numero de operaciones con P&L positivo.
                - losing_trades: Numero de operaciones con P&L negativo.
                - win_rate: Tasa de ganancia empirica del mes.
                - taxable_gain_usd: Ganancia bruta (para SIC 10%).
        """
        query = """
        SELECT
            COUNT(*) as total_trades,
            SUM(pnl) as total_pnl,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
            SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losing_trades,
            SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) as gross_gain,
            SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END) as gross_loss
        FROM trades
        WHERE date LIKE ? AND trade_type LIKE 'SELL%'
        """

        with self._get_connection() as conn:
            cursor = conn.execute(query, (f"{month}%",))
            row = dict(cursor.fetchone())

        total = row.get("total_trades", 0) or 0
        wins = row.get("winning_trades", 0) or 0
        total_pnl = row.get("total_pnl", 0.0) or 0.0
        gross_gain = row.get("gross_gain", 0.0) or 0.0

        summary = {
            "month": month,
            "total_trades": total,
            "total_pnl_usd": round(total_pnl, 2),
            "winning_trades": wins,
            "losing_trades": row.get("losing_trades", 0) or 0,
            "win_rate": round(wins / total, 4) if total > 0 else 0.0,
            "gross_gain_usd": round(gross_gain, 2),
            "taxable_gain_usd": round(gross_gain * 0.10, 2),  # SIC: 10% sobre ganancia bruta
        }

        logger.info(
            f"Resumen {month}: {total} ops | P&L=${total_pnl:+.2f} | "
            f"Win rate={summary['win_rate']*100:.1f}% | "
            f"Impuesto estimado SIC (10%)=${summary['taxable_gain_usd']:.2f}"
        )
        return summary
