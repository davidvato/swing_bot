"""
tests/test_reconcile_and_risk.py — Tests para reconciliación y control de riesgo cripto
======================================================================================
"""

import tempfile
import sqlite3
from unittest.mock import MagicMock
import pytest

from execution.crypto_orders import CryptoOrderManager
from execution.crypto_supervisor import CryptoPositionSupervisor
from logging_.reconcile import AlpacaReconciler


def test_can_open_position_existing_blocked():
    mgr = CryptoOrderManager("dummy_key", "dummy_secret")
    mgr.get_account_equity = MagicMock(return_value=100000.0)

    # Mock open position in Alpaca
    mock_pos = MagicMock()
    mock_pos.symbol = "BTCUSD"
    mock_pos.qty = "0.5"
    mock_pos.market_value = "35000.0"
    mgr.get_open_positions = MagicMock(return_value=[mock_pos])

    allowed, reason = mgr.can_open_position("BTC/USD", 4000.0, 0.05)
    assert not allowed
    assert "Posicion activa existente" in reason


def test_can_open_position_oversized_blocked():
    mgr = CryptoOrderManager("dummy_key", "dummy_secret")
    mgr.get_account_equity = MagicMock(return_value=100000.0)
    mgr.get_open_positions = MagicMock(return_value=[])

    # 5% of 100k is 5k. Trying 15k should be blocked
    allowed, reason = mgr.can_open_position("ETH/USD", 15000.0, 0.05)
    assert not allowed
    assert "excede el techo" in reason


def test_can_open_position_allowed():
    mgr = CryptoOrderManager("dummy_key", "dummy_secret")
    mgr.get_account_equity = MagicMock(return_value=100000.0)
    mgr.get_open_positions = MagicMock(return_value=[])

    allowed, reason = mgr.can_open_position("ETH/USD", 4500.0, 0.05)
    assert allowed
    assert reason == "OK"


def test_crypto_rehydrate_positions_filters_dust():
    mgr = MagicMock()
    # 1 non-trivial position, 1 dust
    pos_active = MagicMock()
    pos_active.symbol = "SOLUSD"
    pos_active.avg_entry_price = "100.0"
    pos_active.qty = "10.0"
    pos_active.market_value = "1000.0"

    mgr.get_open_positions = MagicMock(return_value=[pos_active])

    tl = MagicMock()
    sup = CryptoPositionSupervisor(mgr, tl)

    count = sup.rehydrate_positions()
    assert count == 1
    assert sup.is_position_active("SOL/USD")
    assert sup.is_position_active("SOLUSD")


def test_reconciler_idempotent_sqlite():
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db_path = tmp.name

    reconciler = AlpacaReconciler("dummy", "dummy", db_path=db_path, paper=True)

    # Sample mock orders
    mock_orders = [
        {
            "id": "1",
            "symbol": "NVDA",
            "side": "buy",
            "filled_qty": "10.0",
            "filled_avg_price": "200.0",
            "notional": "2000.0",
            "created_at": "2026-08-31T13:30:00Z"
        },
        {
            "id": "2",
            "symbol": "NVDA",
            "side": "sell",
            "filled_qty": "10.0",
            "filled_avg_price": "220.0",
            "created_at": "2026-09-04T18:30:00Z"
        }
    ]

    reconciler.fetch_all_orders = MagicMock(return_value=mock_orders)

    res1 = reconciler.sync()
    assert res1["equities_inserted"] == 2

    # Second run must insert 0
    res2 = reconciler.sync()
    assert res2["equities_inserted"] == 0

    # Verify rows in DB
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM trades")
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT pnl, trade_type FROM trades WHERE trade_type = 'SELL_TP'")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 200.0  # (220 - 200) * 10
