"""
ml/labeling.py — Triple Barrier Labeling para Meta-Labeling
============================================================
Implementa el método de etiquetado Triple Barrier de Marcos López de Prado
(Advances in Financial Machine Learning, 2018, Capítulo 3).

Concepto:
    Dado un evento de señal en t₀ (donde Capa 1 generó Long=True), se
    construyen tres barreras alrededor del precio de entrada:

        ┌──────────── BARRERA SUPERIOR (TP): entry + ATR × pt_mult
        │
    ────●──── entrada en t₀ (precio = close[t₀])
        │
        └──────────── BARRERA INFERIOR (SL): entry - ATR × sl_mult

    La barrera de tiempo es una ventana de t1_horizon períodos hacia adelante.

    El label se asigna según cuál barrera se toca PRIMERO:
        1 → Barrera Superior (TP hit) → Meta-Label positivo (operar)
        0 → Barrera Inferior (SL hit) o Time Exit → Meta-Label negativo (no operar)

Ventaja sobre TP/SL fijo:
    Las barreras son DINÁMICAS (basadas en ATR), lo que las hace proporcionales
    a la volatilidad real del activo en cada momento. Esto evita que en períodos
    de baja volatilidad el SL sea demasiado ancho y viceversa.

Consideraciones de Data Integrity:
    - window = df.iloc[loc_idx + 1 : end_idx] → solo datos FUTUROS al evento.
    - Las barreras usan ATR calculado en t₀ (pasado), no datos futuros.
    - No hay lookahead bias en el cálculo de barreras.

Referencia:
    López de Prado, M. (2018). "Advances in Financial Machine Learning".
    Wiley. ISBN 978-1-119-48208-6. Capítulos 3 y 4.
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def get_daily_volatility(
    df: pd.DataFrame,
    span: int = 20,
) -> pd.Series:
    """
    Calcula la volatilidad diaria como desviación estándar de log-retornos (EMA).

    Usado como alternativa al ATR cuando la columna 'atr' no está disponible
    (ej. para equities sin ATR precalculado en ingestion.py).

    Args:
        df: DataFrame con columna 'close'.
        span: Período de suavizado EMA (default: 20).

    Returns:
        pd.Series con volatilidad EMA de log-retornos, alineada al índice de df.
    """
    log_ret = np.log(df["close"] / df["close"].shift(1))
    return log_ret.ewm(span=span, min_periods=span).std()


def get_triple_barrier_labels(
    df: pd.DataFrame,
    signal_dates: pd.DatetimeIndex,
    pt_mult: float = 2.0,
    sl_mult: float = 1.0,
    t1_horizon: int = 72,
    atr_col: str = "atr",
    use_volatility_fallback: bool = True,
) -> pd.Series:
    """
    Aplica Triple Barrier Labeling a los índices de señales primarias.

    Para cada fecha en signal_dates (donde Capa 1 generó signal=True),
    examina la ventana de precios futuros y asigna:
        1 → el precio toca la barrera de TP antes que SL o tiempo.
        0 → el precio toca SL o expira el tiempo sin alcanzar TP.

    Args:
        df: DataFrame OHLCV + ATR, indexado cronológicamente (DatetimeIndex).
            Debe estar ordenado ascendentemente por fecha.
        signal_dates: DatetimeIndex de los momentos donde Capa 1 generó True.
            Solo se etiquetan estos índices.
        pt_mult: Multiplicador de ATR para la barrera de Take Profit (default: 2.0).
                 Con pt_mult=2.0 y sl_mult=1.0 se obtiene un ratio R:R de 2:1.
        sl_mult: Multiplicador de ATR para la barrera de Stop Loss (default: 1.0).
        t1_horizon: Número máximo de períodos (filas) hacia adelante como barrera temporal.
                    Para cripto horario: 72 (≈ 3 días).
                    Para equities diario: 5 (≈ 1 semana de trading).
        atr_col: Nombre de la columna ATR en df (default: 'atr').
        use_volatility_fallback: Si True, usa std de log-retornos si ATR no está disponible.

    Returns:
        pd.Series de labels {0, 1} indexada por signal_dates.
        Eventos sin datos suficientes o sin ATR válido son descartados.

    Example:
        >>> labels = get_triple_barrier_labels(
        ...     df=df_hourly,
        ...     signal_dates=signal_index,
        ...     pt_mult=2.0, sl_mult=1.0, t1_horizon=72
        ... )
        >>> print(f"TP Rate: {labels.mean():.1%}")
    """
    if df.empty:
        logger.warning("[Labeling] DataFrame vacío. Retornando serie vacía.")
        return pd.Series(dtype=int, name="meta_label")

    # Validar que el índice es un DatetimeIndex ordenado
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError(
            "El DataFrame debe tener un DatetimeIndex. "
            "Verifica que el ingestion module set_index('timestamp') fue llamado."
        )

    # Calcular volatilidad fallback si ATR no está disponible
    if atr_col not in df.columns and use_volatility_fallback:
        logger.info(
            f"[Labeling] Columna '{atr_col}' no encontrada. "
            "Usando desviación estándar de log-retornos como proxy de volatilidad."
        )
        df = df.copy()
        vol_series = get_daily_volatility(df, span=20)
        df[atr_col] = df["close"] * vol_series  # Volatilidad en términos de precio

    labels: dict = {}
    skipped = 0
    tp_hits = 0
    sl_time_hits = 0

    for t0 in signal_dates:
        # Verificar que t0 existe en el índice del DataFrame
        if t0 not in df.index:
            logger.debug(f"[Labeling] {t0}: Fecha no encontrada en df. Descartada.")
            skipped += 1
            continue

        # Obtener posición entera del índice para slicing eficiente
        loc_idx = df.index.get_loc(t0)
        if isinstance(loc_idx, slice):
            loc_idx = loc_idx.start

        entry_price = float(df["close"].iloc[loc_idx])

        # ── Calcular barreras (usando ATR en t₀, NO datos futuros) ────────────
        atr_val = None
        if atr_col in df.columns:
            raw_atr = df[atr_col].iloc[loc_idx]
            if not (np.isnan(raw_atr) or raw_atr <= 0):
                atr_val = float(raw_atr)

        if atr_val is None:
            logger.debug(
                f"[Labeling] {t0}: ATR no válido (val={atr_val}). "
                "Señal descartada del dataset de entrenamiento."
            )
            skipped += 1
            continue

        upper_barrier = entry_price + (atr_val * pt_mult)
        lower_barrier = entry_price - (atr_val * sl_mult)

        # ── Ventana temporal: solo filas DESPUÉS de t₀ (sin lookahead) ────────
        end_idx = min(loc_idx + t1_horizon + 1, len(df))
        window = df.iloc[loc_idx + 1 : end_idx]

        if window.empty:
            logger.debug(f"[Labeling] {t0}: Sin datos futuros suficientes. Descartada.")
            skipped += 1
            continue

        # ── Evaluación de barreras en orden cronológico ────────────────────────
        label = 0  # Default: Time Exit (meta-label negativo)
        barrier_hit = "TIME"

        for _, bar in window.iterrows():
            # Usar high/low de la vela para detectar toque de barrera intradía
            bar_high = float(bar.get("high", bar["close"]))
            bar_low  = float(bar.get("low",  bar["close"]))

            if bar_high >= upper_barrier:
                label = 1
                barrier_hit = "TP"
                break
            elif bar_low <= lower_barrier:
                label = 0
                barrier_hit = "SL"
                break

        labels[t0] = label

        if label == 1:
            tp_hits += 1
        else:
            sl_time_hits += 1

        logger.debug(
            f"[Labeling] {t0}: entry={entry_price:.4f} | "
            f"ATR={atr_val:.4f} | "
            f"TP={upper_barrier:.4f} | SL={lower_barrier:.4f} | "
            f"→ {barrier_hit} (label={label})"
        )

    result = pd.Series(labels, name="meta_label", dtype=int)
    total = len(result)

    if total > 0:
        logger.info(
            f"[Labeling] Etiquetado completado: {total} señales etiquetadas | "
            f"Descartadas: {skipped} | "
            f"TP Rate: {tp_hits/total*100:.1f}% ({tp_hits}) | "
            f"SL/Time Rate: {sl_time_hits/total*100:.1f}% ({sl_time_hits})"
        )
    else:
        logger.warning(
            f"[Labeling] 0 señales etiquetadas. "
            f"Descartadas: {skipped}. Revisa ATR y datos históricos."
        )

    return result


def get_purged_train_test_split(
    features: pd.DataFrame,
    labels: pd.Series,
    embargo_pct: float = 0.01,
    test_size: float = 0.2,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series, pd.Series]:
    """
    Split cronológico Purged + Embargo para evitar Data Leakage en series temporales.

    A diferencia de train_test_split(shuffle=True), este split:
        1. Respeta el orden cronológico (train = primero, test = último).
        2. Aplica embargo: elimina muestras de train que estén dentro del
           lookback period del test set (evita que la barrera temporal
           de una señal en train contamine el test set).

    Args:
        features: DataFrame de features indexado por DatetimeIndex.
        labels: pd.Series con los meta-labels {0, 1}, mismo índice que features.
        embargo_pct: Fracción del dataset a usar como buffer de embargo (default: 1%).
        test_size: Fracción del dataset para el test set (default: 20%).

    Returns:
        Tupla (X_train, X_test, y_train, y_test) con índice cronológico.
    """
    n = len(features)
    test_start_idx  = int(n * (1 - test_size))
    embargo_end_idx = int(n * (1 - test_size - embargo_pct))

    # Test: últimas test_size% de observaciones
    X_test  = features.iloc[test_start_idx:]
    y_test  = labels.iloc[test_start_idx:]

    # Train: primeras (1 - test_size - embargo_pct)% (con embargo eliminado)
    X_train = features.iloc[:embargo_end_idx]
    y_train = labels.iloc[:embargo_end_idx]

    logger.info(
        f"[Split] Purged Split: train={len(X_train)} | embargo={test_start_idx - embargo_end_idx} | "
        f"test={len(X_test)} | "
        f"Train TP Rate: {y_train.mean():.1%} | Test TP Rate: {y_test.mean():.1%}"
    )

    return X_train, X_test, y_train, y_test
