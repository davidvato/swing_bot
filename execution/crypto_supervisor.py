"""
execution/crypto_supervisor.py — Supervisor de Posiciones Cripto (Monitor)
=========================================================================
Monitorea posiciones cripto ABIERTAS en Alpaca para detectar cuándo el broker
ejecuto automaticamente el TP o el SL (via ordenes Bracket enviadas por
crypto_orders.submit_bracket_buy).

ROL ACTUAL (Bracket Order Mode):
  - Ya NO emula TP/SL localmente (eso lo hace Alpaca en sus servidores).
  - Monitorea el estado de la posicion en Alpaca cada CRYPTO_POLL_INTERVAL_SEC.
  - Cuando la posicion desaparece (fue cerrada por TP o SL del broker),
    consulta la orden de venta ejecutada para obtener el precio de salida real
    y registra la operacion en el trade log (SQLite + Telegram).
  - Si se supera CRYPTO_MAX_HOLD_HOURS sin que el broker haya cerrado la
    posicion: cancela las legs OCO pendientes y envía una venta a mercado
    como fallback de tiempo.

DIFERENCIAS vs supervision.py (equities):
  - Opera 24/7 (sin restriccion de horario de mercado).
  - Tiempo maximo de holding medido en HORAS.
  - Poll interval: CRYPTO_POLL_INTERVAL_SEC (5 min).
  - El precio de salida real se obtiene de la orden ejecutada por Alpaca.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import numpy as np

from config import (
    CRYPTO_ATR_TP_MULT,
    CRYPTO_ATR_SL_MULT,
    CRYPTO_TAKE_PROFIT_PCT,
    CRYPTO_STOP_LOSS_PCT,
    CRYPTO_MAX_HOLD_HOURS,
    CRYPTO_POLL_INTERVAL_SEC,
    CRYPTO_DB_TABLE,
)

logger = logging.getLogger(__name__)


@dataclass
class CryptoPositionRecord:
    """
    Registro de una posicion cripto abierta bajo supervision.

    Attributes:
        symbol: Par de trading (ej. 'BTC/USD').
        entry_price: Precio de compra original.
        qty: Cantidad de tokens comprados.
        entry_time: Timestamp UTC de la compra.
        notional: Importe invertido en USD.
        kelly_pct: Fraccion Kelly aplicada.
        atr_at_entry: ATR-14 al momento de la compra (None si no disponible).
        bracket_order_id: ID de la orden bracket enviada a Alpaca.
        task: Referencia a la tarea asyncio del supervisor.
    """
    symbol: str
    entry_price: float
    qty: float
    entry_time: datetime
    notional: float
    kelly_pct: float
    atr_at_entry: Optional[float] = None
    bracket_order_id: Optional[str] = None
    task: Optional[asyncio.Task] = field(default=None, repr=False)

    @property
    def target_tp(self) -> float:
        """
        Precio objetivo de Take-Profit (referencia para logs).

        Si ATR disponible: entry + (ATR x CRYPTO_ATR_TP_MULT)
        Fallback:          entry x (1 + CRYPTO_TAKE_PROFIT_PCT)
        """
        if self.atr_at_entry and not np.isnan(self.atr_at_entry) and self.atr_at_entry > 0:
            return self.entry_price + (self.atr_at_entry * CRYPTO_ATR_TP_MULT)
        return self.entry_price * (1.0 + CRYPTO_TAKE_PROFIT_PCT)

    @property
    def target_sl(self) -> float:
        """
        Precio objetivo de Stop-Loss (referencia para logs).

        Si ATR disponible: entry - (ATR x CRYPTO_ATR_SL_MULT)
        Fallback:          entry x (1 - CRYPTO_STOP_LOSS_PCT)
        """
        if self.atr_at_entry and not np.isnan(self.atr_at_entry) and self.atr_at_entry > 0:
            return self.entry_price - (self.atr_at_entry * CRYPTO_ATR_SL_MULT)
        return self.entry_price * (1.0 - CRYPTO_STOP_LOSS_PCT)

    @property
    def hours_held(self) -> float:
        """Horas transcurridas desde la entrada."""
        now_utc = datetime.now(timezone.utc)
        return (now_utc - self.entry_time).total_seconds() / 3600.0

    @property
    def tp_pct(self) -> float:
        """Porcentaje de TP sobre el precio de entrada."""
        return (self.target_tp - self.entry_price) / self.entry_price

    @property
    def sl_pct(self) -> float:
        """Porcentaje de SL sobre el precio de entrada (negativo)."""
        return (self.target_sl - self.entry_price) / self.entry_price


class CryptoPositionSupervisor:
    """
    Supervisor asincrono 24/7 de posiciones cripto con ordenes Bracket.

    Monitorea en Alpaca si la posicion fue cerrada automaticamente por el
    broker (TP o SL), registra la salida en el log y maneja el timeout de
    CRYPTO_MAX_HOLD_HOURS como fallback de seguridad.
    """

    def __init__(self, order_manager, trade_logger) -> None:
        self._order_manager = order_manager
        self._trade_logger = trade_logger
        self._positions: dict[str, CryptoPositionRecord] = {}
        self._lock = asyncio.Lock()
        logger.info("CryptoPositionSupervisor inicializado (modo Bracket Order).")

    def add_position(
        self,
        symbol: str,
        entry_price: float,
        qty: float,
        notional: float,
        kelly_pct: float,
        atr_at_entry: Optional[float] = None,
        entry_time: Optional[datetime] = None,
        bracket_order_id: Optional[str] = None,
    ) -> CryptoPositionRecord:
        """
        Registra una posicion cripto para supervision.

        Args:
            symbol: Par cripto (ej. 'BTC/USD').
            entry_price: Precio de compra.
            qty: Cantidad de tokens.
            notional: Importe en USD.
            kelly_pct: Fraccion Kelly aplicada.
            atr_at_entry: ATR-14 al momento de la compra.
            entry_time: Timestamp UTC (default: ahora).
            bracket_order_id: ID de la orden bracket en Alpaca (para tracking).
        """
        if entry_time is None:
            entry_time = datetime.now(timezone.utc)

        record = CryptoPositionRecord(
            symbol=symbol,
            entry_price=entry_price,
            qty=qty,
            entry_time=entry_time,
            notional=notional,
            kelly_pct=kelly_pct,
            atr_at_entry=atr_at_entry,
            bracket_order_id=bracket_order_id,
        )
        self._positions[symbol] = record

        tp_mode = f"ATR×{CRYPTO_ATR_TP_MULT}" if (atr_at_entry and atr_at_entry > 0) else "fijo"
        logger.info(
            f"[CRYPTO {symbol}] Posicion registrada (Bracket): "
            f"entry=${entry_price:.4f}, qty={qty:.8f}, "
            f"TP=${record.target_tp:.4f} (+{record.tp_pct*100:.2f}%, modo={tp_mode}), "
            f"SL=${record.target_sl:.4f} ({record.sl_pct*100:.2f}%), "
            f"ATR={f'{atr_at_entry:.4f}' if atr_at_entry else 'N/A'}, "
            f"BracketID={bracket_order_id or 'N/A'}"
        )
        return record

    async def _supervise_position(self, record: CryptoPositionRecord) -> None:
        """
        Corutina de supervision 24/7 para una posicion cripto con Bracket Order.

        Espera a que Alpaca cierre la posicion automaticamente (TP o SL),
        luego recupera el precio de salida real de la orden ejecutada y
        registra la salida en el trade log.

        Si se supera el timeout CRYPTO_MAX_HOLD_HOURS, cancela las legs OCO
        y ejecuta una venta a mercado.
        """
        symbol = record.symbol
        logger.info(
            f"[CRYPTO {symbol}] Monitor iniciado (Bracket) → "
            f"TP=${record.target_tp:.4f} | SL=${record.target_sl:.4f} | "
            f"Max={CRYPTO_MAX_HOLD_HOURS}h | BracketID={record.bracket_order_id or 'N/A'}"
        )

        while True:
            try:
                await asyncio.sleep(CRYPTO_POLL_INTERVAL_SEC)

                hours_held = record.hours_held

                # ── Verificar si la posicion sigue abierta en Alpaca ──────────
                current_price = await asyncio.get_event_loop().run_in_executor(
                    None, self._order_manager.get_latest_quote, symbol
                )

                # get_latest_quote retorna 0.0 si la posicion ya no existe
                if current_price == 0.0:
                    logger.info(
                        f"[CRYPTO {symbol}] Posicion cerrada por Alpaca "
                        f"(TP o SL ejecutado automaticamente). "
                        f"Recuperando precio de salida..."
                    )
                    exit_price, exit_type = await self._get_broker_exit_info(record)
                    await self._log_exit(record, exit_price, exit_type)
                    break

                logger.debug(
                    f"[CRYPTO {symbol}] Posicion activa: precio=${current_price:.4f} | "
                    f"Horas={hours_held:.1f}/{CRYPTO_MAX_HOLD_HOURS}"
                )

                # ── Fallback de timeout: forzar cierre si supera max horas ───
                if hours_held >= CRYPTO_MAX_HOLD_HOURS:
                    logger.warning(
                        f"[CRYPTO {symbol}] TIMEOUT ({hours_held:.1f}h). "
                        f"Cancelando legs OCO y ejecutando venta a mercado..."
                    )
                    await self._execute_timeout_exit(record, current_price)
                    break

            except asyncio.CancelledError:
                logger.info(f"[CRYPTO {symbol}] Supervision cancelada.")
                break
            except Exception as exc:
                logger.error(
                    f"[CRYPTO {symbol}] Error en supervision: {exc}. "
                    "Reintentando en el siguiente ciclo..."
                )

    async def _get_broker_exit_info(
        self, record: CryptoPositionRecord
    ) -> tuple[float, str]:
        """
        Recupera el precio de salida real y el tipo de cierre desde las
        ordenes ejecutadas en Alpaca.

        Busca ordenes de venta (SELL) en estado 'filled' para el simbolo.
        Si no encuentra informacion, usa el precio de TP calculado como
        aproximacion y reporta como CRYPTO_SELL_TP.

        Returns:
            Tupla (exit_price, exit_type).
        """
        symbol = record.symbol
        alpaca_symbol = symbol.replace("/", "")

        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus, OrderSide as AlpacaSide

            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                symbols=[alpaca_symbol],
                limit=10,
            )
            orders = await asyncio.get_event_loop().run_in_executor(
                None, self._order_manager._client.get_orders, req
            )

            # Buscar la ultima orden de venta ejecutada para este simbolo
            for order in orders:
                if (
                    hasattr(order, "side")
                    and str(order.side) in ("OrderSide.SELL", "sell")
                    and hasattr(order, "filled_avg_price")
                    and order.filled_avg_price is not None
                    and float(order.filled_avg_price) > 0
                ):
                    exit_price = float(order.filled_avg_price)
                    # Determinar tipo por comparacion con precios referencia
                    if exit_price >= record.target_tp * 0.995:
                        exit_type = "CRYPTO_SELL_TP"
                    elif exit_price <= record.target_sl * 1.005:
                        exit_type = "CRYPTO_SELL_SL"
                    else:
                        exit_type = "CRYPTO_SELL_TP"  # Default: asumir TP

                    logger.info(
                        f"[CRYPTO {symbol}] Salida detectada desde orden broker: "
                        f"precio=${exit_price:.6f} | tipo={exit_type} | "
                        f"OrderID={order.id}"
                    )
                    return exit_price, exit_type

        except Exception as exc:
            logger.warning(
                f"[CRYPTO {symbol}] No se pudo recuperar orden de venta: {exc}. "
                "Usando precio TP calculado como aproximacion."
            )

        # Fallback: asumir TP con el precio objetivo calculado
        logger.warning(
            f"[CRYPTO {symbol}] Usando precio TP calculado como fallback: "
            f"${record.target_tp:.6f}"
        )
        return record.target_tp, "CRYPTO_SELL_TP"

    async def _log_exit(
        self,
        record: CryptoPositionRecord,
        exit_price: float,
        exit_type: str,
    ) -> None:
        """Registra la salida en el trade log y elimina la posicion del registro."""
        symbol = record.symbol
        pnl = (exit_price - record.entry_price) * record.qty
        pnl_pct = (exit_price - record.entry_price) / record.entry_price

        trade_data = {
            "ticker": symbol,
            "trade_type": exit_type,
            "notional": record.notional,
            "entry_price": record.entry_price,
            "exit_price": exit_price,
            "qty": record.qty,
            "pnl": round(pnl, 6),
            "pnl_pct": round(pnl_pct, 6),
            "kelly_pct": record.kelly_pct,
            "atr_at_entry": record.atr_at_entry,
            "entry_time": record.entry_time.strftime("%Y-%m-%d %H:%M:%S") if record.entry_time else "N/A",
        }
        self._trade_logger.log_crypto_exit(trade_data)

        pnl_sign = "+" if pnl >= 0 else ""
        logger.info(
            f"[CRYPTO {symbol}] Salida registrada [{exit_type}]: "
            f"P&L={pnl_sign}${pnl:.4f} ({pnl_sign}{pnl_pct*100:.2f}%) | "
            f"Precio=${exit_price:.4f}"
        )

        async with self._lock:
            self._positions.pop(symbol, None)

    async def _execute_timeout_exit(
        self, record: CryptoPositionRecord, current_price: float
    ) -> None:
        """
        Fallback de timeout: cancela las legs OCO pendientes y envia una
        orden de venta a mercado para cerrar la posicion forzadamente.
        """
        symbol = record.symbol

        # Intentar cancelar la orden bracket original si aun esta pendiente
        if record.bracket_order_id:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    self._order_manager._client.cancel_order_by_id,
                    record.bracket_order_id,
                )
                logger.info(
                    f"[CRYPTO {symbol}] Orden bracket {record.bracket_order_id} cancelada."
                )
            except Exception as exc:
                logger.warning(
                    f"[CRYPTO {symbol}] No se pudo cancelar bracket: {exc}. "
                    "Continuando con venta a mercado..."
                )

        # Venta a mercado como fallback
        order = await asyncio.get_event_loop().run_in_executor(
            None, self._order_manager.submit_sell, symbol, record.qty
        )

        if order is None:
            logger.error(
                f"[CRYPTO {symbol}] Fallo la venta a mercado por timeout. "
                "Posicion puede estar aun abierta en Alpaca."
            )
            return

        await self._log_exit(record, current_price, "CRYPTO_SELL_TIME")

    def start(self) -> None:
        """Lanza corutinas de supervision para todas las posiciones registradas."""
        for symbol, record in self._positions.items():
            if record.task is None or record.task.done():
                task = asyncio.create_task(
                    self._supervise_position(record),
                    name=f"crypto_supervisor_{symbol.replace('/', '_')}",
                )
                record.task = task
                logger.info(f"[CRYPTO {symbol}] Monitor de posicion bracket lanzado.")

    def stop_all(self) -> None:
        """Cancela todas las corutinas de supervision activas."""
        cancelled = 0
        for symbol, record in self._positions.items():
            if record.task and not record.task.done():
                record.task.cancel()
                cancelled += 1
        logger.info(f"CryptoSupervisor.stop_all(): {cancelled} tareas canceladas.")

    def is_position_active(self, symbol: str) -> bool:
        """Verifica si un par cripto esta bajo supervision."""
        return symbol in self._positions

    def get_active_positions(self) -> dict[str, CryptoPositionRecord]:
        """Retorna el diccionario de posiciones cripto activas."""
        return {k: v for k, v in self._positions.items()}
