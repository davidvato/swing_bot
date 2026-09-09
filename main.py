"""
main.py — Orquestador Principal del Swing Trading Bot
======================================================
Entry point del sistema. Coordina todos los modulos:
  - Ingesta de datos historicos (Alpaca API)
  - Calculo de señales (SMA-200 / RSI-4)
  - Gestion de riesgo (Half-Kelly)
  - Ejecucion de ordenes (notional fraccionario)
  - Supervision asyncio de posiciones (TP/SL emulado)
  - Planificador semanal (liquidacion del viernes)
  - Trade log (SQLite + CSV mensual)

MODOS DE EJECUCION (CLI flags):
    python main.py                      → Modo produccion (loop completo)
    python main.py --test-connection    → Verificar credenciales y equity
    python main.py --dry-run            → Imprimir señales sin operar
    python main.py --force-friday-close → Forzar liquidacion manual

SEGURIDAD:
    - Las credenciales se cargan EXCLUSIVAMENTE desde .env via dotenv.
    - El TradingClient opera en paper=True incondicionalmente.
    - Ningun secreto se imprime en logs o consola.
"""

import argparse
import asyncio
import logging
import os
import sys
import subprocess
import time
from datetime import datetime, time as dtime
from calendar import monthrange

import pytz
import schedule
from dotenv import load_dotenv

# ─── Modulos del bot ──────────────────────────────────────────────────────────
from config import (
    TICKERS,
    KELLY_WIN_RATE,
    KELLY_WIN_LOSS_RATIO,
    MAX_POSITION_PCT,
    KELLY_FRACTION,
    LOG_FILE,
    TIMEZONE_EST,
    MARKET_OPEN_TIME_EST,
    MARKET_CLOSE_TIME_EST,
    WEEKLY_CLOSE_TIME_EST,
    SUPERVISOR_POLL_INTERVAL_SEC,
    MARKET_REGIME_TICKER,
    CRYPTO_MAX_POSITION_PCT,
)
from data.ingestion import DataClient
from signals.indicators import (
    compute_indicators, generate_signal, get_signal_summary,
    compute_crypto_indicators, generate_crypto_signal,
)
from risk.kelly import compute_notional
from execution.orders import OrderManager
from execution.supervisor import PositionSupervisor
from execution.crypto_orders import CryptoOrderManager
from execution.crypto_supervisor import CryptoPositionSupervisor
from scheduler.weekly_close import initialize as init_scheduler, setup_schedule, friday_liquidation
from logging_.trade_log import TradeLogger
from universe.screener import UniverseScreener
from universe.crypto_screener import CryptoUniverseScreener
from data.crypto_ingestion import CryptoDataClient

# ─── Configuracion del sistema de logging ─────────────────────────────────────
def _setup_logging() -> None:
    """Configura el logging estructurado a archivo y consola."""
    log_format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        datefmt=date_format,
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    # Silenciar logs verbosos de librerias externas
    logging.getLogger("alpaca").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


logger = logging.getLogger("main")

EST = pytz.timezone(TIMEZONE_EST)


# ─── Funciones de utilidad ────────────────────────────────────────────────────

def _load_credentials() -> tuple[str, str]:
    """
    Carga las credenciales de Alpaca desde el archivo .env.

    Returns:
        Tupla (api_key, secret_key).

    Raises:
        SystemExit: Si alguna credencial no esta definida en .env.
    """
    load_dotenv()

    api_key = os.getenv("ALPACA_API_KEY", "").strip()
    secret_key = os.getenv("ALPACA_SECRET_KEY", "").strip()

    if not api_key or not secret_key:
        logger.error(
            "ERROR: ALPACA_API_KEY y/o ALPACA_SECRET_KEY no encontradas en .env. "
            "Copia .env.example → .env y completa tus credenciales."
        )
        sys.exit(1)

    # NUNCA imprimir las credenciales en logs
    logger.info("Credenciales cargadas desde .env correctamente.")
    return api_key, secret_key


def _is_market_open() -> bool:
    """
    Verifica si el mercado NYSE esta actualmente abierto (09:30–16:00 EST).

    Returns:
        True si el mercado esta en horario de operacion.
    """
    now_est = datetime.now(EST)
    # Solo de lunes (0) a viernes (4)
    if now_est.weekday() > 4:
        return False

    open_h, open_m = [int(x) for x in MARKET_OPEN_TIME_EST.split(":")]
    close_h, close_m = [int(x) for x in MARKET_CLOSE_TIME_EST.split(":")]

    market_open = dtime(open_h, open_m)
    market_close = dtime(close_h, close_m)

    return market_open <= now_est.time() < market_close


def _is_last_day_of_month() -> bool:
    """Verifica si hoy es el ultimo dia del mes actual."""
    now = datetime.now(EST)
    last_day = monthrange(now.year, now.month)[1]
    return now.day == last_day


# ─── Modo: Test de conexion ───────────────────────────────────────────────────

def run_connection_test(api_key: str, secret_key: str) -> None:
    """
    Verifica la conexion con Alpaca y muestra el estado de la cuenta.

    Args:
        api_key: Clave publica de Alpaca.
        secret_key: Clave secreta de Alpaca.
    """
    logger.info("=" * 60)
    logger.info("MODO: Test de Conexion")
    logger.info("=" * 60)

    order_manager = OrderManager(api_key, secret_key)

    try:
        account = order_manager.get_account()
        equity = float(account.equity)
        cash = float(account.cash)
        buying_power = float(account.buying_power)

        print("\n" + "=" * 60)
        print("  ALPACA PAPER TRADING — Estado de Cuenta")
        print("=" * 60)
        print(f"  Equity total:      ${equity:>12,.2f} USD")
        print(f"  Cash disponible:   ${cash:>12,.2f} USD")
        print(f"  Buying power:      ${buying_power:>12,.2f} USD")
        print(f"  Modo:              {'PAPER TRADING':>12}")
        print("=" * 60)
        print(f"  Max por posicion (15%): ${equity * MAX_POSITION_PCT:>10,.2f} USD")
        print("=" * 60 + "\n")

        logger.info("Test de conexion EXITOSO.")

    except Exception as exc:
        logger.error(f"Test de conexion FALLIDO: {exc}")
        sys.exit(1)


# ─── Modo: Dry Run ────────────────────────────────────────────────────────────

def run_dry_run(api_key: str, secret_key: str) -> None:
    """
    Analiza señales del dia sin enviar ninguna orden al mercado.

    Descarga datos, calcula indicadores y muestra las señales detectadas.
    Muestra tambien el universo activo y si fue generado por el screener
    o por el fallback estatico.

    Args:
        api_key: Clave publica de Alpaca.
        secret_key: Clave secreta de Alpaca.
    """
    logger.info("=" * 60)
    logger.info("MODO: Dry Run (sin ordenes reales)")
    logger.info("=" * 60)

    data_client = DataClient(api_key, secret_key)
    order_manager = OrderManager(api_key, secret_key)

    # ─── Obtener universo activo ──────────────────────────────────────────────
    screener = UniverseScreener(api_key, secret_key)
    active_tickers = screener.get_active_universe()
    status = screener.get_status()

    equity = order_manager.get_account_equity()
    notional, fraction = compute_notional(
        equity,
        p=KELLY_WIN_RATE,
        b=KELLY_WIN_LOSS_RATIO,
        max_pct=MAX_POSITION_PCT,
        kelly_multiplier=KELLY_FRACTION,
    )

    logger.info(f"Equity: ${equity:,.2f} | Kelly notional: ${notional:,.2f}")

    # Descargar datos historicos
    data_dict = data_client.get_historical_data(active_tickers)

    print("\n" + "=" * 70)
    print("  DRY RUN — Señales de Swing Trading (Mean Reversion)")
    print("=" * 70)
    print(f"  Universo: {status['source']} | "
          f"Actualizado: {status.get('last_updated', 'N/A') or 'Fallback estatico'}")
    print(f"  Tickers activos: {active_tickers}")
    print("-" * 70)
    print(f"  {'Ticker':<8} {'Close':>10} {'SMA-200':>10} {'RSI-4':>8} {'4D':>5} {'SEÑAL':>8}")
    print("-" * 70)

    signals_found = 0
    for ticker in active_tickers:
        if ticker not in data_dict:
            print(f"  {ticker:<8} {'SIN DATOS':>10}")
            continue

        df = compute_indicators(data_dict[ticker])
        summary = get_signal_summary(ticker, df)

        signal_str = "✓ BUY" if summary["signal"] else "—"
        consec_str = "SI" if summary.get("consec_down") else "NO"

        print(
            f"  {ticker:<8} "
            f"${summary.get('close', 0):>9.2f} "
            f"${summary.get('sma_200', 0) or 0:>9.2f} "
            f"{summary.get('rsi_4', 0) or 0:>8.2f} "
            f"{consec_str:>5} "
            f"{signal_str:>8}"
        )

        if summary["signal"]:
            signals_found += 1

    print("-" * 70)
    print(f"  Señales detectadas: {signals_found}/{len(active_tickers)}")
    if signals_found > 0:
        print(f"  Notional por señal: ${notional:,.2f} (Half-Kelly {fraction*100:.1f}%)")
    print("=" * 70 + "\n")

    logger.info(f"Dry run completado: {signals_found} señales detectadas.")


# ─── Ciclo principal de señales diario ───────────────────────────────────────

async def run_daily_signals(
    data_client: DataClient,
    order_manager: OrderManager,
    supervisor: PositionSupervisor,
    trade_logger: TradeLogger,
    screener: UniverseScreener,
) -> None:
    """
    Ejecuta el ciclo de deteccion de señales y envio de ordenes.

    Este ciclo se ejecuta una vez al dia, al inicio de la sesion de mercado.
    Las posiciones abiertas quedan bajo supervision del PositionSupervisor.
    Usa el universo activo del UniverseScreener (dinamico o fallback).

    Args:
        data_client: Cliente de datos historicos.
        order_manager: Gestor de ordenes de mercado.
        supervisor: Supervisor asyncio de TP/SL.
        trade_logger: Bitacora SQLite.
        screener: Screener de universo para obtener tickers activos.
    """
    logger.info("─" * 60)
    logger.info("INICIO DEL CICLO DE SEÑALES DIARIO")
    logger.info("─" * 60)

    # ─── Paso 0: Obtener universo activo ─────────────────────────────────────
    active_tickers = screener.get_active_universe()
    logger.info(f"Universo activo ({len(active_tickers)} tickers): {active_tickers}")

    # ─── Paso 1: Obtener equity actualizado ──────────────────────────────────
    equity = await asyncio.get_event_loop().run_in_executor(
        None, order_manager.get_account_equity
    )

    # ─── Paso 2: Calcular notional via Half-Kelly ──────────────────────────
    notional, kelly_fraction_applied = compute_notional(
        account_equity=equity,
        p=KELLY_WIN_RATE,
        b=KELLY_WIN_LOSS_RATIO,
        max_pct=MAX_POSITION_PCT,
        kelly_multiplier=KELLY_FRACTION,
    )

    if notional <= 0:
        logger.warning("Notional Kelly = 0. No se generan ordenes este ciclo.")
        return

    # ─── Paso 2.5: Validar Regimen de Mercado ────────────────────────────────
    logger.info(f"Validando regimen de mercado con {MARKET_REGIME_TICKER}...")
    regime_data = await asyncio.get_event_loop().run_in_executor(
        None, data_client.get_historical_data, [MARKET_REGIME_TICKER]
    )
    if MARKET_REGIME_TICKER in regime_data and not regime_data[MARKET_REGIME_TICKER].empty:
        df_regime = regime_data[MARKET_REGIME_TICKER]
        if len(df_regime) >= 2:
            last_close = df_regime['close'].iloc[-1]
            prev_close = df_regime['close'].iloc[-2]
            daily_return = (last_close - prev_close) / prev_close
            
            if daily_return < -0.015:  # Caída > 1.5%
                logger.warning(
                    f"REGIMEN DE MERCADO NEGATIVO: {MARKET_REGIME_TICKER} cayó "
                    f"{daily_return*100:.2f}% ayer. Suspendiendo compras hoy."
                )
                return
            else:
                logger.info(f"Regimen de mercado OK: {MARKET_REGIME_TICKER} return = {daily_return*100:.2f}%")

    # ─── Paso 3: Descargar datos historicos ──────────────────────────────────
    logger.info("Descargando datos historicos...")
    data_dict = await asyncio.get_event_loop().run_in_executor(
        None, data_client.get_historical_data, active_tickers
    )

    # ─── Paso 4: Detectar señales y ejecutar compras ─────────────────────────
    orders_placed = 0
    for ticker in active_tickers:
        # No comprar si ya hay posicion abierta en este ticker
        if supervisor.is_position_active(ticker):
            logger.debug(f"[{ticker}] Ya tiene posicion activa. Se omite señal.")
            continue

        if ticker not in data_dict:
            logger.warning(f"[{ticker}] Sin datos historicos. Ticker omitido.")
            continue

        df = compute_indicators(data_dict[ticker])
        signal = generate_signal(df)

        if not signal:
            logger.debug(f"[{ticker}] Sin señal de compra.")
            continue

        logger.info(f"[{ticker}] SEÑAL DE COMPRA DETECTADA. Enviando orden...")

        # Enviar orden de compra notional
        order = await asyncio.get_event_loop().run_in_executor(
            None, order_manager.submit_buy, ticker, notional
        )

        if order is None:
            logger.error(f"[{ticker}] Orden rechazada por la API.")
            continue

        # Obtener precio de entrada aproximado del ultimo cierre
        entry_price = float(df["close"].iloc[-1])
        # Cantidad estimada de acciones = notional / precio_entrada
        estimated_qty = notional / entry_price if entry_price > 0 else 0.0

        # Registrar compra en el trade log
        trade_logger.log_entry({
            "ticker": ticker,
            "notional": notional,
            "entry_price": entry_price,
            "qty": estimated_qty,
            "kelly_pct": kelly_fraction_applied,
        })

        # Registrar posicion para supervision TP/SL
        supervisor.add_position(
            symbol=ticker,
            entry_price=entry_price,
            qty=estimated_qty,
            notional=notional,
            kelly_pct=kelly_fraction_applied,
        )

        orders_placed += 1
        logger.info(
            f"[{ticker}] Orden enviada: ${notional:.2f} nocional | "
            f"Precio ref: ${entry_price:.2f} | "
            f"Order ID: {order.id}"
        )

    # ─── Paso 5: Iniciar supervision de nuevas posiciones ────────────────────
    if orders_placed > 0:
        supervisor.start()
        logger.info(
            f"Ciclo completado: {orders_placed} ordenes enviadas. "
            "Supervisor asyncio activo."
        )
    else:
        logger.info("Ciclo completado: Sin señales de compra hoy.")


# ─── Ciclo de señales cripto (independiente, 24/7) ───────────────────────────

async def run_crypto_signals(
    crypto_data_client: CryptoDataClient,
    crypto_order_manager: CryptoOrderManager,
    crypto_supervisor: CryptoPositionSupervisor,
    trade_logger: TradeLogger,
    crypto_screener: CryptoUniverseScreener,
) -> None:
    """
    Ciclo de deteccion de señales cripto. Corre cada hora (mercado 24/7).
    Completamente aislado del ciclo de equities.
    """
    logger.info("[CRYPTO] Iniciando ciclo de señales cripto...")

    # Rehidratar y arrancar supervisión de posiciones vivas en Alpaca
    rehydrated = await asyncio.get_event_loop().run_in_executor(
        None, crypto_supervisor.rehydrate_positions
    )
    if rehydrated > 0:
        crypto_supervisor.start()

    active_symbols = crypto_screener.get_active_universe()
    logger.info(f"[CRYPTO] Universo activo: {active_symbols}")

    equity = await asyncio.get_event_loop().run_in_executor(
        None, crypto_order_manager.get_account_equity
    )
    notional, kelly_pct = compute_notional(
        account_equity=equity,
        p=KELLY_WIN_RATE,
        b=KELLY_WIN_LOSS_RATIO,
        max_pct=CRYPTO_MAX_POSITION_PCT,
        kelly_multiplier=KELLY_FRACTION,
    )

    if notional <= 0:
        logger.warning("[CRYPTO] Notional Kelly=0. Sin ordenes en este ciclo.")
        return

    data_dict = await asyncio.get_event_loop().run_in_executor(
        None, crypto_data_client.get_historical_data, active_symbols
    )

    orders_placed = 0
    cache_prices = {}

    for symbol in active_symbols:
        if symbol not in data_dict:
            logger.warning(f"[CRYPTO] {symbol}: Sin datos. Omitido.")
            continue

        df = compute_crypto_indicators(data_dict[symbol])
        
        # Guardar datos en el cache de precios para el Dashboard
        if len(df) > 0:
            last = df.iloc[-1]
            prev = df.iloc[-2] if len(df) > 1 else last
            pct_change = 0.0
            if prev["close"] > 0:
                pct_change = ((last["close"] - prev["close"]) / prev["close"]) * 100
                
            cache_prices[symbol] = {
                "price": float(last["close"]),
                "rsi": float(last["rsi"]) if "rsi" in df.columns and not pd.isna(last.get("rsi", float("nan"))) else None,
                "pct_change": float(pct_change)
            }

        if crypto_supervisor.is_position_active(symbol):
            continue

        if not generate_crypto_signal(df):
            continue

        # ── Control Cuantitativo de Riesgo: verificar broker & Portfolio Heat ───
        can_open, risk_reason = await asyncio.get_event_loop().run_in_executor(
            None,
            crypto_order_manager.can_open_position,
            symbol,
            notional,
            CRYPTO_MAX_POSITION_PCT,
        )
        if not can_open:
            logger.warning(
                f"[CRYPTO RISK] {symbol}: Orden omitida por gestion de riesgo: {risk_reason}"
            )
            continue

        logger.info(f"[CRYPTO] {symbol}: SEÑAL DE COMPRA. Enviando orden bracket...")

        entry_price = float(last["close"])
        atr_val = float(last["atr"]) if "atr" in df.columns and not pd.isna(last.get("atr", float("nan"))) else None
        qty = notional / entry_price if entry_price > 0 else 0.0

        # ── Calcular precios de TP y SL con ATR (o fallback % fijo) ──────────
        if atr_val and not pd.isna(atr_val) and atr_val > 0:
            from config import CRYPTO_ATR_TP_MULT, CRYPTO_ATR_SL_MULT
            take_profit_price = entry_price + (atr_val * CRYPTO_ATR_TP_MULT)
            stop_loss_price   = entry_price - (atr_val * CRYPTO_ATR_SL_MULT)
        else:
            from config import CRYPTO_TAKE_PROFIT_PCT, CRYPTO_STOP_LOSS_PCT
            take_profit_price = entry_price * (1.0 + CRYPTO_TAKE_PROFIT_PCT)
            stop_loss_price   = entry_price * (1.0 - CRYPTO_STOP_LOSS_PCT)

        # El precio limite de entrada se fija +0.2% sobre el mercado para
        # garantizar fill rapido (sin quedar en libro indefinidamente).
        limit_entry_price = entry_price * 1.002

        logger.info(
            f"[CRYPTO] {symbol}: entry_limit=${limit_entry_price:.6f} | "
            f"TP=${take_profit_price:.6f} | SL=${stop_loss_price:.6f} | "
            f"qty={qty:.8f} | ATR={f'{atr_val:.4f}' if atr_val else 'N/A'}"
        )

        # ── Enviar orden Bracket a Alpaca ─────────────────────────────────────
        order = await asyncio.get_event_loop().run_in_executor(
            None,
            crypto_order_manager.submit_bracket_buy,
            symbol,
            qty,
            limit_entry_price,
            take_profit_price,
            stop_loss_price,
        )
        if order is None:
            logger.error(
                f"[CRYPTO] {symbol}: Orden bracket rechazada. "
                "Abortando señal para este simbolo."
            )
            continue

        bracket_order_id = str(order.id) if hasattr(order, "id") else None

        trade_logger.log_crypto_entry({
            "ticker": symbol,
            "notional": notional,
            "entry_price": entry_price,
            "qty": qty,
            "kelly_pct": kelly_pct,
            "atr_at_entry": atr_val,
        })

        crypto_supervisor.add_position(
            symbol=symbol,
            entry_price=entry_price,
            qty=qty,
            notional=notional,
            kelly_pct=kelly_pct,
            atr_at_entry=atr_val,
            bracket_order_id=bracket_order_id,
        )
        orders_placed += 1
        logger.info(
            f"[CRYPTO] {symbol}: Orden bracket enviada. ${notional:.2f} nocional | "
            f"ATR: {f'{atr_val:.4f}' if atr_val else 'N/A'} | "
            f"BracketID: {bracket_order_id or 'N/A'}"
        )

    if orders_placed > 0:
        crypto_supervisor.start()

    # --- Persistir Cache de Precios ---
    try:
        import json
        from pathlib import Path
        from datetime import timezone
        from config import CRYPTO_CACHE_FILE
        payload = {
            "prices": cache_prices,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "source": "bot_crypto_cycle"
        }
        Path(CRYPTO_CACHE_FILE).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info(f"[CRYPTO] Cache de precios guardado: {len(cache_prices)} pares.")
    except Exception as exc:
        logger.error(f"[CRYPTO] Error guardando cache de precios: {exc}")

    logger.info(f"[CRYPTO] Ciclo cripto completado: {orders_placed} ordenes.")


import pandas as pd  # noqa: E402 — importado aqui para evitar circular imports

# ─── Loop principal del bot ───────────────────────────────────────────────────

async def run_main_loop(
    order_manager: OrderManager,
    data_client: DataClient,
    supervisor: PositionSupervisor,
    trade_logger: TradeLogger,
    screener: UniverseScreener,
    crypto_data_client: CryptoDataClient = None,
    crypto_order_manager: CryptoOrderManager = None,
    crypto_supervisor: CryptoPositionSupervisor = None,
    crypto_screener: CryptoUniverseScreener = None,
) -> None:
    """
    Loop principal asincrono del bot.

    Espera la apertura del mercado, ejecuta el ciclo de señales una vez
    por sesion, mantiene el supervisor activo, y ejecuta el cierre del viernes
    y la actualizacion del universo del lunes.

    Args:
        order_manager: Gestor de ordenes.
        data_client: Cliente de datos historicos.
        supervisor: Supervisor de posiciones.
        trade_logger: Bitacora de operaciones.
        screener: Screener de universo para la seleccion dinamica semanal.
    """
    logger.info("=" * 60)
    logger.info("BOT INICIADO — MODO PRODUCCION (paper=True)")
    logger.info("=" * 60)
    
    trade_logger.notifier.send_message("🚀 <b>Swing Trading Bot Iniciado</b>\nModo Producción Activo (Paper Trading)")

    # Rehidratacion de estado asincrona al arranque (Equities + Cripto)
    logger.info("Re-hidratando estado de posiciones activas (Equities)...")
    rehydrated = await asyncio.get_event_loop().run_in_executor(
        None, supervisor.rehydrate_positions
    )
    if rehydrated > 0:
        supervisor.start()

    if crypto_supervisor is not None:
        logger.info("Re-hidratando estado de posiciones activas (Cripto)...")
        c_rehydrated = await asyncio.get_event_loop().run_in_executor(
            None, crypto_supervisor.rehydrate_positions
        )
        if c_rehydrated > 0:
            crypto_supervisor.start()

    signals_run_today = False
    last_signal_date = None
    last_crypto_hour = None   # Para ejecutar el ciclo cripto 1x por hora

    while True:
        try:
            now_est = datetime.now(EST)
            today = now_est.date()
            current_hour = now_est.replace(minute=0, second=0, microsecond=0)

            # Ejecutar trabajos pendientes del scheduler (viernes 15:45)
            schedule.run_pending()

            # ─── Ejecutar ciclo de señales una vez al dia (al abrir mercado) ──
            if _is_market_open():
                if last_signal_date != today:
                    logger.info(
                        f"Mercado abierto ({now_est.strftime('%H:%M %Z')}). "
                        "Ejecutando ciclo de señales..."
                    )
                    await run_daily_signals(
                        data_client, order_manager, supervisor, trade_logger, screener
                    )
                    last_signal_date = today

            # ─── Ciclo de cripto: cada hora, 24/7 ────────────────────────────
            if (
                crypto_data_client is not None
                and crypto_order_manager is not None
                and crypto_supervisor is not None
                and crypto_screener is not None
                and last_crypto_hour != current_hour
            ):
                await run_crypto_signals(
                    crypto_data_client, crypto_order_manager,
                    crypto_supervisor, trade_logger, crypto_screener,
                )
                last_crypto_hour = current_hour

            # ─── Exportar CSV al fin de mes ────────────────────────────────────
            if _is_last_day_of_month() and now_est.hour == 16:
                month_str = now_est.strftime("%Y-%m")
                logger.info(f"Exportando resumen mensual: {month_str}...")
                trade_logger.export_csv(month=month_str)
                trade_logger.get_monthly_summary(month=month_str)

            # Esperar 30 segundos antes del siguiente ciclo de verificacion
            await asyncio.sleep(30)

        except asyncio.CancelledError:
            logger.info("Loop principal cancelado. Terminando...")
            break
        except KeyboardInterrupt:
            logger.info("Interrupcion de teclado recibida. Terminando...")
            break
        except Exception as exc:
            logger.error(
                f"Error en loop principal: {exc}. "
                "Reintentando en 60 segundos..."
            )
            await asyncio.sleep(60)



# ─── Entry Point ──────────────────────────────────────────────────────────────

def main() -> None:
    """
    Punto de entrada principal del bot.
    Parsea argumentos CLI, inicializa componentes y lanza el modo apropiado.
    """
    _setup_logging()

    parser = argparse.ArgumentParser(
        description="Swing Trading Bot — Alpaca Paper Trading",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos de uso:
  python main.py                      # Iniciar bot en modo produccion
  python main.py --test-connection    # Verificar credenciales y equity
  python main.py --dry-run            # Ver señales sin operar
  python main.py --force-friday-close # Forzar liquidacion manual
  python main.py --sync-trades        # Sincronizar ordenes de Alpaca a trades.db
        """,
    )
    parser.add_argument(
        "--test-connection",
        action="store_true",
        help="Verificar conexion con Alpaca y mostrar estado de cuenta.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analizar señales sin enviar ordenes al mercado.",
    )
    parser.add_argument(
        "--force-friday-close",
        action="store_true",
        help="Forzar liquidacion total (para testing del scheduler).",
    )
    parser.add_argument(
        "--sync-trades",
        action="store_true",
        help="Auditar y sincronizar retroactivamente todas las ordenes de Alpaca a trades.db.",
    )

    args = parser.parse_args()

    # Cargar credenciales
    api_key, secret_key = _load_credentials()

    # ─── Modo: Sincronizacion de Trades ───────────────────────────────────────
    if args.sync_trades:
        from logging_.reconcile import AlpacaReconciler
        from config import DB_PATH
        logger.info("Iniciando reconciliacion retroactiva de ordenes desde Alpaca...")
        reconciler = AlpacaReconciler(api_key, secret_key, DB_PATH, paper=True)
        res = reconciler.sync()
        print("\n" + "=" * 60)
        print("  RECONCILIACION ALPACA -> TRADES.DB COMPLETADA")
        print("=" * 60)
        print(f"  Ordenes Alpaca auditadas: {res['total_alpaca_orders']}")
        print(f"  Equities insertadas:     {res['equities_inserted']}")
        print(f"  Cripto insertadas:       {res['crypto_inserted']}")
        print("=" * 60 + "\n")
        return

    # ─── Modo: Test de conexion ───────────────────────────────────────────────
    if args.test_connection:
        run_connection_test(api_key, secret_key)
        return

    # ─── Modo: Dry Run ────────────────────────────────────────────────────────
    if args.dry_run:
        run_dry_run(api_key, secret_key)
        return

    # ─── Inicializar todos los componentes ────────────────────────────────────
    trade_logger = TradeLogger()
    order_manager = OrderManager(api_key, secret_key)
    data_client = DataClient(api_key, secret_key)
    supervisor = PositionSupervisor(order_manager, trade_logger)
    screener = UniverseScreener(api_key, secret_key)

    # ─── Inicializar componentes de Cripto ────────────────────────────────────
    # DESHABILITADO TEMPORALMENTE (Cripto no funcional)
    # crypto_order_manager = CryptoOrderManager(api_key, secret_key)
    # crypto_data_client = CryptoDataClient(api_key, secret_key)
    # crypto_supervisor = CryptoPositionSupervisor(crypto_order_manager, trade_logger)
    # crypto_screener = CryptoUniverseScreener()
    # logger.info("Modulo de criptomonedas inicializado (CoinGecko + Alpaca Crypto).")

    # Inyectar dependencias en el scheduler (incluyendo el screener)
    init_scheduler(order_manager, supervisor, trade_logger, screener)
    setup_schedule()

    # ─── Modo: Forzar liquidacion de viernes ──────────────────────────────────
    if args.force_friday_close:
        logger.warning("MODO FORZADO: Ejecutando liquidacion del viernes manualmente...")
        # Bypassear el guard de dia/hora para testing
        import scheduler.weekly_close as wc
        wc._order_manager = order_manager
        wc._supervisor = supervisor
        wc._trade_logger = trade_logger

        # Obtener posiciones y cerrar todo
        open_positions = order_manager.get_open_positions()
        logger.info(f"Posiciones abiertas: {len(open_positions)}")

        supervisor.stop_all()
        order_manager.close_all()

        logger.info("Liquidacion forzada completada.")
        return

    # ─── Modo: Produccion — Loop principal ────────────────────────────────────
    logger.info("Iniciando loop principal del bot y panel de control (Dashboard)...")
    
    dashboard_process = None
    try:
        # Iniciar panel de control
        dashboard_process = subprocess.Popen(
            [sys.executable, "dashboard_app.py"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        logger.info("Dashboard iniciado en http://localhost:8000")

        asyncio.run(
            run_main_loop(
                order_manager, data_client, supervisor, trade_logger, screener,
                crypto_data_client=None,
                crypto_order_manager=None,
                crypto_supervisor=None,
                crypto_screener=None,
            )
        )
    except KeyboardInterrupt:
        logger.info("Bot detenido por el usuario.")
    finally:
        if dashboard_process:
            logger.info("Deteniendo Dashboard...")
            dashboard_process.terminate()
            dashboard_process.wait()
        logger.info("Bot terminado. Hasta la proxima sesion.")


if __name__ == "__main__":
    main()
