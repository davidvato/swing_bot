"""
tests/test_orders.py — Pruebas unitarias para la construccion de ordenes
========================================================================
Valida que las ordenes de compra usen EXCLUSIVAMENTE 'notional' (sin qty)
y que las ordenes de venta usen EXCLUSIVAMENTE 'qty' (sin notional).
Esta distincion es critica para evitar errores HTTP 400 de Alpaca.

Nota: Estas pruebas validan la ESTRUCTURA de las solicitudes de orden
sin conectarse a la API de Alpaca (sin llamadas de red).
"""

import pytest

from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce


class TestBuyOrderConstruction:
    """Pruebas para la estructura de ordenes de compra."""

    def test_buy_order_uses_notional_not_qty(self):
        """
        Una orden de compra con importe nocional NO debe tener qty definido.
        Alpaca retorna HTTP 400 si se especifican AMBOS parametros.
        """
        order = MarketOrderRequest(
            symbol="AAPL",
            notional=1500.75,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        )

        # notional debe estar presente
        assert order.notional == 1500.75, (
            f"notional esperado=1500.75, obtenido={order.notional}"
        )
        # qty NO debe estar presente (debe ser None)
        assert order.qty is None, (
            f"qty debe ser None en orden con notional, obtenido={order.qty}"
        )

    def test_buy_order_side_is_buy(self):
        """El campo 'side' de una orden de compra debe ser OrderSide.BUY."""
        order = MarketOrderRequest(
            symbol="MSFT",
            notional=2000.00,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        )
        assert order.side == OrderSide.BUY, (
            f"Side esperado=BUY, obtenido={order.side}"
        )

    def test_buy_order_time_in_force_is_day(self):
        """El campo 'time_in_force' debe ser DAY (orden intradiaraia)."""
        order = MarketOrderRequest(
            symbol="NVDA",
            notional=3000.00,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        )
        assert order.time_in_force == TimeInForce.DAY, (
            f"TimeInForce esperado=DAY, obtenido={order.time_in_force}"
        )

    def test_buy_order_symbol_is_set(self):
        """El simbolo de la orden debe ser el ticker correcto."""
        ticker = "GOOG"
        order = MarketOrderRequest(
            symbol=ticker,
            notional=1000.00,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        )
        assert order.symbol == ticker

    def test_buy_order_notional_rounded_to_2_decimals(self):
        """El notional de compra debe estar correctamente redondeado."""
        notional = round(1500.123456, 2)
        order = MarketOrderRequest(
            symbol="AMD",
            notional=notional,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        )
        assert order.notional == 1500.12, (
            f"Notional esperado=1500.12 (2 decimales), obtenido={order.notional}"
        )

    def test_buy_order_with_multiple_tickers(self):
        """Verificar que cada ticker genera una orden independiente con su notional."""
        tickers = ["AAPL", "MSFT", "NVDA"]
        notional = 1500.00

        orders = [
            MarketOrderRequest(
                symbol=ticker,
                notional=notional,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
            for ticker in tickers
        ]

        for i, order in enumerate(orders):
            assert order.symbol == tickers[i]
            assert order.notional == notional
            assert order.qty is None


class TestSellOrderConstruction:
    """Pruebas para la estructura de ordenes de venta."""

    def test_sell_order_uses_qty_not_notional(self):
        """
        Una orden de venta debe usar 'qty' (cantidad de acciones)
        y NO 'notional'. Al vender, se conoce la cantidad exacta.
        """
        qty = 8.2191  # Acciones fraccionarias
        order = MarketOrderRequest(
            symbol="AAPL",
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )

        # qty debe estar presente
        assert order.qty == qty, (
            f"qty esperado={qty}, obtenido={order.qty}"
        )
        # notional NO debe estar presente (debe ser None)
        assert order.notional is None, (
            f"notional debe ser None en orden de venta por qty, obtenido={order.notional}"
        )

    def test_sell_order_side_is_sell(self):
        """El campo 'side' de una orden de venta debe ser OrderSide.SELL."""
        order = MarketOrderRequest(
            symbol="TSLA",
            qty=5.5,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        assert order.side == OrderSide.SELL

    def test_sell_order_fractional_qty_is_preserved(self):
        """Las cantidades fraccionarias deben preservarse exactamente."""
        fractional_qty = 0.123456
        order = MarketOrderRequest(
            symbol="PLTR",
            qty=fractional_qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        assert order.qty == fractional_qty

    def test_sell_order_time_in_force_is_day(self):
        """Las ordenes de venta del supervisor deben ser DAY."""
        order = MarketOrderRequest(
            symbol="META",
            qty=10.0,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        assert order.time_in_force == TimeInForce.DAY


class TestOrderMutualExclusivity:
    """Pruebas para garantizar la exclusividad mutua de notional y qty."""

    def test_buy_order_has_notional_none_for_qty(self):
        """
        Regla critica de Alpaca: en una orden notional, qty DEBE ser None.
        Incluir qty junto con notional genera HTTP 400.
        """
        order = MarketOrderRequest(
            symbol="AVGO",
            notional=5000.00,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        )
        assert order.qty is None

    def test_sell_order_has_notional_none(self):
        """
        En una orden de venta por qty, notional DEBE ser None.
        """
        order = MarketOrderRequest(
            symbol="AMD",
            qty=25.75,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        assert order.notional is None

    def test_notional_buy_parameters_summary(self):
        """
        Test de resumen: verifica todos los campos criticos de una orden de compra.
        Esta es la orden que el sistema enviara realmente a Alpaca.
        """
        symbol = "AAPL"
        notional = 15_000.00

        order = MarketOrderRequest(
            symbol=symbol,
            notional=notional,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            # qty=...  ← DEBE ESTAR AUSENTE
        )

        # Validacion completa de la orden
        assert order.symbol == symbol
        assert order.notional == notional
        assert order.side == OrderSide.BUY
        assert order.time_in_force == TimeInForce.DAY
        assert order.qty is None, "CRITICO: qty debe ser None en orden notional"
