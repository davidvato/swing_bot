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
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta, time as dtime
from pathlib import Path
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
    BREAKEVEN_TRIGGER_PCT,
    SUPERVISOR_STATE_FILE,
    EQUITY_DYNAMIC_ATR_TPSL,
    EQUITY_ATR_TP_MULT,
    EQUITY_ATR_SL_MULT,
    EQUITY_MIN_TP_PCT,
    EQUITY_MAX_TP_PCT,
    EQUITY_MIN_SL_PCT,
    EQUITY_MAX_SL_PCT,
)

logger = logging.getLogger(__name__)

EST = pytz.timezone(TIMEZONE_EST)


def is_market_open_now(dt: Optional[datetime] = None) -> bool:
    """
    Retorna True si la sesion regular de bolsa en EE.UU. esta activa (09:30 - 16:00 EST, L-V).
    """
    if dt is None:
        dt = datetime.now(EST)
    # Lunes=0, Domingo=6
    if dt.weekday() >= 5:
        return False
    market_open = dtime(9, 30)
    market_close = dtime(16, 0)
    return market_open <= dt.time() < market_close


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
        entry_trade_id: ID de la fila BUY en SQLite para vincular round-trip.
        task: Referencia a la tarea asyncio para cancelacion (no serializable).
        breakeven_activated: True cuando el SL fue movido a precio de entrada.
        tp_pct: Porcentaje objetivo de Take-Profit (fijo o dinamico por ATR).
        sl_pct: Porcentaje de Stop-Loss (fijo o dinamico por ATR).
        atr_at_entry: ATR del activo al momento de entrar.
    """
    symbol: str
    entry_price: float
    qty: float
    entry_date: date
    notional: float
    kelly_pct: float
    entry_trade_id: Optional[int] = field(default=None)  # ID del BUY en SQLite
    task: Optional[asyncio.Task] = field(default=None, repr=False)
    highest_price: float = field(default=0.0)
    breakeven_activated: bool = field(default=False)     # Break-Even Stop activado
    _sl_floor: float = field(default=0.0)                # Floor del SL tras break-even
    tp_pct: float = field(default=TAKE_PROFIT_PCT)
    sl_pct: float = field(default=STOP_LOSS_PCT)
    atr_at_entry: float = field(default=0.0)

    def __post_init__(self):
        if self.highest_price <= 0.0:
            self.highest_price = self.entry_price
        if self.breakeven_activated and self._sl_floor <= 0.0:
            self._sl_floor = self.entry_price

    @property
    def target_tp(self) -> float:
        """Precio objetivo de Take-Profit (+tp_pct%)."""
        return self.entry_price * (1.0 + self.tp_pct)

    @property
    def target_sl(self) -> float:
        """Precio objetivo de Stop-Loss dinamico.
        
        - Si break-even esta activado: SL = max(trailing_sl, entry_price).
          Garantiza que nunca cerremos debajo del precio de entrada una vez
          que el break-even fue activado.
        - Si break-even NO esta activado: SL = highest_price * (1 - sl_pct)
          (trailing stop clasico desde el maximo).
        """
        trailing_sl = self.highest_price * (1.0 - self.sl_pct)
        if self.breakeven_activated:
            return max(trailing_sl, self._sl_floor)
        return trailing_sl

    @property
    def days_held(self) -> int:
        """Dias calendario transcurridos desde la entrada."""
        return (date.today() - self.entry_date).days

    def activate_breakeven(self) -> None:
        """Mueve el floor del SL a precio de entrada (protege capital)."""
        if not self.breakeven_activated:
            self._sl_floor = self.entry_price
            self.breakeven_activated = True
            logger.info(
                f"[{self.symbol}] BREAK-EVEN STOP ACTIVADO: SL floor = ${self._sl_floor:.2f} "
                f"(entry_price). El trade no puede cerrar en rojo."
            )



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

    def _save_state(self) -> None:
        """Persiste el estado actual de trailing stop y break-even en disco."""
        try:
            state = {}
            for sym, rec in self._positions.items():
                state[sym] = {
                    "symbol": rec.symbol,
                    "entry_price": rec.entry_price,
                    "qty": rec.qty,
                    "entry_date": rec.entry_date.isoformat(),
                    "notional": rec.notional,
                    "kelly_pct": rec.kelly_pct,
                    "entry_trade_id": rec.entry_trade_id,
                    "highest_price": rec.highest_price,
                    "breakeven_activated": rec.breakeven_activated,
                    "_sl_floor": rec._sl_floor,
                    "tp_pct": rec.tp_pct,
                    "sl_pct": rec.sl_pct,
                    "atr_at_entry": rec.atr_at_entry,
                }
            Path(SUPERVISOR_STATE_FILE).write_text(
                json.dumps(state, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.error(f"Error guardando estado del supervisor: {exc}")

    def _load_state(self) -> dict:
        """Carga el estado previo de las posiciones desde disco."""
        p = Path(SUPERVISOR_STATE_FILE)
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"Error cargando {SUPERVISOR_STATE_FILE}: {exc}")
            return {}

    def add_position(
        self,
        symbol: str,
        entry_price: float,
        qty: float,
        notional: float,
        kelly_pct: float,
        entry_date: Optional[date] = None,
        entry_trade_id: Optional[int] = None,
        highest_price: Optional[float] = None,
        breakeven_activated: bool = False,
        sl_floor: float = 0.0,
        atr_at_entry: float = 0.0,
        tp_pct: Optional[float] = None,
        sl_pct: Optional[float] = None,
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
            entry_trade_id: ID del BUY en SQLite para vincular round-trip.
            highest_price: Maximo historico alcanzado (para restauracion).
            breakeven_activated: Si el break-even ya fue gatillado.
            sl_floor: Suelo del SL si break-even esta activo.
            atr_at_entry: ATR del activo en entrada (para TP/SL dinamico).
            tp_pct: % Take-Profit (si es None, calcula dinamico por ATR o fallback).
            sl_pct: % Stop-Loss (si es None, calcula dinamico por ATR o fallback).

        Returns:
            PositionRecord recien creado y registrado.
        """
        if entry_date is None:
            entry_date = date.today()

        # Determinar TP y SL dinámico por ATR o fallback
        if tp_pct is None:
            if EQUITY_DYNAMIC_ATR_TPSL and atr_at_entry > 0 and entry_price > 0:
                calc_tp = (atr_at_entry * EQUITY_ATR_TP_MULT) / entry_price
                tp_pct = max(min(calc_tp, EQUITY_MAX_TP_PCT), EQUITY_MIN_TP_PCT)
            else:
                tp_pct = TAKE_PROFIT_PCT

        if sl_pct is None:
            if EQUITY_DYNAMIC_ATR_TPSL and atr_at_entry > 0 and entry_price > 0:
                calc_sl = (atr_at_entry * EQUITY_ATR_SL_MULT) / entry_price
                sl_pct = max(min(calc_sl, EQUITY_MAX_SL_PCT), EQUITY_MIN_SL_PCT)
            else:
                sl_pct = STOP_LOSS_PCT

        record = PositionRecord(
            symbol=symbol,
            entry_price=entry_price,
            qty=qty,
            entry_date=entry_date,
            notional=notional,
            kelly_pct=kelly_pct,
            entry_trade_id=entry_trade_id,
            highest_price=highest_price if highest_price and highest_price > 0 else entry_price,
            breakeven_activated=breakeven_activated,
            _sl_floor=sl_floor,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
            atr_at_entry=atr_at_entry,
        )
        self._positions[symbol] = record
        self._save_state()
        logger.info(
            f"[{symbol}] Posicion registrada: "
            f"entry=${entry_price:.2f}, qty={qty:.6f}, "
            f"TP=${record.target_tp:.2f} (+{record.tp_pct*100:.1f}%), "
            f"SL=${record.target_sl:.2f} (-{record.sl_pct*100:.1f}%), "
            f"Highest=${record.highest_price:.2f}, BreakEven={record.breakeven_activated}"
            + (f", BUY_ID={entry_trade_id}" if entry_trade_id else "")
        )
        return record


    async def _supervise_position(self, record: PositionRecord) -> None:
        """
        Corutina principal de supervision para una posicion individual.

        Ciclo de monitoreo cada SUPERVISOR_POLL_INTERVAL_SEC segundos:
          1. Consulta horario de mercado (suspension fuera de hora regular).
          2. Consulta precio actual de la posicion.
          3. Evalua condiciones de salida: TP, SL, tiempo maximo.
          4. Ejecuta orden de venta de mercado si se activa alguna condicion.
          5. Registra la operacion en el trade log.
          6. Termina la corutina tras la venta.

        Args:
            record: Registro de la posicion a supervisar.
        """
        symbol = record.symbol
        logger.info(
            f"[{symbol}] Supervision iniciada -> "
            f"TP=${record.target_tp:.2f} (+{record.tp_pct*100:.1f}%), "
            f"SL=${record.target_sl:.2f} (-{record.sl_pct*100:.1f}%), "
            f"Max={MAX_HOLD_DAYS} dias"
        )

        while True:
            try:
                # ── Market Hours Awareness: Verificar sesion regular (09:30 - 16:00 EST) ──
                now_est = datetime.now(EST)
                if not is_market_open_now(now_est):
                    logger.debug(
                        f"[{symbol}] Mercado de Equities fuera de sesion regular ({now_est.strftime('%a %H:%M')} EST). "
                        "Supervisor en reposo (60s)..."
                    )
                    await asyncio.sleep(60)
                    continue

                await asyncio.sleep(SUPERVISOR_POLL_INTERVAL_SEC)

                # Consultar precio actual via la posicion abierta en Alpaca
                current_price = await asyncio.get_event_loop().run_in_executor(
                    None,
                    self._order_manager.get_latest_quote,
                    symbol,
                )

                # Si precio = 0, re-confirmar para descartar fallo transitorio de red
                if current_price == 0.0:
                    await asyncio.sleep(2)
                    recheck_price = await asyncio.get_event_loop().run_in_executor(
                        None,
                        self._order_manager.get_latest_quote,
                        symbol,
                    )
                    if recheck_price == 0.0:
                        logger.info(
                            f"[{symbol}] Posicion confirmada cerrada externamente. "
                            "Terminando supervision."
                        )
                        break
                    current_price = recheck_price
                    
                # Actualizar trailing SL
                if current_price > record.highest_price:
                    record.highest_price = current_price
                    self._save_state()

                # ─── Evaluar activacion de Break-Even Stop ────────────────────────
                # Si el activo subió BREAKEVEN_TRIGGER_PCT desde entrada,
                # activar break-even: el SL floor se mueve a entry_price.
                gain_pct = (current_price - record.entry_price) / record.entry_price
                if not record.breakeven_activated and gain_pct >= BREAKEVEN_TRIGGER_PCT:
                    record.activate_breakeven()
                    self._save_state()

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
                    # Si la caída es catastrófica (>= 1.5x STOP_LOSS_PCT), cortar inmediatamente
                    catastrophic_loss = pnl_pct <= -(STOP_LOSS_PCT * 1.5)

                    if now_est < grace_end and not catastrophic_loss:
                        logger.debug(
                            f"[{symbol}] Precio <= SL pero dentro del periodo de gracia "
                            f"(hasta {grace_end.strftime('%H:%M')}). Ignorando SL menor."
                        )
                    else:
                        exit_type = TRADE_TYPE_SELL_SL
                        if catastrophic_loss:
                            logger.warning(
                                f"[{symbol}] STOP LOSS CATASTROFICO: caída severa {pnl_pct*100:.2f}%. "
                                "Bypasseando periodo de gracia."
                            )
                        else:
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
            # 1. Intentar liquidación nativa completa via close_position (evita errores de redondeo fraccionario)
            order = None
            if hasattr(self._order_manager, "close_position"):
                try:
                    order = await asyncio.get_event_loop().run_in_executor(
                        None,
                        self._order_manager.close_position,
                        symbol,
                    )
                except Exception as exc:
                    logger.warning(
                        f"[{symbol}] close_position fallo: {exc}. Reintentando con submit_sell."
                    )

            # 2. Fallback a submit_sell por qty
            if order is None:
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

            # Registrar en el trade log (con entry_trade_id para vincular round-trip)
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
                "entry_trade_id": record.entry_trade_id,  # ID del BUY padre (trazabilidad)
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
                self._save_state()

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

    def stop_position(self, symbol: str) -> None:
        """Detiene la supervision de una posicion individual sin afectar a las demas."""
        record = self._positions.get(symbol)
        if record and record.task and not record.task.done():
            record.task.cancel()
            logger.info(f"[{symbol}] Tarea de supervision cancelada individualmente.")

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
        si no estaban siendo supervisadas. Restaura el estado de trailing stop
        desde supervisor_state.json y metadata historica de SQLite.
        
        Returns:
            Cantidad de posiciones rehidratadas.
        """
        try:
            open_positions = self._order_manager.get_open_positions()
        except Exception as exc:
            logger.error(f"Error rehidratando posiciones: {exc}")
            return 0
            
        saved_state = self._load_state()
        rehydrated = 0
        for pos in open_positions:
            symbol = pos.symbol
            if self.is_position_active(symbol):
                continue
                
            entry_price = float(pos.avg_entry_price)
            qty = float(pos.qty)
            
            # Restaurar snapshot previo si existe
            saved = saved_state.get(symbol, {})
            highest_price = saved.get("highest_price", entry_price)
            breakeven_activated = saved.get("breakeven_activated", False)
            sl_floor = saved.get("_sl_floor", 0.0)
            tp_pct = saved.get("tp_pct")
            sl_pct = saved.get("sl_pct")
            atr_at_entry = saved.get("atr_at_entry", 0.0)

            # Buscar metadata original en trade log
            last_buy = self._trade_logger.get_latest_buy(symbol)
            if last_buy:
                notional = last_buy.get("notional") or (entry_price * qty)
                kelly_pct = last_buy.get("kelly_pct") or 0.0
                entry_trade_id = last_buy.get("id")
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
                entry_trade_id = None
                
            self.add_position(
                symbol=symbol,
                entry_price=entry_price,
                qty=qty,
                notional=notional,
                kelly_pct=kelly_pct,
                entry_date=entry_date,
                entry_trade_id=entry_trade_id,
                highest_price=highest_price,
                breakeven_activated=breakeven_activated,
                sl_floor=sl_floor,
                atr_at_entry=atr_at_entry,
                tp_pct=tp_pct,
                sl_pct=sl_pct,
            )
            rehydrated += 1
            
        if rehydrated > 0:
            logger.info(f"Rehidratacion completada: {rehydrated} posiciones asimiladas (estado restaurado).")
            
        return rehydrated
