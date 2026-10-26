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
