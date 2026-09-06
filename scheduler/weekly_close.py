"""
scheduler/weekly_close.py — Planificador de Liquidacion Semanal (Viernes)
==========================================================================
Implementa el planificador que ejecuta la liquidacion total del portafolio
cada viernes a las 15:45 EST (15 minutos antes del cierre del mercado NYSE).

DISEÑO DE RESILIENCIA:
    Se usa doble validacion para garantizar la ejecucion del cierre semanal:
    1. Modulo 'schedule': dispara la funcion segun el calendario.
    2. Guard interno is_friday_close_time(): verifica la condicion en tiempo real.
    Esta redundancia protege ante reinicios del proceso, derives de reloj, etc.

ADVERTENCIA CRITICA:
    La liquidacion invoca close_all_positions(cancel_orders=True).
    El parametro cancel_orders=True es OBLIGATORIO para evitar la condicion
    de carrera "Insufficient qty available for order" que ocurre cuando hay
    ordenes pendientes que intentan ejecutarse sobre posiciones en proceso
    de liquidacion.
"""

import logging
from datetime import datetime, time as dtime

import pytz
import schedule

from config import (
    WEEKLY_CLOSE_TIME_EST,
    TIMEZONE_EST,
    MARKET_CLOSE_TIME_EST,
    TRADE_TYPE_SELL_EOW,
    UNIVERSE_UPDATE_TIME_EST,
)

logger = logging.getLogger(__name__)

EST = pytz.timezone(TIMEZONE_EST)

# Referencias globales a los componentes del bot (inyectadas por main.py)
_order_manager = None
_supervisor = None
_trade_logger = None
_screener = None          # UniverseScreener — inyectado por main.py


def initialize(order_manager, supervisor, trade_logger, screener=None) -> None:
    """
    Inyecta las dependencias necesarias para el scheduler semanal.

    Debe llamarse desde main.py antes de arrancar el loop de schedule.

    Args:
        order_manager: Instancia de OrderManager para ejecutar close_all().
        supervisor: Instancia de PositionSupervisor para cancelar tareas asyncio.
        trade_logger: Instancia de TradeLogger para registrar la liquidacion.
        screener: Instancia de UniverseScreener para la actualizacion semanal
                  del universo. Si es None, la actualizacion de universo se
                  omite sin error.
    """
    global _order_manager, _supervisor, _trade_logger, _screener
    _order_manager = order_manager
    _supervisor = supervisor
    _trade_logger = trade_logger
    _screener = screener
    logger.info(
        "WeeklyClose scheduler inicializado. "
        f"Liquidacion programada cada viernes a las {WEEKLY_CLOSE_TIME_EST} EST. "
        f"Actualizacion de universo cada lunes a las {UNIVERSE_UPDATE_TIME_EST} EST."
    )


def is_friday_close_time() -> bool:
    """
    Verifica si el momento actual es viernes despues de las 15:45 EST.

    Esta funcion actua como guard de seguridad independiente del scheduler
    para garantizar que la liquidacion solo se ejecute en el momento correcto.

    Returns:
        True si es viernes y la hora actual EST >= 15:45.
        False en cualquier otro momento.
    """
    now_est = datetime.now(EST)
    is_friday = now_est.weekday() == 4  # 0=Lunes, 4=Viernes

    close_hour, close_min = [int(x) for x in WEEKLY_CLOSE_TIME_EST.split(":")]
    is_after_close = now_est.time() >= dtime(close_hour, close_min)

    return is_friday and is_after_close


def is_monday_update_time() -> bool:
    """
    Verifica si el momento actual es lunes despues de las 09:35 EST.

    Guard de seguridad para la actualizacion del universo: protege contra
    ejecuciones multiples si el scheduler dispara varias veces.

    Returns:
        True si es lunes y la hora actual EST >= UNIVERSE_UPDATE_TIME_EST.
        False en cualquier otro momento.
    """
    now_est = datetime.now(EST)
    is_monday = now_est.weekday() == 0  # 0=Lunes

    upd_hour, upd_min = [int(x) for x in UNIVERSE_UPDATE_TIME_EST.split(":")]
    is_after_update = now_est.time() >= dtime(upd_hour, upd_min)

    return is_monday and is_after_update


def update_universe() -> list[str] | None:
    """
    Ejecuta el screener de universo para actualizar el Top-N de tickers.

    Invocada automaticamente cada lunes a las 09:35 EST por el scheduler.
    Tambien puede llamarse manualmente para forzar una actualizacion.

    Returns:
        Lista de tickers seleccionados, o None si el screener no esta
        configurado o falla el guard de horario.
    """
    global _screener

    if _screener is None:
        logger.warning(
            "update_universe() llamada pero _screener no esta inicializado. "
            "Asegurate de pasar el screener a initialize()."
        )
        return None

    if not is_monday_update_time():
        logger.debug(
            "update_universe() llamada fuera de horario (lunes 09:35+ EST). "
            "Guard activado: no se actualiza el universo."
        )
        return None

    now_est = datetime.now(EST)
    logger.info(
        f"ACTUALIZACION DE UNIVERSO — "
        f"{now_est.strftime('%Y-%m-%d %H:%M:%S %Z')} — "
        "Ejecutando screener..."
    )

    try:
        new_universe = _screener.select_universe()
        logger.info(
            f"Universo actualizado exitosamente: {new_universe}"
        )
        return new_universe
    except Exception as exc:
        logger.error(
            f"Error critico durante la actualizacion del universo: {exc}. "
            "El universo anterior permanece activo."
        )
        return None


def friday_liquidation() -> None:
    """
    Ejecuta la liquidacion total del portafolio.

    Secuencia de acciones:
    1. Verificar guard interno (is_friday_close_time).
    2. Obtener lista de posiciones abiertas y sus precios para el log.
    3. Cancelar todas las corutinas del supervisor asyncio.
    4. Ejecutar close_all_positions(cancel_orders=True) via OrderManager.
    5. Registrar cada posicion cerrada en el trade log con tipo SELL_EOW.

    Esta funcion es llamada por el modulo 'schedule' y tambien puede
    invocarse manualmente con --force-friday-close.
    """
    global _order_manager, _supervisor, _trade_logger

    if _order_manager is None:
        logger.error(
            "friday_liquidation() llamada antes de initialize(). "
            "El scheduler no esta configurado correctamente."
        )
        return

    # Guard de seguridad: verificar que realmente es viernes a las 15:45+
    if not is_friday_close_time():
        logger.debug(
            "friday_liquidation() llamada fuera de hora. "
            "Guard activado: no se ejecuta la liquidacion."
        )
        return

    now_est = datetime.now(EST)
    logger.warning(
        f"INICIO LIQUIDACION SEMANAL — {now_est.strftime('%Y-%m-%d %H:%M:%S %Z')} — "
        "Cerrando todas las posiciones..."
    )

    # ─── Paso 1: Capturar estado de posiciones ANTES de cerrar ────────────────
    open_positions = []
    try:
        open_positions = _order_manager.get_open_positions()
        logger.info(
            f"Posiciones abiertas a liquidar: {len(open_positions)}"
        )
        for pos in open_positions:
            logger.info(
                f"  {pos.symbol}: {pos.qty} acciones @ ${float(pos.avg_entry_price):.2f} "
                f"| Valor actual: ${float(pos.market_value):.2f} "
                f"| P&L: ${float(pos.unrealized_pl):.2f}"
            )
    except Exception as exc:
        logger.error(f"Error al obtener posiciones abiertas: {exc}")

    # ─── Paso 2: Detener el supervisor asyncio ────────────────────────────────
    if _supervisor is not None:
        try:
            _supervisor.stop_all()
            logger.info("Supervisor asyncio detenido.")
        except Exception as exc:
            logger.error(f"Error al detener el supervisor: {exc}")

    # ─── Paso 3: Ejecutar liquidacion total ───────────────────────────────────
    try:
        _order_manager.close_all()  # Invoca close_all_positions(cancel_orders=True)
    except Exception as exc:
        logger.error(
            f"Error critico durante liquidacion: {exc}. "
            "Verificar cuenta en Alpaca Dashboard inmediatamente."
        )
        return

    # ─── Paso 4: Registrar cada cierre en el trade log ────────────────────────
    if _trade_logger is not None:
        for pos in open_positions:
            try:
                symbol = pos.symbol
                entry_price = float(pos.avg_entry_price)
                current_price = float(pos.current_price)
                qty = float(pos.qty)
                pnl = float(pos.unrealized_pl)
                pnl_pct = (current_price - entry_price) / entry_price if entry_price != 0 else 0.0
                notional = entry_price * qty

                trade_data = {
                    "ticker": symbol,
                    "trade_type": TRADE_TYPE_SELL_EOW,
                    "notional": round(notional, 2),
                    "entry_price": entry_price,
                    "exit_price": current_price,
                    "qty": qty,
                    "pnl": round(pnl, 4),
                    "pnl_pct": round(pnl_pct, 6),
                    "kelly_pct": None,  # No disponible en liquidacion de viernes
                }
                _trade_logger.log_exit(trade_data)

            except Exception as exc:
                logger.error(
                    f"Error registrando cierre de {pos.symbol} en trade log: {exc}"
                )

    logger.warning(
        f"LIQUIDACION SEMANAL COMPLETADA — {len(open_positions)} posiciones cerradas."
    )


def setup_schedule() -> None:
    """
    Configura los trabajos programados del bot.

    Jobs registrados:
      - Viernes WEEKLY_CLOSE_TIME_EST: friday_liquidation() — liquidacion total.
      - Lunes UNIVERSE_UPDATE_TIME_EST: update_universe() — seleccion dinamica.

    NOTA: El modulo 'schedule' trabaja con la hora del sistema. Si el servidor
    esta en CST (UTC-6), la hora configurada debe ajustarse:
    - EST 15:45 = CST 14:45 (horario estandar)
    - EDT 15:45 = CDT 14:45 (horario de verano)
    Para mayor robustez, los guards internos usan pytz y validan
    independientemente en zona America/New_York.
    """
    schedule.every().friday.at(WEEKLY_CLOSE_TIME_EST).do(friday_liquidation)
    schedule.every().monday.at(UNIVERSE_UPDATE_TIME_EST).do(update_universe)
    logger.info(
        f"Schedule configurado:\n"
        f"  - friday_liquidation() → cada viernes a las {WEEKLY_CLOSE_TIME_EST}\n"
        f"  - update_universe()    → cada lunes   a las {UNIVERSE_UPDATE_TIME_EST}"
    )


def run_pending_jobs() -> None:
    """
    Ejecuta los trabajos pendientes del scheduler.

    Debe llamarse periodicamente desde el loop principal de main.py.
    """
    schedule.run_pending()
