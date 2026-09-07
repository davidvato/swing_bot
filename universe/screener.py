"""
universe/screener.py — Seleccion Dinamica del Universo de Trading
=================================================================
Selecciona semanalmente el Top-N de acciones con mayor liquidez
del pool de large-caps del S&P 500.

Criterios de seleccion (en orden de aplicacion):
  1. Universo base: pool curado de ~80 large-caps del S&P 500.
  2. Filtro de precio: cierre >= UNIVERSE_MIN_PRICE (default $10).
  3. Filtro de volumen: promedio 30d >= UNIVERSE_MIN_AVG_VOLUME (default 5M).
  4. Ranking por liquidity_score = precio_cierre * volumen_promedio.
  5. Seleccion del Top-N (default 10).

Resiliencia:
  - Cache JSON en disco para sobrevivir reinicios del bot.
  - Fallback a config.TICKERS si el screener falla o retorna < 3 tickers.
"""

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from config import (
    BATCH_SIZE,
    REQUEST_INTERVAL_SEC,
    MAX_RETRY_ATTEMPTS,
    UNIVERSE_SIZE,
    UNIVERSE_MIN_PRICE,
    UNIVERSE_MIN_AVG_VOLUME,
    UNIVERSE_LOOKBACK_DAYS,
    UNIVERSE_CACHE_FILE,
    TICKERS as FALLBACK_TICKERS,
)

logger = logging.getLogger(__name__)

# ─── Pool de Candidatos (~80 Large-caps del S&P 500) ─────────────────────────
# Seleccion curada de las principales empresas por sector:
# Technology, Financials, Healthcare, Consumer, Industrials, Energy.
# Este pool se revisa manualmente en cada rebalanceo trimestral del S&P 500.
SP500_CANDIDATE_POOL: list[str] = [
    # ── Tecnologia (Big Tech + Semiconductores + Cloud) ──────────────────────
    "AAPL",  # Apple Inc.
    "MSFT",  # Microsoft Corporation
    "NVDA",  # NVIDIA Corporation
    "GOOG",  # Alphabet Inc. (Class C)
    "GOOGL", # Alphabet Inc. (Class A)
    "META",  # Meta Platforms Inc.
    "AMZN",  # Amazon.com Inc.
    "TSLA",  # Tesla Inc.
    "AVGO",  # Broadcom Inc.
    "AMD",   # Advanced Micro Devices
    "PLTR",  # Palantir Technologies
    "ORCL",  # Oracle Corporation
    "CRM",   # Salesforce Inc.
    "ADBE",  # Adobe Inc.
    "INTC",  # Intel Corporation
    "QCOM",  # Qualcomm Inc.
    "TXN",   # Texas Instruments
    "AMAT",  # Applied Materials
    "MU",    # Micron Technology
    "LRCX",  # Lam Research
    "KLAC",  # KLA Corporation
    "NFLX",  # Netflix Inc.
    "NOW",   # ServiceNow Inc.
    "SNOW",  # Snowflake Inc.
    "PANW",  # Palo Alto Networks
    "CRWD",  # CrowdStrike Holdings
    "FTNT",  # Fortinet Inc.
    "DELL",  # Dell Technologies
    "HPQ",   # HP Inc.
    "IBM",   # International Business Machines
    # ── Finanzas ─────────────────────────────────────────────────────────────
    "JPM",   # JPMorgan Chase & Co.
    "BAC",   # Bank of America Corp.
    "WFC",   # Wells Fargo & Co.
    "GS",    # Goldman Sachs Group
    "MS",    # Morgan Stanley
    "BLK",   # BlackRock Inc.
    "AXP",   # American Express Co.
    "V",     # Visa Inc.
    "MA",    # Mastercard Inc.
    "PYPL",  # PayPal Holdings
    "C",     # Citigroup Inc.
    "USB",   # U.S. Bancorp
    # ── Salud ─────────────────────────────────────────────────────────────────
    "UNH",   # UnitedHealth Group
    "JNJ",   # Johnson & Johnson
    "PFE",   # Pfizer Inc.
    "MRK",   # Merck & Co.
    "ABBV",  # AbbVie Inc.
    "LLY",   # Eli Lilly and Company
    "BMY",   # Bristol-Myers Squibb
    "AMGN",  # Amgen Inc.
    "GILD",  # Gilead Sciences
    "CVS",   # CVS Health Corp.
    "CI",    # Cigna Group
    # ── Consumo Discrecional y Esencial ──────────────────────────────────────
    "HD",    # Home Depot Inc.
    "WMT",   # Walmart Inc.
    "COST",  # Costco Wholesale
    "TGT",   # Target Corporation
    "NKE",   # Nike Inc.
    "MCD",   # McDonald's Corp.
    "SBUX",  # Starbucks Corp.
    "PG",    # Procter & Gamble Co.
    "KO",    # Coca-Cola Co.
    "PEP",   # PepsiCo Inc.
    # ── Industriales y Energia ────────────────────────────────────────────────
    "BA",    # Boeing Co.
    "CAT",   # Caterpillar Inc.
    "GE",    # GE Aerospace
    "HON",   # Honeywell International
    "LMT",   # Lockheed Martin
    "RTX",   # RTX Corporation
    "UPS",   # United Parcel Service
    "XOM",   # Exxon Mobil Corp.
    "CVX",   # Chevron Corp.
    "COP",   # ConocoPhillips
    # ── Comunicaciones y Media ────────────────────────────────────────────────
    "DIS",   # Walt Disney Co.
    "CMCSA", # Comcast Corp.
    "T",     # AT&T Inc.
    "VZ",    # Verizon Communications
    "TMUS",  # T-Mobile US Inc.
]


class UniverseScreener:
    """
    Seleccionador dinamico del universo de trading.

    Filtra y rankea el pool de candidatos del S&P 500 semanalmente
    basandose en liquidez (precio * volumen promedio).

    Attributes:
        _api_key: Clave publica de Alpaca.
        _secret_key: Clave secreta de Alpaca.
        _active_universe: Lista activa de tickers seleccionados.
        _last_updated: Timestamp de la ultima actualizacion.
        _scores: Diccionario {ticker: liquidity_score} del ultimo screener.
        _cache_path: Path al archivo de cache JSON.
    """

    def __init__(self, api_key: str, secret_key: str) -> None:
        """
        Inicializa el screener de universo.

        Args:
            api_key: Clave publica de Alpaca.
            secret_key: Clave secreta de Alpaca.
        """
        from alpaca.data.historical import StockHistoricalDataClient
        self._client = StockHistoricalDataClient(
            api_key=api_key, secret_key=secret_key
        )
        self._active_universe: list[str] = []
        self._last_updated: datetime | None = None
        self._scores: dict[str, float] = {}
        self._cache_path = Path(UNIVERSE_CACHE_FILE)

        # Intentar cargar cache existente al arrancar
        self._load_cache()
        logger.info(
            f"UniverseScreener inicializado. "
            f"Universo activo: {self._active_universe or 'sin cargar (usando fallback)'}"
        )

    # ─── Cache ────────────────────────────────────────────────────────────────

    def _save_cache(self) -> None:
        """Persiste el universo activo y sus scores en disco (JSON)."""
        try:
            payload = {
                "active_universe": self._active_universe,
                "scores": self._scores,
                "last_updated": (
                    self._last_updated.isoformat() if self._last_updated else None
                ),
            }
            self._cache_path.write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
            logger.info(f"Cache del universo guardado en '{self._cache_path}'.")
        except Exception as exc:
            logger.warning(f"No se pudo guardar el cache del universo: {exc}")

    def _load_cache(self) -> None:
        """Carga el universo desde el cache JSON si existe."""
        if not self._cache_path.exists():
            logger.info(
                f"Cache '{self._cache_path}' no encontrado. "
                "Se usara fallback hasta la primera actualizacion."
            )
            return
        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
            self._active_universe = payload.get("active_universe", [])
            self._scores = payload.get("scores", {})
            last_updated_str = payload.get("last_updated")
            if last_updated_str:
                self._last_updated = datetime.fromisoformat(last_updated_str)
            logger.info(
                f"Cache del universo cargado desde '{self._cache_path}'. "
                f"Tickers: {self._active_universe} | "
                f"Actualizado: {self._last_updated}"
            )
        except Exception as exc:
            logger.warning(
                f"Error al leer cache del universo: {exc}. "
                "Se usara fallback hasta la primera actualizacion."
            )

    # ─── Descarga de datos ────────────────────────────────────────────────────

    def _fetch_bars(self, tickers: list[str]) -> dict[str, pd.DataFrame]:
        """
        Descarga barras diarias para el pool de candidatos.

        Usa el mismo patron de batching/rate-limit que DataClient.

        Args:
            tickers: Lista de tickers a descargar.

        Returns:
            Diccionario {ticker: DataFrame} con barras normalizadas.
        """
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.enums import DataFeed

        end_date = datetime.now()
        start_date = end_date - timedelta(days=int(UNIVERSE_LOOKBACK_DAYS * 1.5))

        results: dict[str, pd.DataFrame] = {}

        for i in range(0, len(tickers), BATCH_SIZE):
            batch = tickers[i: i + BATCH_SIZE]
            logger.debug(f"[Screener] Descargando lote: {batch}")

            for ticker in batch:
                wait = 1.0
                for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
                    try:
                        request = StockBarsRequest(
                            symbol_or_symbols=ticker,
                            timeframe=TimeFrame.Day,
                            start=start_date,
                            end=end_date,
                            feed=DataFeed.IEX
                        )
                        bars = self._client.get_stock_bars(request)
                        df = bars.df
                        if isinstance(df.index, pd.MultiIndex):
                            df = df.reset_index()

                        # Normalizar columnas
                        df.columns = [c.lower() for c in df.columns]
                        if "symbol" in df.columns:
                            df = df[df["symbol"] == ticker].copy()

                        for col in ["close", "volume"]:
                            if col in df.columns:
                                df[col] = pd.to_numeric(df[col], errors="coerce")

                        df = df.dropna(subset=["close"])
                        if len(df) >= 10:
                            results[ticker] = df
                        break  # Exito

                    except Exception as exc:
                        err = str(exc).lower()
                        if "429" in err or "too many requests" in err:
                            logger.warning(
                                f"[Screener] HTTP 429 en {ticker} "
                                f"(intento {attempt}/{MAX_RETRY_ATTEMPTS}). "
                                f"Esperando {wait:.1f}s..."
                            )
                            time.sleep(wait)
                            wait *= 2
                        else:
                            logger.debug(
                                f"[Screener] {ticker} omitido: {exc}"
                            )
                            break

                time.sleep(REQUEST_INTERVAL_SEC)

        return results

    # ─── Logica de Seleccion ──────────────────────────────────────────────────

    def select_universe(self, n: int = UNIVERSE_SIZE) -> list[str]:
        """
        Ejecuta el screener y retorna el Top-N de tickers por liquidez.

        Secuencia:
          1. Descargar UNIVERSE_LOOKBACK_DAYS de barras para el pool.
          2. Calcular avg_volume y last_price para cada ticker.
          3. Filtrar: price >= UNIVERSE_MIN_PRICE y volume >= UNIVERSE_MIN_AVG_VOLUME.
          4. Calcular liquidity_score = last_price * avg_volume.
          5. Ordenar descendente por liquidity_score.
          6. Retornar Top-N simbolos.

        Args:
            n: Numero de tickers a seleccionar (default: UNIVERSE_SIZE).

        Returns:
            Lista de tickers seleccionados ordenada por score descendente.
            Si el proceso falla, retorna el universo anterior o FALLBACK_TICKERS.
        """
        logger.info(
            f"[Screener] Iniciando seleccion de universo. "
            f"Pool: {len(SP500_CANDIDATE_POOL)} candidatos → Top-{n}"
        )

        try:
            # ── Paso 1: Descargar datos del pool completo ─────────────────────
            data_dict = self._fetch_bars(SP500_CANDIDATE_POOL)
            logger.info(
                f"[Screener] Datos descargados para {len(data_dict)}/{len(SP500_CANDIDATE_POOL)} candidatos."
            )

            if not data_dict:
                raise RuntimeError("No se obtuvieron datos del pool de candidatos.")

            # ── Paso 2: Calcular metricas por ticker ──────────────────────────
            rows = []
            for ticker, df in data_dict.items():
                last_price = float(df["close"].iloc[-1]) if "close" in df.columns else 0.0
                avg_volume = (
                    float(df["volume"].tail(UNIVERSE_LOOKBACK_DAYS).mean())
                    if "volume" in df.columns
                    else 0.0
                )
                rows.append({
                    "ticker": ticker,
                    "last_price": last_price,
                    "avg_volume": avg_volume,
                    "liquidity_score": last_price * avg_volume,
                })

            metrics_df = pd.DataFrame(rows)

            # ── Paso 3: Aplicar filtros ───────────────────────────────────────
            before = len(metrics_df)
            metrics_df = metrics_df[
                (metrics_df["last_price"] >= UNIVERSE_MIN_PRICE) &
                (metrics_df["avg_volume"] >= UNIVERSE_MIN_AVG_VOLUME)
            ]
            after = len(metrics_df)
            logger.info(
                f"[Screener] Filtros aplicados: {before} candidatos → "
                f"{after} pasan (precio >= ${UNIVERSE_MIN_PRICE}, "
                f"volumen >= {UNIVERSE_MIN_AVG_VOLUME:,})"
            )

            if after < 3:
                raise RuntimeError(
                    f"Solo {after} tickers pasaron los filtros. "
                    "Criterios demasiado restrictivos."
                )

            # ── Paso 4: Ranking y seleccion Top-N ────────────────────────────
            metrics_df = metrics_df.sort_values("liquidity_score", ascending=False)
            top_n = metrics_df.head(n)

            selected = top_n["ticker"].tolist()
            scores = dict(zip(top_n["ticker"], top_n["liquidity_score"].round(2)))

            # ── Paso 5: Actualizar estado y persistir ─────────────────────────
            self._active_universe = selected
            self._scores = scores
            self._last_updated = datetime.now()
            self._save_cache()

            logger.info(
                f"[Screener] Universo actualizado exitosamente: {selected}"
            )
            for ticker in selected:
                score = scores.get(ticker, 0)
                row = metrics_df[metrics_df["ticker"] == ticker].iloc[0]
                logger.info(
                    f"  {ticker}: precio=${row['last_price']:.2f} | "
                    f"vol_avg={row['avg_volume']:,.0f} | "
                    f"score={score:,.0f}"
                )

            return selected

        except Exception as exc:
            logger.error(
                f"[Screener] Error durante la seleccion del universo: {exc}. "
                f"Usando universo {'anterior' if self._active_universe else 'fallback estatico'}."
            )
            return self._active_universe if self._active_universe else list(FALLBACK_TICKERS)

    # ─── Getters ──────────────────────────────────────────────────────────────

    def get_active_universe(self) -> list[str]:
        """
        Retorna el universo activo actual.

        Si aun no se ha ejecutado el screener (primera vez sin cache),
        retorna el fallback estatico de config.TICKERS.

        Returns:
            Lista de tickers activos.
        """
        if not self._active_universe:
            logger.warning(
                "[Screener] Universo aun no inicializado. "
                "Usando TICKERS estatico de config.py como fallback."
            )
            return list(FALLBACK_TICKERS)
        return list(self._active_universe)

    def get_status(self) -> dict:
        """
        Retorna el estado completo del screener para el dashboard.

        Returns:
            Diccionario con universe, scores, last_updated y source.
        """
        if not self._active_universe:
            return {
                "active_universe": list(FALLBACK_TICKERS),
                "scores": {},
                "last_updated": None,
                "source": "static_fallback",
                "universe_size": len(FALLBACK_TICKERS),
                "candidate_pool_size": len(SP500_CANDIDATE_POOL),
            }

        return {
            "active_universe": self._active_universe,
            "scores": self._scores,
            "last_updated": (
                self._last_updated.isoformat() if self._last_updated else None
            ),
            "source": "dynamic_screener",
            "universe_size": len(self._active_universe),
            "candidate_pool_size": len(SP500_CANDIDATE_POOL),
        }
