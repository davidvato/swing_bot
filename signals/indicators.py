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
