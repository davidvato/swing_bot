"""
execution/crypto_supervisor.py — Supervisor Asincrono de Posiciones Cripto
=========================================================================
Emula TP/SL para posiciones cripto usando ATR dinamico (Average True Range).

DIFERENCIAS CLAVE vs execution/supervisor.py (equities):
  - TP/SL basados en ATR: se adaptan a la volatilidad real del activo.
    TP = entry + (atr × CRYPTO_ATR_TP_MULT)   → Risk/Reward 2:1
    SL = entry - (atr × CRYPTO_ATR_SL_MULT)
  - Fallback a % fijo si ATR no disponible (datos insuficientes).
  - Trailing SL dinamico: SL sube con el precio para proteger ganancias.
  - Opera 24/7 (sin restriccion de horario de mercado).
  - Tiempo maximo de holding medido en HORAS (no dias).
  - Poll interval: CRYPTO_POLL_INTERVAL_SEC (5 min vs 60s de equities).
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
        task: Referencia a la tarea asyncio del supervisor.
        highest_price: Maximo precio alcanzado desde la entrada (para trailing SL).
    """
    symbol: str
    entry_price: float
    qty: float
    entry_time: datetime
    notional: float
    kelly_pct: float
    atr_at_entry: Optional[float] = None
    task: Optional[asyncio.Task] = field(default=None, repr=False)
    highest_price: float = field(init=False)

    def __post_init__(self):
        self.highest_price = self.entry_price

    @property
    def target_tp(self) -> float:
        """
        Precio objetivo de Take-Profit.

        Si ATR disponible: entry + (ATR × CRYPTO_ATR_TP_MULT)
        Fallback:          entry × (1 + CRYPTO_TAKE_PROFIT_PCT)
        """
        if self.atr_at_entry and not np.isnan(self.atr_at_entry) and self.atr_at_entry > 0:
            return self.entry_price + (self.atr_at_entry * CRYPTO_ATR_TP_MULT)
        return self.entry_price * (1.0 + CRYPTO_TAKE_PROFIT_PCT)

    @property
    def target_sl(self) -> float:
        """
        Precio objetivo de Stop-Loss (trailing desde el maximo alcanzado).

        Si ATR disponible: highest_price - (ATR × CRYPTO_ATR_SL_MULT)
        Fallback:          highest_price × (1 - CRYPTO_STOP_LOSS_PCT)
        El SL sube con el precio (trailing) para proteger ganancias.
        """
        if self.atr_at_entry and not np.isnan(self.atr_at_entry) and self.atr_at_entry > 0:
            return self.highest_price - (self.atr_at_entry * CRYPTO_ATR_SL_MULT)
        return self.highest_price * (1.0 - CRYPTO_STOP_LOSS_PCT)

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
    Supervisor asincrono 24/7 de posiciones cripto con ATR dinamico.
    """

    def __init__(self, order_manager, trade_logger) -> None:
        self._order_manager = order_manager
        self._trade_logger = trade_logger
        self._positions: dict[str, CryptoPositionRecord] = {}
        self._lock = asyncio.Lock()
        logger.info("CryptoPositionSupervisor inicializado.")

    def add_position(
        self,
        symbol: str,
        entry_price: float,
        qty: float,
        notional: float,
        kelly_pct: float,
        atr_at_entry: Optional[float] = None,
        entry_time: Optional[datetime] = None,
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
        )
        self._positions[symbol] = record

        tp_mode = f"ATR×{CRYPTO_ATR_TP_MULT}" if (atr_at_entry and atr_at_entry > 0) else "fijo"
        logger.info(
            f"[CRYPTO {symbol}] Posicion registrada: "
            f"entry=${entry_price:.4f}, qty={qty:.8f}, "
            f"TP=${record.target_tp:.4f} (+{record.tp_pct*100:.2f}%, modo={tp_mode}), "
            f"SL=${record.target_sl:.4f} ({record.sl_pct*100:.2f}%), "
            f"ATR={atr_at_entry:.4f if atr_at_entry else 'N/A'}"
        )
        return record

    async def _supervise_position(self, record: CryptoPositionRecord) -> None:
        """Corutina de supervision 24/7 para una posicion cripto."""
        symbol = record.symbol
        logger.info(
            f"[CRYPTO {symbol}] Supervision iniciada → "
            f"TP=${record.target_tp:.4f} | SL=${record.target_sl:.4f} | "
            f"Max={CRYPTO_MAX_HOLD_HOURS}h"
        )

        while True:
            try:
                await asyncio.sleep(CRYPTO_POLL_INTERVAL_SEC)

                current_price = await asyncio.get_event_loop().run_in_executor(
                    None, self._order_manager.get_latest_quote, symbol
                )

                if current_price == 0.0:
                    logger.info(
                        f"[CRYPTO {symbol}] Precio=0, posicion ya cerrada externamente."
                    )
                    break

                # Actualizar trailing SL
                if current_price > record.highest_price:
                    record.highest_price = current_price
                    logger.debug(
                        f"[CRYPTO {symbol}] Nuevo maximo: ${current_price:.4f} | "
                        f"Trailing SL actualizado: ${record.target_sl:.4f}"
                    )

                hours_held = record.hours_held
                pnl_pct = (current_price - record.entry_price) / record.entry_price

                logger.debug(
                    f"[CRYPTO {symbol}] Precio=${current_price:.4f} | "
                    f"TP=${record.target_tp:.4f} | SL=${record.target_sl:.4f} | "
                    f"Horas={hours_held:.1f}"
                )

                exit_type = None

                if current_price >= record.target_tp:
                    exit_type = "CRYPTO_SELL_TP"
                    logger.info(
                        f"[CRYPTO {symbol}] TAKE PROFIT: "
                        f"${current_price:.4f} >= ${record.target_tp:.4f} "
                        f"(+{pnl_pct*100:.2f}%)"
                    )
                elif current_price <= record.target_sl:
                    exit_type = "CRYPTO_SELL_SL"
                    logger.warning(
                        f"[CRYPTO {symbol}] STOP LOSS: "
                        f"${current_price:.4f} <= ${record.target_sl:.4f} "
                        f"({pnl_pct*100:.2f}%)"
                    )
                elif hours_held >= CRYPTO_MAX_HOLD_HOURS:
                    exit_type = "CRYPTO_SELL_TIME"
                    logger.info(
                        f"[CRYPTO {symbol}] MAXIMO DE HORAS: "
                        f"{hours_held:.1f}h >= {CRYPTO_MAX_HOLD_HOURS}h | "
                        f"P&L: {pnl_pct*100:.2f}%"
                    )

                if exit_type is not None:
                    await self._execute_exit(record, current_price, exit_type)
                    break

            except asyncio.CancelledError:
                logger.info(f"[CRYPTO {symbol}] Supervision cancelada.")
                break
            except Exception as exc:
                logger.error(
                    f"[CRYPTO {symbol}] Error en supervision: {exc}. "
                    "Reintentando en el siguiente ciclo..."
                )

    async def _execute_exit(
        self,
        record: CryptoPositionRecord,
        exit_price: float,
        exit_type: str,
    ) -> None:
        """Ejecuta la venta y registra la operacion en el trade log."""
        symbol = record.symbol
        try:
            order = await asyncio.get_event_loop().run_in_executor(
                None, self._order_manager.submit_sell, symbol, record.qty
            )

            if order is None:
                logger.error(f"[CRYPTO {symbol}] Orden de venta retorno None.")
                return

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
            }
            self._trade_logger.log_crypto_exit(trade_data)

            pnl_sign = "+" if pnl >= 0 else ""
            logger.info(
                f"[CRYPTO {symbol}] Salida [{exit_type}]: "
                f"P&L={pnl_sign}${pnl:.4f} ({pnl_sign}{pnl_pct*100:.2f}%) | "
                f"Precio=${exit_price:.4f}"
            )

        except Exception as exc:
            logger.error(f"[CRYPTO {symbol}] Error ejecutando salida: {exc}")
        finally:
            async with self._lock:
                self._positions.pop(symbol, None)

    def start(self) -> None:
        """Lanza corutinas de supervision para todas las posiciones registradas."""
        for symbol, record in self._positions.items():
            if record.task is None or record.task.done():
                task = asyncio.create_task(
                    self._supervise_position(record),
                    name=f"crypto_supervisor_{symbol.replace('/', '_')}",
                )
                record.task = task
                logger.info(f"[CRYPTO {symbol}] Tarea de supervision lanzada.")

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
