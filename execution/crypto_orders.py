"""
execution/crypto_orders.py — Gestor de Ordenes para Criptomonedas
=================================================================
Encapsula las operaciones de compra y venta de activos cripto usando
el TradingClient de alpaca-py con soporte para pares cripto.

Diferencias clave vs execution/orders.py (equities):
  - Los pares cripto usan formato 'BTC/USD' (con slash).
  - Las ordenes cripto son notionales por defecto (no qty fija).
  - No hay restriccion de horario (cripto opera 24/7).
  - El mercado de cripto siempre esta 'abierto' para Alpaca.
"""

import logging
from typing import Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

logger = logging.getLogger(__name__)


class CryptoOrderManager:
    """
    Gestor de ordenes de mercado para criptomonedas via Alpaca.

    Reutiliza el TradingClient del bot (paper=True) usando los
    endpoints de cripto de Alpaca.
    """

    def __init__(self, api_key: str, secret_key: str) -> None:
        """
        Inicializa el gestor de ordenes cripto.

        Args:
            api_key: Clave publica de Alpaca.
            secret_key: Clave secreta de Alpaca.
        """
        self._client = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)
        logger.info("CryptoOrderManager inicializado (paper=True).")

    def get_account_equity(self) -> float:
        """Retorna el equity total de la cuenta Alpaca (compartido con equities)."""
        try:
            account = self._client.get_account()
            return float(account.equity)
        except Exception as exc:
            logger.error(f"[CRYPTO] Error obteniendo equity: {exc}")
            return 0.0

    def submit_buy(self, symbol: str, notional: float) -> Optional[object]:
        """
        Envía una orden de compra notional para un par cripto.

        Los pares cripto en Alpaca usan 'BTC/USD' como symbol y
        admiten ordenes notionales (en USD) para fracciones.

        Args:
            symbol: Par cripto Alpaca (ej. 'BTC/USD').
            notional: Importe en USD a invertir.

        Returns:
            Objeto Order de Alpaca, o None si falla.
        """
        try:
            # Alpaca cripto usa el symbol sin slash para OrderRequest: 'BTCUSD'
            alpaca_symbol = symbol.replace("/", "")
            request = MarketOrderRequest(
                symbol=alpaca_symbol,
                notional=round(notional, 2),
                side=OrderSide.BUY,
                time_in_force=TimeInForce.GTC,
            )
            order = self._client.submit_order(request)
            logger.info(
                f"[CRYPTO BUY] {symbol}: ${notional:.2f} nocional | "
                f"Order ID: {order.id}"
            )
            return order
        except Exception as exc:
            logger.error(f"[CRYPTO BUY] {symbol}: Error enviando orden. {exc}")
            return None

    def submit_sell(self, symbol: str, qty: float) -> Optional[object]:
        """
        Envía una orden de venta por cantidad para un par cripto.

        Args:
            symbol: Par cripto Alpaca (ej. 'BTC/USD').
            qty: Cantidad de tokens a vender.

        Returns:
            Objeto Order de Alpaca, o None si falla.
        """
        try:
            alpaca_symbol = symbol.replace("/", "")
            request = MarketOrderRequest(
                symbol=alpaca_symbol,
                qty=round(qty, 8),   # Cripto admite hasta 8 decimales
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
            )
            order = self._client.submit_order(request)
            logger.info(
                f"[CRYPTO SELL] {symbol}: qty={qty:.8f} | "
                f"Order ID: {order.id}"
            )
            return order
        except Exception as exc:
            logger.error(f"[CRYPTO SELL] {symbol}: Error enviando orden. {exc}")
            return None

    def get_latest_quote(self, symbol: str) -> float:
        """
        Obtiene el precio de cierre mas reciente de un par cripto
        usando las posiciones abiertas en Alpaca.

        Args:
            symbol: Par cripto Alpaca (ej. 'BTC/USD').

        Returns:
            Precio actual del activo, o 0.0 si no hay posicion o falla.
        """
        try:
            alpaca_symbol = symbol.replace("/", "")
            position = self._client.get_open_position(alpaca_symbol)
            return float(position.current_price)
        except Exception:
            return 0.0

    def get_open_positions(self) -> list:
        """Retorna todas las posiciones cripto abiertas en Alpaca."""
        try:
            all_positions = self._client.get_all_positions()
            # Filtrar solo posiciones cripto (symbol contiene 'USD' y no es equity)
            # Alpaca distingue por asset_class
            crypto_positions = [
                p for p in all_positions
                if hasattr(p, "asset_class") and str(p.asset_class) == "AssetClass.CRYPTO"
            ]
            return crypto_positions
        except Exception as exc:
            logger.error(f"[CRYPTO] Error obteniendo posiciones: {exc}")
            return []
