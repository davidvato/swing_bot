"""
tests/test_equity_risk_supervisor.py — Pruebas para gestion de riesgo, ejecucion y supervisor de Equities
========================================================================================================
Valida las mejoras cuantitativas y de microestructura implementadas:
1. Validaciones previas de riesgo en OrderManager.can_open_position (heat, max positions, buying power).
2. Metodo nativo OrderManager.close_position.
3. Filtrado de barras en progreso con DataClient.filter_completed_bars.
4. Persistencia y reanudacion de estado en PositionSupervisor (_save_state, _load_state).
5. Cancelacion selectiva de posiciones en PositionSupervisor.stop_position.
"""

import os
import json
import tempfile
import asyncio
import zoneinfo
from unittest.mock import MagicMock, patch
import pandas as pd
import pytest

from execution.orders import OrderManager
from execution.supervisor import PositionSupervisor, PositionRecord
from data.ingestion import DataClient
import config


# ============================================================================
# 1. Pruebas de Filtro de Barras en DataClient
# ============================================================================

def test_filter_completed_bars_drops_today_bar():
    """Valida que una barra con fecha de hoy (incompleta durante la sesion) sea descartada."""
    tz_ny = zoneinfo.ZoneInfo(config.TIMEZONE_EST)
    today_ny = pd.Timestamp.now(tz=tz_ny).normalize()
    yesterday_ny = today_ny - pd.Timedelta(days=1)
    two_days_ago_ny = today_ny - pd.Timedelta(days=2)

    df = pd.DataFrame(
        {
            "open": [100.0, 102.0, 105.0],
            "high": [103.0, 106.0, 107.0],
            "low": [99.0, 101.0, 104.0],
            "close": [102.0, 105.0, 106.0],
            "volume": [1_000_000, 1_200_000, 500_000],
        },
        index=pd.DatetimeIndex([two_days_ago_ny, yesterday_ny, today_ny]),
    )

    filtered_df = DataClient.filter_completed_bars(df)
    assert len(filtered_df) == 2
    assert today_ny not in filtered_df.index
    assert yesterday_ny in filtered_df.index
    assert two_days_ago_ny in filtered_df.index


def test_filter_completed_bars_preserves_past_data():
    """Valida que no se modifique un DataFrame cuyas barras son todas anteriores a hoy."""
    tz_ny = zoneinfo.ZoneInfo(config.TIMEZONE_EST)
    today_ny = pd.Timestamp.now(tz=tz_ny).normalize()
    yesterday_ny = today_ny - pd.Timedelta(days=1)
    two_days_ago_ny = today_ny - pd.Timedelta(days=2)

    df = pd.DataFrame(
        {
            "close": [100.0, 102.0],
            "volume": [500_000, 600_000],
        },
        index=pd.DatetimeIndex([two_days_ago_ny, yesterday_ny]),
    )

    filtered_df = DataClient.filter_completed_bars(df)
    assert len(filtered_df) == 2


def test_filter_completed_bars_empty():
    """DataFrames vacios deben retornar vacios sin excepcion."""
    empty_df = pd.DataFrame()
    assert DataClient.filter_completed_bars(empty_df).empty


# ============================================================================
# 2. Pruebas de Control de Riesgo en OrderManager.can_open_position
# ============================================================================

def test_equity_can_open_position_existing_blocked():
    mgr = OrderManager("dummy_key", "dummy_secret")
    mgr.get_account_equity = MagicMock(return_value=10_000.0)

    pos_aapl = MagicMock()
    pos_aapl.symbol = "AAPL"
    pos_aapl.market_value = "1500.0"
    mgr.get_open_positions = MagicMock(return_value=[pos_aapl])

    allowed, reason = mgr.can_open_position("AAPL", 500.0, 0.10)
    assert not allowed
    assert "Posicion activa existente" in reason


def test_equity_can_open_position_single_cap_exceeded():
    mgr = OrderManager("dummy_key", "dummy_secret")
    mgr.get_account_equity = MagicMock(return_value=10_000.0)
    mgr.get_open_positions = MagicMock(return_value=[])

    # 10% de 10,000 = 1,000 max. Intentar 1,500 debe bloquearse.
    allowed, reason = mgr.can_open_position("NVDA", 1500.0, 0.10)
    assert not allowed
    assert "excede el techo" in reason


def test_equity_can_open_position_max_positions_exceeded():
    mgr = OrderManager("dummy_key", "dummy_secret")
    mgr.get_account_equity = MagicMock(return_value=10_000.0)

    # Simular que ya se alcanzo el maximo de posiciones (EQUITY_MAX_OPEN_POSITIONS = 4)
    positions = []
    for sym in ["MSFT", "GOOG", "AMZN", "META"]:
        p = MagicMock()
        p.symbol = sym
        p.market_value = "1000.0"
        positions.append(p)

    mgr.get_open_positions = MagicMock(return_value=positions)

    allowed, reason = mgr.can_open_position("NVDA", 500.0, 0.10)
    assert not allowed
    assert "Limite de posiciones simultaneas alcanzado" in reason


def test_equity_can_open_position_heat_exceeded():
    mgr = OrderManager("dummy_key", "dummy_secret")
    mgr.get_account_equity = MagicMock(return_value=10_000.0)

    # 2 posiciones con 5,500 total = 55% de portfolio heat.
    # Intentar agregar 1,000 (10%) llevaria el calor a 65% > 60% maximo.
    pos1 = MagicMock()
    pos1.symbol = "AAPL"
    pos1.market_value = "3000.0"
    pos2 = MagicMock()
    pos2.symbol = "MSFT"
    pos2.market_value = "2500.0"
    mgr.get_open_positions = MagicMock(return_value=[pos1, pos2])

    allowed, reason = mgr.can_open_position("NVDA", 1000.0, 0.10)
    assert not allowed
    assert "Portfolio Heat excedido" in reason


def test_equity_can_open_position_buying_power_exceeded():
    mgr = OrderManager("dummy_key", "dummy_secret")
    mgr.get_account_equity = MagicMock(return_value=10_000.0)
    mgr.get_open_positions = MagicMock(return_value=[])

    # Buying power disponible solo $400, orden de $800
    mock_acc = MagicMock()
    mock_acc.buying_power = "400.0"
    mgr.get_account = MagicMock(return_value=mock_acc)

    with patch("execution.orders.USE_TEST_BUDGET", False):
        allowed, reason = mgr.can_open_position("NVDA", 800.0, 0.10)
        assert not allowed
        assert "Buying power insuficiente" in reason


def test_equity_can_open_position_allowed():
    mgr = OrderManager("dummy_key", "dummy_secret")
    mgr.get_account_equity = MagicMock(return_value=10_000.0)
    mgr.get_open_positions = MagicMock(return_value=[])

    mock_acc = MagicMock()
    mock_acc.buying_power = "5000.0"
    mgr.get_account = MagicMock(return_value=mock_acc)

    allowed, reason = mgr.can_open_position("NVDA", 800.0, 0.10)
    assert allowed
    assert reason == "OK"


def test_equity_close_position_calls_alpaca_natively():
    """Valida que close_position invoque el endpoint nativo de Alpaca TradingClient."""
    mgr = OrderManager("dummy_key", "dummy_secret")
    mock_order = MagicMock()
    mock_order.id = "order_close_123"
    mock_order.status = "accepted"
    mgr._client.close_position = MagicMock(return_value=mock_order)

    result = mgr.close_position("AMZN")
    assert result == mock_order
    mgr._client.close_position.assert_called_once_with(symbol_or_asset_id="AMZN")


# ============================================================================
# 3. Pruebas de Persistencia y Cancelacion en PositionSupervisor
# ============================================================================

def test_supervisor_state_persistence_roundtrip():
    """Valida que el estado del supervisor guarde y restaure trailing stops y breakeven."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        state_file = os.path.join(tmp_dir, "test_supervisor_state.json")

        with patch("execution.supervisor.SUPERVISOR_STATE_FILE", state_file):
            mock_orders = MagicMock()
            mock_trade_log = MagicMock()
            sup = PositionSupervisor(order_manager=mock_orders, trade_logger=mock_trade_log)

            # Registrar dos posiciones
            sup.add_position("AAPL", entry_price=150.0, qty=10.0, notional=1500.0, kelly_pct=0.10, entry_trade_id=101)
            sup.add_position("NVDA", entry_price=200.0, qty=5.0, notional=1000.0, kelly_pct=0.10, entry_trade_id=102)

            # Simular evolucion de precios en AAPL
            sup._positions["AAPL"].highest_price = 156.0
            sup._positions["AAPL"].breakeven_activated = True
            sup._positions["AAPL"]._sl_floor = 150.0

            # Guardar estado a disco
            sup._save_state()
            assert os.path.exists(state_file)

            # Crear nueva instancia de supervisor y cargar estado
            sup2 = PositionSupervisor(order_manager=mock_orders, trade_logger=mock_trade_log)
            loaded_state = sup2._load_state()

            assert "AAPL" in loaded_state
            assert loaded_state["AAPL"]["highest_price"] == 156.0
            assert loaded_state["AAPL"]["breakeven_activated"] is True
            assert loaded_state["AAPL"]["_sl_floor"] == 150.0
            assert loaded_state["AAPL"]["entry_trade_id"] == 101

            assert "NVDA" in loaded_state
            assert loaded_state["NVDA"]["highest_price"] == 200.0
            assert loaded_state["NVDA"]["breakeven_activated"] is False


@pytest.mark.asyncio
async def test_supervisor_stop_position_selective():
    """Valida que stop_position cancele solo la tarea del simbolo especificado."""
    mock_orders = MagicMock()
    mock_trade_log = MagicMock()
    sup = PositionSupervisor(order_manager=mock_orders, trade_logger=mock_trade_log)

    # Registrar posiciones
    rec_aapl = sup.add_position("AAPL", 150.0, 10.0, notional=1500.0, kelly_pct=0.10)
    rec_msft = sup.add_position("MSFT", 300.0, 5.0, notional=1500.0, kelly_pct=0.10)

    # Simular tareas asyncio corriendo
    async def dummy_loop():
        while True:
            await asyncio.sleep(0.1)

    rec_aapl.task = asyncio.create_task(dummy_loop())
    rec_msft.task = asyncio.create_task(dummy_loop())

    # Detener solo AAPL
    sup.stop_position("AAPL")

    await asyncio.sleep(0.01)

    assert rec_aapl.task.cancelled()
    assert not rec_msft.task.cancelled()

    # Limpieza
    rec_msft.task.cancel()


# ============================================================================
# 4. Pruebas de Market Hours Awareness y TP/SL Dinámico ATR
# ============================================================================

def test_is_market_open_now_logic():
    from datetime import datetime
    import zoneinfo
    from execution.supervisor import is_market_open_now

    tz_est = zoneinfo.ZoneInfo("America/New_York")
    # Martes 11:30 AM EST -> Abierto
    dt_open = datetime(2026, 10, 27, 11, 30, tzinfo=tz_est)
    assert is_market_open_now(dt_open) is True

    # Martes 08:30 AM EST -> Cerrado (pre-market)
    dt_pre = datetime(2026, 10, 27, 8, 30, tzinfo=tz_est)
    assert is_market_open_now(dt_pre) is False

    # Martes 17:00 PM EST -> Cerrado (post-market)
    dt_post = datetime(2026, 10, 27, 17, 0, tzinfo=tz_est)
    assert is_market_open_now(dt_post) is False

    # Domingo 12:00 PM EST -> Cerrado (fin de semana)
    dt_weekend = datetime(2026, 10, 25, 12, 0, tzinfo=tz_est)
    assert is_market_open_now(dt_weekend) is False


def test_dynamic_atr_tpsl_calculation_bounds():
    """Valida el cálculo de TP y SL dinámico por ATR con pisos y techos."""
    mock_orders = MagicMock()
    mock_trade_log = MagicMock()
    sup = PositionSupervisor(order_manager=mock_orders, trade_logger=mock_trade_log)

    # 1. Activo muy estable (KO): entry=100.0, ATR=1.0 (1%)
    # TP sin techo = 1.0 * 1.5 = 1.5% -> debe aplicarse el PISO de 2.5% (0.025)
    # SL sin piso  = 1.0 * 1.2 = 1.2% -> debe aplicarse el PISO de 2.0% (0.020)
    rec_stable = sup.add_position("KO", entry_price=100.0, qty=10.0, notional=1000.0, kelly_pct=0.10, atr_at_entry=1.0)
    assert rec_stable.tp_pct == config.EQUITY_MIN_TP_PCT
    assert rec_stable.sl_pct == config.EQUITY_MIN_SL_PCT
    assert rec_stable.target_tp == 100.0 * 1.025
    assert rec_stable.target_sl == 100.0 * (1.0 - 0.020)

    # 2. Activo muy volátil (NVDA): entry=100.0, ATR=5.0 (5%)
    # TP sin techo = 5.0 * 1.5 = 7.5% -> debe aplicarse el TECHO de 6.0% (0.060)
    # SL sin techo = 5.0 * 1.2 = 6.0% -> debe aplicarse el TECHO de 5.0% (0.050)
    rec_vol = sup.add_position("NVDA", entry_price=100.0, qty=10.0, notional=1000.0, kelly_pct=0.10, atr_at_entry=5.0)
    assert rec_vol.tp_pct == config.EQUITY_MAX_TP_PCT
    assert rec_vol.sl_pct == config.EQUITY_MAX_SL_PCT
    assert rec_vol.target_tp == 100.0 * 1.060
    assert rec_vol.target_sl == 100.0 * (1.0 - 0.050)


# ============================================================================
# 5. Pruebas de Circuit Breaker, Fill Synchronization y Spread Control
# ============================================================================

def test_wait_for_order_fill_success():
    mgr = OrderManager("dummy_key", "dummy_secret")
    mock_order = MagicMock()
    mock_order.status = "filled"
    mock_order.symbol = "AAPL"
    mock_order.filled_avg_price = "150.25"
    mock_order.filled_qty = "10.0"
    mgr._client.get_order_by_id = MagicMock(return_value=mock_order)

    filled = mgr.wait_for_order_fill("ord_123", timeout=1.0)
    assert filled == mock_order
    assert float(filled.filled_avg_price) == 150.25


def test_get_spread_pct():
    mgr = OrderManager("dummy_key", "dummy_secret")
    mock_quote = MagicMock()
    mock_quote.ask_price = 100.20
    mock_quote.bid_price = 100.00
    mgr._data_client.get_stock_latest_quote = MagicMock(return_value={"AAPL": mock_quote})

    spread = mgr.get_spread_pct("AAPL")
    # mid = 100.10, spread = 0.20 / 100.10 ≈ 0.001998
    assert 0.0019 < spread < 0.0021


def test_circuit_breaker_blocks_on_consecutive_losses():
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db_path = tmp.name

    with patch("execution.orders.DB_PATH", db_path):
        # Crear tabla y simular 3 pérdidas consecutivas
        import sqlite3
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE trades (id INTEGER PRIMARY KEY, date TEXT, ticker TEXT, "
                "trade_type TEXT, pnl REAL, pnl_pct REAL, notional REAL)"
            )
            conn.execute("INSERT INTO trades VALUES (1, '2026-10-20', 'AAPL', 'SELL_SL', -50.0, -0.04, 1250.0)")
            conn.execute("INSERT INTO trades VALUES (2, '2026-10-21', 'MSFT', 'SELL_SL', -40.0, -0.04, 1000.0)")
            conn.execute("INSERT INTO trades VALUES (3, '2026-10-22', 'NVDA', 'SELL_SL', -60.0, -0.04, 1500.0)")

        mgr = OrderManager("dummy_key", "dummy_secret")
        mgr.get_account_equity = MagicMock(return_value=10_000.0)
        mgr.get_open_positions = MagicMock(return_value=[])

        allowed, reason = mgr.can_open_position("AMZN", 500.0, 0.10)
        assert not allowed
        assert "Circuit Breaker ACTIVO" in reason
        assert "perdidas consecutivas" in reason


# ============================================================================
# 6. Pruebas de Analítica Cuantitativa y Filtro de Earnings
# ============================================================================

def test_earnings_filter_resilience():
    from signals.earnings import EarningsCalendarFilter
    filt = EarningsCalendarFilter()
    # No debe arrojar excepcion y retorna bool
    result = filt.has_upcoming_earnings("AAPL")
    assert isinstance(result, bool)


def test_trade_logger_quantitative_metrics():
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db_path = tmp.name

    from logging_.trade_log import TradeLogger
    tl = TradeLogger(db_path=db_path)

    # Insertar 3 ganadores y 1 perdedor
    # Trade 1: +100 USD (+5%)
    # Trade 2: +60 USD (+3%)
    # Trade 3: +40 USD (+2%)
    # Trade 4: -50 USD (-2.5%)
    # Gross profit = 200, Gross loss = 50 -> Profit Factor = 4.0
    # Net PnL = 150, Win Rate = 75%
    tl.log_exit({
        "ticker": "AAPL", "notional": 2000.0, "exit_price": 105.0,
        "entry_price": 100.0, "qty": 20.0, "pnl": 100.0, "pnl_pct": 0.05,
        "trade_type": "SELL_TP",
    })
    tl.log_exit({
        "ticker": "MSFT", "notional": 2000.0, "exit_price": 103.0,
        "entry_price": 100.0, "qty": 20.0, "pnl": 60.0, "pnl_pct": 0.03,
        "trade_type": "SELL_TP",
    })
    tl.log_exit({
        "ticker": "NVDA", "notional": 2000.0, "exit_price": 102.0,
        "entry_price": 100.0, "qty": 20.0, "pnl": 40.0, "pnl_pct": 0.02,
        "trade_type": "SELL_TP",
    })
    tl.log_exit({
        "ticker": "AMZN", "notional": 2000.0, "exit_price": 97.5,
        "entry_price": 100.0, "qty": 20.0, "pnl": -50.0, "pnl_pct": -0.025,
        "trade_type": "SELL_SL",
    })

    metrics = tl.get_quantitative_metrics()
    assert metrics["total_trades"] == 4
    assert metrics["winning_trades"] == 3
    assert metrics["losing_trades"] == 1
    assert metrics["win_rate"] == 0.75
    assert metrics["profit_factor"] == 4.0
    assert metrics["net_pnl_usd"] == 150.0
    assert metrics["gross_profit_usd"] == 200.0
    assert metrics["gross_loss_usd"] == 50.0
    assert metrics["sharpe_ratio"] > 0
    assert metrics["sortino_ratio"] > 0
    assert metrics["expectancy_usd"] > 0

