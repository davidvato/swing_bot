"""
execution/orders.py — Gestor de Ordenes con Alpaca TradingClient
=================================================================
Encapsula toda la interaccion de ordenes con la API de Alpaca.

Restricciones criticas implementadas:
- paper=True incondicionalmente en TradingClient.
- Las compras usan EXCLUSIVAMENTE el parametro 'notional' (sin 'qty').
- Las ventas usan EXCLUSIVAMENTE el parametro 'qty' (sin 'notional').
- Decorador @rate_limited previene errores HTTP 429.
- close_all_positions(cancel_orders=True) es obligatorio para evitar
  la condicion de carrera "Insufficient qty" en el cierre semanal.
"""

import time
import logging
import functools
from typing import Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetAssetsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass
from alpaca.trading.models import Order, Position, TradeAccount

from config import REQUEST_INTERVAL_SEC, MAX_RETRY_ATTEMPTS, USE_TEST_BUDGET, TEST_BUDGET_USD

logger = logging.getLogger(__name__)


def rate_limited(func):
    """
    Decorador que aplica pausa minima entre llamadas a la API
    y reintenta con backoff exponencial ante errores HTTP 429.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        wait = 1.0
        for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
            try:
                time.sleep(REQUEST_INTERVAL_SEC)
                return func(*args, **kwargs)
            except Exception as exc:
                error_str = str(exc).lower()
                if "429" in error_str or "too many requests" in error_str:
                    logger.warning(
                        f"[{func.__name__}] HTTP 429 (intento {attempt}/{MAX_RETRY_ATTEMPTS}). "
                        f"Esperando {wait:.1f}s..."
                    )
                    time.sleep(wait)
                    wait *= 2
                    if attempt == MAX_RETRY_ATTEMPTS:
                        raise RuntimeError(
                            f"[{func.__name__}] Se agotaron {MAX_RETRY_ATTEMPTS} "
                            "reintentos por HTTP 429."
                        ) from exc
                else:
                    raise
    return wrapper


class OrderManager:
    """
    Gestor de ordenes de mercado sobre Alpaca Paper Trading.

    Toda la interaccion con el broker pasa por esta clase.
    La flag paper=True es incondicional e inmutable.
    """

    def __init__(self, api_key: str, secret_key: str) -> None:
        """
        Inicializa el TradingClient en modo paper trading exclusivamente.

        Args:
            api_key: Clave publica de Alpaca (desde .env).
            secret_key: Clave secreta de Alpaca (desde .env).
        """
        # paper=True es INCONDICIONALMENTE requerido por el sistema
        self._client = TradingClient(
            api_key=api_key,
            secret_key=secret_key,
            paper=True,  # NUNCA cambiar a False sin autorizacion explicita del usuario
        )
        logger.info("OrderManager inicializado en modo PAPER TRADING (paper=True).")

    @rate_limited
    def get_account(self) -> TradeAccount:
        """Retorna el objeto de cuenta de Alpaca con todos sus atributos."""
        return self._client.get_account()

    def get_account_equity(self) -> float:
        """
        Retorna el equity actual de la cuenta en dolares.

        El equity incluye el efectivo disponible mas el valor de mercado
        de todas las posiciones abiertas.

        Returns:
            Equity de la cuenta en USD como float.
        """
        if USE_TEST_BUDGET:
            logger.info(f"MODO PRUEBA ACTIVADO: Usando equity simulado de ${TEST_BUDGET_USD}")
            return TEST_BUDGET_USD

        account = self.get_account()
        equity = float(account.equity)
        logger.info(f"Equity de cuenta: ${equity:,.2f}")
        return equity

    @rate_limited
    def submit_buy(self, symbol: str, notional: float) -> Optional[Order]:
        """
        Envia una orden de compra de mercado usando importe nocional (dolares).

        IMPORTANTE: Esta funcion usa EXCLUSIVAMENTE el parametro 'notional'.
        El parametro 'qty' es deliberadamente omitido para evitar errores HTTP 400
        con el sistema de fractional shares de Alpaca.

        Args:
            symbol: Simbolo bursatil (e.g., "AAPL").
            notional: Importe en dolares a invertir (e.g., 1500.75).

        Returns:
            Objeto Order de Alpaca si la orden es aceptada, None si hay error.
        """
        if notional <= 0:
            logger.warning(
                f"[{symbol}] Notional invalido: ${notional:.2f}. Orden no enviada."
            )
            return None

        order_request = MarketOrderRequest(
            symbol=symbol,
            notional=round(notional, 2),   # USD con 2 decimales
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            # qty=...  ← OMITIDO INTENCIONALMENTE (causa HTTP 400 si se incluye con notional)
        )

        order = self._client.submit_order(order_data=order_request)
        logger.info(
            f"[BUY] {symbol}: ${notional:,.2f} nocional -> "
            f"Order ID: {order.id} | Status: {order.status}"
        )
        return order

    @rate_limited
    def submit_sell(self, symbol: str, qty: float) -> Optional[Order]:
        """
        Envia una orden de venta de mercado para liquidar una posicion.

        Usa el parametro 'qty' (cantidad de acciones) en lugar de 'notional'
        porque al vender se conoce la cantidad exacta de acciones en posesion.

        Args:
            symbol: Simbolo bursatil a vender.
            qty: Cantidad de acciones a vender (puede ser fraccionaria).

        Returns:
            Objeto Order de Alpaca si la orden es aceptada, None si hay error.
        """
        if qty <= 0:
            logger.warning(
                f"[{symbol}] Cantidad invalida: {qty}. Orden de venta no enviada."
            )
            return None

        order_request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            # notional=...  ← OMITIDO INTENCIONALMENTE (no aplica en ordenes de venta por qty)
        )

        order = self._client.submit_order(order_data=order_request)
        logger.info(
            f"[SELL] {symbol}: {qty} acciones -> "
            f"Order ID: {order.id} | Status: {order.status}"
        )
        return order

    @rate_limited
    def get_open_positions(self) -> list[Position]:
        """
        Retorna las posiciones abiertas de EQUITY (us_equity) en la cuenta.

        Excluye explícitamente posiciones cripto para evitar que el supervisor
        de equities intente cerrarlas con órdenes de bolsa inválidas para cripto.

        Returns:
            Lista de objetos Position de Alpaca (solo us_equity).
        """
        all_positions = self._client.get_all_positions()
        # Filtrar solo equities — cripto tiene asset_class == 'crypto'
        positions = [
            p for p in all_positions
            if str(getattr(p, "asset_class", "us_equity")) != "AssetClass.CRYPTO"
            and str(getattr(p, "asset_class", "us_equity")) != "crypto"
        ]
        logger.debug(
            f"Posiciones abiertas: {len(positions)} equity "
            f"(total Alpaca: {len(all_positions)})"
        )
        return positions

    @rate_limited
    def get_latest_quote(self, symbol: str) -> float:
        """
        Obtiene el precio mas reciente de un simbolo para el supervisor TP/SL.

        Usa el precio de cierre de la ultima barra disponible como proxy
        del precio actual (adecuado para supervision con intervalo de 60s).

        Args:
            symbol: Simbolo bursatil.

        Returns:
            Precio actual de mercado como float.
        """
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestQuoteRequest

        # Reusar credenciales del trading client no es directo;
        # se usa get_all_positions para obtener el precio actual de la posicion
        positions = self._client.get_all_positions()
        for pos in positions:
            if pos.symbol == symbol:
                current_price = float(pos.current_price)
                logger.debug(f"[{symbol}] Precio actual: ${current_price:.2f}")
                return current_price

        # Si no hay posicion abierta, retornar 0 (posicion ya cerrada)
        logger.warning(
            f"[{symbol}] No se encontro posicion abierta al consultar precio."
        )
        return 0.0

    def close_all(self) -> None:
        """
        Cierra TODAS las posiciones abiertas y cancela TODAS las ordenes pendientes.

        El uso de cancel_orders=True es IMPERATIVO para evitar la condicion de
        carrera 'Insufficient qty available for order' causada por ordenes latentes
        que intentan ejecutarse sobre acciones que ya estan siendo liquidadas.

        Este metodo es llamado exclusivamente por el planificador de viernes.
        """
        logger.warning(
            "LIQUIDACION TOTAL: Cerrando todas las posiciones y cancelando "
            "todas las ordenes pendientes..."
        )
        try:
            # cancel_orders=True: cancela ordenes ANTES de cerrar posiciones
            self._client.close_all_positions(cancel_orders=True)
            logger.info(
                "LIQUIDACION TOTAL: Todas las posiciones cerradas exitosamente."
            )
        except Exception as exc:
            logger.error(
                f"Error durante la liquidacion total: {exc}. "
                "Verificar manualmente el estado de la cuenta en Alpaca Dashboard."
            )
            raise
