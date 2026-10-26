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
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

from config import (
    REQUEST_INTERVAL_SEC,
    MAX_RETRY_ATTEMPTS,
    USE_TEST_BUDGET,
    TEST_BUDGET_USD,
    EQUITY_MAX_PORTFOLIO_HEAT,
    EQUITY_MAX_OPEN_POSITIONS,
)

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
        Inicializa el TradingClient en modo paper trading exclusivamente
        y el StockHistoricalDataClient para cotizaciones de mercado en tiempo real.

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
        self._data_client = StockHistoricalDataClient(
            api_key=api_key,
            secret_key=secret_key,
        )
        logger.info(
            "OrderManager inicializado en modo PAPER TRADING (paper=True) con feeds de cotización en tiempo real."
        )

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
    def close_position(self, symbol: str) -> Optional[Order]:
        """
        Cierra completamente una posicion de equity abierta usando el endpoint
        nativo de Alpaca close_position.

        Elimina cualquier discrepancia de redondeo en acciones fraccionarias
        o errores de 'insufficient qty available'.

        Args:
            symbol: Simbolo bursatil a liquidar.

        Returns:
            Objeto Order si la posicion fue liquidada, None si ya estaba cerrada.
        """
        try:
            order = self._client.close_position(symbol_or_asset_id=symbol)
            logger.info(f"[CLOSE_POSITION] {symbol} liquidada exitosamente en Alpaca.")
            return order
        except Exception as exc:
            err_msg = str(exc).lower()
            if "position does not exist" in err_msg or "404" in err_msg:
                logger.info(f"[CLOSE_POSITION] {symbol} ya no existe en Alpaca (cerrada previamente).")
                return None
            logger.error(f"[CLOSE_POSITION] Error al cerrar posicion {symbol}: {exc}")
            raise

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

    def can_open_position(
        self,
        symbol: str,
        notional: float,
        max_position_pct: float,
        max_heat_pct: float = EQUITY_MAX_PORTFOLIO_HEAT,
        max_positions: int = EQUITY_MAX_OPEN_POSITIONS,
    ) -> tuple[bool, str]:
        """
        Evalua si la cuenta puede abrir una nueva posicion en Equities respetando:
        1. No duplicidad de ticker.
        2. Limite individual por posicion (% equity).
        3. Techo maximo de posiciones simultaneas (EQUITY_MAX_OPEN_POSITIONS).
        4. Limite de Portfolio Heat global (EQUITY_MAX_PORTFOLIO_HEAT).
        5. Disponibilidad de buying power / cash.

        Returns:
            Tupla (allowed: bool, reason: str).
        """
        try:
            equity = self.get_account_equity()
            if equity <= 0:
                return False, f"Equity no valido: ${equity}"

            # Regla 2: Techo individual
            max_notional_allowed = equity * max_position_pct * 1.05  # 5% de holgura por redondeo
            if notional > max_notional_allowed:
                return False, f"Notional ${notional:.2f} excede el techo permitido (${max_notional_allowed:.2f})"

            open_positions = self.get_open_positions()

            # Regla 1: No duplicar ticker
            for pos in open_positions:
                if pos.symbol == symbol:
                    return False, f"Posicion activa existente en {symbol}"

            # Regla 3: Limite de posiciones simultaneas
            if len(open_positions) >= max_positions:
                return False, f"Limite de posiciones simultaneas alcanzado ({len(open_positions)}/{max_positions})"

            # Regla 4: Portfolio Heat global
            current_heat_usd = sum(abs(float(p.market_value)) for p in open_positions)
            if (current_heat_usd + notional) > (equity * max_heat_pct):
                return False, (
                    f"Portfolio Heat excedido: actual=${current_heat_usd:.2f} + nueva=${notional:.2f} "
                    f"> max=${equity * max_heat_pct:.2f} ({max_heat_pct*100:.0f}%)"
                )

            # Regla 5: Buying power / cash real (si no esta en budget simulado)
            if not USE_TEST_BUDGET:
                acct = self.get_account()
                bp = float(acct.buying_power)
                if notional > bp:
                    return False, f"Buying power insuficiente: requiere ${notional:.2f}, disponible ${bp:.2f}"

            return True, "OK"
        except Exception as exc:
            logger.error(f"Error evaluando can_open_position para {symbol}: {exc}")
            return False, f"Error interno: {exc}"

    @rate_limited
    def get_latest_quote(self, symbol: str) -> float:
        """
        Obtiene el precio mas reciente en tiempo real para el supervisor TP/SL.

        Consulta el ultimo trade ejecutado en el mercado via StockHistoricalDataClient
        para evitar el retraso de 15 minutos de pos.current_price en Alpaca Paper Trading.
        Verifica previamente si la posicion sigue abierta en la cuenta.

        Args:
            symbol: Simbolo bursatil.

        Returns:
            Precio actual de mercado como float (0.0 si la posicion ya fue cerrada).
        """
        # 1. Verificar si la posicion sigue abierta en Alpaca
        try:
            pos = self._client.get_open_position(symbol)
        except Exception:
            # Posicion no existe o ya fue cerrada
            logger.info(f"[{symbol}] Posicion no encontrada en Alpaca (cerrada o inexistente).")
            return 0.0

        # 2. Consultar precio en tiempo real del mercado (latest trade)
        try:
            req = StockLatestTradeRequest(symbol_or_symbols=symbol)
            res = self._data_client.get_stock_latest_trade(req)
            if symbol in res and res[symbol].price > 0:
                price = float(res[symbol].price)
                logger.debug(f"[{symbol}] Precio tiempo real (latest trade): ${price:.2f}")
                return price
        except Exception as exc:
            logger.warning(
                f"[{symbol}] Error consultando latest trade: {exc}. Usando fallback de posicion."
            )

        # 3. Fallback: precio registrado en el objeto Position
        return float(pos.current_price) if pos and pos.current_price else 0.0

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
