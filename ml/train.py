"""
ml/train.py — Pipeline Offline de Entrenamiento del Meta-Model
==============================================================
Script standalone para entrenar y serializar el modelo LightGBM
de Meta-Labeling. NO corre en producción.

Flujo completo:
    1. Descarga datos históricos desde Alpaca (historial frío)
    2. Aplica Capa 1 → detecta señales históricas (primarias)
    3. Triple Barrier Labeling → genera targets binarios {0, 1}
    4. Feature Engineering → build_stationary_features()
    5. Purged TimeSeriesSplit → evita Data Leakage
    6. Entrena LightGBM con class_weight balanceado
    7. Serializa booster → ml/models/meta_label_{asset}.txt
    8. Reporta métricas: AUC-ROC, Precision, Recall, F1, Log Loss

Uso:
    # Entrenar sobre cripto (recomendado primero — más datos)
    python ml/train.py --asset crypto

    # Entrenar sobre equities
    python ml/train.py --asset equity

    # Control de lookback (horas para cripto, días para equity)
    python ml/train.py --asset crypto --lookback 8760   # 1 año de barras horarias

Dependencias:
    pip install lightgbm>=4.0.0 scikit-learn>=1.3.0 shap>=0.43.0

IMPORTANTE — Integridad Temporal:
    El split de entrenamiento es SIEMPRE cronológico (Purged + Embargo).
    NUNCA se usa train_test_split con shuffle=True.
    El test set son SIEMPRE las observaciones más recientes.
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ── Ajustar path para importar módulos del bot ────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd

from dotenv import load_dotenv
import os

from ml.features import build_stationary_features, FEATURE_NAMES
from ml.labeling import get_triple_barrier_labels, get_purged_train_test_split

# Configurar logging del script
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ml.train")

# ── Directorio de modelos serializados ────────────────────────────────────────
MODELS_DIR = Path(__file__).parent / "models"
MODELS_DIR.mkdir(exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de descarga y señales
# ─────────────────────────────────────────────────────────────────────────────

def _download_crypto_data(api_key: str, secret_key: str, symbols: list[str], lookback_hours: int) -> dict:
    """Descarga datos históricos horarios de cripto desde Alpaca."""
    from data.crypto_ingestion import CryptoDataClient
    client = CryptoDataClient(api_key, secret_key)
    logger.info(f"Descargando {len(symbols)} pares cripto | lookback={lookback_hours}h...")
    return client.get_historical_data(symbols, lookback_hours=lookback_hours)


def _download_equity_data(api_key: str, secret_key: str, tickers: list[str], lookback_days: int) -> dict:
    """Descarga datos históricos diarios de equities desde Alpaca."""
    from data.ingestion import DataClient
    client = DataClient(api_key, secret_key)
    logger.info(f"Descargando {len(tickers)} tickers equity | lookback={lookback_days}d...")
    return client.get_historical_data(tickers, lookback_days=lookback_days)


def _get_crypto_signals(df: pd.DataFrame) -> pd.DatetimeIndex:
    """Aplica Capa 1 (generate_crypto_signal) en modo vectorizado histórico."""
    from signals.indicators import compute_crypto_indicators, generate_crypto_signal

    df_ind = compute_crypto_indicators(df)
    signal_dates = []

    for i in range(len(df_ind)):
        sub = df_ind.iloc[: i + 1]
        if len(sub) < 2:
            continue
        if generate_crypto_signal(sub):
            signal_dates.append(sub.index[-1])

    logger.info(f"  Capa 1 detectó {len(signal_dates)} señales históricas")
    return pd.DatetimeIndex(signal_dates)


def _get_equity_signals(df: pd.DataFrame) -> pd.DatetimeIndex:
    """Aplica Capa 1 (generate_signal) en modo vectorizado histórico."""
    from signals.indicators import compute_indicators, generate_signal

    df_ind = compute_indicators(df)
    signal_dates = []

    for i in range(len(df_ind)):
        sub = df_ind.iloc[: i + 1]
        if len(sub) < 2:
            continue
        if generate_signal(sub):
            signal_dates.append(sub.index[-1])

    logger.info(f"  Capa 1 detectó {len(signal_dates)} señales históricas")
    return pd.DatetimeIndex(signal_dates)


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline de entrenamiento
# ─────────────────────────────────────────────────────────────────────────────

def build_dataset(
    data_dict: dict,
    signal_fn,
    asset_type: str,
    pt_mult: float,
    sl_mult: float,
    t1_horizon: int,
) -> tuple[pd.DataFrame, pd.Series]:
    """
    Construye el dataset de entrenamiento combinando todos los activos.

    Para cada activo:
        1. Detecta señales históricas con signal_fn (Capa 1).
        2. Etiqueta señales con Triple Barrier.
        3. Extrae features estacionarias en cada señal.
        4. Alinea features con labels por índice.

    Returns:
        Tupla (X, y) donde X es el DataFrame de features y y los meta-labels.
    """
    all_X = []
    all_y = []

    for symbol, df in data_dict.items():
        logger.info(f"\n── Procesando {symbol} ({len(df)} barras) ──")

        try:
            # Paso 1: Calcular indicadores de Capa 1
            if asset_type == "crypto":
                from signals.indicators import compute_crypto_indicators
                df_ind = compute_crypto_indicators(df)
            else:
                from signals.indicators import compute_indicators
                df_ind = compute_indicators(df)

            # Paso 2: Detectar señales históricas (Capa 1)
            signal_dates = signal_fn(df)

            if len(signal_dates) < 5:
                logger.warning(f"  {symbol}: Solo {len(signal_dates)} señales. Mínimo requerido: 5. Omitido.")
                continue

            # Paso 3: Triple Barrier Labeling
            labels = get_triple_barrier_labels(
                df=df_ind,
                signal_dates=signal_dates,
                pt_mult=pt_mult,
                sl_mult=sl_mult,
                t1_horizon=t1_horizon,
                atr_col="atr",
            )

            if labels.empty:
                logger.warning(f"  {symbol}: 0 etiquetas generadas. Omitido.")
                continue

            # Paso 4: Feature Engineering en los timestamps de señal
            features_full = build_stationary_features(df_ind)
            # Filtrar solo los timestamps de señal que tienen features válidas
            valid_idx = labels.index.intersection(features_full.index)

            if len(valid_idx) < 5:
                logger.warning(f"  {symbol}: Solo {len(valid_idx)} muestras válidas. Omitido.")
                continue

            X_symbol = features_full.loc[valid_idx]
            y_symbol = labels.loc[valid_idx]

            all_X.append(X_symbol)
            all_y.append(y_symbol)

            logger.info(
                f"  {symbol}: ✓ {len(X_symbol)} muestras | "
                f"TP Rate: {y_symbol.mean():.1%}"
            )

        except Exception as exc:
            logger.error(f"  {symbol}: Error en pipeline: {exc}. Omitido.")

    if not all_X:
        raise RuntimeError(
            "No se pudo construir el dataset. Verifica los datos históricos y el ATR."
        )

    X = pd.concat(all_X).sort_index()
    y = pd.concat(all_y).sort_index()

    # Verificar alineación
    assert X.index.equals(y.index), "Error de alineación entre features y labels."

    logger.info(
        f"\n── Dataset consolidado: {len(X)} muestras | "
        f"TP Rate global: {y.mean():.1%} | "
        f"Activos procesados: {len(all_X)} ──"
    )
    return X, y


def train_lightgbm(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_test: pd.DataFrame,
    y_test: pd.Series,
    output_path: Path,
) -> None:
    """
    Entrena el clasificador LightGBM y serializa el booster.

    Parámetros clave:
        - objective: binary (clasificación binaria P(TP))
        - metric: binary_logloss + auc
        - class_weight: balanced (compensa desbalance TP/SL)
        - n_estimators: 500 con early_stopping_rounds=50
        - verbosity: -1 (silencioso salvo logging propio)
    """
    try:
        import lightgbm as lgb
        from sklearn.metrics import (
            roc_auc_score, precision_score, recall_score,
            f1_score, log_loss, classification_report
        )
    except ImportError as e:
        raise ImportError(
            f"Dependencia no instalada: {e}. "
            "Ejecuta: pip install lightgbm scikit-learn"
        ) from e

    logger.info("\n── Iniciando entrenamiento LightGBM ──")
    logger.info(f"  Train: {len(X_train)} muestras | Test: {len(X_test)} muestras")

    # Calcular scale_pos_weight para manejar desbalance de clases
    neg_count = (y_train == 0).sum()
    pos_count = (y_train == 1).sum()
    scale_pos_weight = neg_count / pos_count if pos_count > 0 else 1.0
    logger.info(
        f"  Clase 0 (SL/Time): {neg_count} | "
        f"Clase 1 (TP): {pos_count} | "
        f"scale_pos_weight: {scale_pos_weight:.2f}"
    )

    # ── Parámetros del modelo ──────────────────────────────────────────────────
    params = {
        "objective":        "binary",
        "metric":           ["binary_logloss", "auc"],
        "learning_rate":    0.05,
        "n_estimators":     500,
        "num_leaves":       31,
        "max_depth":        -1,
        "min_child_samples": 20,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq":     5,
        "scale_pos_weight": scale_pos_weight,
        "verbose":          -1,
        "n_jobs":           -1,
        "random_state":     42,
    }

    dtrain = lgb.Dataset(X_train, label=y_train, feature_name=FEATURE_NAMES)
    dtest  = lgb.Dataset(X_test,  label=y_test,  reference=dtrain)

    callbacks = [
        lgb.early_stopping(stopping_rounds=50, verbose=True),
        lgb.log_evaluation(period=50),
    ]

    booster = lgb.train(
        params=params,
        train_set=dtrain,
        valid_sets=[dtrain, dtest],
        valid_names=["train", "test"],
        callbacks=callbacks,
    )

    # ── Métricas de Evaluación ─────────────────────────────────────────────────
    y_prob = booster.predict(X_test)
    y_pred = (y_prob >= 0.5).astype(int)

    auc      = roc_auc_score(y_test, y_prob)
    logloss  = log_loss(y_test, y_prob)
    prec     = precision_score(y_test, y_pred, zero_division=0)
    rec      = recall_score(y_test, y_pred, zero_division=0)
    f1       = f1_score(y_test, y_pred, zero_division=0)

    logger.info("\n── Métricas de Evaluación (Test Set) ──")
    logger.info(f"  AUC-ROC:   {auc:.4f}  (objetivo: > 0.55)")
    logger.info(f"  Log Loss:  {logloss:.4f}")
    logger.info(f"  Precision: {prec:.4f}")
    logger.info(f"  Recall:    {rec:.4f}")
    logger.info(f"  F1 Score:  {f1:.4f}")
    logger.info(f"\n{classification_report(y_test, y_pred, target_names=['SL/Time', 'TP'])}")

    # Feature Importance
    feat_imp = dict(zip(
        booster.feature_name(),
        booster.feature_importance(importance_type="gain").tolist()
    ))
    feat_imp_sorted = sorted(feat_imp.items(), key=lambda x: x[1], reverse=True)
    logger.info("\n── Feature Importance (gain) ──")
    for feat, imp in feat_imp_sorted:
        bar = "█" * int(imp / max(v for _, v in feat_imp_sorted) * 20)
        logger.info(f"  {feat:<15}: {bar} ({imp:.1f})")

    # Advertencia si el AUC es bajo
    if auc < 0.55:
        logger.warning(
            f"\n⚠ AUC-ROC={auc:.4f} < 0.55. El modelo tiene escaso poder predictivo. "
            "Considera: más datos históricos, ajuste de t1_horizon, o revisar features."
        )

    # ── Serializar el Booster ──────────────────────────────────────────────────
    booster.save_model(str(output_path))
    logger.info(f"\n✓ Modelo guardado en: {output_path.resolve()}")
    logger.info(f"  Árboles: {booster.num_trees()} | Best iteration: {booster.best_iteration}")


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Entrenamiento offline del Meta-Model LightGBM",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python ml/train.py --asset crypto
  python ml/train.py --asset equity --lookback 365
  python ml/train.py --asset crypto --lookback 8760 --pt-mult 2.5 --sl-mult 1.0
        """,
    )
    parser.add_argument(
        "--asset", choices=["crypto", "equity"], default="crypto",
        help="Tipo de activo a entrenar (default: crypto)"
    )
    parser.add_argument(
        "--lookback", type=int, default=8760,
        help="Horas (cripto) o días (equity) de historial a descargar (default: 8760h = 1 año)"
    )
    parser.add_argument(
        "--pt-mult", type=float, default=2.0,
        help="Multiplicador ATR para barrera de TP (default: 2.0)"
    )
    parser.add_argument(
        "--sl-mult", type=float, default=1.0,
        help="Multiplicador ATR para barrera de SL (default: 1.0)"
    )
    parser.add_argument(
        "--horizon", type=int, default=None,
        help="Horizonte temporal en períodos (default: 72h para cripto, 5d para equity)"
    )
    args = parser.parse_args()

    # ── Cargar credenciales ────────────────────────────────────────────────────
    load_dotenv()
    api_key    = os.getenv("ALPACA_API_KEY", "").strip()
    secret_key = os.getenv("ALPACA_SECRET_KEY", "").strip()
    if not api_key or not secret_key:
        logger.error("ERROR: ALPACA_API_KEY y/o ALPACA_SECRET_KEY no encontradas en .env")
        sys.exit(1)

    # ── Configurar parámetros según tipo de activo ────────────────────────────
    if args.asset == "crypto":
        from config import CRYPTO_FALLBACK_TICKERS
        symbols = CRYPTO_FALLBACK_TICKERS[:10]
        t1_horizon = args.horizon or 72  # 72 horas ≈ 3 días
        output_path = MODELS_DIR / "meta_label_crypto.txt"
        data_dict = _download_crypto_data(api_key, secret_key, symbols, args.lookback)
        signal_fn = _get_crypto_signals
        asset_label = "Crypto"
    else:
        from config import TICKERS
        tickers = TICKERS
        t1_horizon = args.horizon or 5   # 5 sesiones ≈ 1 semana
        output_path = MODELS_DIR / "meta_label_equity.txt"
        data_dict = _download_equity_data(api_key, secret_key, tickers, args.lookback)
        signal_fn = _get_equity_signals
        asset_label = "Equity"

    logger.info(
        f"\n{'='*60}\n"
        f"  ENTRENAMIENTO META-LABEL LIGHTGBM — {asset_label}\n"
        f"{'='*60}\n"
        f"  Activos:      {list(data_dict.keys())}\n"
        f"  Lookback:     {args.lookback} {'horas' if args.asset == 'crypto' else 'días'}\n"
        f"  Triple Barrier: TP×{args.pt_mult} | SL×{args.sl_mult} | Horizon={t1_horizon}\n"
        f"  Output:       {output_path}\n"
        f"{'='*60}"
    )

    # ── Construcción del dataset ───────────────────────────────────────────────
    X, y = build_dataset(
        data_dict=data_dict,
        signal_fn=signal_fn,
        asset_type=args.asset,
        pt_mult=args.pt_mult,
        sl_mult=args.sl_mult,
        t1_horizon=t1_horizon,
    )

    # ── Split cronológico Purged ───────────────────────────────────────────────
    X_train, X_test, y_train, y_test = get_purged_train_test_split(
        features=X,
        labels=y,
        embargo_pct=0.01,
        test_size=0.20,
    )

    # ── Entrenamiento ──────────────────────────────────────────────────────────
    train_lightgbm(X_train, y_train, X_test, y_test, output_path)

    logger.info(
        f"\n{'='*60}\n"
        f"  ✓ ENTRENAMIENTO COMPLETADO\n"
        f"  Modelo: {output_path.name}\n"
        f"  Ahora puedes reiniciar el bot — la Capa 2 se activará automáticamente.\n"
        f"{'='*60}"
    )


if __name__ == "__main__":
    main()
