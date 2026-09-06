"""
data/crypto_ingestion.py — Ingesta de Datos Historicos de Cripto (Alpaca)
=========================================================================
Descarga barras horarias de precios para el universo de criptomonedas.
Usa CryptoHistoricalDataClient de alpaca-py.

Diferencias clave vs data/ingestion.py (equities):
  - TimeFrame.Hour en vez de TimeFrame.Day (mercado cripto 24/7).
  - Calcula ATR-14 sobre cada DataFrame descargado para uso en TP/SL dinamico.
  - Retorna el mismo contrato: dict[symbol, pd.DataFrame].
"""

import time
import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame

from config import (
    CRYPTO_ATR_PERIOD,
    REQUEST_INTERVAL_SEC,
    MAX_RETRY_ATTEMPTS,
)

logger = logging.getLogger(__name__)


def _compute_atr(df: pd.DataFrame, period: int = CRYPTO_ATR_PERIOD) -> pd.Series:
    """
    Calcula el Average True Range (ATR) de Wilder.

    ATR mide la volatilidad del activo promediando el rango real (True Range)
    de los ultimos `period` velas.

    True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)

    Args:
        df: DataFrame con columnas high, low, close.
        period: Periodo de suavizado (default: CRYPTO_ATR_PERIOD=14).

    Returns:
        Serie de ATR alineada al indice del DataFrame.
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]

    hl = high - low
    hc = (high - close.shift(1)).abs()
    lc = (low - close.shift(1)).abs()

    true_range = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    # Wilder smoothing: EMA con alpha=1/period
    atr = true_range.ewm(com=period - 1, min_periods=period).mean()
    return atr


class CryptoDataClient:
    """
    Cliente de datos historicos de cripto con ATR integrado.

    Usa barras horarias para mayor granularidad en mercados 24/7.
    El ATR calculado es incluido en el DataFrame como columna 'atr'
    para ser consumido por el CryptoPositionSupervisor.
    """

    def __init__(self, api_key: str, secret_key: str) -> None:
        """
        Inicializa el cliente de cripto de Alpaca.

        Args:
            api_key: Clave publica de Alpaca.
            secret_key: Clave secreta de Alpaca.
        """
        # CryptoHistoricalDataClient no requiere paper=True/False
        self._client = CryptoHistoricalDataClient(
            api_key=api_key, secret_key=secret_key
        )
        logger.info("CryptoDataClient inicializado.")

    def _fetch_bars_with_retry(self, request: CryptoBarsRequest) -> pd.DataFrame:
        """Ejecuta la peticion con reintentos y backoff exponencial."""
        wait = 1.0
        for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
            try:
                bars = self._client.get_crypto_bars(request)
                df = bars.df
                if isinstance(df.index, pd.MultiIndex):
                    df = df.reset_index()
                return df
            except Exception as exc:
                error_str = str(exc).lower()
                if "429" in error_str or "too many requests" in error_str:
                    logger.warning(
                        f"HTTP 429 (intento {attempt}/{MAX_RETRY_ATTEMPTS}). "
                        f"Esperando {wait:.1f}s..."
                    )
                    time.sleep(wait)
                    wait *= 2
                else:
                    logger.error(f"Error descargando datos cripto: {exc}")
                    raise
        raise RuntimeError(
            f"Se agotaron {MAX_RETRY_ATTEMPTS} reintentos. HTTP 429."
        )

    def _normalize_dataframe(self, df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """Normaliza el DataFrame al formato canonico con ATR incluido."""
        if "symbol" in df.columns:
            df = df[df["symbol"] == symbol].copy()

        df.columns = [c.lower() for c in df.columns]
        cols = ["timestamp", "open", "high", "low", "close", "volume"]
        available = [c for c in cols if c in df.columns]
        df = df[available].copy()

        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            df = df.set_index("timestamp")
            df.index = df.index.tz_convert("UTC")

        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna(subset=["close"])
        df = df.sort_index()

        # Calcular ATR y agregarlo al DataFrame
        if len(df) >= CRYPTO_ATR_PERIOD and all(
            c in df.columns for c in ["high", "low", "close"]
        ):
            df["atr"] = _compute_atr(df, CRYPTO_ATR_PERIOD)
        else:
            df["atr"] = np.nan

        logger.debug(f"[CRYPTO] {symbol}: {len(df)} barras horarias descargadas.")
        return df

    def get_historical_data(
        self,
        symbols: list[str],
        lookback_hours: int = 300,
    ) -> dict[str, pd.DataFrame]:
        """
        Descarga datos historicos horarios para la lista de pares cripto.

        Args:
            symbols: Lista de pares Alpaca (ej. ['BTC/USD', 'ETH/USD']).
            lookback_hours: Horas de historico a descargar (default: 300h ≈ 12.5 dias).
                            Suficiente para SMA-50 en barras horarias + ATR-14.

        Returns:
            dict[symbol, DataFrame] con OHLCV + columna 'atr'.
        """
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(hours=lookback_hours)

        results: dict[str, pd.DataFrame] = {}

        for symbol in symbols:
            try:
                request = CryptoBarsRequest(
                    symbol_or_symbols=symbol,
                    timeframe=TimeFrame.Hour,
                    start=start_date,
                    end=end_date,
                )
                df_raw = self._fetch_bars_with_retry(request)
                df_norm = self._normalize_dataframe(df_raw, symbol)

                if len(df_norm) < 50:
                    logger.warning(
                        f"[CRYPTO] {symbol}: Datos insuficientes ({len(df_norm)} barras). "
                        "Ticker omitido."
                    )
                    continue

                results[symbol] = df_norm
                atr_val = df_norm["atr"].iloc[-1]
                logger.info(
                    f"[CRYPTO] {symbol}: OK ({len(df_norm)} barras) | "
                    f"ATR-{CRYPTO_ATR_PERIOD}: {atr_val:.4f}"
                )

            except Exception as exc:
                logger.error(
                    f"[CRYPTO] {symbol}: Error al procesar. Omitido. Detalle: {exc}"
                )

            time.sleep(REQUEST_INTERVAL_SEC)

        logger.info(
            f"[CRYPTO] Ingesta completada: {len(results)}/{len(symbols)} pares."
        )
        return results

    def get_latest_price(self, symbol: str) -> float:
        """
        Retorna el precio mas reciente de un par cripto.

        Args:
            symbol: Par de trading (ej. 'BTC/USD').

        Returns:
            Precio de cierre de la ultima barra, o 0.0 si falla.
        """
        try:
            end = datetime.utcnow()
            start = end - timedelta(hours=2)
            request = CryptoBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Hour,
                start=start,
                end=end,
            )
            df_raw = self._fetch_bars_with_retry(request)
            if df_raw.empty:
                return 0.0
            df = self._normalize_dataframe(df_raw, symbol)
            return float(df["close"].iloc[-1]) if not df.empty else 0.0
        except Exception as exc:
            logger.error(f"[CRYPTO] Error obteniendo precio de {symbol}: {exc}")
            return 0.0
