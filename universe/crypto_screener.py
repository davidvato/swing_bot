"""
universe/crypto_screener.py — Screener Dinamico de Universo Cripto
==================================================================
Consulta CoinGecko API (gratuita, sin API key) para obtener el Top-N
de criptomonedas por capitalizacion de mercado, filtrando stablecoins
y tokens sin soporte en Alpaca.

Patron identico al UniverseScreener de equities:
  - Cache JSON persistido en disco (crypto_universe_cache.json).
  - Fallback a CRYPTO_FALLBACK_TICKERS si la API falla.
  - Actualizacion semanal (cada lunes 00:01 UTC).
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from config import (
    COINGECKO_API_URL,
    COINGECKO_TO_ALPACA,
    COINGECKO_STABLECOIN_IDS,
    CRYPTO_UNIVERSE_SIZE,
    CRYPTO_UNIVERSE_CACHE,
    CRYPTO_FALLBACK_TICKERS,
)

logger = logging.getLogger(__name__)


class CryptoUniverseScreener:
    """
    Screener de universo cripto basado en capitalizacion de mercado (CoinGecko).

    Selecciona el Top-N de criptomonedas por market cap, excluyendo
    stablecoins y tokens no disponibles en Alpaca.
    """

    def __init__(self) -> None:
        self._cache_path = Path(CRYPTO_UNIVERSE_CACHE)
        self._active_universe: list[str] = []
        logger.info("CryptoUniverseScreener inicializado.")

    def _fetch_coingecko_top(self, top_n: int = 30) -> list[dict]:
        """
        Descarga el ranking de capitalización de mercado desde CoinGecko.

        Args:
            top_n: Cantidad de activos a descargar (se filtrara despues).

        Returns:
            Lista de dicts con id, symbol, current_price, market_cap.
        """
        url = f"{COINGECKO_API_URL}/coins/markets"
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": top_n,
            "page": 1,
            "sparkline": "false",
            "price_change_percentage": "24h",
        }
        try:
            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            logger.info(f"CoinGecko: {len(data)} activos descargados.")
            return data
        except requests.exceptions.Timeout:
            logger.warning("CoinGecko: Timeout. Usando fallback.")
            return []
        except requests.exceptions.HTTPError as exc:
            # Respetar rate limit de CoinGecko (50 req/min en plan gratuito)
            if exc.response.status_code == 429:
                logger.warning("CoinGecko: Rate limit (429). Esperando 60s...")
                time.sleep(60)
            logger.error(f"CoinGecko HTTP error: {exc}")
            return []
        except Exception as exc:
            logger.error(f"CoinGecko error inesperado: {exc}")
            return []

    def select_universe(self) -> list[str]:
        """
        Selecciona el Top-N de criptomonedas por market cap.

        Filtra:
          1. Stablecoins (USDT, USDC, DAI, etc.)
          2. Tokens sin soporte en Alpaca (no estan en COINGECKO_TO_ALPACA)

        Returns:
            Lista de pares Alpaca (ej. ['BTC/USD', 'ETH/USD', ...]).
        """
        raw_data = self._fetch_coingecko_top(top_n=30)

        if not raw_data:
            logger.warning("CoinGecko sin datos. Usando fallback estatico.")
            return CRYPTO_FALLBACK_TICKERS[: CRYPTO_UNIVERSE_SIZE]

        selected: list[str] = []
        scores: dict[str, float] = {}

        for coin in raw_data:
            coin_id = coin.get("id", "")
            market_cap = coin.get("market_cap") or 0
            price_change_24h = coin.get("price_change_percentage_24h") or 0.0
            current_price = coin.get("current_price") or 0

            # Filtrar stablecoins
            if coin_id in COINGECKO_STABLECOIN_IDS:
                logger.debug(f"[CRYPTO SCREENER] Excluido stablecoin: {coin_id}")
                continue

            # Verificar soporte en Alpaca
            alpaca_pair = COINGECKO_TO_ALPACA.get(coin_id)
            if not alpaca_pair:
                logger.debug(f"[CRYPTO SCREENER] Sin soporte Alpaca: {coin_id}")
                continue

            selected.append(alpaca_pair)
            scores[alpaca_pair] = market_cap
            logger.debug(
                f"[CRYPTO SCREENER] Aceptado: {alpaca_pair} | "
                f"Market Cap: ${market_cap:,.0f} | "
                f"24h: {price_change_24h:+.2f}% | "
                f"Precio: ${current_price:,.4f}"
            )

            if len(selected) >= CRYPTO_UNIVERSE_SIZE:
                break

        if len(selected) < CRYPTO_UNIVERSE_SIZE:
            # Rellenar con fallback si el screener no llego al top-N
            for ticker in CRYPTO_FALLBACK_TICKERS:
                if ticker not in selected:
                    selected.append(ticker)
                if len(selected) >= CRYPTO_UNIVERSE_SIZE:
                    break

        # Persistir cache
        self._save_cache(selected[:CRYPTO_UNIVERSE_SIZE], scores)
        self._active_universe = selected[:CRYPTO_UNIVERSE_SIZE]

        logger.info(
            f"[CRYPTO SCREENER] Universo seleccionado: {self._active_universe}"
        )
        return self._active_universe

    def _save_cache(self, universe: list[str], scores: dict) -> None:
        """Persiste el universo seleccionado en JSON."""
        payload = {
            "active_universe": universe,
            "scores": scores,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "source": "coingecko_screener",
            "universe_size": len(universe),
        }
        try:
            self._cache_path.write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
            logger.info(f"Cache cripto guardado: {self._cache_path}")
        except Exception as exc:
            logger.error(f"Error guardando cache cripto: {exc}")

    def get_active_universe(self) -> list[str]:
        """
        Retorna el universo activo. Lee del cache si existe, sino fallback.

        Returns:
            Lista de pares Alpaca activos.
        """
        if self._cache_path.exists():
            try:
                payload = json.loads(
                    self._cache_path.read_text(encoding="utf-8")
                )
                universe = payload.get("active_universe", [])
                if universe:
                    self._active_universe = universe
                    return universe
            except Exception as exc:
                logger.error(f"Error leyendo cache cripto: {exc}")

        logger.warning("Cache cripto no disponible. Usando fallback estatico.")
        return CRYPTO_FALLBACK_TICKERS[:CRYPTO_UNIVERSE_SIZE]

    def get_status(self) -> dict:
        """Retorna el estado del screener para el dashboard."""
        if self._cache_path.exists():
            try:
                payload = json.loads(
                    self._cache_path.read_text(encoding="utf-8")
                )
                return payload
            except Exception:
                pass
        return {
            "active_universe": CRYPTO_FALLBACK_TICKERS[:CRYPTO_UNIVERSE_SIZE],
            "scores": {},
            "last_updated": None,
            "source": "static_fallback",
            "universe_size": CRYPTO_UNIVERSE_SIZE,
        }
