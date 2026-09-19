"""
ml/features.py — Feature Engineering Estacionario para LightGBM
================================================================
Transforma series OHLCV + indicadores técnicos en variables
estacionarias aptas para árboles de decisión (LightGBM / XGBoost).

PRINCIPIO FUNDAMENTAL:
    Ninguna feature puede ser un precio absoluto (Open, High, Low, Close).
    Todas son retornos relativos, distancias porcentuales, ratios o
    indicadores ya normalizados. Series no-estacionarias destruyen la
    generalización de los modelos de árbol (overfitting a niveles de precio).

Features generadas:
    log_ret_1    : Log-retorno del último período (t vs t-1)
    log_ret_5    : Log-retorno de los últimos 5 períodos (momentum corto)
    log_ret_20   : Log-retorno de los últimos 20 períodos (momentum medio)
    dist_sma     : Distancia porcentual del cierre a la SMA: (close-SMA)/SMA
    rsi_norm     : RSI normalizado a [0, 1] (RSI / 100)
    norm_atr     : ATR relativo: ATR / close (volatilidad normalizada)
    vol_ratio    : RVOL — Volumen relativo vs. media móvil de 20 períodos
    consec_down  : Indicador binario de días consecutivos a la baja (0 o 1)

Compatibilidad:
    Funciona con el output de:
        - compute_indicators(df)        → equities (SMA_200, RSI_4)
        - compute_crypto_indicators(df) → cripto   (SMA_50, RSI_4, atr)
"""

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Nombres canónicos de las features — el orden DEBE mantenerse constante
# entre entrenamiento e inferencia para que el booster sea reproducible.
FEATURE_NAMES: list[str] = [
    "log_ret_1",
    "log_ret_5",
    "log_ret_20",
    "dist_sma",
    "rsi_norm",
    "norm_atr",
    "vol_ratio",
    "consec_down",
]


def build_stationary_features(
    df: pd.DataFrame,
    sma_col: str | None = None,
) -> pd.DataFrame:
    """
    Construye el vector de features estacionarias para Meta-Labeling.

    Detecta automáticamente las columnas SMA_*, RSI_* y CONSEC_DOWN*
    producidas por compute_indicators() o compute_crypto_indicators().

    Args:
        df: DataFrame con columnas OHLCV + indicadores calculados.
            Debe provenir DESPUÉS de compute_indicators() para garantizar
            la existencia de SMA_*, RSI_* y CONSEC_DOWN_*.
        sma_col: Nombre explícito de la columna SMA (ej. 'SMA_200', 'SMA_50').
                 Si None, se detecta automáticamente (primer match 'SMA_*').

    Returns:
        pd.DataFrame con exactamente las columnas de FEATURE_NAMES.
        Las filas con NaN son eliminadas (dropna). Para inferencia en
        tiempo real, usar `.iloc[-1:]` sobre el resultado.

    Raises:
        ValueError: Si no se encuentra ninguna columna SMA_* en df.

    Ejemplo:
        >>> df_with_indicators = compute_crypto_indicators(raw_df)
        >>> features = build_stationary_features(df_with_indicators)
        >>> ml_prob = model.predict_proba(features.iloc[-1:])
    """
    if df.empty:
        logger.warning("[Features] DataFrame vacío recibido. Retornando DataFrame vacío.")
        return pd.DataFrame(columns=FEATURE_NAMES)

    f = pd.DataFrame(index=df.index)

    # ── 1. Retornos Logarítmicos (Estacionarios) ──────────────────────────────
    # log(P_t / P_{t-k}) es estacionario para cualquier k.
    # Se usa retrospectivo (.shift positivo) → nunca lookahead bias.
    close = df["close"]
    f["log_ret_1"]  = np.log(close / close.shift(1))
    f["log_ret_5"]  = np.log(close / close.shift(5))
    f["log_ret_20"] = np.log(close / close.shift(20))

    # ── 2. Distancia Normalizada a SMA ────────────────────────────────────────
    # (close - SMA) / SMA → oscila alrededor de 0, sin tendencia de nivel.
    if sma_col is None:
        sma_candidates = [c for c in df.columns if c.startswith("SMA_")]
        if not sma_candidates:
            raise ValueError(
                "No se encontró columna SMA_* en el DataFrame. "
                "Asegúrate de llamar compute_indicators() antes de esta función."
            )
        sma_col = sma_candidates[0]

    sma_series = df[sma_col].replace(0, np.nan)
    f["dist_sma"] = (close - sma_series) / sma_series

    # ── 3. RSI Normalizado [0, 1] ──────────────────────────────────────────────
    # RSI ya es un oscilador relativo. Dividir por 100 para escala [0,1].
    rsi_candidates = [c for c in df.columns if c.startswith("RSI_")]
    if rsi_candidates:
        f["rsi_norm"] = df[rsi_candidates[0]] / 100.0
    else:
        logger.warning("[Features] No se encontró columna RSI_*. Usando NaN → será eliminado.")
        f["rsi_norm"] = np.nan

    # ── 4. Volatilidad Normalizada (Normalized ATR) ───────────────────────────
    # ATR / close → volatilidad relativa al nivel de precio (escala-independiente).
    if "atr" in df.columns:
        f["norm_atr"] = df["atr"] / close.replace(0, np.nan)
    else:
        # Para equities que no calculan ATR en ingestion, usar std rolling como proxy
        logger.debug("[Features] Columna 'atr' no encontrada. Usando std-20 como proxy de volatilidad.")
        f["norm_atr"] = close.pct_change().rolling(window=20, min_periods=10).std()

    # ── 5. Volumen Relativo (RVOL) ────────────────────────────────────────────
    # Volumen actual / media de volumen de 20 períodos → > 1 = spike de volumen.
    if "volume" in df.columns:
        vol_ma = df["volume"].rolling(window=20, min_periods=10).mean()
        f["vol_ratio"] = df["volume"] / vol_ma.replace(0, np.nan)
    else:
        logger.debug("[Features] Columna 'volume' no encontrada. vol_ratio = 1.0 (neutro).")
        f["vol_ratio"] = 1.0

    # ── 6. Días Consecutivos a la Baja (Señal Categórica Binaria) ────────────
    # Convierte el bool/object de CONSEC_DOWN a int {0, 1} para LightGBM.
    consec_candidates = [c for c in df.columns if "CONSEC_DOWN" in c.upper()]
    if consec_candidates:
        f["consec_down"] = df[consec_candidates[0]].astype(int)
    else:
        logger.debug("[Features] Columna CONSEC_DOWN no encontrada. consec_down = 0.")
        f["consec_down"] = 0

    # ── Asegurar orden canónico y eliminar filas con NaN ──────────────────────
    result = f[FEATURE_NAMES].dropna()

    if result.empty:
        logger.warning(
            "[Features] El DataFrame de features quedó vacío después de dropna. "
            "Puede indicar datos históricos insuficientes."
        )
    else:
        logger.debug(
            f"[Features] {len(result)} filas de features generadas "
            f"(de {len(df)} originales). SMA detectada: {sma_col}."
        )

    return result
