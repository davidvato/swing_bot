"""
tests/test_trade_log.py — Pruebas unitarias para la bitacora SQLite
====================================================================
Valida la insercion correcta de registros BUY/SELL, el calculo de P&L,
el esquema de la tabla, y el formato del CSV exportado para reporte SIC.
Usa bases de datos SQLite en memoria para aislamiento total entre tests.
"""

import os
import csv
import math
import pytest
import tempfile

from logging_.trade_log import TradeLogger, CSV_COLUMNS


# ─── Fixture: Logger con base de datos temporal ───────────────────────────────

@pytest.fixture
def logger_tmp():
    """
    Crea un TradeLogger con una base de datos SQLite en un archivo temporal.
    Se elimina automaticamente al terminar cada test.

    Nota: En Windows, SQLite puede retener el lock del archivo brevemente.
    El teardown usa un retry con gc.collect() para asegurar la liberacion.
    """
    import gc
    import time as _time

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_path = f.name

    trade_logger = TradeLogger(db_path=tmp_path)
    yield trade_logger

    # Cleanup con tolerancia a Windows file locking
    del trade_logger
    gc.collect()
    for _ in range(5):
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            break
        except PermissionError:
            _time.sleep(0.1)


# ─── Datos de prueba ──────────────────────────────────────────────────────────

SAMPLE_BUY = {
    "ticker": "AAPL",
    "notional": 15_000.00,
    "entry_price": 182.50,
    "qty": 82.1918,
    "kelly_pct": 0.15,
}

SAMPLE_SELL_TP = {
    "ticker": "AAPL",
    "trade_type": "SELL_TP",
    "notional": 15_000.00,
    "entry_price": 182.50,
    "exit_price": 200.75,  # +10.0% TP activado
    "qty": 82.1918,
    "pnl": 1500.00,
    "pnl_pct": 0.10,
    "kelly_pct": 0.15,
}

SAMPLE_SELL_SL = {
    "ticker": "MSFT",
    "trade_type": "SELL_SL",
    "notional": 15_000.00,
    "entry_price": 400.00,
    "exit_price": 380.00,  # -5% SL activado
    "qty": 37.5,
    "pnl": -750.00,
    "pnl_pct": -0.05,
    "kelly_pct": 0.15,
}

SAMPLE_SELL_EOW = {
    "ticker": "NVDA",
    "trade_type": "SELL_EOW",
    "notional": 15_000.00,
    "entry_price": 850.00,
    "exit_price": 860.00,
    "qty": 17.647,
    "pnl": 176.47,
    "pnl_pct": 0.01176,
    "kelly_pct": None,
}


# ─── Tests de insercion ───────────────────────────────────────────────────────

class TestLogEntry:
    """Pruebas para el metodo log_entry() (registro de compras BUY)."""

    def test_insert_buy_returns_valid_id(self, logger_tmp):
        """log_entry() debe retornar un ID entero positivo."""
        row_id = logger_tmp.log_entry(SAMPLE_BUY)
        assert isinstance(row_id, int)
        assert row_id > 0

    def test_insert_buy_record_is_retrievable(self, logger_tmp):
        """La fila insertada debe ser recuperable via consulta SQLite."""
        row_id = logger_tmp.log_entry(SAMPLE_BUY)

        import sqlite3
        with sqlite3.connect(logger_tmp._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM trades WHERE id = ?", (row_id,)
            ).fetchone()

        assert row is not None, f"Fila con ID {row_id} no encontrada en la base de datos"
        assert row["ticker"] == "AAPL"
        assert row["trade_type"] == "BUY"
        assert row["notional"] == 15_000.00
        assert abs(row["entry_price"] - 182.50) < 0.001
        assert abs(row["qty"] - 82.1918) < 0.001
        assert abs(row["kelly_pct"] - 0.15) < 0.001

    def test_buy_sets_exit_price_to_none(self, logger_tmp):
        """Al registrar una compra, exit_price debe ser NULL (desconocido)."""
        row_id = logger_tmp.log_entry(SAMPLE_BUY)

        import sqlite3
        with sqlite3.connect(logger_tmp._db_path) as conn:
            row = conn.execute(
                "SELECT exit_price, pnl, pnl_pct FROM trades WHERE id = ?", (row_id,)
            ).fetchone()

        assert row[0] is None, "exit_price debe ser NULL en registro BUY"
        assert row[1] is None, "pnl debe ser NULL en registro BUY"
        assert row[2] is None, "pnl_pct debe ser NULL en registro BUY"

    def test_buy_trade_type_is_forced_to_buy(self, logger_tmp):
        """El trade_type siempre debe ser 'BUY', independientemente del input."""
        data = {**SAMPLE_BUY, "trade_type": "SELL_TP"}  # Intentar forzar otro tipo
        row_id = logger_tmp.log_entry(data)

        import sqlite3
        with sqlite3.connect(logger_tmp._db_path) as conn:
            row = conn.execute(
                "SELECT trade_type FROM trades WHERE id = ?", (row_id,)
            ).fetchone()

        assert row[0] == "BUY", f"trade_type esperado='BUY', obtenido='{row[0]}'"

    def test_multiple_buys_get_unique_ids(self, logger_tmp):
        """Multiples inserciones deben tener IDs unicos e incrementales."""
        id1 = logger_tmp.log_entry({**SAMPLE_BUY, "ticker": "AAPL"})
        id2 = logger_tmp.log_entry({**SAMPLE_BUY, "ticker": "MSFT"})
        id3 = logger_tmp.log_entry({**SAMPLE_BUY, "ticker": "NVDA"})

        assert id1 < id2 < id3, "IDs deben ser unicos e incrementales"


# ─── Tests de salida ──────────────────────────────────────────────────────────

class TestLogExit:
    """Pruebas para el metodo log_exit() (registro de ventas SELL_*)."""

    def test_insert_sell_tp_record(self, logger_tmp):
        """log_exit() debe insertar correctamente una venta de Take-Profit."""
        row_id = logger_tmp.log_exit(SAMPLE_SELL_TP)
        assert isinstance(row_id, int)
        assert row_id > 0

    def test_pnl_calculation_is_correct(self, logger_tmp):
        """El P&L registrado debe ser exactamente el calculado por el supervisor."""
        row_id = logger_tmp.log_exit(SAMPLE_SELL_TP)

        import sqlite3
        with sqlite3.connect(logger_tmp._db_path) as conn:
            row = conn.execute(
                "SELECT pnl, pnl_pct, exit_price FROM trades WHERE id = ?", (row_id,)
            ).fetchone()

        # Verificar valores del P&L
        assert abs(row[0] - 1500.00) < 0.01, f"P&L esperado=1500.00, obtenido={row[0]}"
        assert abs(row[1] - 0.10) < 0.001, f"P&L% esperado=0.10, obtenido={row[1]}"
        assert abs(row[2] - 200.75) < 0.001, f"Exit price esperado=200.75, obtenido={row[2]}"

    def test_stop_loss_pnl_is_negative(self, logger_tmp):
        """Un Stop-Loss debe registrar un P&L negativo."""
        row_id = logger_tmp.log_exit(SAMPLE_SELL_SL)

        import sqlite3
        with sqlite3.connect(logger_tmp._db_path) as conn:
            row = conn.execute(
                "SELECT pnl, pnl_pct, trade_type FROM trades WHERE id = ?", (row_id,)
            ).fetchone()

        assert row[0] < 0, f"P&L de Stop-Loss debe ser negativo, obtenido={row[0]}"
        assert row[1] < 0, f"P&L% de Stop-Loss debe ser negativo, obtenido={row[1]}"
        assert row[2] == "SELL_SL"

    def test_eow_kelly_pct_can_be_none(self, logger_tmp):
        """En la liquidacion del viernes (EOW), kelly_pct puede ser NULL."""
        row_id = logger_tmp.log_exit(SAMPLE_SELL_EOW)

        import sqlite3
        with sqlite3.connect(logger_tmp._db_path) as conn:
            row = conn.execute(
                "SELECT kelly_pct, trade_type FROM trades WHERE id = ?", (row_id,)
            ).fetchone()

        assert row[0] is None, "kelly_pct debe ser NULL para SELL_EOW"
        assert row[1] == "SELL_EOW"

    def test_all_sell_types_are_accepted(self, logger_tmp):
        """Los 4 tipos de venta validos deben poder insertarse sin error."""
        sell_types = ["SELL_TP", "SELL_SL", "SELL_EOW", "SELL_5D"]
        base = {
            "ticker": "TEST",
            "notional": 1000.0,
            "entry_price": 100.0,
            "exit_price": 105.0,
            "qty": 10.0,
            "pnl": 50.0,
            "pnl_pct": 0.05,
            "kelly_pct": 0.15,
        }
        for sell_type in sell_types:
            row_id = logger_tmp.log_exit({**base, "trade_type": sell_type})
            assert row_id > 0, f"Insercion de {sell_type} fallida"


# ─── Tests de exportacion CSV ─────────────────────────────────────────────────

class TestExportCSV:
    """Pruebas para la exportacion CSV del trade log."""

    def test_csv_file_is_created(self, logger_tmp, tmp_path, monkeypatch):
        """export_csv() debe crear un archivo CSV en el directorio actual."""
        monkeypatch.chdir(tmp_path)
        logger_tmp.log_entry(SAMPLE_BUY)
        logger_tmp.log_exit(SAMPLE_SELL_TP)

        filepath = logger_tmp.export_csv()
        assert os.path.exists(filepath), f"CSV no encontrado en: {filepath}"

    def test_csv_has_required_columns(self, logger_tmp, tmp_path, monkeypatch):
        """El CSV debe contener exactamente las columnas definidas en CSV_COLUMNS."""
        monkeypatch.chdir(tmp_path)
        logger_tmp.log_entry(SAMPLE_BUY)
        filepath = logger_tmp.export_csv()

        with open(filepath, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames

        for col in CSV_COLUMNS:
            assert col in headers, f"Columna requerida '{col}' no encontrada en CSV"

    def test_csv_contains_correct_number_of_rows(self, logger_tmp, tmp_path, monkeypatch):
        """El CSV debe contener exactamente los registros insertados."""
        monkeypatch.chdir(tmp_path)
        # Insertar 3 registros: 1 BUY + 1 SELL_TP + 1 SELL_SL
        logger_tmp.log_entry(SAMPLE_BUY)
        logger_tmp.log_exit(SAMPLE_SELL_TP)
        logger_tmp.log_exit(SAMPLE_SELL_SL)

        filepath = logger_tmp.export_csv()
        with open(filepath, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        assert len(rows) == 3, f"Esperados 3 registros, obtenidos {len(rows)}"

    def test_csv_monthly_filter_works(self, logger_tmp, tmp_path, monkeypatch):
        """
        El filtro por mes debe exportar unicamente los registros del mes especificado.
        """
        monkeypatch.chdir(tmp_path)

        # Insertar un registro (la fecha actual)
        logger_tmp.log_entry(SAMPLE_BUY)

        # Exportar con filtro del mes actual
        from datetime import datetime
        current_month = datetime.now().strftime("%Y-%m")
        filepath = logger_tmp.export_csv(month=current_month)

        with open(filepath, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        assert len(rows) >= 1, (
            f"El filtro mensual no retorno registros del mes {current_month}"
        )

    def test_csv_filename_format(self, logger_tmp, tmp_path, monkeypatch):
        """El nombre del CSV mensual debe seguir el formato 'trades_YYYY_MM.csv'."""
        monkeypatch.chdir(tmp_path)
        logger_tmp.log_entry(SAMPLE_BUY)

        filepath = logger_tmp.export_csv(month="2026-08")
        filename = os.path.basename(filepath)
        assert filename == "trades_2026_08.csv", (
            f"Nombre de archivo esperado='trades_2026_08.csv', obtenido='{filename}'"
        )


# ─── Tests del resumen mensual SIC ───────────────────────────────────────────

class TestMonthlySummary:
    """Pruebas para el resumen mensual utilizado en reportes SIC."""

    def test_monthly_summary_calculates_total_pnl(self, logger_tmp):
        """El P&L total debe ser la suma de todos los P&L del mes."""
        logger_tmp.log_exit(SAMPLE_SELL_TP)    # +1500.00
        logger_tmp.log_exit(SAMPLE_SELL_SL)    # -750.00

        from datetime import datetime
        month = datetime.now().strftime("%Y-%m")
        summary = logger_tmp.get_monthly_summary(month)

        expected_pnl = 1500.00 + (-750.00)
        assert abs(summary["total_pnl_usd"] - expected_pnl) < 0.01, (
            f"P&L total esperado={expected_pnl}, obtenido={summary['total_pnl_usd']}"
        )

    def test_monthly_summary_taxable_gain_is_10_percent(self, logger_tmp):
        """
        El impuesto SIC estimado (10%) sobre la ganancia bruta debe ser correcto.
        Solo se grava la ganancia bruta (ganancias positivas), no el P&L neto.
        """
        logger_tmp.log_exit(SAMPLE_SELL_TP)  # +1500.00 ganancia

        from datetime import datetime
        month = datetime.now().strftime("%Y-%m")
        summary = logger_tmp.get_monthly_summary(month)

        expected_tax = round(1500.00 * 0.10, 2)  # = 150.00
        assert abs(summary["taxable_gain_usd"] - expected_tax) < 0.01, (
            f"Impuesto SIC esperado=${expected_tax}, obtenido=${summary['taxable_gain_usd']}"
        )

    def test_monthly_summary_win_rate(self, logger_tmp):
        """El win rate del resumen debe ser correcto (1 win de 2 operaciones = 50%)."""
        logger_tmp.log_exit(SAMPLE_SELL_TP)   # Ganancia = 1 win
        logger_tmp.log_exit(SAMPLE_SELL_SL)   # Perdida  = 1 loss

        from datetime import datetime
        month = datetime.now().strftime("%Y-%m")
        summary = logger_tmp.get_monthly_summary(month)

        assert summary["total_trades"] == 2
        assert summary["winning_trades"] == 1
        assert summary["losing_trades"] == 1
        assert abs(summary["win_rate"] - 0.50) < 0.001
