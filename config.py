"""
config.py — Constantes globales del Swing Trading Bot
=====================================================
Centraliza TODOS los parametros configurables del sistema.
Para modificar el comportamiento del bot, editar UNICAMENTE este archivo.
Las credenciales sensibles se cargan exclusivamente desde .env via dotenv.
"""

# ─── MODO PRUEBA: Límite de Budget ─────────────────────────────────────────
USE_TEST_BUDGET: bool = True       # Activa el límite de capital para pruebas
TEST_BUDGET_USD: float = 200.0     # Capital simulado total disponible

# ─── Universo de Trading (Fallback Estatico) ─────────────────────────────────
# IMPORTANTE: Esta lista es el FALLBACK de emergencia del bot.
# El universo operativo real es calculado dinamicamente cada lunes
# por universe/screener.py (UniverseScreener). Solo se usa esta lista
# si el screener falla O si no existe cache previo al arrancar el bot.
TICKERS: list[str] = [
    "AAPL",   # Apple Inc. (Tecnología)
    "NVDA",   # NVIDIA Corporation (Semiconductores)
    "AMZN",   # Amazon.com Inc. (Consumo / Cloud)
    "META",   # Meta Platforms Inc. (Tecnología / Redes)
    "GOOG",   # Alphabet Inc. (Tecnología / Búsqueda)
    "TSLA",   # Tesla Inc. (Automotriz / Tecnología)
    "AVGO",   # Broadcom Inc. (Semiconductores)
    "LLY",    # Eli Lilly and Company (Salud / Farmacéutica)
    "JPM",    # JPMorgan Chase & Co. (Finanzas)
    "NFLX",   # Netflix Inc. (Comunicaciones / Streaming)
]

# ─── Seleccion Dinamica de Universo (Universe Screener) ───────────────────────
UNIVERSE_SIZE: int = 10               # Numero de tickers a seleccionar (Top-N)
UNIVERSE_MAX_PER_SECTOR: int = 4      # Maximo de tickers por sector en universo dinamico (diversificacion)
UNIVERSE_MIN_PRICE: float = 10.0      # Precio minimo de cierre ($) para filtrar
# Feed de datos para screening e ingesta: 'iex' (gratuito) o 'sip' (consolidado de pago)
import os
UNIVERSE_DATA_FEED: str = os.getenv("ALPACA_DATA_FEED", "iex").lower()
# Volumen promedio minimo (30d): En feed IEX (1-3% market share), las large-caps promedian 500k-3M.
# Si se usa feed SIP consolidado, el umbral es 5M.
UNIVERSE_MIN_AVG_VOLUME: int = 300_000 if UNIVERSE_DATA_FEED == "iex" else 5_000_000
UNIVERSE_LOOKBACK_DAYS: int = 30      # Dias de historico para calcular metricas
UNIVERSE_CACHE_FILE: str = "universe_cache.json"  # Cache JSON del universo activo
# Hora de actualizacion del universo (lunes, 5 min despues de apertura)
UNIVERSE_UPDATE_TIME_EST: str = "09:35"  # Formato HH:MM (hora EST)

# ─── Parametros de Datos Historicos ──────────────────────────────────────────
LOOKBACK_DAYS: int = 250        # Sesiones de mercado a descargar (margen holgado para SMA-200)
BATCH_SIZE: int = 5             # Tickers por lote (control de rate limit)
MARKET_REGIME_TICKER: str = "SPY" # Ticker para el filtro de régimen de mercado

# ─── Parametros de Indicadores Tecnicos ──────────────────────────────────────
SMA_PERIOD: int = 200           # Periodo de la Media Movil Simple (SMA)
RSI_PERIOD: int = 4             # Periodo del Indice de Fuerza Relativa (RSI)
RSI_OVERSOLD: float = 30.0      # Umbral RSI para condicion de sobreventa
CONSEC_DOWN_DAYS: int = 4       # Dias consecutivos a la baja (señal alternativa)

# ─── Criterio de Kelly Fraccional (Half-Kelly) ───────────────────────────────
# CALIBRADO CON DATOS REALES (16 round-trips al 2026-10-18):
#   WR empírico = 50.0%, Profit Factor = 1.30 (Avg Win / Avg Loss)
#   Ratio real = $436.25 / $335.92 ≈ 1.30
KELLY_WIN_RATE: float = 0.50    # Tasa de ganancia empirica real (actualizado de 0.60)
KELLY_WIN_LOSS_RATIO: float = 1.30  # Ratio Ganancia/Perdida real (actualizado de 1.0)
KELLY_FRACTION: float = 0.5     # Fraccion de Kelly aplicada (0.5 = Half-Kelly)
MAX_POSITION_PCT: float = 0.15  # Techo duro maximo por operacion (15% del equity)

# ─── Control Cuantitativo de Riesgo Global en Equities (Portfolio Heat) ────────
EQUITY_MAX_PORTFOLIO_HEAT: float = 0.60  # Techo maximo global de capital en equities (60% del equity total)
EQUITY_MAX_OPEN_POSITIONS: int = 4       # Maximo de posiciones de equities abiertas concurrentes

# ─── Parametros de Salida (TP/SL emulados) ───────────────────────────────────
TAKE_PROFIT_PCT: float = 0.04   # Take-Profit base (fallback): salida con +4% de ganancia
STOP_LOSS_PCT: float = 0.04     # Stop-Loss base (fallback): salida con -4% de perdida
MAX_HOLD_DAYS: int = 5          # Dias maximos de holding antes de salida forzada
SL_GRACE_PERIOD_MINS: int = 15  # Periodo de gracia (minutos) en la apertura para SL (no aplica si caída > 1.5x SL)

# ─── TP/SL Dinámico basado en ATR para Equities ───────────────────────────────
# Adapta el objetivo de ganancia y el riesgo al régimen de volatilidad de cada acción.
# Fórmula: TP = entry + (ATR × 1.5), SL = entry - (ATR × 1.2), acotado entre pisos y techos.
EQUITY_DYNAMIC_ATR_TPSL: bool = True     # True = calcular TP/SL proporcional al ATR del ticker
EQUITY_ATR_TP_MULT: float = 1.5          # Multiplicador TP (1.5x ATR)
EQUITY_ATR_SL_MULT: float = 1.2          # Multiplicador SL (1.2x ATR)
EQUITY_MIN_TP_PCT: float = 0.025         # Piso mínimo de TP (+2.5%) para activos estables
EQUITY_MAX_TP_PCT: float = 0.060         # Techo máximo de TP (+6.0%)
EQUITY_MIN_SL_PCT: float = 0.020         # Piso mínimo de SL (-2.0%)
EQUITY_MAX_SL_PCT: float = 0.050         # Techo máximo de SL (-5.0%) para activos muy volátiles

# ─── Circuit Breaker a Nivel de Cartera (Control Sistémico de Riesgo) ─────────
# Detiene compras temporales ante rachas de pérdidas o caídas pronunciadas del equity.
CIRCUIT_BREAKER_ENABLED: bool = True
CIRCUIT_BREAKER_MAX_CONSEC_LOSSES: int = 3   # Pausar si hay 3 pérdidas consecutivas cerradas
CIRCUIT_BREAKER_MAX_WEEKLY_DD_PCT: float = 0.05 # Pausar si el drawdown semanal supera el 5%
CIRCUIT_BREAKER_COOLDOWN_HOURS: int = 48    # Horas de enfriamiento antes de reanudar

# ─── Filtro de Anuncios de Resultados Trimestrales (Earnings Risk) ────────────
# Previene entrar en activos con anuncio de resultados en la ventana de +/- 48h.
EARNINGS_FILTER_ENABLED: bool = True
EARNINGS_WINDOW_DAYS: int = 2

# ─── Control de Ejecución y Slippage ──────────────────────────────────────────
MAX_ALLOWED_SPREAD_PCT: float = 0.0025   # Spread máximo tolerado (0.25%) para compras de mercado
ORDER_FILL_TIMEOUT_SEC: float = 3.0      # Tiempo máximo de espera para confirmar fill real

# ─── Break-Even Trailing Stop ─────────────────────────────────────────────────
# Si la posicion alcanza BREAKEVEN_TRIGGER_PCT de ganancia, el Stop-Loss se
# mueve a break-even (precio de entrada) para proteger capital.
# Calibrado a +2.8% para evitar asfixiar la volatilidad intradia normal (+-1.5%) antes de tocar el +4%.
BREAKEVEN_TRIGGER_PCT: float = 0.028  # Umbral para activar break-even stop (+2.8%)

# ─── EOW Inteligente (End-Of-Week Condicional) ────────────────────────────────
# Permite mantener posiciones durante el fin de semana SI el activo cotiza
# POR ENCIMA del precio de entrada (posicion en ganancia o break-even).
# Si EOW_HOLD_IF_PROFITABLE=True: solo se liquida la posicion el viernes si
# esta en perdida o si ha excedido MAX_HOLD_DAYS.
# Poner en False para restaurar el comportamiento original (liquidar todo).
EOW_HOLD_IF_PROFITABLE: bool = True  # True = No liquidar viernes si la posicion va ganando

# ─── Filtro de Volatilidad Relativa (Anti-Whipsaw) ───────────────────────────
# Filtra activos con volatilidad relativa (ATR/Close) excesiva antes de entrar.
# Activos con ATR/Close > MAX_RELATIVE_ATR son omitidos aunque tengan señal RSI.
# Previene entrar en activos como PLTR en fase de alta volatilidad sin rebote.
# Poner en None para desactivar el filtro.
MAX_RELATIVE_ATR: float = 0.04  # Maximo ATR/Close permitido (4% diario = muy volátil)
ATR_FILTER_PERIOD: int = 14     # Periodo ATR para calcular volatilidad relativa

# ─── Planificador Semanal ─────────────────────────────────────────────────────
WEEKLY_CLOSE_TIME_EST: str = "15:45"    # Hora de liquidacion viernes (EST)
MARKET_OPEN_TIME_EST: str = "09:30"    # Apertura del mercado (EST)
MARKET_CLOSE_TIME_EST: str = "16:00"   # Cierre del mercado (EST)
TIMEZONE_EST: str = "America/New_York"  # Zona horaria de referencia

# ─── Rate Limiting de la API de Alpaca ───────────────────────────────────────
API_RATE_LIMIT_RPM: int = 200           # Limite maximo de requests por minuto
REQUEST_INTERVAL_SEC: float = 0.35      # Pausa minima entre requests (segundos)
MAX_RETRY_ATTEMPTS: int = 3             # Reintentos maximos ante HTTP 429
SUPERVISOR_POLL_INTERVAL_SEC: int = 15  # Intervalo de monitoreo TP/SL en tiempo real (15 segundos)

# ─── Almacenamiento de Datos ──────────────────────────────────────────────────
DB_PATH: str = "trades.db"              # Ruta de la base de datos SQLite
LOG_FILE: str = "bot.log"              # Archivo de log del sistema
SUPERVISOR_STATE_FILE: str = "supervisor_state.json"  # Persistencia de estado de trailing stop
# Nota: UNIVERSE_CACHE_FILE definido en la seccion Universe Screener

# ─── Tipos de Operacion (para el Trade Log) ──────────────────────────────────
TRADE_TYPE_BUY: str = "BUY"            # Compra inicial por señal
TRADE_TYPE_SELL_TP: str = "SELL_TP"   # Venta por Take-Profit (+10%)
TRADE_TYPE_SELL_SL: str = "SELL_SL"   # Venta por Stop-Loss (-5%)
TRADE_TYPE_SELL_EOW: str = "SELL_EOW" # Venta del viernes (End-Of-Week)
TRADE_TYPE_SELL_5D: str = "SELL_5D"   # Venta por maximo de 5 dias de holding

# --- Telegram Notifications --------------------------------------------------
import os
TELEGRAM_BOT_TOKEN: str = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID: str = os.getenv('TELEGRAM_CHAT_ID', '')

# ─── Crypto Module ────────────────────────────────────────────────────────────
# IMPORTANTE: Todos los parametros CRYPTO_* estan completamente aislados.
# Nunca se mezclan con los parametros del motor de equities.

# Universo fallback (si CoinGecko falla) — Top-10 por market cap, sin stablecoins
CRYPTO_FALLBACK_TICKERS: list[str] = [
    "BTC/USD",   # Bitcoin
    "ETH/USD",   # Ethereum
    "BNB/USD",   # BNB
    "SOL/USD",   # Solana
    "XRP/USD",   # Ripple
    "DOGE/USD",  # Dogecoin
    "ADA/USD",   # Cardano
    "AVAX/USD",  # Avalanche
    "LINK/USD",  # Chainlink
    "DOT/USD",   # Polkadot
]
CRYPTO_UNIVERSE_SIZE: int = 10
CRYPTO_UNIVERSE_CACHE: str = "crypto_universe_cache.json"
CRYPTO_UNIVERSE_UPDATE_UTC: str = "00:01"  # Cada domingo/lunes medianoche UTC

# Parametros de indicadores (calibrados para Trend-Following / Momentum en Cripto)
CRYPTO_SMA_MACRO: int = 200          # Filtro de tendencia macro (solo operar en bull market)
CRYPTO_EMA_FAST: int = 9             # EMA rapida para crossover
CRYPTO_EMA_SLOW: int = 21            # EMA lenta para crossover
CRYPTO_RSI_PERIOD: int = 14          # RSI estandar para medir fuerza del momentum
CRYPTO_RSI_MOMENTUM_MIN: float = 50.0 # RSI minimo para confirmar fuerza alcista

# ─── TP/SL Dinamico basado en ATR (Average True Range) ───────────────────────
# Formula:
#   TP = entry_price + (ATR_14 × CRYPTO_ATR_TP_MULT)  → R:R 1.25:1
#   SL = entry_price - (ATR_14 × CRYPTO_ATR_SL_MULT)
# Ventaja: Se adapta automaticamente a la volatilidad real del activo.
# CRYPTO_ATR_SL_MULT subido de 1.0 → 1.2 para reducir stop hunts/whipsaw.
# Con SL_MULT=1.2 damos un 20% mas de holgura ante volatilidad intradiaria.
CRYPTO_ATR_PERIOD: int = 14          # Periodo ATR (estandar de industria)
CRYPTO_ATR_TP_MULT: float = 1.5      # Multiplicador TP → 1.5x ATR sobre precio de entrada
CRYPTO_ATR_SL_MULT: float = 1.2      # Multiplicador SL → 1.2x ATR (subido de 1.0, anti-whipsaw)
# Fallback fijo si ATR no se puede calcular (datos insuficientes)
CRYPTO_TAKE_PROFIT_PCT: float = 0.05
CRYPTO_STOP_LOSS_PCT: float = 0.05

# Parametros de gestion de posicion y riesgo (Cripto)
CRYPTO_MAX_HOLD_HOURS: int = 72      # Maximo de holding en horas (cripto no cierra)
CRYPTO_MAX_POSITION_PCT: float = 0.05  # 5% del equity por posicion (conservador individual)
CRYPTO_MAX_PORTFOLIO_HEAT: float = 0.20 # Limite maximo global de capital en cripto (ej. 20% = 4 posiciones vivas)
CRYPTO_POLL_INTERVAL_SEC: int = 30  # Supervisor revisa cada 30 segundos (24/7)

# Criterio de Kelly aislado para Cripto (R:R asimetrico 1.5 a 1)
CRYPTO_KELLY_WIN_RATE: float = 0.42      # Base historica de la senal Momentum
CRYPTO_KELLY_WIN_LOSS_RATIO: float = 1.5 # b = 1.5 (TP 1.5 ATR / SL 1.0 ATR)

# Almacenamiento
CRYPTO_CACHE_FILE: str = "crypto_prices_cache.json"
CRYPTO_DB_TABLE: str = "crypto_trades"  # Tabla SQLite separada para cripto

# CoinGecko API (gratuita, sin API key)
COINGECKO_API_URL: str = "https://api.coingecko.com/api/v3"

# Mapa CoinGecko symbol → par Alpaca (para filtrado y conversion)
COINGECKO_TO_ALPACA: dict = {
    "bitcoin": "BTC/USD", "ethereum": "ETH/USD", "binancecoin": "BNB/USD",
    "solana": "SOL/USD", "ripple": "XRP/USD", "dogecoin": "DOGE/USD",
    "cardano": "ADA/USD", "avalanche-2": "AVAX/USD", "chainlink": "LINK/USD",
    "polkadot": "DOT/USD", "shiba-inu": "SHIB/USD", "litecoin": "LTC/USD",
    "uniswap": "UNI/USD", "bitcoin-cash": "BCH/USD", "stellar": "XLM/USD",
    "monero": "XMR/USD", "ethereum-classic": "ETC/USD", "filecoin": "FIL/USD",
    "aave": "AAVE/USD", "the-graph": "GRT/USD",
}
# IDs de stablecoins a excluir del ranking de CoinGecko
COINGECKO_STABLECOIN_IDS: set = {
    "tether", "usd-coin", "dai", "binance-usd", "true-usd",
    "pax-dollar", "frax", "usdd", "gemini-dollar", "terrausd",
}

# ─── ML Module (Meta-Labeling LightGBM — Capa 2) ─────────────────────────────
# Activa el filtro de segundo nivel basado en LightGBM que predice
# P(Take Profit antes de Stop Loss o Time Exit) — Triple Barrier Method.
#
# Si ML_ENABLED = False, o si el archivo de modelo no existe,
# el bot opera en modo degradado: solo Capa 1 + Kelly estático (comportamiento previo).
# Esto garantiza zero downtime durante el onboarding del modelo.

ML_ENABLED: bool = False                 # DESACTIVADO: modelo actual AUC=0.54 (insuficiente)
                                         # Re-activar cuando AUC > 0.55 tras re-entrenamiento

# Umbral mínimo de P(TP) para aprobar una señal de Capa 1.
# Señales con prob < ML_THRESHOLD son descartadas (no se envía orden).
# Rango recomendado: [0.52, 0.65]. Umbral más alto = menos trades, más selectivo.
ML_THRESHOLD: float = 0.55

# Rutas a los archivos de booster LightGBM serializados.
# Generados ejecutando: python ml/train.py --asset [crypto|equity]
ML_CRYPTO_MODEL_PATH: str = "ml/models/meta_label_crypto.txt"
ML_EQUITY_MODEL_PATH: str = "ml/models/meta_label_equity.txt"

# ── Parámetros de Triple Barrier para el script de entrenamiento offline ──────
# Estos valores controlan la geometría de las barreras en ml/train.py.
# Deben ser consistentes entre los entrenamientos sucesivos para reproducibilidad.

# Multiplicador ATR → barrera de TP. pt_mult=1.5, sl_mult=1.0 → Ratio R:R 1.5:1.
ML_TB_PT_MULT: float = 1.5
ML_TB_SL_MULT: float = 1.0

# Horizonte temporal máximo (en períodos) antes de declarar Time Exit.
# Cripto (barras horarias): 72h ≈ 3 días
# Equity (barras diarias):  5 sesiones ≈ 1 semana de trading
ML_TB_HORIZON_CRYPTO: int = 72
ML_TB_HORIZON_EQUITY: int = 5
