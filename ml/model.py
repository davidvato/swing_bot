"""
ml/model.py — Wrapper LightGBM para Inferencia en Producción
=============================================================
Encapsula la carga, inferencia y manejo de errores del modelo LightGBM.

Diseño de latencia:
    El booster se carga en memoria UNA SOLA VEZ al arrancar el bot
    (en main.py durante la inicialización). La inferencia en el loop
    de señales llama a predict_proba() directamente sobre el booster
    en memoria → latencia < 1ms por predicción (sin I/O de disco).

Thread-safety:
    LightGBM.Booster.predict() es thread-safe para lecturas concurrentes.
    Compatible con asyncio.get_event_loop().run_in_executor() sin locks.

Modo degradado:
    Si el archivo de modelo no existe (aún no entrenado), MetaLabelModel
    retorna None desde load() y el bot opera con Kelly estático (Capa 1 sola).
    Esto garantiza zero downtime durante el período de onboarding.

Uso típico en producción:
    # Al arrancar el bot (una sola vez):
    ml_model = MetaLabelModel.load("ml/models/meta_label_crypto.txt")

    # En cada señal detectada (inferencia):
    features = build_stationary_features(df_with_indicators)
    prob = ml_model.predict_proba(features.iloc[-1:])
    if prob >= ML_THRESHOLD:
        notional = compute_notional(equity, p=prob, ...)
        submit_buy(...)
"""

import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from ml.features import FEATURE_NAMES

logger = logging.getLogger(__name__)


class MetaLabelModel:
    """
    Wrapper del modelo LightGBM para inferencia en tiempo real.

    Carga el booster LightGBM serializado (.txt) desde disco una sola vez
    y expone la interfaz predict_proba() para el loop de producción.

    Attributes:
        _booster: LightGBM Booster cargado en memoria.
        _feature_names: Lista de nombres de features esperadas por el modelo.
        model_path: Ruta del archivo de modelo (para logging/debug).
    """

    def __init__(self, booster, feature_names: list[str], model_path: str = "") -> None:
        self._booster = booster
        self._feature_names = feature_names
        self.model_path = model_path

    @classmethod
    def load(cls, model_path: str) -> Optional["MetaLabelModel"]:
        """
        Carga el booster LightGBM desde un archivo .txt serializado.

        No lanza excepción si el archivo no existe — retorna None para
        permitir el modo degradado (bot sin Capa 2).

        Args:
            model_path: Ruta al archivo de modelo serializado por LightGBM.
                        Generado por ml/train.py → booster.save_model(path).

        Returns:
            MetaLabelModel lista para inferencia, o None si:
                - El archivo no existe (modelo aún no entrenado).
                - LightGBM no está instalado.
                - El archivo está corrupto.

        Raises:
            None — todos los errores son capturados y logueados.
        """
        try:
            import lightgbm as lgb  # Import diferido para evitar crash si no instalado
        except ImportError:
            logger.error(
                "[MetaLabelModel] lightgbm no está instalado. "
                "Instala con: pip install lightgbm>=4.0.0. "
                "La Capa 2 (Meta-Model) quedará DESACTIVADA."
            )
            return None

        path = Path(model_path)
        if not path.exists():
            logger.warning(
                f"[MetaLabelModel] Archivo de modelo no encontrado: {path.resolve()}\n"
                "  → Capa 2 DESACTIVADA. El bot opera con Kelly estático (Capa 1 sola).\n"
                "  → Ejecuta 'python ml/train.py' para entrenar y activar la Capa 2."
            )
            return None

        try:
            booster = lgb.Booster(model_file=str(path))
            feature_names = booster.feature_name()

            # Validar que el modelo fue entrenado con las features esperadas
            missing = set(FEATURE_NAMES) - set(feature_names)
            if missing:
                logger.warning(
                    f"[MetaLabelModel] El modelo tiene features faltantes vs FEATURE_NAMES: {missing}. "
                    "Puede indicar incompatibilidad de versión. Se cargará igualmente."
                )

            logger.info(
                f"[MetaLabelModel] ✓ Modelo cargado desde {path.name} | "
                f"Features: {feature_names} | "
                f"Árboles: {booster.num_trees()} | "
                f"Capa 2 ACTIVA."
            )
            return cls(booster, feature_names, str(path))

        except Exception as exc:
            logger.error(
                f"[MetaLabelModel] Error cargando modelo desde {path}: {exc}. "
                "Capa 2 DESACTIVADA."
            )
            return None

    def predict_proba(self, features: pd.DataFrame) -> float:
        """
        Predice la probabilidad de que la señal de Capa 1 termine en TP.

        Retorna P(meta_label = 1), es decir, la probabilidad de que el precio
        toque la barrera superior (Take Profit) antes que la inferior (Stop Loss)
        o que expire el horizonte temporal.

        Args:
            features: DataFrame de 1 fila con las columnas de FEATURE_NAMES.
                      Típicamente: build_stationary_features(df).iloc[-1:]

        Returns:
            float en [0.0, 1.0]. Retorna 0.5 (neutral/sin-edge) ante cualquier error.
            - > ML_THRESHOLD → señal aprobada → operar
            - < ML_THRESHOLD → señal filtrada → no operar

        Note:
            0.5 como fallback es conservador: no filtra ni aprueba señales en caso
            de error, dejando la decisión a la lógica de Capa 1 + Kelly base.
        """
        try:
            if features.empty:
                logger.warning("[MetaLabelModel] Features DataFrame vacío. Retornando 0.5.")
                return 0.5

            # Alinear al orden exacto de features que espera el booster
            aligned = features.reindex(columns=self._feature_names)

            if aligned.isnull().any().any():
                nan_cols = aligned.columns[aligned.isnull().any()].tolist()
                logger.warning(
                    f"[MetaLabelModel] NaN en features: {nan_cols}. "
                    "LightGBM manejará NaN internamente (si fue entrenado con ellos)."
                )

            x = aligned.values  # shape: (1, n_features)
            raw_pred = self._booster.predict(x)  # shape: (1,) para clasificación binaria

            prob = float(raw_pred[0])
            prob = max(0.0, min(1.0, prob))  # Clamp defensivo

            logger.debug(f"[MetaLabelModel] P(TP) = {prob:.4f}")
            return prob

        except Exception as exc:
            logger.error(
                f"[MetaLabelModel] Error en inferencia: {exc}. "
                "Retornando 0.5 (neutral)."
            )
            return 0.5

    @property
    def is_loaded(self) -> bool:
        """Indica si el modelo está disponible para inferencia."""
        return self._booster is not None

    def get_feature_importance(self, importance_type: str = "gain") -> dict[str, float]:
        """
        Retorna la importancia de cada feature del modelo.

        Útil para debugging y monitoreo del comportamiento del modelo.

        Args:
            importance_type: 'gain' (default), 'split', o 'weight'.

        Returns:
            Diccionario {feature_name: importance_value} ordenado descendente.
        """
        try:
            importances = self._booster.feature_importance(importance_type=importance_type)
            result = dict(zip(self._feature_names, importances.tolist()))
            return dict(sorted(result.items(), key=lambda x: x[1], reverse=True))
        except Exception as exc:
            logger.error(f"[MetaLabelModel] Error obteniendo feature importance: {exc}")
            return {}
