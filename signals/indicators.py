"""
signals/indicators.py — Señales Cuantitativas de Mean Reversion
================================================================
Calcula indicadores tecnicos (SMA-200, RSI-4) y genera la señal
booleana de entrada LONG basada en la estrategia de Mean Reversion.

Implementacion con pandas puro (sin numba/pandas-ta) para compatibilidad
con Python 3.14+.

Logica de señal de compra (LONG):
  1. El precio de cierre esta ESTRICTAMENTE sobre la SMA-200 (filtro de tendencia).
  2. Y al menos UNA de estas condiciones:
     a. RSI-4 < 30 (condicion de sobreventa).
     b. Los ultimos 4 cierres han sido consecutivamente decrecientes.
"""

import logging
import numpy as np
import pandas as pd

from config import (
    SMA_PERIOD,
    RSI_PERIOD,
    RSI_OVERSOLD,
    CONSEC_DOWN_DAYS,
    CRYPTO_SMA_MACRO,
    CRYPTO_EMA_FAST,
    CRYPTO_EMA_SLOW,
    CRYPTO_RSI_PERIOD,
    CRYPTO_RSI_MOMENTUM_MIN,
)

logger = logging.getLogger(__name__)

# Nombres canonicos de las columnas de indicadores
COL_SMA = f"SMA_{SMA_PERIOD}"
COL_RSI = f"RSI_{RSI_PERIOD}"
COL_CONSEC_DOWN = "CONSEC_DOWN"


def _compute_sma(series: pd.Series, period: int) -> pd.Series:
    """
    Media Movil Simple (SMA) usando pandas rolling.
    Equivalente a pandas_ta.sma(length=period) sin numba.
    """
    return series.rolling(window=period, min_periods=period).mean()


def _compute_ema(series: pd.Series, period: int) -> pd.Series:
    """
    Media Movil Exponencial (EMA).
    Equivalente a pandas_ta.ema(length=period) sin numba.
    """
    return series.ewm(span=period, min_periods=period).mean()


def _compute_rsi(series: pd.Series, period: int) -> pd.Series:
    """
    Indice de Fuerza Relativa (RSI) usando el metodo de Wilder (EMA suavizado).
    Equivalente a pandas_ta.rsi(length=period) sin numba.

    Metodo: EMA con alpha = 1/period (com = period - 1).
    """
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    # Wilder Smoothing: EMA con alpha=1/period, equivalente a com=period-1
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def check_consecutive_down(df: pd.DataFrame, n: int = CONSEC_DOWN_DAYS) -> bool:
    """
    Verifica si los ultimos N cierres son estrictamente decrecientes.

    Un cierre estrictamente decreciente significa que:
        close[t] < close[t-1] < close[t-2] < ... < close[t-(n-1)]

    Args:
        df: DataFrame con columna 'close' ordenado cronologicamente.
        n: Numero de dias consecutivos a verificar (default: CONSEC_DOWN_DAYS=4).

    Returns:
        True si los ultimos n cierres son estrictamente decrecientes, False en caso contrario.
        Retorna False si hay menos de n filas disponibles.
    """
    if len(df) < n:
        return False

    closes = df["close"].iloc[-n:].values
    # Verificar que cada cierre es menor al anterior
    return all(closes[i] < closes[i - 1] for i in range(1, n))


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calcula SMA-200, RSI-4 y la señal de dias consecutivos a la baja.

    Utiliza pandas-ta para el calculo vectorizado de los indicadores.
    Los indicadores son agregados como nuevas columnas al DataFrame.

    Args:
        df: DataFrame con columna 'close' y al menos 200 sesiones de datos.

    Returns:
        DataFrame original con columnas adicionales:
            - SMA_200: Media Movil Simple de 200 periodos.
            - RSI_4: Indice de Fuerza Relativa de 4 periodos.
            - CONSEC_DOWN: True si los ultimos 4 cierres son decrecientes.

    Raises:
        ValueError: Si la columna 'close' no existe en el DataFrame.
    """
    if "close" not in df.columns:
        raise ValueError(
            "El DataFrame debe contener la columna 'close'. "
            f"Columnas disponibles: {list(df.columns)}"
        )

    df = df.copy()

    # ─── SMA-200 (pandas rolling mean) ───────────────────────────────────────
    df[COL_SMA] = _compute_sma(df["close"], SMA_PERIOD)

    # ─── RSI-4 (Wilder EMA method) ────────────────────────────────────────────
    df[COL_RSI] = _compute_rsi(df["close"], RSI_PERIOD)

    # ─── Dias Consecutivos a la Baja (rolling window) ─────────────────────────
    # Para cada fila, verificar si los ultimos CONSEC_DOWN_DAYS cierres decrecen
    consec_down_values = []
    for idx in range(len(df)):
        sub_df = df.iloc[: idx + 1]
        consec_down_values.append(check_consecutive_down(sub_df, CONSEC_DOWN_DAYS))
    df[COL_CONSEC_DOWN] = consec_down_values

    logger.debug(
        f"Indicadores calculados: SMA={df[COL_SMA].iloc[-1]:.2f}, "
        f"RSI={df[COL_RSI].iloc[-1]:.2f}, "
        f"ConsecDown={df[COL_CONSEC_DOWN].iloc[-1]}"
        if not df.empty else "DataFrame vacio."
    )

    return df


def generate_signal(df: pd.DataFrame) -> bool:
    """
    Evalua la condicion de señal de compra LONG para la ultima sesion.

    Logica estricta (TODAS las condiciones deben cumplirse):
        SEÑAL = (close_actual > SMA_200_actual)
                AND
                (RSI_4_actual < RSI_OVERSOLD OR CONSEC_DOWN == True)

    Args:
        df: DataFrame con indicadores ya calculados (output de compute_indicators).

    Returns:
        True si se detecta una señal de entrada LONG valida.
        False en cualquier otro caso (incluye NaN en indicadores por datos insuficientes).
    """
    if df.empty:
        logger.debug("DataFrame vacio: sin señal.")
        return False

    # Requiere columnas de indicadores
    required_cols = [COL_SMA, COL_RSI, COL_CONSEC_DOWN]
    for col in required_cols:
        if col not in df.columns:
            logger.warning(
                f"Columna '{col}' no encontrada. Ejecuta compute_indicators() primero."
            )
            return False

    # Obtener la ultima fila disponible
    last = df.iloc[-1]

    close = last["close"]
    sma_200 = last[COL_SMA]
    rsi_4 = last[COL_RSI]
    consec_down = bool(last[COL_CONSEC_DOWN])

    # Validar que los indicadores no sean NaN
    import math
    if any(math.isnan(v) for v in [close, sma_200, rsi_4] if isinstance(v, float)):
        logger.debug(
            f"Indicadores con NaN (datos insuficientes para SMA-{SMA_PERIOD}). Sin señal."
        )
        return False

    # Condicion 1: Precio sobre la SMA-200 (filtro de tendencia alcista)
    above_sma = close > sma_200

    # Condicion 2: Sobreventa (RSI < 30) O consolidacion bajista (4 dias consecutivos)
    oversold_rsi = rsi_4 < RSI_OVERSOLD
    signal = above_sma and (oversold_rsi or consec_down)

    logger.info(
        f"Señal evaluada -> close={close:.2f}, SMA={sma_200:.2f}, "
        f"RSI={rsi_4:.2f}, ConsecDown={consec_down} -> "
        f"AboveSMA={above_sma}, OversoldRSI={oversold_rsi} -> SEÑAL={signal}"
    )

    return bool(signal)  # Garantizar Python bool nativo (no np.bool_)


def get_signal_summary(ticker: str, df: pd.DataFrame) -> dict:
    """
    Retorna un resumen estructurado del estado del indicador para un ticker.

    Util para el modo --dry-run y para debugging.

    Args:
        ticker: Simbolo del ticker.
        df: DataFrame con indicadores calculados.

    Returns:
        Diccionario con estado completo del ticker y su señal.
    """
    if df.empty or COL_SMA not in df.columns:
        return {
            "ticker": ticker,
            "signal": False,
            "error": "Datos o indicadores insuficientes",
        }

    last = df.iloc[-1]
    return {
        "ticker": ticker,
        "close": round(float(last["close"]), 4),
        "sma_200": round(float(last[COL_SMA]), 4) if pd.notna(last[COL_SMA]) else None,
        "rsi_4": round(float(last[COL_RSI]), 4) if pd.notna(last[COL_RSI]) else None,
        "consec_down": bool(last[COL_CONSEC_DOWN]),
        "above_sma": bool(last["close"] > last[COL_SMA]) if pd.notna(last[COL_SMA]) else False,
        "signal": generate_signal(df),
    }


# ─── Funciones de Cripto (parametros aislados CRYPTO_*) ───────────────────────

COL_CRYPTO_SMA_MACRO = f"SMA_{CRYPTO_SMA_MACRO}"
COL_CRYPTO_EMA_FAST = f"EMA_{CRYPTO_EMA_FAST}"
COL_CRYPTO_EMA_SLOW = f"EMA_{CRYPTO_EMA_SLOW}"
COL_CRYPTO_RSI = f"RSI_{CRYPTO_RSI_PERIOD}"


def compute_crypto_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calcula indicadores tecnicos para activos cripto usando la estrategia Momentum Crossover.

    Args:
        df: DataFrame con columna 'close' y suficientes sesiones historicas (>=200).

    Returns:
        DataFrame con columnas adicionales:
            - SMA_200 (Macro filter)
            - EMA_9 (Fast)
            - EMA_21 (Slow)
            - RSI_14 (Momentum strength)
    """
    if "close" not in df.columns:
        raise ValueError(
            "El DataFrame debe contener la columna 'close'. "
            f"Columnas disponibles: {list(df.columns)}"
        )

    df = df.copy()
    
    # Filtro de tendencia macro
    df[COL_CRYPTO_SMA_MACRO] = _compute_sma(df["close"], CRYPTO_SMA_MACRO)
    
    # EMAs para Crossover
    df[COL_CRYPTO_EMA_FAST] = _compute_ema(df["close"], CRYPTO_EMA_FAST)
    df[COL_CRYPTO_EMA_SLOW] = _compute_ema(df["close"], CRYPTO_EMA_SLOW)
    
    # RSI para fuerza del momentum
    df[COL_CRYPTO_RSI] = _compute_rsi(df["close"], CRYPTO_RSI_PERIOD)

    return df


def generate_crypto_signal(df: pd.DataFrame) -> bool:
    """
    Evalua la condicion de señal de compra LONG para cripto (Momentum Crossover).

    Logica:
        1. Filtro Macro: close > SMA_200 (mercado alcista)
        2. Gatillo: EMA rápida (9) cruzó por encima de EMA lenta (21)
        3. Filtro de Momentum: RSI_14 > 50

    Args:
        df: DataFrame con indicadores cripto calculados.

    Returns:
        True si hay señal de entrada LONG valida.
    """
    if len(df) < 2:  # Necesitamos al menos 2 barras para evaluar un cruce
        return False

    required = [COL_CRYPTO_SMA_MACRO, COL_CRYPTO_EMA_FAST, COL_CRYPTO_EMA_SLOW, COL_CRYPTO_RSI]
    for col in required:
        if col not in df.columns:
            logger.warning(f"[CRYPTO] Columna '{col}' no encontrada. Sin señal.")
            return False

    last = df.iloc[-1]
    prev = df.iloc[-2]

    close = last["close"]
    sma_macro = last[COL_CRYPTO_SMA_MACRO]
    ema_fast_last = last[COL_CRYPTO_EMA_FAST]
    ema_slow_last = last[COL_CRYPTO_EMA_SLOW]
    ema_fast_prev = prev[COL_CRYPTO_EMA_FAST]
    ema_slow_prev = prev[COL_CRYPTO_EMA_SLOW]
    rsi = last[COL_CRYPTO_RSI]

    import math
    if any(math.isnan(v) for v in [close, sma_macro, ema_fast_last, ema_slow_last, ema_fast_prev, ema_slow_prev, rsi] if isinstance(v, float)):
        return False

    # 1. Filtro de Tendencia Macro
    macro_uptrend = close > sma_macro

    # 2. Crossover Alcista (Fast cruza por encima de Slow)
    # Ayer estaba por debajo o igual, hoy esta estrictamente por encima
    crossover_up = (ema_fast_prev <= ema_slow_prev) and (ema_fast_last > ema_slow_last)

    # 3. Fuerza de Momentum (RSI > 50)
    strong_momentum = rsi > CRYPTO_RSI_MOMENTUM_MIN

    signal = macro_uptrend and crossover_up and strong_momentum

    logger.debug(
        f"[CRYPTO] Signal -> C>{COL_CRYPTO_SMA_MACRO}={macro_uptrend}, "
        f"CrossUp({CRYPTO_EMA_FAST},{CRYPTO_EMA_SLOW})={crossover_up}, "
        f"RSI_{CRYPTO_RSI_PERIOD}={rsi:.1f}>50={strong_momentum} -> SIGNAL={signal}"
    )
    return bool(signal)


def get_crypto_signal_summary(symbol: str, df: pd.DataFrame) -> dict:
    """Retorna un resumen estructurado del estado de señal cripto."""
    if df.empty or COL_CRYPTO_SMA_MACRO not in df.columns:
        return {"ticker": symbol, "signal": False, "error": "Datos insuficientes"}

    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last
    
    atr_val = float(last["atr"]) if "atr" in df.columns and pd.notna(last.get("atr")) else None
    
    crossover = False
    if pd.notna(prev[COL_CRYPTO_EMA_FAST]) and pd.notna(last[COL_CRYPTO_EMA_FAST]):
        crossover = (prev[COL_CRYPTO_EMA_FAST] <= prev[COL_CRYPTO_EMA_SLOW]) and (last[COL_CRYPTO_EMA_FAST] > last[COL_CRYPTO_EMA_SLOW])

    return {
        "ticker": symbol,
        "close": round(float(last["close"]), 4),
        "sma_macro": round(float(last[COL_CRYPTO_SMA_MACRO]), 4) if pd.notna(last[COL_CRYPTO_SMA_MACRO]) else None,
        "ema_fast": round(float(last[COL_CRYPTO_EMA_FAST]), 4) if pd.notna(last[COL_CRYPTO_EMA_FAST]) else None,
        "ema_slow": round(float(last[COL_CRYPTO_EMA_SLOW]), 4) if pd.notna(last[COL_CRYPTO_EMA_SLOW]) else None,
        "rsi": round(float(last[COL_CRYPTO_RSI]), 4) if pd.notna(last[COL_CRYPTO_RSI]) else None,
        "macro_uptrend": bool(last["close"] > last[COL_CRYPTO_SMA_MACRO]) if pd.notna(last[COL_CRYPTO_SMA_MACRO]) else False,
        "crossover_up": crossover,
        "atr": round(atr_val, 4) if atr_val else None,
        "signal": generate_crypto_signal(df),
    }

