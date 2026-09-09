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

ORDENES BRACKET (OCO):
  submit_bracket_buy() envia una sola orden con tres legs:
    1. Entrada: Limit Buy ligeramente por encima del precio actual.
    2. TP leg:  Limit Sell al precio de Take-Profit calculado por ATR.
    3. SL leg:  Stop Sell al precio de Stop-Loss calculado por ATR.
  Alpaca vigila el precio en sus servidores 24/7, eliminando la
  dependencia del supervisor local de polling cada 5 minutos.
"""

import logging
from typing import Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
    TakeProfitRequest,
    StopLossRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce

from config import CRYPTO_MAX_POSITION_PCT

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

    def submit_bracket_buy(
        self,
        symbol: str,
        qty: float,
        limit_price: float,
        take_profit_price: float,
        stop_loss_price: float,
    ) -> Optional[object]:
        """
        Envia una orden Bracket (OCO) para un par cripto.

        Alpaca gestiona automaticamente los legs de TP y SL en sus servidores,
        eliminando la necesidad de supervision local por polling.

        La orden se compone de:
          - Entrada: Limit Buy a `limit_price` (ligeramente sobre el mercado).
          - TP leg:  Limit Sell a `take_profit_price`.
          - SL leg:  Stop Sell a `stop_loss_price`.

        Args:
            symbol: Par cripto Alpaca (ej. 'BTC/USD').
            qty: Cantidad exacta de tokens a comprar.
            limit_price: Precio limite de la orden de entrada (USD).
            take_profit_price: Precio objetivo de Take-Profit (USD).
            stop_loss_price: Precio de Stop-Loss (USD).

        Returns:
            Objeto Order de Alpaca con los legs adjuntos, o None si falla.
        """
        try:
            alpaca_symbol = symbol.replace("/", "")
            request = LimitOrderRequest(
                symbol=alpaca_symbol,
                qty=round(qty, 8),
                side=OrderSide.BUY,
                time_in_force=TimeInForce.GTC,
                limit_price=round(limit_price, 8),
                take_profit=TakeProfitRequest(
                    limit_price=round(take_profit_price, 8)
                ),
                stop_loss=StopLossRequest(
                    stop_price=round(stop_loss_price, 8)
                ),
            )
            order = self._client.submit_order(request)
            logger.info(
                f"[CRYPTO BRACKET BUY] {symbol}: qty={qty:.8f} | "
                f"Entry Limit=${limit_price:.6f} | "
                f"TP=${take_profit_price:.6f} | "
                f"SL=${stop_loss_price:.6f} | "
                f"Order ID: {order.id}"
            )
            return order
        except Exception as exc:
            logger.error(
                f"[CRYPTO BRACKET BUY] {symbol}: Error enviando orden bracket. {exc}. "
                "Verifique que la cuenta tenga saldo suficiente y que los precios sean validos."
            )
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

    def get_open_positions(self, include_dust: bool = False) -> list:
        """
        Retorna todas las posiciones cripto abiertas en Alpaca.

        Args:
            include_dust: Si False, ignora residuos con valor < $1 USD o qty trivial.
        """
        try:
            all_positions = self._client.get_all_positions()
            crypto_positions = []
            for p in all_positions:
                sym = p.symbol
                is_crypto = (
                    (hasattr(p, "asset_class") and "crypto" in str(p.asset_class).lower())
                    or "/" in sym
                    or (sym.endswith("USD") and sym not in ("AAPL", "MSFT", "NVDA", "GOOG", "AMZN", "META", "TSLA", "AVGO", "PLTR", "AMD", "SPY"))
                )
                if not is_crypto:
                    continue

                if not include_dust:
                    market_val = abs(float(p.market_value)) if p.market_value else 0.0
                    qty = abs(float(p.qty)) if p.qty else 0.0
                    if market_val < 1.0 or qty < 1e-6:
                        continue  # Filtrar polvo residual (dust)

                crypto_positions.append(p)
            return crypto_positions
        except Exception as exc:
            logger.error(f"[CRYPTO] Error obteniendo posiciones: {exc}")
            return []

    def can_open_position(
        self,
        symbol: str,
        notional: float,
        max_pct: float = CRYPTO_MAX_POSITION_PCT,
    ) -> tuple[bool, str]:
        """
        Valida rigurosamente los controles de riesgo cuantitativo antes de abrir posición:
        1. Comprueba si el símbolo ya tiene una posición viva no trivial en Alpaca.
        2. Comprueba que el nocional solicitado no exceda el límite del portafolio (5% del equity).
        3. Previene la sobreacumulación (overexposure/portfolio heat) como la ocurrida en LINK/USD.

        Args:
            symbol: Par cripto (ej. 'BTC/USD').
            notional: Importe nocional de la orden propuesta (USD).
            max_pct: Porcentaje máximo permitido por posición (default: 5%).

        Returns:
            Tupla (is_allowed: bool, reason: str).
        """
        alpaca_sym = symbol.replace("/", "")
        equity = self.get_account_equity()
        if equity <= 0:
            return False, "Equity no disponible o en cero en broker Alpaca."

        # 1. Verificar si ya existe posición en el broker
        open_positions = self.get_open_positions(include_dust=False)
        for pos in open_positions:
            if pos.symbol == alpaca_sym or pos.symbol == symbol:
                mkt_val = float(pos.market_value) if pos.market_value else 0.0
                return (
                    False,
                    f"Posicion activa existente en Alpaca: {pos.qty} tokens (~${mkt_val:.2f} USD). "
                    f"Regla de riesgo: prohibido promediar a la baja o sobreacumular.",
                )

        # 2. Verificar límite individual del % de equity
        max_allowed_notional = equity * max_pct * 1.02  # margen 2% por slippage
        if notional > max_allowed_notional:
            return (
                False,
                f"Nocional propuesto (${notional:.2f}) excede el techo del {max_pct*100:.1f}% "
                f"del equity (${equity * max_pct:.2f} USD).",
            )

        return True, "OK"
