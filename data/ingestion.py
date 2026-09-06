"""
data/ingestion.py — Ingesta de Datos Historicos de Alpaca
==========================================================
Responsable de descargar y normalizar las barras diarias de precios
para el universo de tickers configurado.

Caracteristicas:
- Usa StockHistoricalDataClient y StockBarsRequest de alpaca-py.
- Descarga en lotes (batch) respetando el rate limit de 200 RPM.
- Backoff exponencial automatico ante errores HTTP 429.
- Retorna dict[ticker → pd.DataFrame] con columnas normalizadas.
"""

import time
import logging
from datetime import datetime, timedelta

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from config import (
    LOOKBACK_DAYS,
    BATCH_SIZE,
    REQUEST_INTERVAL_SEC,
    MAX_RETRY_ATTEMPTS,
)

logger = logging.getLogger(__name__)


class DataClient:
    """
    Cliente de datos historicos con rate limiting y reintentos automaticos.
    """

    def __init__(self, api_key: str, secret_key: str) -> None:
        """
        Inicializa el cliente de datos historicos de Alpaca.

        Args:
            api_key: Clave publica de Alpaca (cargada desde .env).
            secret_key: Clave secreta de Alpaca (cargada desde .env).
        """
        self._client = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)
        logger.info("DataClient inicializado correctamente.")

    def _fetch_bars_with_retry(self, request: StockBarsRequest) -> pd.DataFrame:
        """
        Ejecuta la peticion de barras con reintentos y backoff exponencial.

        Args:
            request: Objeto StockBarsRequest configurado.

        Returns:
            DataFrame de pandas con las barras de precios.

        Raises:
            RuntimeError: Si se agotan los reintentos maximos.
        """
        wait = 1.0
        for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
            try:
                bars = self._client.get_stock_bars(request)
                df = bars.df
                # Resetear index para trabajar con columnas planas
                if isinstance(df.index, pd.MultiIndex):
                    df = df.reset_index()
                return df
            except Exception as exc:
                error_str = str(exc).lower()
                if "429" in error_str or "too many requests" in error_str:
                    logger.warning(
                        f"HTTP 429 recibido (intento {attempt}/{MAX_RETRY_ATTEMPTS}). "
                        f"Esperando {wait:.1f}s..."
                    )
                    time.sleep(wait)
                    wait *= 2  # Backoff exponencial: 1s → 2s → 4s
                else:
                    logger.error(f"Error al descargar datos: {exc}")
                    raise

        raise RuntimeError(
            f"Se agotaron {MAX_RETRY_ATTEMPTS} reintentos. Ultima excepcion: HTTP 429."
        )

    def _normalize_dataframe(self, df: pd.DataFrame, ticker: str) -> pd.DataFrame:
        """
        Normaliza el DataFrame de barras a un formato canonico.

        Columnas estandarizadas: open, high, low, close, volume, timestamp.
        El indice es el timestamp de la sesion.

        Args:
            df: DataFrame crudo de la API.
            ticker: Simbolo del ticker para filtrado si hay MultiIndex.

        Returns:
            DataFrame normalizado con indice de fecha.
        """
        # Filtrar por symbol si la columna existe (respuesta multi-ticker)
        if "symbol" in df.columns:
            df = df[df["symbol"] == ticker].copy()

        # Normalizar nombres de columnas a minusculas
        df.columns = [c.lower() for c in df.columns]

        # Seleccionar columnas relevantes
        cols = ["timestamp", "open", "high", "low", "close", "volume"]
        available = [c for c in cols if c in df.columns]
        df = df[available].copy()

        # Parsear timestamp y establecer como indice
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            df = df.set_index("timestamp")
            df.index = df.index.tz_convert("America/New_York")

        # Asegurar tipos numericos
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # Eliminar filas con NaN en close (datos invalidos)
        df = df.dropna(subset=["close"])

        # Ordenar cronologicamente ascendente
        df = df.sort_index()

        logger.debug(f"{ticker}: {len(df)} sesiones descargadas.")
        return df

    def get_historical_data(
        self,
        tickers: list[str],
        lookback_days: int = LOOKBACK_DAYS,
    ) -> dict[str, pd.DataFrame]:
        """
        Descarga datos historicos diarios para la lista de tickers.

        Procesa en lotes de BATCH_SIZE con pausa entre lotes para respetar
        el rate limit de 200 RPM de la API de Alpaca.

        Args:
            tickers: Lista de simbolos bursatiles a descargar.
            lookback_days: Numero de dias calendario hacia atras (default: 200).

        Returns:
            Diccionario {ticker: DataFrame} con datos normalizados.
            Tickers con error son excluidos del resultado (con log de warning).
        """
        end_date = datetime.now()
        # Agregar margen de dias calendario para cubrir 200 sesiones de mercado
        start_date = end_date - timedelta(days=int(lookback_days * 1.5))

        results: dict[str, pd.DataFrame] = {}

        # Procesar en lotes para respetar el rate limit
        for i in range(0, len(tickers), BATCH_SIZE):
            batch = tickers[i : i + BATCH_SIZE]
            logger.info(
                f"Descargando lote {i // BATCH_SIZE + 1}: {batch}"
            )

            for ticker in batch:
                try:
                    request = StockBarsRequest(
                        symbol_or_symbols=ticker,
                        timeframe=TimeFrame.Day,
                        start=start_date,
                        end=end_date
                    )
                    df_raw = self._fetch_bars_with_retry(request)
                    df_norm = self._normalize_dataframe(df_raw, ticker)

                    if len(df_norm) < 10:
                        logger.warning(
                            f"{ticker}: Datos insuficientes ({len(df_norm)} barras). "
                            "Se omite este ticker."
                        )
                        continue

                    results[ticker] = df_norm
                    logger.info(f"{ticker}: OK ({len(df_norm)} sesiones)")

                except Exception as exc:
                    logger.error(
                        f"{ticker}: Error al procesar datos. Ticker omitido. "
                        f"Detalle: {exc}"
                    )

                # Pausa entre requests para respetar rate limit (200 RPM)
                time.sleep(REQUEST_INTERVAL_SEC)

        logger.info(
            f"Ingesta completada: {len(results)}/{len(tickers)} tickers descargados."
        )
        return results
