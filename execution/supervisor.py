"""
execution/supervisor.py — Supervisor Asincrono de Posiciones (TP/SL emulado)
=============================================================================
Emula las Bracket Orders (Take-Profit + Stop-Loss) de Alpaca mediante un
loop asyncio que monitorea cada posicion abierta y ejecuta ventas de mercado
cuando se perforan los umbrales de ganancia o perdida.

RAZON DE EXISTENCIA:
    Alpaca NO permite adjuntar Take-Profit ni Stop-Loss a ordenes notionales
    fraccionarias. Esta clase resuelve esa limitacion de la plataforma.

ARQUITECTURA:
    - Cada posicion activa tiene su propia corutina asyncio.
    - El loop principal del supervisor corre en paralelo con el orquestador.
    - Las corutinas verifican el precio cada SUPERVISOR_POLL_INTERVAL_SEC (60s).
    - El supervisor se detiene limpiamente cuando se invoca stop_all().

LIMITACIONES (documentadas):
    - La ejecucion de ventas tiene latencia de red (tipicamente < 1s en paper).
    - Los umbrales TP/SL son aproximados, no garantizan ejecucion exacta al precio.
    - Durante alta volatilidad intradiaria, el precio real de ejecucion puede
      diferir del precio de activacion del umbral (slippage).
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta, time as dtime
from typing import Optional

import pytz

from config import (
    TAKE_PROFIT_PCT,
    STOP_LOSS_PCT,
    MAX_HOLD_DAYS,
    SUPERVISOR_POLL_INTERVAL_SEC,
    TRADE_TYPE_SELL_TP,
    TRADE_TYPE_SELL_SL,
    TRADE_TYPE_SELL_5D,
    TIMEZONE_EST,
    SL_GRACE_PERIOD_MINS,
    MARKET_OPEN_TIME_EST,
)

logger = logging.getLogger(__name__)

EST = pytz.timezone(TIMEZONE_EST)


@dataclass
class PositionRecord:
    """
    Registro inmutable de una posicion abierta supervisada.

    Attributes:
        symbol: Simbolo bursatil de la posicion.
        entry_price: Precio de compra original (para calcular TP/SL).
        qty: Cantidad de acciones (fraccionarias) en posesion.
        entry_date: Fecha de entrada al mercado (para limite de 5 dias).
        notional: Importe invertido originalmente en USD.
        kelly_pct: Fraccion de Kelly aplicada al momento de la compra.
        task: Referencia a la tarea asyncio para cancelacion (no serializable).
    """
    symbol: str
    entry_price: float
    qty: float
    entry_date: date
    notional: float
    kelly_pct: float
    task: Optional[asyncio.Task] = field(default=None, repr=False)
    highest_price: float = field(init=False)

    def __post_init__(self):
        self.highest_price = self.entry_price

    @property
    def target_tp(self) -> float:
        """Precio objetivo de Take-Profit (+TAKE_PROFIT_PCT%)."""
        return self.entry_price * (1.0 + TAKE_PROFIT_PCT)

    @property
    def target_sl(self) -> float:
        """Precio objetivo de Stop-Loss dinámico (-STOP_LOSS_PCT% desde el máximo)."""
        return self.highest_price * (1.0 - STOP_LOSS_PCT)

    @property
    def days_held(self) -> int:
        """Dias calendario transcurridos desde la entrada."""
        return (date.today() - self.entry_date).days


class PositionSupervisor:
    """
    Supervisor asincrono de posiciones abiertas.

    Lanza una corutina asyncio por cada posicion activa.
    Cada corutina monitorea precio y ejecuta ventas de mercado
    cuando se alcanza TP, SL o el limite de dias de holding.
    """

    def __init__(self, order_manager, trade_logger) -> None:
        """
        Inicializa el supervisor con referencias al gestor de ordenes y al log.

        Args:
            order_manager: Instancia de OrderManager para ejecutar ventas.
            trade_logger: Instancia de TradeLogger para registrar las salidas.
        """
        self._order_manager = order_manager
        self._trade_logger = trade_logger
        self._positions: dict[str, PositionRecord] = {}
        self._lock = asyncio.Lock()
        logger.info("PositionSupervisor inicializado.")

    def add_position(
        self,
        symbol: str,
        entry_price: float,
        qty: float,
        notional: float,
        kelly_pct: float,
        entry_date: Optional[date] = None,
    ) -> PositionRecord:
        """
        Registra una nueva posicion para supervision y lanza su corutina.

        Args:
            symbol: Simbolo bursatil.
            entry_price: Precio de compra.
            qty: Cantidad de acciones adquiridas.
            notional: Importe invertido en USD.
            kelly_pct: Fraccion Kelly aplicada.
            entry_date: Fecha de entrada (default: hoy).

        Returns:
            PositionRecord recien creado y registrado.
        """
        if entry_date is None:
            entry_date = date.today()

        record = PositionRecord(
            symbol=symbol,
            entry_price=entry_price,
            qty=qty,
            entry_date=entry_date,
            notional=notional,
            kelly_pct=kelly_pct,
        )
        self._positions[symbol] = record
        logger.info(
            f"[{symbol}] Posicion registrada: "
            f"entry=${entry_price:.2f}, qty={qty:.6f}, "
            f"TP=${record.target_tp:.2f}, SL=${record.target_sl:.2f}"
        )
        return record

    async def _supervise_position(self, record: PositionRecord) -> None:
        """
        Corutina principal de supervision para una posicion individual.

        Ciclo de monitoreo cada SUPERVISOR_POLL_INTERVAL_SEC segundos:
          1. Consulta precio actual de la posicion.
          2. Evalua condiciones de salida: TP, SL, tiempo maximo.
          3. Ejecuta orden de venta de mercado si se activa alguna condicion.
          4. Registra la operacion en el trade log.
          5. Termina la corutina tras la venta.

        Args:
            record: Registro de la posicion a supervisar.
        """
        symbol = record.symbol
        logger.info(
            f"[{symbol}] Supervision iniciada -> "
            f"TP=${record.target_tp:.2f} (+{TAKE_PROFIT_PCT*100:.0f}%), "
            f"SL=${record.target_sl:.2f} (-{STOP_LOSS_PCT*100:.0f}%), "
            f"Max={MAX_HOLD_DAYS} dias"
        )

        while True:
            try:
                await asyncio.sleep(SUPERVISOR_POLL_INTERVAL_SEC)

                # Consultar precio actual via la posicion abierta en Alpaca
                current_price = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self._order_manager.get_latest_quote,
                    symbol,
                )

                # Si precio = 0, la posicion ya fue cerrada externamente
                if current_price == 0.0:
                    logger.info(
                        f"[{symbol}] Posicion ya cerrada externamente. "
                        "Terminando supervision."
                    )
                    break
                    
                # Actualizar trailing SL
                if current_price > record.highest_price:
                    record.highest_price = current_price

                days_held = record.days_held
                logger.debug(
                    f"[{symbol}] Precio actual=${current_price:.2f} | "
                    f"TP=${record.target_tp:.2f} | SL=${record.target_sl:.2f} | "
                    f"Dias={days_held}"
                )

                # ─── Evaluar condiciones de salida ────────────────────────────
                exit_type = None
                pnl_pct = (current_price - record.entry_price) / record.entry_price

                if current_price >= record.target_tp:
                    exit_type = TRADE_TYPE_SELL_TP
                    logger.info(
                        f"[{symbol}] TAKE PROFIT activado: "
                        f"${current_price:.2f} >= ${record.target_tp:.2f} "
                        f"(+{pnl_pct*100:.2f}%)"
                    )

                elif current_price <= record.target_sl:
                    # Validar periodo de gracia (gap down trap)
                    now_est = datetime.now(EST)
                    open_h, open_m = [int(x) for x in MARKET_OPEN_TIME_EST.split(":")]
                    market_open = now_est.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
                    grace_end = market_open + timedelta(minutes=SL_GRACE_PERIOD_MINS)
                    
                    if now_est < grace_end:
                        logger.debug(
                            f"[{symbol}] Precio <= SL pero dentro del periodo de gracia "
                            f"(hasta {grace_end.strftime('%H:%M')}). Ignorando SL."
                        )
                    else:
                        exit_type = TRADE_TYPE_SELL_SL
                        logger.warning(
                            f"[{symbol}] STOP LOSS activado: "
                            f"${current_price:.2f} <= ${record.target_sl:.2f} "
                            f"({pnl_pct*100:.2f}%)"
                        )

                elif days_held >= MAX_HOLD_DAYS:
                    exit_type = TRADE_TYPE_SELL_5D
                    logger.info(
                        f"[{symbol}] MAXIMO DE DIAS alcanzado: "
                        f"{days_held} dias >= {MAX_HOLD_DAYS}. "
                        f"P&L actual: {pnl_pct*100:.2f}%"
                    )

                # ─── Ejecutar salida si se activo alguna condicion ────────────
                if exit_type is not None:
                    await self._execute_exit(record, current_price, exit_type)
                    break  # Terminar la corutina de supervision

            except asyncio.CancelledError:
                logger.info(
                    f"[{symbol}] Supervision cancelada (liquidacion de viernes)."
                )
                break
            except Exception as exc:
                logger.error(
                    f"[{symbol}] Error en supervision: {exc}. "
                    "Reintentando en el siguiente ciclo..."
                )
                # No re-lanzar: continuar supervisando ante errores transitorios

    async def _execute_exit(
        self,
        record: PositionRecord,
        exit_price: float,
        exit_type: str,
    ) -> None:
        """
        Ejecuta la orden de venta de mercado y registra la operacion.

        Args:
            record: Registro de la posicion a cerrar.
            exit_price: Precio de referencia al momento de la salida.
            exit_type: Tipo de salida (SELL_TP, SELL_SL, SELL_5D).
        """
        symbol = record.symbol
        success = False
        try:
            # Ejecutar venta en hilo separado (operacion bloqueante)
            order = await asyncio.get_event_loop().run_in_executor(
                None,
                self._order_manager.submit_sell,
                symbol,
                record.qty,
            )

            if order is None:
                logger.error(
                    f"[{symbol}] Orden de venta retorno None. "
                    "Manteniendo posicion."
                )
                return

            # Calcular P&L
            pnl = (exit_price - record.entry_price) * record.qty
            pnl_pct = (exit_price - record.entry_price) / record.entry_price

            # Registrar en el trade log
            trade_data = {
                "ticker": symbol,
                "trade_type": exit_type,
                "notional": record.notional,
                "entry_price": record.entry_price,
                "exit_price": exit_price,
                "qty": record.qty,
                "pnl": round(pnl, 4),
                "pnl_pct": round(pnl_pct, 6),
                "kelly_pct": record.kelly_pct,
                "entry_time": record.entry_date.strftime("%Y-%m-%d") if record.entry_date else "N/A",
            }
            self._trade_logger.log_exit(trade_data)

            logger.info(
                f"[{symbol}] Salida ejecutada [{exit_type}]: "
                f"P&L=${pnl:+.2f} ({pnl_pct*100:+.2f}%) | "
                f"Precio salida=${exit_price:.2f}"
            )
            success = True

        except Exception as exc:
            logger.error(
                f"[{symbol}] Error ejecutando salida [{exit_type}]: {exc}"
            )
        finally:
            # Remover la posicion del registro interno solo si la orden fue un exito
            if success:
                async with self._lock:
                    self._positions.pop(symbol, None)

    def start(self) -> None:
        """
        Lanza las corutinas asyncio para todas las posiciones registradas.

        Debe llamarse despues de add_position() para iniciar el monitoreo.
        Usa asyncio.create_task() para ejecucion concurrente.
        """
        for symbol, record in self._positions.items():
            if record.task is None or record.task.done():
                task = asyncio.create_task(
                    self._supervise_position(record),
                    name=f"supervisor_{symbol}",
                )
                record.task = task
                logger.info(f"[{symbol}] Tarea de supervision lanzada.")

    def stop_all(self) -> None:
        """
        Cancela todas las corutinas de supervision activas.

        Llamado por el planificador de viernes antes de la liquidacion total.
        La cancelacion es cooperativa: cada corutina termina en su proximo
        punto de suspension (asyncio.sleep).
        """
        cancelled = 0
        for symbol, record in self._positions.items():
            if record.task and not record.task.done():
                record.task.cancel()
                cancelled += 1
                logger.info(f"[{symbol}] Tarea de supervision cancelada.")

        logger.info(
            f"PositionSupervisor.stop_all(): {cancelled} tareas canceladas."
        )

    def get_active_positions(self) -> dict[str, PositionRecord]:
        """Retorna el diccionario de posiciones actualmente bajo supervision."""
        return {k: v for k, v in self._positions.items()}

    def is_position_active(self, symbol: str) -> bool:
        """Verifica si un ticker esta actualmente siendo supervisado."""
        return symbol in self._positions

    def rehydrate_positions(self) -> int:
        """
        Consulta las posiciones abiertas en Alpaca y reanuda su supervision
        si no estaban siendo supervisadas. Extrae metadata historica de SQLite.
        
        Returns:
            Cantidad de posiciones rehidratadas.
        """
        try:
            open_positions = self._order_manager.get_open_positions()
        except Exception as exc:
            logger.error(f"Error rehidratando posiciones: {exc}")
            return 0
            
        rehydrated = 0
        for pos in open_positions:
            symbol = pos.symbol
            if self.is_position_active(symbol):
                continue
                
            entry_price = float(pos.avg_entry_price)
            qty = float(pos.qty)
            
            # Buscar metadata original
            last_buy = self._trade_logger.get_latest_buy(symbol)
            if last_buy:
                notional = last_buy.get("notional") or (entry_price * qty)
                kelly_pct = last_buy.get("kelly_pct") or 0.0
                try:
                    # Parsing '2026-08-31 09:30:32 EDT' -> date
                    date_str = last_buy.get("date", "").split(" ")[0]
                    entry_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                except Exception:
                    entry_date = date.today()
            else:
                notional = entry_price * qty
                kelly_pct = 0.0
                entry_date = date.today()
                
            self.add_position(
                symbol=symbol,
                entry_price=entry_price,
                qty=qty,
                notional=notional,
                kelly_pct=kelly_pct,
                entry_date=entry_date,
            )
            rehydrated += 1
            
        if rehydrated > 0:
            logger.info(f"Rehidratacion completada: {rehydrated} posiciones asimiladas.")
            
        return rehydrated
